"""Alarm hooks.

Cadence fires named alarms on invariant-relevant events. In M1 the sink records
alarms in memory and logs them; production would page/alert. The canonical alarm
names are:

* ``raw_to_cloud_violation``   — a raw-to-D1/R2 write was attempted and blocked.
* ``credential_vault_access``  — the NAS-only credential vault was read/written.
* ``replication_queue_depth``  — the Cloudflare-D1 replica queue crossed its threshold.

Alarm payloads must themselves be non-verbatim (ids/hashes/counts/reasons only).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cadence.obs.logging import get_logger, log_event

_KNOWN_ALARMS = frozenset(
    {"raw_to_cloud_violation", "credential_vault_access", "replication_queue_depth"}
)


@dataclass(frozen=True)
class Alarm:
    """A single fired alarm."""

    name: str
    payload: dict[str, Any]
    fired_at: datetime


@dataclass
class AlarmSink:
    """In-memory alarm sink that also logs each alarm as a structured record."""

    _alarms: list[Alarm] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def fire(self, name: str, payload: dict[str, Any] | None = None) -> Alarm:
        """Record and log an alarm. Unknown names are allowed but flagged in the log."""
        alarm = Alarm(name=name, payload=dict(payload or {}), fired_at=datetime.now(tz=UTC))
        with self._lock:
            self._alarms.append(alarm)
        log_event(
            get_logger("obs.alarms"),
            logging.WARNING if name in _KNOWN_ALARMS else logging.ERROR,
            f"alarm:{name}",
            alarm=name,
            known=name in _KNOWN_ALARMS,
            **alarm.payload,
        )
        return alarm

    def alarms(self, name: str | None = None) -> list[Alarm]:
        """Return fired alarms, optionally filtered by ``name``."""
        with self._lock:
            if name is None:
                return list(self._alarms)
            return [a for a in self._alarms if a.name == name]

    def count(self, name: str | None = None) -> int:
        return len(self.alarms(name))

    def clear(self) -> None:
        with self._lock:
            self._alarms.clear()


_SINK = AlarmSink()


def get_alarm_sink() -> AlarmSink:
    """Return the process-wide alarm sink."""
    return _SINK


__all__ = ["Alarm", "AlarmSink", "get_alarm_sink"]
