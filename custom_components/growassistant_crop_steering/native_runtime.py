"""Entry-bound composition for explicitly injected simulated native controllers.

Normal HA setup never constructs this container. Initialization may reconcile a
recorded pump target with OFF; it is deliberately separate from construction.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .config import configured_entity_value
from .const import BOOLEAN_STATE_DEFAULTS, CONF_PUMP_SWITCH, DOMAIN
from .cycle import _NativeCycleController
from .native_configuration import validated_configuration
from .pump import _OWNER_KEY, PumpController, PumpState, _protected
from .scheduler import _NativeScheduler
from .shot_engine import _ShotCoordinator

_LOGGER = logging.getLogger(__name__)


class NativeRuntimeState(StrEnum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY_STOPPED = "ready_stopped"
    RUNNING = "running"  # Reserved; no runtime start API in R1.
    STOPPING = "stopping"
    BLOCKED = "blocked"
    CLOSED = "closed"


class NativeRuntime:
    """Own lifecycle composition, leaving all irrigation gates to controllers.

    The entry's runtime_data is the container slot, not another owner registry.
    PumpController retains the existing HA-wide actuator ownership mechanism.
    Callers must inject a pump with explicit simulation ownership evidence.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, *, pump: PumpController
    ) -> None:
        previous = native_runtime(entry)
        if previous is not None and previous.state != NativeRuntimeState.CLOSED:
            raise RuntimeError("Entry still owns a native runtime")
        if pump.hass is not hass or pump.entry is not entry:
            raise ValueError("Pump must belong to this exact entry")
        if pump.state != PumpState.NEW or _OWNER_KEY in hass.data:
            raise RuntimeError("Native pump already owned or initialized")
        reset = hass.data.get(DOMAIN, {}).get("auto_reset", {}).get(entry.entry_id)
        if reset is not None and reset._lock.locked():
            raise RuntimeError("Legacy reset still in progress")
        pending = hass.data.get(f"{DOMAIN}.legacy_actions", {})
        if any(
            pending.get(other.entry_id, 0)
            and (
                other is entry
                or configured_entity_value(other, CONF_PUMP_SWITCH)
                == configured_entity_value(entry, CONF_PUMP_SWITCH)
            )
            for other in hass.config_entries.async_entries(DOMAIN)
        ):
            raise RuntimeError("Legacy service still in progress")
        self.hass = hass
        self.entry = entry
        self.pump = pump
        self.shots = _ShotCoordinator(hass, entry, pump)
        self.cycle = _NativeCycleController(self.shots)
        self.scheduler = _NativeScheduler(hass, entry, self.shots, self.cycle)
        self.state = NativeRuntimeState.CREATED
        self.error: str | None = None
        self._attempted: list[
            PumpController
            | _ShotCoordinator
            | _NativeCycleController
            | _NativeScheduler
        ] = []
        self._lock = asyncio.Lock()
        self._configuration = None
        self._remove_update = None
        self._remove_states = None
        entry.runtime_data = self

    async def async_initialize(self) -> None:
        """Initialize simulation in dependency order, without starting evaluation."""
        async with self._lock:
            if self.state != NativeRuntimeState.CREATED:
                raise RuntimeError("Runtime already initialized or closed")
            ownership = self.pump.ownership
            target = configured_entity_value(self.entry, CONF_PUMP_SWITCH)
            try:
                self._configuration = validated_configuration(self.hass, self.entry)
            except ValueError as err:
                self.error = str(err)
                self.state = NativeRuntimeState.BLOCKED
                raise
            if ownership is None or ownership.reasons(target):
                raise ValueError("Explicit simulated pump ownership required")
            self._bindings = {
                key: configured_entity_value(self.entry, key)
                for key in (
                    "pump_switch",
                    "vwc_sensor",
                    "led_sunrise",
                    "led_sunset",
                    "led_day_sensor",
                    "drain_sensor",
                    "drain_tray_sensor",
                )
            }
            self.shots.ownership = ownership
            self.state = NativeRuntimeState.INITIALIZING
            try:
                for controller in (self.pump, self.shots, self.cycle, self.scheduler):
                    # Include a stage which raises after acquiring only some resources.
                    self._attempted.append(controller)
                    await controller.async_initialize()
                self.state = NativeRuntimeState.READY_STOPPED
                self._remove_update = self.entry.add_update_listener(
                    self._options_changed
                )
                self._remove_states = self.hass.bus.async_listen(
                    "state_changed", self._state_changed
                )
            except BaseException as err:
                self.error = f"Initialization failed: {err}"
                await _protected(
                    self.hass.async_create_background_task(
                        self._close(), "native initialization rollback"
                    )
                )
                raise

    @callback
    def _state_changed(self, event):
        raw = {**self.entry.data, **self.entry.options}
        # Current sensor readings are assessed by the shot controller. Only helper
        # parameter/schedule changes invalidate configuration here.
        entities = {
            value
            for key, value in raw.items()
            if isinstance(value, str)
            and key
            not in {
                "pump_switch",
                "vwc_sensor",
                "led_day_sensor",
                "drain_sensor",
                "drain_tray_sensor",
                "last_shot",
                "last_shot_type",
            }
        }
        if event.data.get("entity_id") in entities:
            self.hass.async_create_task(self._options_changed(self.hass, self.entry))

    async def _options_changed(self, hass, entry):
        """Refresh in place; every parameter change conservatively revokes decisions."""
        if self.state != NativeRuntimeState.READY_STOPPED:
            return
        try:
            updated = validated_configuration(hass, entry)
        except ValueError as err:
            self.error = str(err)
            updated = None
        ignored = {
            *(set(BOOLEAN_STATE_DEFAULTS) - {"p3_emergency_enabled"}),
            "last_shot",
            "last_shot_type",
            "p2_ref_vwc",
            "pump_state",
            "led_day_sensor",
            "drain_sensor",
            "drain_tray_sensor",
        }
        ignored.update(
            key for key in self._configuration.config if key.endswith("shots_done")
        )
        changed = updated is None or any(
            updated.config[key] != value
            for key, value in self._configuration.config.items()
            if key not in ignored
        )
        # Mapping identities matter even when both sensors currently report dry.
        bindings = {
            key: configured_entity_value(entry, key)
            for key in (
                "pump_switch",
                "vwc_sensor",
                "led_sunrise",
                "led_sunset",
                "led_day_sensor",
                "drain_sensor",
                "drain_tray_sensor",
            )
        }
        changed = changed or bindings != self._bindings
        critical = {
            "pump_switch",
            "vwc_sensor",
            "led_sunrise",
            "led_sunset",
            "p1_mode",
            "p2_mode",
            "p0_transpiration_min",
            "p1_duration_min",
            "p2_end_offset_min",
        }
        must_stop = (
            updated is None
            or bindings != self._bindings
            or any(
                updated.config[key] != self._configuration.config[key]
                for key in critical
            )
        )
        must_stop = must_stop or (changed and self.shots.execution_active)
        if must_stop:
            await self._async_stop_for_review(
                self.error or "configuration_changed_requires_review"
            )
        elif changed:
            # No reservation exists: use refreshed parameters on the next explicit
            # evaluation. Never request evaluation or reconstruct controllers here.
            self.shots._generation += 1
            self.shots._interrupt("parameters_changed")
        if updated is not None:
            self._configuration = updated

    async def _async_stop_for_review(self, reason):
        """Invalidate before awaiting; caller cancellation cannot abandon OFF."""
        self.state = NativeRuntimeState.BLOCKED
        self.error = reason
        self.shots._generation += 1
        self.shots._interrupt(reason)
        self.cycle.block(reason)
        await _protected(
            self.hass.async_create_background_task(
                self._stop_execution(), "native configuration/service stop"
            )
        )

    async def _stop_execution(self):
        try:
            await self.scheduler.async_stop()
        finally:
            await self.pump.async_stop()

    async def async_close(self) -> bool:
        """Serialize shutdown with initialization; cancellation cannot abandon it."""
        return await _protected(
            self.hass.async_create_background_task(self._locked_close(), "native close")
        )

    async def _locked_close(self) -> bool:
        async with self._lock:
            return await self._close()

    async def _close(self) -> bool:
        if self.state == NativeRuntimeState.CLOSED:
            return True
        self.state = NativeRuntimeState.STOPPING
        if self._remove_update is not None:
            self._remove_update()
            self._remove_update = None
        if self._remove_states is not None:
            self._remove_states()
            self._remove_states = None
        complete = True
        pump_closed = False
        for controller in reversed(self._attempted):
            if controller is self.pump and pump_closed:
                continue
            try:
                # A cycle write can outlive its close timeout. Stop execution but
                # retain the owners until that write has actually settled.
                if (
                    controller is self.shots
                    and self.cycle._write is not None
                    and not self.cycle._write.done()
                ):
                    self.shots._interrupt("integration_stopping")
                    await self.pump.async_stop()
                    complete = False
                    break
                if await controller.async_close() is False:
                    complete = False
                    # Shots already closes the pump. Do not implicitly retry an
                    # ambiguous OFF in the same shutdown attempt.
                    break
                if controller is self.shots:
                    pump_closed = True
            except (Exception, asyncio.CancelledError) as err:
                complete = False
                self.error = f"Shutdown failed: {err}"
                _LOGGER.exception("Native shutdown failed for %s", self.entry.entry_id)
        if complete:
            self.state = NativeRuntimeState.CLOSED
        else:
            self.state = NativeRuntimeState.BLOCKED
            self.error = self.error or "Native shutdown incomplete; owners retained"
            _LOGGER.error("%s: %s", self.entry.entry_id, self.error)
        return complete


def native_runtime(entry: ConfigEntry) -> NativeRuntime | None:
    """Read the entry slot without creating a controller or claiming an owner."""
    value = getattr(entry, "runtime_data", None)
    return value if isinstance(value, NativeRuntime) else None
