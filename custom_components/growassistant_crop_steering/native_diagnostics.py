"""Read-only observations; deliberately independent of native execution objects."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util import json as json_util

from .config import configured_entity_value
from .const import CONF_PUMP_SWITCH, CONF_VWC_SENSOR, DOMAIN
from .irrigation import capture_inputs, evaluate
from .native_safety import (
    RecoveryAssessment,
    SensorPolicy,
    assess_recovery,
    assess_sensors,
    migration_eligibility,
)
from .vwc import normalize_vwc_sensors

DATA_DIAGNOSTICS = "native_diagnostics"
SENSOR_NAMES = {
    "native_engine_status": "Native engine status",
    "native_vwc_configured_count": "Native VWC configured count",
    "native_vwc_valid_count": "Native VWC valid count",
    "native_vwc_rejected_count": "Native VWC rejected count",
    "native_recovery_classification": "Native recovery classification",
    "native_pump_ownership": "Native pump ownership",
    "native_migration_readiness": "Native migration readiness",
    "native_last_shot_outcome": "Native last shot outcome",
}
BINARY_SENSOR_NAMES = {
    "native_activation_available": "Native activation available",
    "native_sensor_readiness": "Native sensor readiness",
    "native_vwc_quorum": "Native VWC quorum",
    "native_vwc_disagreement": "Native VWC disagreement",
}
_LOGGER = logging.getLogger(__name__)
_STORES = ("pump_recovery", "shot_journal", "native_cycle")
_SHOT_STATES = {
    "idle",
    "reserved",
    "starting",
    "running",
    "stopping",
    "completed",
    "interrupted",
    "fault",
}
_CYCLE_PHASES = {
    "p0_transpiration",
    "p1_morning",
    "p2_midday",
    "p3_dryback",
    "ended",
}
_OUTCOMES = {
    "completed",
    "interrupted",
    "failed_activation",
    "unconfirmed_shutdown",
    "accounting_or_storage_fault",
}
_FLAGS = ("p1_active", "p1_done", "p1_window_opened_today", "p2_ref_vwc")
_MESSAGES = {
    "invalid_vwc": "A VWC reading is invalid or outside 0–100%.",
    "vwc_unavailable": "No fresh valid VWC readings are available.",
    "sensor_quorum_failed": "Too few configured VWC sensors are fresh and valid.",
    "sensor_disagreement": "Valid VWC readings exceed the permitted spread.",
    "pump_off_not_verified": "Physical pump-off has not been independently verified.",
    "pump_ownership_unknown": "Exclusive pump ownership has not been verified.",
    "blueprint_ownership_unresolved": "Blueprint relinquishment has not been verified.",
    "state_not_reconciled": "Native and Blueprint cycle state has not been reconciled.",
    "recovery_not_evaluated": "Current native recovery has not been evaluated.",
    "production_activation_prohibited": "No reviewed production activation mechanism exists.",
    "storage_inconsistency": "Historical storage is invalid or could not be read.",
    "interrupted_shot": "Historical shot execution was interrupted or incomplete.",
    "possible_partial_delivery": "Historical delivery may have been partial; volume is unknown.",
    "shot_review_required": "Historical shot evidence requires review.",
    "incomplete_accounting": "Historical shot accounting is incomplete or inconsistent.",
    "unconfirmed_pump_shutdown": "Historical pump shutdown has not been reconciled.",
    "incomplete_cycle_transition": "A historical cycle transition was not committed.",
    "cycle_persistence_inconsistent": "Historical cycle values disagree with current options.",
    "cycle_restart_requires_review": "Historical native cycle requires restart review.",
    "missed_sunrise_or_cycle_reset": "Historical cycle identity differs from the current cycle.",
    "pump_restart_requires_review": "Historical pump recovery requires review.",
    "pump_shutdown_reconciled_after_restart": "Historical shutdown reconciliation still requires review.",
}


def reason_attributes(reasons: tuple[str, ...]) -> dict[str, Any]:
    """Keep machine codes and human explanations together."""
    return {
        "blocking_reasons": reasons,
        "blocking_explanations": [
            _MESSAGES.get(r, r.replace("_", " ").capitalize()) for r in reasons
        ],
    }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _load_store_json(path: str) -> Any:
    """Read the Store envelope without Store's migration/corrupt-file repair.

    Even Store(read_only=True).async_load() can rename corrupt journals. Loading
    raw JSON in HA's executor preserves the original file and never migrates it.
    """
    return json_util.load_json(path, default=None)


def _valid_history(kind: str, value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if kind == "pump_recovery":
        target = value.get("entity_id")
        return (
            isinstance(target, str)
            and valid_entity_id(target)
            and target.split(".")[0] in {"switch", "input_boolean"}
        )
    if kind == "native_cycle":
        return (
            _timestamp(value.get("cycle_id")) is not None
            and isinstance(value.get("values"), dict)
            and isinstance(value.get("phase"), str)
            and value.get("phase") in _CYCLE_PHASES
        )
    return isinstance(value.get("operations"), dict) and all(
        isinstance(operation, str)
        and isinstance(record, dict)
        and isinstance(record.get("state"), str)
        and record["state"] in _SHOT_STATES
        for operation, record in value["operations"].items()
    )


class NativeDiagnosticsCoordinator:
    """One live assessment and a labelled immutable setup-time history snapshot."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.signal = f"{DOMAIN}_native_diagnostics_{entry.entry_id}"
        self.history: dict[str, Any] = {}
        self.history_status: dict[str, str] = {}
        self.loaded_at: str | None = None
        self.data: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._remove_listeners: list[Any] = []
        self._remove_states: Any = None
        self._closed = False

    async def async_load(self) -> None:
        """Read only: no controller initialization, saves, deletes or repair."""
        for kind in _STORES:
            try:
                async with asyncio.timeout(5):
                    envelope = await self.hass.async_add_executor_job(
                        _load_store_json,
                        self.hass.config.path(
                            ".storage", f"{DOMAIN}.{kind}.{self.entry.entry_id}"
                        ),
                    )
                if envelope is None:
                    value = None
                elif (
                    isinstance(envelope, dict)
                    and envelope.get("version") == 1
                    and envelope.get("minor_version", 1) == 1
                    and "data" in envelope
                ):
                    value = envelope["data"]
                    if value is None:
                        self.history_status[kind] = (
                            "no_pending_record"
                            if kind == "pump_recovery"
                            else "invalid"
                        )
                        continue
                else:
                    self.history_status[kind] = "unsupported_or_invalid_envelope"
                    continue
                status = (
                    "missing"
                    if value is None
                    else "loaded"
                    if _valid_history(kind, value)
                    else "invalid"
                )
            except Exception:
                status, value = "load_failed", None
                _LOGGER.warning(
                    "Could not read native diagnostic history %s for %s",
                    kind,
                    self.entry.entry_id,
                )
            self.history_status[kind] = status
            if status == "loaded":
                self.history[kind] = deepcopy(value)
        self.loaded_at = dt_util.utcnow().isoformat()
        self.async_refresh()

    @callback
    def async_start(self) -> None:
        """Install only diagnostic listeners after successful platform setup."""
        self._bind_states()
        self._remove_listeners = [
            async_track_time_interval(
                self.hass, self.async_refresh, timedelta(seconds=60)
            ),
            self.entry.add_update_listener(self._async_options_changed),
        ]

    async def _async_options_changed(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        if not self._closed:
            self._bind_states()
            self.async_refresh()

    @callback
    def _bind_states(self) -> None:
        if self._remove_states:
            self._remove_states()
            self._remove_states = None
        # Include pure decision inputs for historical cycle/checkpoint comparison.
        entities = set(
            normalize_vwc_sensors(configured_entity_value(self.entry, CONF_VWC_SENSOR))
        )
        for value in {**self.entry.data, **self.entry.options}.values():
            if isinstance(value, str) and valid_entity_id(value):
                entities.add(value)
        if entities:
            self._remove_states = async_track_state_change_event(
                self.hass, entities, self.async_refresh
            )

    @callback
    def async_stop(self) -> None:
        self._closed = True
        if self._remove_states:
            self._remove_states()
            self._remove_states = None
        for remove in self._remove_listeners:
            remove()
        self._remove_listeners.clear()

    def _historical_recovery(self) -> RecoveryAssessment:
        # Conservative interpretation on a copy; original journals remain untouched.
        journal = deepcopy(self.history.get("shot_journal", {"operations": {}}))
        records = journal["operations"].values()
        for record in records:
            if record["state"] in {"interrupted", "fault"}:
                record["recovery_required"] = True
            if record["state"] == "completed" and record.get("accounted") is not True:
                journal["accounting_inconsistent"] = True
            if record["state"] in {"starting", "running", "stopping"}:
                record["delivery_uncertain"] = True
        checkpoint = journal.get("last_accounting")
        cycle = self.history.get("native_cycle")
        cycle_reason = None
        current_cycle = evaluate(
            capture_inputs(self.hass, self.entry), dt_util.now()
        ).cycle_id
        if checkpoint is not None:
            if (
                not isinstance(checkpoint, dict)
                or not isinstance(checkpoint.get("values"), dict)
                or not checkpoint["values"]
            ):
                journal["accounting_inconsistent"] = True
            elif current_cycle != checkpoint.get("cycle_id"):
                journal["missed_cycle_reset"] = True
            elif any(
                self.entry.options.get(key) != value
                for key, value in checkpoint["values"].items()
            ):
                journal["accounting_inconsistent"] = True
        elif any(
            r["state"] == "completed" and r.get("accounted") is True for r in records
        ):
            journal["accounting_inconsistent"] = True
        if cycle:
            cycle_reason = (
                "missed_sunrise_or_cycle_reset"
                if current_cycle != cycle["cycle_id"]
                else "cycle_persistence_inconsistent"
                if cycle.get("pending")
                or any(
                    self.entry.options.get(key) != cycle["values"].get(key)
                    for key in _FLAGS
                )
                else "cycle_restart_requires_review"
            )
        return assess_recovery(
            journal=journal,
            pump_unresolved="pump_recovery" in self.history,
            pump_restart_unresolved="pump_recovery" in self.history,
            cycle=cycle,
            cycle_reason=cycle_reason,
            sensor_reasons=(),
            storage_failed=any(
                status in {"invalid", "load_failed", "unsupported_or_invalid_envelope"}
                for status in self.history_status.values()
            ),
        )

    def _last_outcome(self) -> tuple[str | None, dict[str, Any]]:
        operations = self.history.get("shot_journal", {}).get("operations", {})
        candidates = []
        # Do not choose an older record if a newer operation has uncertain ordering.
        for operation, record in operations.items():
            stamp = _timestamp(record.get("reserved_at"))
            if stamp is None:
                return None, {"evidence": "insufficient_history"}
            candidates.append((stamp, operation, record))
        if not candidates:
            return None, {"evidence": "no_history"}
        latest = max(item[0] for item in candidates)
        selected = [item for item in candidates if item[0] == latest]
        if len(selected) != 1:
            return None, {"evidence": "ambiguous_history"}
        _, operation, record = selected[0]
        outcome = record.get("outcome")
        return (
            outcome if isinstance(outcome, str) and outcome in _OUTCOMES else None,
            {
                "evidence": "persisted_history",
                "operation_id": operation,
                **{
                    key: record.get(key)
                    for key in (
                        "state",
                        "reserved_at",
                        "finished_at",
                        "delivery",
                        "delivery_uncertain",
                        "accounted",
                        "shutdown_confirmed",
                    )
                },
            },
        )

    @callback
    def async_refresh(self, event: Any = None) -> None:
        """Assess observed state without calling any HA service or native API."""
        if self._closed:
            return
        now = dt_util.utcnow()
        policy = SensorPolicy()
        sensors = assess_sensors(
            self.hass, configured_entity_value(self.entry, CONF_VWC_SENSOR), now, policy
        )
        historical = self._historical_recovery()
        recovery = RecoveryAssessment(
            "not_evaluated", (*historical.reasons, "recovery_not_evaluated"), True
        )
        pump_entity = configured_entity_value(self.entry, CONF_PUMP_SWITCH)
        pump = (
            self.hass.states.get(pump_entity)
            if isinstance(pump_entity, str) and valid_entity_id(pump_entity)
            else None
        )
        migration = migration_eligibility(
            recovery,
            sensors,
            None,
            pump_entity,
            pump_off_verified=False,
            state_reconciled=False,
        )
        reasons = tuple(
            dict.fromkeys(
                (
                    *migration["reasons"],
                    "blueprint_ownership_unresolved",
                    "production_activation_prohibited",
                )
            )
        )
        observed = {"evidence": "live_observation", "evaluated_at": now.isoformat()}
        history = {
            "evidence": "persisted_snapshot",
            "loaded_at": self.loaded_at,
            "stores": dict(self.history_status),
        }
        sensor_attrs = {
            **observed,
            **sensors.diagnostic(),
            "freshness_policy": asdict(policy),
            "timestamp_source": "HA last_updated; receipt time, not physical acquisition time",
            **reason_attributes(sensors.reasons),
        }
        quorum = (
            sensors.configured > 0
            and sensors.valid >= policy.minimum_valid
            and sensors.valid * 100 >= sensors.configured * policy.minimum_percent
        )
        disagreement = (
            None if sensors.valid < 2 else "sensor_disagreement" in sensors.reasons
        )
        last, last_attrs = self._last_outcome()
        self.data = {
            "native_engine_status": (
                "disabled",
                {
                    "evidence": "production_policy",
                    "execution_state": "not_evaluated",
                    "production_executor_policy": "legacy_blueprint",
                    "native_stack_initialized_by_diagnostics": False,
                },
            ),
            "native_activation_available": (
                False,
                {"evidence": "production_policy", **reason_attributes(reasons)},
            ),
            "native_sensor_readiness": (not sensors.reasons, sensor_attrs),
            "native_vwc_configured_count": (sensors.configured, observed),
            "native_vwc_valid_count": (sensors.valid, observed),
            "native_vwc_rejected_count": (
                len(sensors.rejected),
                {
                    **observed,
                    "rejected": sensors.rejected,
                    "stale_count": sum(r == "stale" for r in sensors.rejected.values()),
                    "unavailable_count": sum(
                        r == "unavailable" for r in sensors.rejected.values()
                    ),
                },
            ),
            "native_vwc_quorum": (quorum, sensor_attrs),
            "native_vwc_disagreement": (disagreement, sensor_attrs),
            "native_recovery_classification": (
                "historical_review_required"
                if historical.reasons
                else "historical_evidence_present"
                if self.history
                else "not_evaluated",
                {
                    **history,
                    "current_recovery": "not_evaluated",
                    "historical_review_required": bool(historical.reasons),
                    **reason_attributes(historical.reasons),
                },
            ),
            "native_pump_ownership": (
                "unverified",
                {
                    "evidence": "not_evaluated",
                    "pump_entity": pump_entity,
                    "observed_state": None if pump is None else pump.state,
                    "observed_at": None
                    if pump is None
                    else pump.last_updated.isoformat(),
                    "physical_pump_off_verified": False,
                },
            ),
            "native_migration_readiness": (
                "ineligible",
                {
                    **observed,
                    **history,
                    "evidence": "live_assessment_and_persisted_snapshot",
                    **migration,
                    **reason_attributes(reasons),
                    "sensor_readiness": "ready" if not sensors.reasons else "blocked",
                    "recovery_readiness": "not_evaluated",
                    "pump_off_verification": "unverified",
                    "blueprint_relinquishment": "unverified",
                    "exclusive_pump_ownership": "unverified",
                    "cycle_state_reconciliation": "not_evaluated",
                    "overall_activation_eligibility": False,
                },
            ),
            "native_last_shot_outcome": (last, {**history, **last_attrs}),
        }
        async_dispatcher_send(self.hass, self.signal)


class NativeDiagnosticEntity(Entity):
    """Common push entity, attached to the existing integration device."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NativeDiagnosticsCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self.key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {"identifiers": {(DOMAIN, coordinator.entry.entry_id)}}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self.async_write_ha_state
            )
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.data[self.key][1]
