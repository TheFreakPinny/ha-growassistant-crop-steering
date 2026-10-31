"""Entry-bound composition for explicitly injected simulated native controllers.

Normal HA setup never constructs this container. Initialization may reconcile a
recorded pump target with OFF; it is deliberately separate from construction.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config import configured_entity_value
from .const import CONF_PUMP_SWITCH
from .cycle import _NativeCycleController
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
        entry.runtime_data = self

    async def async_initialize(self) -> None:
        """Initialize simulation in dependency order, without starting evaluation."""
        async with self._lock:
            if self.state != NativeRuntimeState.CREATED:
                raise RuntimeError("Runtime already initialized or closed")
            ownership = self.pump.ownership
            target = configured_entity_value(self.entry, CONF_PUMP_SWITCH)
            if ownership is None or ownership.reasons(target):
                raise ValueError("Explicit simulated pump ownership required")
            self.shots.ownership = ownership
            self.state = NativeRuntimeState.INITIALIZING
            try:
                for controller in (self.pump, self.shots, self.cycle, self.scheduler):
                    # Include a stage which raises after acquiring only some resources.
                    self._attempted.append(controller)
                    await controller.async_initialize()
                self.state = NativeRuntimeState.READY_STOPPED
            except BaseException as err:
                self.error = f"Initialization failed: {err}"
                await _protected(
                    self.hass.async_create_background_task(
                        self._close(), "native initialization rollback"
                    )
                )
                raise

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
