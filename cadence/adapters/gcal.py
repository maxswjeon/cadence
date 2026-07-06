"""Google Calendar reference adapter — events + attendees → ``calendar_event`` facts.

Fixtures-based (no live network call): :meth:`GoogleCalendarAdapter.fetch` reads raw
event records from an injected list or a fixture JSON file (see
``tests/fixtures/google_calendar/``). :meth:`GoogleCalendarAdapter.normalize` stores
the **verbatim** raw record (including the free-text ``description``) in NAS and
returns an :class:`~cadence.adapters.base.Event` carrying only structured fields
(schedule, status, location, attendee handles/counts) and a short non-verbatim
summary built from the event title — never the description body.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cadence.adapters.base import (
    AcquisitionTier,
    Adapter,
    CredentialVault,
    Event,
    RawRecord,
    registry,
)
from cadence.stores.nas import BlobRef, NASStore


def _store_verbatim(nas: NASStore, raw: RawRecord) -> BlobRef:
    """Serialize ``raw`` deterministically and store it verbatim in NAS."""
    payload = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    return nas.put(payload)


@registry.register
class GoogleCalendarAdapter(Adapter):
    """Per-account Google Calendar adapter (events/attendees → ``calendar.event``)."""

    provider = "google_calendar"
    acquisition_tier = AcquisitionTier.OAUTH

    def __init__(
        self,
        account_ref: str,
        *,
        vault: CredentialVault | None = None,
        nas: NASStore | None = None,
        records: list[RawRecord] | None = None,
        fixture_path: str | Path | None = None,
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self._nas = nas or NASStore()
        self._records = records
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None

    def fetch(self) -> list[RawRecord]:
        # A live client would resolve the OAuth token from the vault here; fixtures
        # never make a live call so the result is unused.
        self.credentials()
        if self._records is not None:
            return list(self._records)
        if self._fixture_path is not None:
            return json.loads(self._fixture_path.read_text())
        raise ValueError(
            "GoogleCalendarAdapter requires 'records' or 'fixture_path' "
            "(fixtures-based; no live API)"
        )

    def normalize(self, raw: RawRecord) -> Event:
        ref = _store_verbatim(self._nas, raw)
        attendees = raw.get("attendees", [])
        structured: dict[str, Any] = {
            "calendar_id": raw.get("calendar_id"),
            "status": raw.get("status"),
            "all_day": raw.get("all_day", False),
            "starts_at": raw.get("start"),
            "ends_at": raw.get("end"),
            "location": raw.get("location"),
            "organizer": raw.get("organizer"),
            "attendees": [a["email"] for a in attendees],
            "attendee_count": len(attendees),
            "accepted_count": sum(1 for a in attendees if a.get("response_status") == "accepted"),
        }
        return Event(
            event_id=raw["id"],
            source=self.provider,
            account_ref=self.account_ref,
            kind="calendar.event",
            occurred_at=raw["start"],
            payload_hash=ref.hash,
            raw_evidence_ref=ref.id,
            summary=f"event: {raw['summary']}",
            confidence=0.95,
            structured=structured,
        )


__all__ = ["GoogleCalendarAdapter"]
