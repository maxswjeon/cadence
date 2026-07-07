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
from typing import TYPE_CHECKING, Any

from cadence.adapters.base import (
    AcquisitionTier,
    Adapter,
    CredentialVault,
    Event,
    RawRecord,
    registry,
)
from cadence.adapters.live import decode_cursor, encode_cursor
from cadence.stores.nas import BlobRef, NASStore

if TYPE_CHECKING:  # httpx is a runtime dep, imported lazily so the fixtures path is light
    import httpx


def _store_verbatim(nas: NASStore, raw: RawRecord) -> BlobRef:
    """Serialize ``raw`` deterministically and store it verbatim in NAS."""
    payload = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    return nas.put(payload)


def _api_event_to_record(item: dict[str, Any], calendar_id: str) -> RawRecord:
    """Map one Google-Calendar API ``events`` item to the internal raw record.

    A timed event carries ``start.dateTime``; an all-day event carries ``start.date``.
    Attendee ``responseStatus`` is renamed to the internal ``response_status``. The
    free-text ``description`` is carried through only so NAS keeps the verbatim record —
    :meth:`normalize` never lifts it into an Event field.
    """
    start = item.get("start", {})
    end = item.get("end", {})
    all_day = "date" in start
    return {
        "id": item["id"],
        "calendar_id": calendar_id,
        "summary": item.get("summary", ""),
        "description": item.get("description"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": all_day,
        "status": item.get("status"),
        "location": item.get("location"),
        "organizer": (item.get("organizer") or {}).get("email"),
        "attendees": [
            {"email": a.get("email"), "response_status": a.get("responseStatus")}
            for a in item.get("attendees", [])
        ],
    }


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
        http_client: httpx.Client | None = None,
        api_base: str = "https://www.googleapis.com/calendar/v3",
        calendar_id: str = "primary",
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self._nas = nas or NASStore()
        self._records = records
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None
        #: Injected httpx client for the live path (a test points it at a FAKE server).
        self._http = http_client
        self._api_base = api_base.rstrip("/")
        self._calendar_id = calendar_id

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

    # -- live incremental poll (events.list with syncToken) ---------------------- #

    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        """Fetch changed events via the Calendar ``syncToken`` incremental protocol.

        Resume state is ``{"sync_token": <token>}``. The first poll (no token) is a full
        sync; every response returns a ``nextSyncToken`` that the following poll sends as
        ``syncToken`` to receive only what changed since. Pagination via ``nextPageToken``
        is followed to completion so the sync token is only taken from the final page.
        """
        if self._http is None:
            raise ValueError("GoogleCalendarAdapter.poll requires an injected http_client")
        state = decode_cursor(cursor)
        sync_token = state.get("sync_token")
        token = self._token()
        events: list[Event] = []
        page_token: str | None = None
        next_sync: str | None = sync_token
        while True:
            body = self._fetch_page(sync_token, page_token, token)
            for item in body.get("items", []):
                raw = _api_event_to_record(item, self._calendar_id)
                events.append(self.finalize(self.normalize(raw)))
            next_sync = body.get("nextSyncToken") or next_sync
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return events, encode_cursor({"sync_token": next_sync})

    def _token(self) -> str | None:
        creds = self.credentials()
        return creds.get("access_token") or creds.get("token")

    def _fetch_page(
        self, sync_token: str | None, page_token: str | None, token: str | None
    ) -> dict[str, Any]:
        assert self._http is not None
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params: dict[str, str] = {"singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token
        resp = self._http.get(
            f"{self._api_base}/calendars/{self._calendar_id}/events",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

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
