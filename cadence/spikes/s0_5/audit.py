"""Per-trigger audit log (Decision D + S0.5 mandatory control).

Distinct from :mod:`cadence.obs.egress` (which audits raw-content **leaving the
machine**) and :mod:`cadence.obs.alarms` (invariant-violation alarms): this log audits
the **lifecycle of every recording trigger** — armed, confirmed, cancelled, aborted for
a non-participant, stopped, purged — so a self-review (or, later, counsel) can
reconstruct exactly what happened around any capture decision. It follows the same
in-memory-ledger + structured-log shape as ``RawEgressLog``/``AlarmSink`` (record,
log via :func:`cadence.obs.logging.log_event`, keep an in-memory list) rather than
reimplementing that pattern differently.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cadence.obs.logging import get_logger, log_event


@dataclass(frozen=True)
class AuditEntry:
    """One audited recording-trigger lifecycle event."""

    session_id: str
    source: str  # "co_presence" | "meeting" | "phone_call" | ...
    # "armed" | "confirmed" | "cancelled" | "aborted_non_participant" | "stopped" | "purged"
    event: str
    detail: str
    recorded_at: datetime


@dataclass
class TriggerAuditLog:
    """Append-only per-trigger audit ledger."""

    _entries: list[AuditEntry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, session_id: str, source: str, event: str, *, detail: str = "") -> AuditEntry:
        entry = AuditEntry(
            session_id=session_id,
            source=source,
            event=event,
            detail=detail,
            recorded_at=datetime.now(tz=UTC),
        )
        with self._lock:
            self._entries.append(entry)
        log_event(
            get_logger("spikes.s0_5.audit"),
            20,  # logging.INFO
            f"recording_trigger:{event}",
            session_id=session_id,
            source=source,
            event=event,
            detail=detail,
        )
        return entry

    def for_session(self, session_id: str) -> list[AuditEntry]:
        with self._lock:
            return [e for e in self._entries if e.session_id == session_id]

    def all(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)


__all__ = ["AuditEntry", "TriggerAuditLog"]
