"""Non-participant abort/purge hook + retention/destruction (TTL) job.

Two related but distinct controls from Decision D + S0.5:

* :class:`PurgeHook` — invoked the instant a recording session is found to include a
  non-participant (someone the owner is not a party to a conversation with); it must
  destroy whatever was captured so far, not just stop capturing more. Wired into
  :mod:`cadence.spikes.s0_5.recording_gate`'s ``RecordingGate.confirm_participant``.
* :class:`RetentionPurgeJob` — a scheduled sweep that destroys any *retained* recording
  once it exceeds its configured TTL, proving retention is actually enforced rather
  than just documented.

M1 excludes live recording (see ``AGENTS.md``), so no real audio flows through here.
:class:`InMemoryBufferPurgeHook` stands in for "captured bytes" with an in-memory dict,
while :class:`NASBlobPurgeHook` is the real hook: it destroys the content-addressed
recording blobs a real pipeline would have written to :class:`cadence.stores.nas.NASStore`
by calling its irreversible, path-safe :meth:`~cadence.stores.nas.NASStore.delete`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from cadence.spikes.s0_5.audit import TriggerAuditLog
from cadence.stores.nas import BlobRef, NASStore


class PurgeHook(ABC):
    """Destroys whatever has been captured for ``session_id`` so far."""

    #: Whether :meth:`purge` destroys durable on-disk evidence (a real NAS delete) rather
    #: than an in-memory/test buffer. The :class:`~cadence.spikes.s0_5.gate.ComplianceGate`
    #: refuses to open capture unless the wired purge hook is backed by a real delete.
    backed_by_real_delete: ClassVar[bool] = False

    @abstractmethod
    def purge(self, session_id: str, *, reason: str) -> None: ...


class InMemoryBufferPurgeHook(PurgeHook):
    """Spike/test double: "captured" bytes live in a dict; purge clears the entry."""

    def __init__(self, audit: TriggerAuditLog, *, source: str) -> None:
        self._audit = audit
        self._source = source
        self.buffers: dict[str, bytes] = {}

    def write(self, session_id: str, chunk: bytes) -> None:
        self.buffers[session_id] = self.buffers.get(session_id, b"") + chunk

    def purge(self, session_id: str, *, reason: str) -> None:
        self.buffers.pop(session_id, None)
        self._audit.record(session_id, self._source, "purged", detail=reason)


class NASBlobPurgeHook(PurgeHook):
    """Real purge hook: destroys the NAS-stored recording blobs for a session.

    Captured chunks are written to the content-addressed :class:`NASStore`; the hook
    tracks the resulting blob ids per session so an abort (``NON_PARTICIPANT_PRESENT``)
    or a retention-TTL expiry can genuinely delete them from disk — not merely forget a
    reference. Every deletion goes through :meth:`NASStore.delete` (path-safe, fail-closed,
    audited as ``nas_blob_deleted``) and is idempotent, so a re-purge of an
    already-destroyed session is a no-op.
    """

    backed_by_real_delete: ClassVar[bool] = True

    def __init__(self, nas: NASStore, audit: TriggerAuditLog, *, source: str) -> None:
        self._nas = nas
        self._audit = audit
        self._source = source
        self._blobs: dict[str, list[str]] = {}

    def write(self, session_id: str, chunk: bytes) -> BlobRef:
        """Store a captured chunk in the NAS and track its blob id for later purge."""
        ref = self._nas.put(chunk)
        self._blobs.setdefault(session_id, []).append(ref.id)
        return ref

    def blob_ids(self, session_id: str) -> list[str]:
        """The NAS blob ids currently tracked for ``session_id`` (empty once purged)."""
        return list(self._blobs.get(session_id, []))

    def delete_session(self, session_id: str, *, reason: str) -> int:
        """Delete every tracked NAS blob for the session; return how many existed.

        Idempotent: pops the tracking entry so a second call deletes nothing, and each
        underlying :meth:`NASStore.delete` is itself idempotent for an already-absent blob.
        """
        deleted = 0
        for blob_id in self._blobs.pop(session_id, []):
            if self._nas.delete(blob_id, reason=reason):
                deleted += 1
        return deleted

    def purge(self, session_id: str, *, reason: str) -> None:
        self.delete_session(session_id, reason=reason)
        self._audit.record(session_id, self._source, "purged", detail=reason)

    def retention_purger(
        self, *, reason: str = "retention TTL expired"
    ) -> Callable[[str], None]:
        """An ``on_purge`` callback for :class:`RetentionPurgeJob` that deletes NAS blobs.

        The retention job audits the ``purged`` event itself, so this callback only
        performs the (audited) NAS deletion — it must not double-record the audit entry.
        """

        def _purge(session_id: str) -> None:
            self.delete_session(session_id, reason=reason)

        return _purge


@dataclass(frozen=True)
class RetainedRecording:
    """Metadata for one recording under retention (never the verbatim bytes)."""

    session_id: str
    source: str
    stored_at: datetime
    ttl: timedelta
    purged: bool = False

    @property
    def expires_at(self) -> datetime:
        return self.stored_at + self.ttl

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass
class RetentionPurgeJob:
    """Sweeps registered recordings and purges (deletes) any past their TTL.

    ``on_purge`` is an injectable per-session callback invoked before a recording is
    marked purged; wire it to :meth:`NASBlobPurgeHook.retention_purger` so an expired
    recording's content-addressed NAS blobs are genuinely deleted (via
    :meth:`NASStore.delete`), not merely unregistered.
    """

    audit: TriggerAuditLog
    _registry: dict[str, RetainedRecording] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, recording: RetainedRecording) -> None:
        with self._lock:
            self._registry[recording.session_id] = recording

    def run(
        self, now: datetime | None = None, *, on_purge: Callable[[str], None] | None = None
    ) -> list[str]:
        """Purge every expired, not-yet-purged recording. Returns the purged session ids."""
        now = now or datetime.now(tz=UTC)
        purged_ids: list[str] = []
        with self._lock:
            for session_id, rec in list(self._registry.items()):
                if rec.purged or not rec.is_expired(now):
                    continue
                if on_purge is not None:
                    on_purge(session_id)
                self._registry[session_id] = RetainedRecording(
                    session_id=rec.session_id,
                    source=rec.source,
                    stored_at=rec.stored_at,
                    ttl=rec.ttl,
                    purged=True,
                )
                purged_ids.append(session_id)
        for session_id in purged_ids:
            rec = self._registry[session_id]
            self.audit.record(
                session_id,
                rec.source,
                "purged",
                detail=f"retention TTL expired at {rec.expires_at.isoformat()}",
            )
        return purged_ids

    def is_purged(self, session_id: str) -> bool:
        rec = self._registry.get(session_id)
        return rec is not None and rec.purged


__all__ = [
    "PurgeHook",
    "InMemoryBufferPurgeHook",
    "NASBlobPurgeHook",
    "RetainedRecording",
    "RetentionPurgeJob",
]
