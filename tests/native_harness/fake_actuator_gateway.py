"""Isolated model: no production imports this module; no hardware adapter exists."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal

from custom_components.growassistant_crop_steering.actuator_contract import (
    ActuatorIdentity,
    CommandIntent,
    OwnershipEpoch,
    OwnershipLease,
    validate_command,
    validate_lease,
)
from custom_components.growassistant_crop_steering.recovery_revision import (
    BootEpoch,
    OwnershipGeneration,
)


@dataclass
class FakeClock:
    tick: float = 0

    def advance(self, duration: float) -> None:
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("monotonic duration required")
        self.tick += duration


@dataclass(frozen=True)
class Receipt:
    """Model result, deliberately separate from design-only CommandReceipt.

    Executed means simulated execution only. No receipt attests physical OFF.
    """

    intent: CommandIntent
    gateway_boot: str
    status: Literal["accepted", "executing", "executed", "uncertain", "rejected"]
    observed_state: Literal["on", "off", "unknown"]
    tick: float
    reason: str = ""
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class JournalSnapshot:
    """Only journal data, never the live actor or independent model witness."""

    schema: int
    revision: int
    boot: int
    generation: int
    records: dict[str, Receipt]
    stops: tuple[SafetyReceipt, ...]


@dataclass
class FakeWitness:
    """Test assumption: survives journal restore, NOT a hardware trust root.

    Losing or rolling back this witness together with storage is unprovable.
    Callers must represent that case with witness=None; ON then stays blocked.
    """

    expected: JournalSnapshot | None = None
    blocked: bool = False


@dataclass(frozen=True)
class PendingCommit:
    expected: JournalSnapshot
    proposed: JournalSnapshot
    runtime: int
    stop_serial: int


@dataclass
class FakePersistence:
    """Synchronous atomic journal and separately retained model witness.

    :delay commits but loses its acknowledgement until acknowledge() is called.
    Receipts can arrive in any order; none clear the uncertainty latch.
    Reorderable storage proposals are deliberately unsupported and fail closed.
    """

    _actor: _FakeActuator = field(default_factory=lambda: _FakeActuator(), init=False)
    witness: FakeWitness | None = field(default_factory=FakeWitness)
    stop_serial: int = 0
    runtime: int = 0
    schema: int = 1
    revision: int = 0
    boot: int = 0
    generation: int = 0
    records: dict[str, Receipt] = field(default_factory=dict)
    stops: tuple[SafetyReceipt, ...] = ()
    fail: str | None = None
    delayed_receipts: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.witness is not None and self.witness.expected is None:
            empty = JournalSnapshot(1, 0, 0, 0, {}, ())
            if self.snapshot() != empty:
                # Loading journal data must never silently mint a trust root.
                self.witness.blocked = True
            else:
                self.witness.expected = self.snapshot()

    def snapshot(self) -> JournalSnapshot:
        return deepcopy(
            JournalSnapshot(
                self.schema,
                self.revision,
                self.boot,
                self.generation,
                self.records,
                self.stops,
            )
        )

    def _install(self, snapshot: JournalSnapshot) -> None:
        for name in ("schema", "revision", "boot", "generation", "records", "stops"):
            setattr(self, name, deepcopy(getattr(snapshot, name)))

    def restore(self, snapshot: JournalSnapshot) -> None:
        """Fault injection: never restore actor, witness, runtime or stop fence."""
        self._install(snapshot)
        if not self.check():
            self.stop_serial += 1
            self._actor.off()

    def check(self) -> bool:
        witness = self.witness
        valid = (
            witness is not None
            and not witness.blocked
            and type(self.schema) is int
            and self.schema == 1
            and self.snapshot() == witness.expected
        )
        if not valid and witness is not None:
            self.poison()
        return valid

    def poison(self) -> None:
        if self.witness is not None:
            self.witness.blocked = True

    def lookup(self, command_id: str) -> Receipt | None:
        if not isinstance(self.records, dict):
            return None
        receipt = self.records.get(command_id)
        if not isinstance(receipt, Receipt):
            return None
        if not self.check():
            return replace(receipt, status="uncertain", reason="storage_untrusted")
        return receipt

    def stage(self, proposed: JournalSnapshot) -> PendingCommit:
        """Capture the authority fence for a deterministic storage proposal."""
        pending = PendingCommit(
            self.snapshot(), deepcopy(proposed), self.runtime, self.stop_serial
        )
        # A queued commit has no synchronous durable acknowledgement. This
        # model cannot safely continue ON while storage may reorder proposals.
        self.poison()
        return pending

    def deliver(self, commit: PendingCommit) -> bool:
        """Reject every queued proposal: no asynchronous commit protocol exists.

        Even a fabricated proposal with matching diagnostic fences cannot install
        data or turn a late storage response into fresh ON authority.
        """
        self.poison()
        return False

    def acknowledge(self, index: int = 0) -> tuple[str, int]:
        """Late acknowledgement is historical evidence only, never an unlock."""
        return self.delayed_receipts.pop(index)

    def write(self, phase: str, mutation: Callable[[], None]) -> None:
        if not self.check():
            raise OSError("untrusted or uncertain fake storage")
        failure = self.fail
        if failure == f"{phase}:before":
            self.fail = None
            self.poison()
            raise OSError("fake persistence before commit")
        mutation()
        self.revision += 1
        self.witness.expected = self.snapshot()
        if failure in {f"{phase}:after", f"{phase}:delay"}:
            self.fail = None
            if failure.endswith(":delay"):
                self.delayed_receipts.append((phase, self.revision))
            self.poison()
            raise OSError("fake persistence acknowledgement unknown")


class _FakeActuator:
    """Fixed in-memory output; apply is atomic and never awaits or sends IO."""

    def __init__(self) -> None:
        self.state = "unknown"
        self.events: list[str] = []
        self.off_confirmable = True

    def off(self) -> bool:
        if not self.off_confirmable:
            self.state = "unknown"
            return False
        if self.state != "off":
            self.events.append("off")
        self.state = "off"
        return True

    def on(self) -> None:
        self.events.append("on")
        self.state = "on"


@dataclass(frozen=True)
class SafetyReceipt:
    gateway_boot: str
    reason: str
    result: Literal["software_off", "unknown"]
    tick: float
    native_activation_allowed: Literal[False] = field(default=False, init=False)
    coordination_ready: Literal[False] = field(default=False, init=False)


class FakeGateway:
    native_activation_allowed = False
    coordination_ready = False

    def __init__(
        self,
        actuator: ActuatorIdentity,
        clock: FakeClock,
        persistence: FakePersistence,
        *,
        watchdog_timeout: float = 10,
    ) -> None:
        if not math.isfinite(watchdog_timeout) or watchdog_timeout <= 0:
            raise ValueError("finite positive watchdog timeout required")
        self.actuator = actuator
        self.clock = clock
        self.store = persistence
        self._actor = persistence._actor
        persistence.runtime += 1
        self._runtime = persistence.runtime
        self._stop_serial = persistence.stop_serial
        self._lock = asyncio.Lock()
        self._lease: OwnershipLease | None = None
        self._last_sequence = 0
        self._execution_sequence = 0
        self._last_tick = clock.tick
        self._watchdog_timeout = watchdog_timeout
        self._heartbeat = clock.tick
        self.stopped = True
        self.fault = False
        self.off_receipts: list[SafetyReceipt] = []
        self.boot = "uncommitted"
        try:
            self.store.write("boot", self._boot_commit)
            self.boot = f"fake-boot-{self.store.boot}"
            for key, receipt in tuple(self.store.records.items()):
                if receipt.status in {"accepted", "executing"}:
                    uncertain = replace(
                        receipt,
                        status="uncertain",
                        observed_state="unknown",
                        reason="interrupted_by_restart",
                    )
                    self.store.write(
                        "recover",
                        lambda k=key, r=uncertain: self.store.records.__setitem__(k, r),
                    )
        except OSError:
            self.fault = True
        self.safety_off("boot")

    def _boot_commit(self) -> None:
        self.store.boot += 1

    @property
    def state(self) -> str:
        return self._actor.state

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._actor.events)

    @property
    def lease(self) -> OwnershipLease | None:
        return self._lease

    def _current(self) -> bool:
        return (
            self._runtime == self.store.runtime
            and self.boot == f"fake-boot-{self.store.boot}"
        )

    def safety_off(self, reason: str) -> bool:
        """Latch before any output attempt. No lease, lock or persistence needed."""
        self.store.stop_serial += 1
        self._stop_serial = self.store.stop_serial
        self.stopped = True
        self._lease = None
        confirmed = self._actor.off()
        receipt = SafetyReceipt(
            self.boot,
            reason,
            "software_off" if confirmed else "unknown",
            self.clock.tick,
        )
        self.off_receipts.append(receipt)
        # OFF and the actor-wide fence precede every fallible audit operation.
        # Obsolete instances may stop the actor but must never write its journal.
        if self._current():
            try:
                self.store.write(
                    "stop",
                    lambda: setattr(self.store, "stops", self.store.stops + (receipt,)),
                )
            except OSError:
                self.fault = True
        return confirmed

    def poll(self) -> None:
        """Explicit deterministic watchdog scheduling; never uses system time."""
        if not self.store.check():
            self.fault = True
            self.safety_off("storage_untrusted")
            return
        now = self.clock.tick
        if not math.isfinite(now) or now < self._last_tick:
            self.fault = True
            self.safety_off("clock_fault")
            return
        self._last_tick = now
        if (
            not self._current()
            or self._stop_serial != self.store.stop_serial
            or now - self._heartbeat >= self._watchdog_timeout
            or self.state == "unknown"
            or (self._lease is not None and now >= self._lease.expires_tick)
        ):
            self.safety_off("watchdog_or_lease_or_state")

    def heartbeat(self) -> None:
        self.poll()
        if not self.stopped:
            self._heartbeat = self.clock.tick

    def connection_lost(self) -> None:
        self.safety_off("ha_connection_lost")

    def unknown_state(self) -> None:
        self._actor.state = "unknown"
        self.safety_off("unknown_state")

    def set_off_confirmation(self, available: bool) -> None:
        """Fault injection into the fixed fake, never an external actor adapter."""
        self._actor.off_confirmable = available

    def recheck(self) -> bool:
        """Explicit model-only reconciliation; never releases production gates."""
        self.poll()
        if self.fault or not self._current() or self._lease is not None:
            return False
        if not self._actor.off():
            return False
        self.stopped = False
        self._heartbeat = self.clock.tick
        return True

    def grant(self, owner: str, ha_boot: BootEpoch, duration: float) -> OwnershipLease:
        self.poll()
        if self.stopped or self.fault or self._lease is not None or self.state != "off":
            raise ValueError("gateway not ready for a lease")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("finite positive lease required")
        epoch = OwnershipEpoch(
            self.boot, OwnershipGeneration(self.store.generation + 1)
        )
        lease = OwnershipLease(
            self.actuator,
            epoch,
            owner,
            ha_boot,
            f"{self.boot}-lease-{epoch.generation.value}",
            self.clock.tick,
            self.clock.tick + duration,
        )
        if validate_lease(
            lease,
            actuator=self.actuator,
            epoch=epoch,
            ha_boot=ha_boot,
            now_tick=self.clock.tick,
        ).blockers:
            raise ValueError("invalid lease")
        try:
            self.store.write(
                "grant",
                lambda: setattr(self.store, "generation", epoch.generation.value),
            )
        except OSError:
            self.fault = True
            self.safety_off("persistence")
            raise
        self._lease = lease
        self._last_sequence = 0
        self._execution_sequence = 0
        return lease

    def revoke(self) -> None:
        self.safety_off("lease_revoked")

    def _receipt(self, intent: CommandIntent, status: str, reason: str = "") -> Receipt:
        return Receipt(intent, self.boot, status, self.state, self.clock.tick, reason)

    def _blocker(self, intent: CommandIntent, last_sequence: int) -> str:
        self.poll()
        if self.stopped or self.fault or self._lease is None:
            return "gateway_stopped_or_no_lease"
        blockers = validate_command(
            intent,
            self._lease,
            actuator=self.actuator,
            epoch=self._lease.epoch,
            ha_boot=self._lease.ha_boot,
            now_tick=self.clock.tick,
            last_sequence=last_sequence,
        ).blockers
        return ",".join(blockers)

    def accept(self, intent: CommandIntent) -> Receipt:
        """Synchronous atomic admission, independent of execution lock."""
        self.poll()
        existing = self.store.lookup(intent.command_id)
        if existing is not None:
            if existing.intent != intent:
                return self._receipt(intent, "rejected", "command_id_conflict")
            return existing
        reason = self._blocker(intent, self._last_sequence)
        if reason:
            return self._receipt(intent, "rejected", reason)
        receipt = self._receipt(intent, "accepted")
        try:
            self.store.write(
                "accept",
                lambda: self.store.records.__setitem__(intent.command_id, receipt),
            )
        except OSError:
            self.fault = True
            self.safety_off("persistence")
            return self._receipt(intent, "uncertain", "accept_commit_unknown")
        self._last_sequence = intent.sequence
        return receipt

    async def execute(
        self,
        intent: CommandIntent,
        *,
        before_apply: Callable[[], Awaitable[None]] | None = None,
        after_apply: Callable[[], Awaitable[None]] | None = None,
    ) -> Receipt:
        """No implicit acceptance or replay. Hooks simulate transport/ack delays."""
        async with self._lock:
            self.poll()
            receipt = self.store.lookup(intent.command_id)
            if receipt is None or receipt.intent != intent:
                return self._receipt(intent, "rejected", "not_accepted")
            if receipt.status != "accepted":
                return receipt
            # Reserve execution order before awaiting; admission can proceed meanwhile.
            reason = self._blocker(intent, self._execution_sequence)
            if reason or receipt.gateway_boot != self.boot:
                return self._finish(intent, "rejected", reason or "old_boot")
            try:
                if before_apply:
                    await before_apply()
                # Last irreversible point: no await between revalidation and apply.
                reason = self._blocker(intent, self._execution_sequence)
                if reason or self.store.records.get(intent.command_id) != receipt:
                    return self._finish(intent, "rejected", reason or "intent_changed")
                if intent.action == "on" and self.state == "on":
                    return self._finish(intent, "rejected", "already_on")
                self._execution_sequence = intent.sequence
                executing = self._receipt(intent, "executing")
                self.store.write(
                    "execute",
                    lambda: self.store.records.__setitem__(
                        intent.command_id, executing
                    ),
                )
                if intent.action == "on":
                    self._actor.on()
                else:
                    self._actor.off()
                if after_apply:
                    await after_apply()
                if self._blocker(intent, 0):
                    self.safety_off("invalidated_during_operation")
                    return self._finish(
                        intent, "uncertain", "invalidated_during_operation"
                    )
                return self._finish(intent, "executed")
            except (Exception, asyncio.CancelledError):
                self.fault = True
                self.safety_off("execution_interrupted")
                result = self._finish(intent, "uncertain", "execution_interrupted")
                if asyncio.current_task().cancelling():
                    raise
                return result

    def _finish(self, intent: CommandIntent, status: str, reason: str = "") -> Receipt:
        receipt = self._receipt(intent, status, reason)
        # An obsolete process must never overwrite the next boot's journal.
        if not self._current():
            return replace(receipt, status="uncertain", reason="obsolete_boot")
        try:
            self.store.write(
                "finish",
                lambda: self.store.records.__setitem__(intent.command_id, receipt),
            )
        except OSError:
            self.fault = True
            self.safety_off("receipt_commit_unknown")
            return self._receipt(intent, "uncertain", "receipt_commit_unknown")
        return receipt
