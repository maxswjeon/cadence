"""Non-participant abort/purge hook + retention/destruction (TTL) job.

Two related but distinct controls from Decision D + S0.5:

* :class:`PurgeHook` — invoked the instant a recording session is found to include a
  non-participant (someone the owner is not a party to a conversation with); it must
  destroy whatever was captured so far, not just stop capturing more. Wired into
  :mod:`cadence.spikes.s0_5.recording_gate`'s ``RecordingGate.confirm_participant``.
* :class:`RetentionPurgeJob` — a scheduled sweep that destroys any *retained* recording
  once it exceeds its configured TTL, proving retention is actually enforced rather
  than just documented.

Neither module holds real audio: M1 excludes live recording (see ``AGENTS.md``), so
these operate over a lightweight in-memory registry that stands in for "whatever the
real recording pipeline would have written to NAS." A real implementation purges via
:class:`cadence.stores.nas.NASStore`, which does not yet expose a delete — see
``s0_5.md`` for that gap.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cadence.spikes.s0_5.audit import TriggerAuditLog


class PurgeHook(ABC):
    """Destroys whatever has been captured for ``session_id`` so far."""

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

    A real implementation calls into the raw-evidence store (NAS) to delete the
    referenced blob; here ``on_purge`` is an injectable callback so the spike/tests can
    verify destruction happened without a real store.
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
    "RetainedRecording",
    "RetentionPurgeJob",
]
