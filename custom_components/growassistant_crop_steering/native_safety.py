"""Private development safety policies. No production activation or repair API."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .vwc import normalize_vwc_sensors


@dataclass(frozen=True)
class SensorPolicy:
    """Conservative harness settings; never registered as integration options."""

    maximum_age_s: float = 300
    minimum_valid: int = 1
    minimum_percent: float = 100
    maximum_spread: float = 10

    def valid(self) -> bool:
        return (
            type(self.minimum_valid) is int
            and self.minimum_valid >= 1
            and all(
                type(value) in {int, float} and math.isfinite(value)
                for value in (
                    self.maximum_age_s,
                    self.minimum_percent,
                    self.maximum_spread,
                )
            )
            and self.maximum_age_s > 0
            and 0 < self.minimum_percent <= 100
            and 0 <= self.maximum_spread <= 100
        )


@dataclass(frozen=True)
class SensorAssessment:
    average: float | None
    configured: int
    valid: int
    rejected: dict[str, str]
    reasons: tuple[str, ...]

    def diagnostic(self) -> dict[str, Any]:
        return asdict(self)


def assess_sensors(
    hass: HomeAssistant,
    configured: Any,
    now: datetime,
    policy: SensorPolicy,
    overrides: dict[str, Any] | None = None,
) -> SensorAssessment:
    """Use original last_updated, never startup time or a restored-value report.

    HA timestamps establish receipt, not physical sample time. Integrations which
    republish historical values need independent source freshness verification.
    """
    entities = list(dict.fromkeys(normalize_vwc_sensors(configured)))
    rejected: dict[str, str] = {}
    values = []
    reasons = []
    if not policy.valid():
        return SensorAssessment(None, len(entities), 0, {}, ("invalid_sensor_policy",))
    for entity in entities:
        state = (
            overrides[entity]
            if overrides and entity in overrides
            else hass.states.get(entity)
        )
        reason = None
        if state is None or state.state in {"unknown", "unavailable", ""}:
            reason = "unavailable"
        elif state.attributes.get("restored"):
            reason = "restored_value"
        else:
            try:
                value = float(state.state)
                if not math.isfinite(value) or not 0 <= value <= 100:
                    reason = "invalid_reading"
                if reason is not None:
                    rejected[entity] = reason
                    continue
                stamp = state.last_updated
                if stamp.tzinfo is None or now.tzinfo is None:
                    reason = "invalid_timestamp"
                else:
                    age = (now - stamp).total_seconds()
                    if age < 0:
                        reason = "future_timestamp"
                    elif age > policy.maximum_age_s:
                        reason = "stale"
            except (ValueError, TypeError, OverflowError):
                reason = "invalid_reading"
        if reason:
            rejected[entity] = reason
        else:
            values.append(value)
    # Corruption blocks even with quorum: never average away a faulty sensor.
    if any(value == "invalid_reading" for value in rejected.values()):
        reasons.append("invalid_vwc")
    if not values:
        reasons.append("vwc_unavailable")
    if (
        len(values) < policy.minimum_valid
        or not entities
        or len(values) * 100 < len(entities) * policy.minimum_percent
    ):
        reasons.append("sensor_quorum_failed")
    if values and max(values) - min(values) > policy.maximum_spread:
        reasons.append("sensor_disagreement")
    return SensorAssessment(
        sum(values) / len(values) if values else None,
        len(entities),
        len(values),
        rejected,
        tuple(reasons),
    )


@dataclass(frozen=True)
class OwnershipEvidence:
    """Review evidence, deliberately incapable of authorizing production here.

    HA cannot prove absence of arbitrary external writers. Only a local simulated
    harness is admissible in this phase. No persisted acknowledgment is trusted.
    """

    pump_entity: str
    legacy_relinquished: bool = False
    related_writers_reviewed: bool = False
    exclusive_control_verified: bool = False
    simulated_pump: bool = False

    def reasons(self, pump_entity: str | None) -> tuple[str, ...]:
        reasons = []
        if self.pump_entity != pump_entity:
            reasons.append("pump_ownership_target_changed")
        if self.legacy_relinquished is not True:
            reasons.append("blueprint_ownership_unresolved")
        if (
            self.related_writers_reviewed is not True
            or self.exclusive_control_verified is not True
        ):
            reasons.append("pump_ownership_unverified")
        if self.simulated_pump is not True:
            reasons.append("production_activation_prohibited")
        return tuple(reasons)


@dataclass(frozen=True)
class RecoveryAssessment:
    """Review diagnostics are never activation permission or fault clearance."""

    restart: str
    reasons: tuple[str, ...]
    review_required: bool
    native_activation_allowed: bool = False

    def diagnostic(self) -> dict[str, Any]:
        return asdict(self)


def assess_recovery(
    *,
    journal: dict[str, Any],
    pump_unresolved: bool,
    cycle: dict[str, Any] | None,
    cycle_reason: str | None,
    sensor_reasons: tuple[str, ...],
    storage_failed: bool = False,
    pump_restart_unresolved: bool = False,
    pump_reasons: tuple[str, ...] = (),
) -> RecoveryAssessment:
    reasons = list(pump_reasons)
    if pump_restart_unresolved or journal.get("pump_restart_requires_review"):
        reasons.append(
            "pump_restart_requires_review"
            if pump_unresolved
            else "pump_shutdown_reconciled_after_restart"
        )
    if pump_unresolved:
        reasons.append("unconfirmed_pump_shutdown")
    for record in journal.get("operations", {}).values():
        if record.get("outcome") == "unconfirmed_shutdown":
            reasons.append("unconfirmed_pump_shutdown")
        if (
            record.get("accounting_pending")
            or record.get("outcome") == "accounting_or_storage_fault"
        ):
            reasons.append("incomplete_accounting")
        if record.get("state") not in {"completed", "interrupted", "fault"}:
            reasons.append("interrupted_shot")
        if record.get("state") == "interrupted" and record.get("recovery_required"):
            reasons.append("interrupted_shot")
        if record.get("delivery") == "possible_partial" or record.get(
            "delivery_uncertain"
        ):
            reasons.append("possible_partial_delivery")
        if record.get("recovery_required"):
            reasons.append("shot_review_required")
    if journal.get("accounting_inconsistent"):
        reasons.append("incomplete_accounting")
    if journal.get("recovery_required") or storage_failed:
        reasons.append("storage_inconsistency")
    if journal.get("missed_cycle_reset"):
        reasons.append("missed_sunrise_or_cycle_reset")
    if cycle and cycle.get("pending"):
        reasons.append("incomplete_cycle_transition")
    if cycle_reason:
        reasons.append(cycle_reason)
    if any("sunrise" in reason or "reset" in reason for reason in reasons):
        reasons.append("missed_sunrise_or_cycle_reset")
    if sensor_reasons:
        reasons.append("sensor_unavailability")
        reasons.extend(sensor_reasons)
    reasons = list(dict.fromkeys(reasons))
    return RecoveryAssessment(
        "unsafe_recovery"
        if any(reason != "cycle_restart_requires_review" for reason in reasons)
        else "clean_restart",
        tuple(reasons),
        bool(reasons),
    )


def migration_eligibility(
    recovery: RecoveryAssessment,
    sensors: SensorAssessment,
    ownership: OwnershipEvidence | None,
    pump_entity: str | None,
    *,
    pump_off_verified: bool,
    state_reconciled: bool,
    rollback: bool = False,
) -> dict[str, Any]:
    """Read-only future review checklist; always prohibits native activation."""
    reasons = list(recovery.reasons) + list(sensors.reasons)
    if pump_off_verified is not True:
        reasons.append("pump_off_not_verified")
    if state_reconciled is not True:
        reasons.append("state_not_reconciled")
    reasons.extend(
        ("pump_ownership_unknown",)
        if ownership is None
        else ownership.reasons(pump_entity)
    )
    return {
        "procedure": "rollback" if rollback else "migration",
        "review_eligible": not reasons,
        "reasons": tuple(dict.fromkeys(reasons)),
        "native_activation_allowed": False,
    }
