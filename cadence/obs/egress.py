"""Raw-content egress log.

Per Decision E there are **exactly two** sanctioned, non-silent raw-content egress
channels; every egress to either is recorded here for auditability:

* :attr:`EgressChannel.LLM_TEXT`    — raw text to the user-configured LLM provider.
* :attr:`EgressChannel.DAGLO_AUDIO` — raw audio to Daglo (daglo.ai) STT.

This is an **audit** log: it records that raw content left the NAS boundary, keyed by
a content **hash** and metadata — never the verbatim payload itself. In M1 nothing is
actually egressed (audio capture is excluded; the LLM path is not wired) — this is the
interface + ledger the future audio/inference pipeline writes to.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from cadence.obs.logging import get_logger, log_event


class EgressChannel(StrEnum):
    """The two (and only two) sanctioned raw-content egress channels."""

    LLM_TEXT = "llm_text"
    DAGLO_AUDIO = "daglo_audio"


@dataclass(frozen=True)
class EgressRecord:
    """One audited raw-content egress event (hash + metadata, never verbatim)."""

    channel: EgressChannel
    content_hash: str
    destination: str
    byte_len: int
    source_event_ids: tuple[str, ...]
    recorded_at: datetime


@dataclass
class RawEgressLog:
    """In-memory append-only ledger of raw-content egress events."""

    _records: list[EgressRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        channel: EgressChannel,
        *,
        content_hash: str,
        destination: str,
        byte_len: int = 0,
        source_event_ids: tuple[str, ...] = (),
    ) -> EgressRecord:
        """Append an egress event and emit a structured (non-verbatim) log line."""
        rec = EgressRecord(
            channel=channel,
            content_hash=content_hash,
            destination=destination,
            byte_len=byte_len,
            source_event_ids=tuple(source_event_ids),
            recorded_at=datetime.now(tz=UTC),
        )
        with self._lock:
            self._records.append(rec)
        log_event(
            get_logger("obs.egress"),
            20,  # logging.INFO
            f"raw_egress:{channel.value}",
            channel=channel.value,
            content_hash=content_hash,
            destination=destination,
            byte_len=byte_len,
        )
        return rec

    def records(self, channel: EgressChannel | None = None) -> list[EgressRecord]:
        with self._lock:
            if channel is None:
                return list(self._records)
            return [r for r in self._records if r.channel is channel]

    def count(self, channel: EgressChannel | None = None) -> int:
        return len(self.records(channel))


_LOG = RawEgressLog()


def get_egress_log() -> RawEgressLog:
    """Return the process-wide raw-content egress log."""
    return _LOG


__all__ = ["EgressChannel", "EgressRecord", "RawEgressLog", "get_egress_log"]
