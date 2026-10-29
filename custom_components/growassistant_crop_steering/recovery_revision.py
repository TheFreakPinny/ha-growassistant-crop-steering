"""Revision metadata in the existing HA journal envelope, never a second journal.

Only the strict adapter's completed atomic write is an acknowledgment. This is
local filesystem completion, not isolation, a multi-store commit or a hardware
power-loss guarantee. Legacy payloads are never silently certified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store
from homeassistant.util import json as json_util

from .const import DOMAIN

REVISION_SCHEMA = 1
REVISION_FIELD = "participant_revision"
LEGACY_METADATA = {"legacy": True}


class RevisionError(RuntimeError):
    """Storage completion or revision evidence is not established."""


def content_digest(data) -> str:
    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            plain(data), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class JournalIdentity:
    entry_id: str
    pump_entity: str
    store_key: str
    journal_id: str


@dataclass(frozen=True)
class JournalReceipt:
    identity: JournalIdentity
    revision: int
    digest: str
    schema: int = REVISION_SCHEMA


def decode_receipt(raw, data, entry_id, entity, key) -> JournalReceipt:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "entry_id",
        "pump_entity",
        "store_key",
        "journal_id",
        "revision",
        "digest",
        "metadata_digest",
    }:
        raise RevisionError("revision_unknown")
    if (
        type(raw["schema"]) is not int
        or raw["schema"] != REVISION_SCHEMA
        or type(raw["revision"]) is not int
        or raw["revision"] < 1
        or any(
            not isinstance(raw[k], str) or not raw[k]
            for k in ("entry_id", "pump_entity", "store_key", "journal_id", "digest")
        )
        or (raw["entry_id"], raw["pump_entity"], raw["store_key"])
        != (entry_id, entity, key)
        or raw["digest"] != content_digest(data)
        or raw["metadata_digest"]
        != content_digest({k: v for k, v in raw.items() if k != "metadata_digest"})
    ):
        raise RevisionError("revision_invalid")
    try:
        if UUID(raw["journal_id"]).hex != raw["journal_id"]:
            raise ValueError
    except ValueError:
        raise RevisionError("revision_identity_invalid") from None
    return JournalReceipt(
        JournalIdentity(entry_id, entity, key, raw["journal_id"]),
        raw["revision"],
        raw["digest"],
    )


def encode_receipt(receipt: JournalReceipt):
    identity = receipt.identity
    metadata = {
        "schema": receipt.schema,
        "entry_id": identity.entry_id,
        "pump_entity": identity.pump_entity,
        "store_key": identity.store_key,
        "journal_id": identity.journal_id,
        "revision": receipt.revision,
        "digest": receipt.digest,
    }
    return {**metadata, "metadata_digest": content_digest(metadata)}


class _ParticipantStore(Store):
    """Keep HA Store v1 payloads; add revision metadata to the same envelope.

    Bypass HA's migration/corrupt-file fallback and load cache on reads. Wrap
    low-level exceptions before Store can swallow WriteError. Refuse shutdown
    deferral/read-only saves and require the actual writer to have returned.
    """

    def __init__(self, hass, key):
        super().__init__(hass, 1, key, atomic_writes=True)
        self._candidate = None
        self._completed = False
        self._writing = False

    async def async_read_versioned(self):
        def read():
            path = Path(self.path)
            if list(path.parent.glob(path.name + ".corrupt*")):
                raise RevisionError("journal_corrupt")
            # load_json is also the established HA fixture's storage boundary.
            try:
                envelope = json_util.load_json(self.path)
            except Exception as err:
                raise RevisionError("journal_unreadable") from err
            if envelope == {}:
                if path.exists():
                    raise RevisionError("journal_corrupt")
                return None, None
            return self.decode_envelope(envelope)

        return await self.hass.async_add_executor_job(read)

    def decode_envelope(self, envelope):
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("version")) is not int
            or envelope["version"] != 1
            or type(envelope.get("minor_version", 1)) is not int
            or envelope.get("minor_version", 1) != 1
            or envelope.get("key") != self.key
            or "data" not in envelope
        ):
            raise RevisionError("journal_schema_unknown")
        return envelope["data"], envelope.get(REVISION_FIELD) or LEGACY_METADATA

    async def async_load(self):
        data, self.loaded_revision = await self.async_read_versioned()
        return data

    async def async_save_versioned(self, data, metadata):
        if self.hass.state is CoreState.stopping or self._read_only or self._writing:
            raise RevisionError("storage_unavailable")
        self._writing = True
        try:
            self._candidate, self._completed = deepcopy(metadata), False
            await self.async_save(data)
            if not self._completed:
                raise RevisionError("storage_completion_unknown")
        finally:
            self._writing = False

    async def _async_write_data(self, envelope):
        if self._candidate is not None and self._candidate["digest"] != content_digest(
            envelope["data"]
        ):
            raise RevisionError("journal_write_conflict")
        envelope[REVISION_FIELD] = deepcopy(self._candidate)
        try:
            await super()._async_write_data(envelope)
        except Exception as err:
            raise RevisionError("storage_failed") from err
        self._completed = True


class JournalRevision:
    """Per-journal completion state; metadata lives only in that journal's file.

    Uncertainty is sticky for this instance, including a late successful write.
    Legacy histories can still be saved, but never acquire revision evidence.
    """

    def __init__(self, entry_id, entity, store_key):
        self.identity = JournalIdentity(entry_id, entity, store_key, uuid4().hex)
        self.receipt: JournalReceipt | None = None
        self.status = "unknown"
        self.generation = 0
        self._fresh = False
        self._busy = False
        self._uncertain = False

    async def load(self, store):
        data = await store.async_load()
        metadata = getattr(store, "loaded_revision", None)
        self._fresh = data is None and metadata is None
        if metadata is not None and metadata != LEGACY_METADATA:
            self.receipt = decode_receipt(
                metadata,
                data,
                self.identity.entry_id,
                self.identity.pump_entity,
                self.identity.store_key,
            )
            self.identity = self.receipt.identity
            self.status = "confirmed"
        else:
            self.status = "absent" if self._fresh else "legacy"
        return data

    def evidence(self, data, entity):
        receipt = self.receipt
        if (
            self._busy
            or self._uncertain
            or receipt is None
            or entity != self.identity.pump_entity
        ):
            return None
        try:
            return receipt if receipt.digest == content_digest(data) else None
        except (ValueError, TypeError):
            return None

    async def save(self, store, data, *, timeout, create_task):
        if self._busy:
            raise RevisionError("journal_write_busy")
        if isinstance(store, Store) and store.key != self.identity.store_key:
            raise RevisionError("journal_identity_conflict")
        # A prior uncertain completion cannot be repaired in this runtime.
        # Existing safety shutdown may still save its conservative payload.
        digest = content_digest(data)
        if (
            self.receipt is not None
            and self.receipt.digest == digest
            and not self._uncertain
        ):
            return
        self._busy = True
        self.generation += 1
        previous_status = self.status
        self.status = "pending"
        candidate = None
        if not self._uncertain and (self._fresh or self.receipt is not None):
            candidate = JournalReceipt(
                self.identity,
                1 if self.receipt is None else self.receipt.revision + 1,
                digest,
            )
        try:
            metadata = None if candidate is None else encode_receipt(candidate)
            if self._uncertain:
                metadata = {
                    "schema": REVISION_SCHEMA,
                    "status": "uncertain",
                    "last_confirmed": None
                    if self.receipt is None
                    else encode_receipt(self.receipt),
                    "digest": digest,
                }
            if hasattr(store, "async_save_versioned"):
                work = store.async_save_versioned(deepcopy(data), metadata)
            else:
                # Old explicit test doubles can exercise behavior, not attest IO.
                candidate = None
                work = store.async_save(deepcopy(data))
            task = create_task(work)
            task.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise RevisionError("journal_write_timeout")
            task.result()
            if candidate is not None:
                self.receipt = candidate
                self._fresh = False
                self.status = "confirmed"
            else:
                self.status = previous_status
        except BaseException:
            self._uncertain = True
            self.status = "uncertain"
            raise
        finally:
            self._busy = False


@dataclass(frozen=True)
class BootEpoch:
    value: str


@dataclass(frozen=True)
class ControllerEpoch:
    value: str


@dataclass(frozen=True)
class PumpResourceEpoch:
    value: str
    entry_id: str
    entity_id: str | None


@dataclass(frozen=True)
class OwnershipGeneration:
    value: int


@dataclass(frozen=True)
class SafetyGeneration:
    value: int


@dataclass(frozen=True)
class RuntimeEpochs:
    boot: BootEpoch
    controller: ControllerEpoch
    resource: PumpResourceEpoch
    ownership_generation: OwnershipGeneration
    safety_generation: SafetyGeneration


class ParticipantRuntime:
    def __init__(self, hass, entry, resource=None):
        self.boot = hass.data.setdefault(
            f"{DOMAIN}.recovery_boot_epoch", BootEpoch(uuid4().hex)
        )
        self.controller = ControllerEpoch(uuid4().hex)
        self.resource = uuid4().hex if resource is None else resource.resource
        self.entry = entry
        self.ownership_generation = 0
        self.safety_generation = 0
        self._remove = None
        self._closed = False

    def start(self):
        if self._remove is None and not self._closed:
            self._remove = self.entry.add_update_listener(self._entry_changed)

    async def _entry_changed(self, hass, entry):
        # Conservative: every entry update revokes local ownership evidence,
        # including target ABA where callbacks see only the final target.
        self.ownership_changed()
        self.invalidate()

    def close(self):
        self.invalidate()
        self._closed = True
        if self._remove is not None:
            self._remove()
            self._remove = None

    def snapshot(self, entity, generation=0):
        return RuntimeEpochs(
            self.boot,
            self.controller,
            PumpResourceEpoch(self.resource, self.entry.entry_id, entity),
            OwnershipGeneration(self.ownership_generation),
            SafetyGeneration(self.safety_generation + generation),
        )

    def ownership_changed(self):
        self.ownership_generation += 1

    def invalidate(self):
        self.safety_generation += 1
