"""Email reference adapter — messages → task/deadline candidates.

Fixtures-based (no live network call): :meth:`EmailAdapter.fetch` reads raw message
records from an injected list or a fixture JSON file (see ``tests/fixtures/email/``).
:meth:`EmailAdapter.normalize` stores the **verbatim** raw record (including the
message ``body``) in NAS and returns an :class:`~cadence.adapters.base.Event` carrying
only structured fields (sender, recipients, labels, attachment flag) and a short
non-verbatim summary built from the subject line — never the message body. The
(later-wave) deadline extractor attaches downstream to derive ``deadline`` rows from
these events; this adapter does not parse deadlines itself.
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
class EmailAdapter(Adapter):
    """Per-account email adapter (messages → ``email.message`` task/deadline candidates)."""

    provider = "email"
    acquisition_tier = AcquisitionTier.OFFICIAL_API

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
        # A live client would resolve the mailbox token from the vault here; fixtures
        # never make a live call so the result is unused.
        self.credentials()
        if self._records is not None:
            return list(self._records)
        if self._fixture_path is not None:
            return json.loads(self._fixture_path.read_text())
        raise ValueError(
            "EmailAdapter requires 'records' or 'fixture_path' (fixtures-based; no live API)"
        )

    def normalize(self, raw: RawRecord) -> Event:
        ref = _store_verbatim(self._nas, raw)
        structured: dict[str, Any] = {
            "thread_id": raw.get("thread_id"),
            "sender": raw.get("from"),
            "recipient_count": len(raw.get("to", [])),
            "labels": raw.get("labels", []),
            "has_attachment": raw.get("has_attachment", False),
        }
        return Event(
            event_id=raw["message_id"],
            source=self.provider,
            account_ref=self.account_ref,
            kind="email.message",
            occurred_at=raw["received_at"],
            payload_hash=ref.hash,
            raw_evidence_ref=ref.id,
            summary=f"email: {raw['subject']}",
            confidence=0.9,
            structured=structured,
        )


__all__ = ["EmailAdapter"]
