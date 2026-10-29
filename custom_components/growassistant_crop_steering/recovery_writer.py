"""Diagnostic writer observations; HA events cannot establish actuator isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery_revision import RuntimeEpochs
from .recovery_snapshot import ResourceBinding

if TYPE_CHECKING:
    from .recovery_participant import EvidenceRevision, GateRequest


class WriterReason(StrEnum):
    CONFLICT = "writer_conflict_detected"
    VISIBILITY = "writer_visibility_incomplete"
    EPOCH = "ownership_epoch_changed"
    UNPROVEN = "external_writers_unproven"
    EXCLUSIVE = "exclusive_isolation_confirmed"
    OWNER_UNKNOWN = "internal_owner_unknown"
    EXPIRED = "writer_evidence_expired"
    REVISION = "revision_conflict"


@dataclass(frozen=True)
class WriterObservation:
    """Context is an attribution hint, never authenticated writer identity."""

    event_type: str
    domain: str | None
    service: str | None
    context_id: str | None
    parent_id: str | None
    user_id: str | None
    observed_at: datetime
    conflict: bool


@dataclass(frozen=True)
class WriterIsolationEvidence:
    binding: ResourceBinding
    runtime: RuntimeEpochs
    revisions: tuple[EvidenceRevision, ...]
    attempt_id: str
    owner_id: str | None
    internal_owner_known: bool
    monitor_id: str
    monitor_generation: int
    monitoring_since: datetime
    monitoring_active: bool
    writers: tuple[WriterObservation, ...]
    conflicts: tuple[WriterObservation, ...]
    unknown_writers: tuple[str, ...]
    checked_at: datetime
    valid_until: datetime
    reasons: tuple[WriterReason, ...]
    # No current issuer implements an exclusive actuator/gateway contract.
    exclusive_isolation_confirmed: Literal[False] = field(default=False, init=False)
    native_activation_allowed: Literal[False] = field(default=False, init=False)

    @property
    def no_foreign_writers_observed(self) -> bool:
        return not self.writers


class _WriterObserver:
    """Private collector lifetime only; no service interception or production setup.

    All observed pump writes are untrusted, including apparent internal contexts.
    Missing events cannot be detected reliably and remain an explicit blocker.
    Retain a bounded history; overflow is sticky visibility loss, never success.
    """

    def __init__(self, pump, revoke):
        self.pump = pump
        self.hass = pump.hass
        self._revoke = revoke
        self.monitor_id = uuid4().hex
        self.generation = 0
        self.since = dt_util.utcnow()
        self.active = True
        self._writers: list[WriterObservation] = []
        self._conflict = False
        self._gap = False
        self._remove = []
        try:
            self._remove.append(
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._service)
            )
            self._remove.append(
                self.hass.bus.async_listen("state_changed", self._state)
            )
            self._remove.append(
                self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._stop)
            )
        except Exception:
            self.close()

    def _entity(self):
        return self.pump.safety_snapshot().binding.entity_id

    @callback
    def _service(self, event):
        domain = event.data.get("domain")
        service = event.data.get("service")
        data = event.data.get("service_data")
        # Scripts/custom integrations can target the actuator indirectly. HA
        # does not provide a complete target/call graph: retain those as unknown.
        if (
            not isinstance(data, dict)
            or not isinstance(domain, str)
            or not isinstance(service, str)
        ):
            self.visibility_lost()
            return
        targets = data.get("entity_id")
        if isinstance(targets, str):
            targets = tuple(part.strip() for part in targets.split(","))
        elif not isinstance(targets, (list, tuple)):
            targets = ()
        explicit = self._entity() in targets or "all" in targets
        if explicit or domain in {"switch", "homeassistant", "script", "automation"}:
            self._record(event, explicit)

    @callback
    def _state(self, event):
        if event.data.get("entity_id") == self._entity():
            self._record(event, True)

    def _record(self, event, conflict):
        self.generation += 1
        self._conflict |= conflict
        context = event.context
        observation = WriterObservation(
            event.event_type,
            event.data.get("domain"),
            event.data.get("service"),
            getattr(context, "id", None),
            getattr(context, "parent_id", None),
            getattr(context, "user_id", None),
            dt_util.utcnow(),
            conflict,
        )
        if len(self._writers) < 128:
            self._writers.append(observation)
        else:
            self._gap = True
            # Keep a concrete conflict visible even if earlier retained events
            # only had unresolved indirect targets.
            if conflict:
                self._writers[-1] = observation
        self._revoke(WriterReason.CONFLICT if conflict else WriterReason.VISIBILITY)

    def visibility_lost(self):
        """Explicitly report known gaps; their absence cannot prove continuity."""
        self._gap = True
        self.generation += 1
        self._revoke(WriterReason.VISIBILITY)

    @callback
    def _stop(self, event):
        self.close()

    def close(self):
        self.active = False
        self.visibility_lost()
        for remove in self._remove:
            remove()
        self._remove.clear()

    def capture(self, request: GateRequest) -> WriterIsolationEvidence:
        snapshot = self.pump.safety_snapshot()
        revision = next((r for r in request.revisions if r.source == "pump"), None)
        owned = (
            self.hass.data.get(f"{DOMAIN}.native_pump_owner") is self.pump
            and self.pump.ownership is not None
            and self.pump.ownership.pump_entity == snapshot.binding.entity_id
            and snapshot.state not in {"new", "closed"}
            and not self.pump._closing
        )
        reasons = [WriterReason.VISIBILITY, WriterReason.UNPROVEN]
        if not owned:
            reasons.append(WriterReason.OWNER_UNKNOWN)
        if self._conflict:
            reasons.append(WriterReason.CONFLICT)
        if (
            snapshot.binding != request.binding
            or revision is None
            or revision.runtime != snapshot.runtime
            or request.boot_epoch != snapshot.runtime.boot
        ):
            reasons.append(WriterReason.EPOCH)
        if revision is None or (
            revision.receipt != snapshot.version.receipt
            or revision.durable_revision != snapshot.version.revision
        ):
            reasons.append(WriterReason.REVISION)
        if not 0 <= (dt_util.utcnow() - request.captured_at).total_seconds() <= 30:
            reasons.append(WriterReason.EXPIRED)
        unknown = [
            "pre_monitor_history",
            "unobserved_ha_calls",
            "indirect_targets",
            "external_integrations",
            "device_network_writers",
            "direct_actuator_access",
        ]
        if self._gap or not self.active:
            unknown.append("monitoring_gap")
        return WriterIsolationEvidence(
            snapshot.binding,
            snapshot.runtime,
            request.revisions,
            request.attempt_id,
            snapshot.runtime.controller.value if owned else None,
            owned,
            self.monitor_id,
            self.generation,
            self.since,
            self.active,
            tuple(self._writers),
            tuple(w for w in self._writers if w.conflict),
            tuple(unknown),
            request.captured_at,
            request.captured_at + timedelta(seconds=30),
            tuple(reasons),
        )
