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
from cadence.adapters.live import MailboxClient, decode_cursor, encode_cursor
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
        mailbox: MailboxClient | None = None,
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self._nas = nas or NASStore()
        self._records = records
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None
        #: Injected IMAP seam for the live path. IMAP is not HTTP, so — unlike the
        #: GitHub/Calendar adapters' httpx client — the live mailbox is a small
        #: :class:`~cadence.adapters.live.MailboxClient` a test fills with fixtures and the
        #: runtime backs with :class:`ImapMailboxClient`.
        self._mailbox = mailbox

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

    # -- live incremental poll (IMAP: fetch messages with UID > last_uid) -------- #

    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        """Fetch messages newer than the last-seen IMAP UID.

        Resume state is ``{"last_uid": <int>}`` — IMAP UIDs increase monotonically within
        a mailbox's UIDVALIDITY, so ``UID > last_uid`` yields exactly the messages that
        arrived since the previous poll. The new cursor is the highest UID fetched (or the
        prior one when nothing new arrived).
        """
        if self._mailbox is None:
            raise ValueError("EmailAdapter.poll requires an injected mailbox client")
        state = decode_cursor(cursor)
        last_uid = state.get("last_uid")
        records, highest = self._mailbox.fetch_since(last_uid)
        events = [self.finalize(self.normalize(raw)) for raw in records]
        next_uid = highest if highest is not None else last_uid
        return events, encode_cursor({"last_uid": next_uid})

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


# --------------------------------------------------------------------------- #
# Runtime IMAP mailbox client (NOT exercised by tests — live IMAP connection)
# --------------------------------------------------------------------------- #


class ImapMailboxClient:
    """Live :class:`~cadence.adapters.live.MailboxClient` over IMAP (runtime plug-in).

    The email adapter's on-box equivalent of devbox's ``LiveProbe``: the only seam that
    touches a real server, so it is never used in tests (which inject a fake mailbox).
    Connects, selects the mailbox, ``UID SEARCH``es for messages with ``UID > last_uid``,
    and returns their parsed records plus the highest UID seen. Bodies are read so NAS can
    keep the verbatim message; the adapter's :meth:`~EmailAdapter.normalize` drops them
    from any Event field.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        mailbox: str = "INBOX",
        port: int = 993,
        use_ssl: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._use_ssl = use_ssl

    def fetch_since(self, last_uid: int | None) -> tuple[list[RawRecord], int | None]:
        import email as email_lib
        import imaplib
        from email.utils import parsedate_to_datetime

        conn = (
            imaplib.IMAP4_SSL(self._host, self._port)
            if self._use_ssl
            else imaplib.IMAP4(self._host, self._port)
        )
        try:
            conn.login(self._username, self._password)
            conn.select(self._mailbox, readonly=True)
            low = (last_uid + 1) if last_uid is not None else 1
            typ, data = conn.uid("search", None, f"UID {low}:*")
            if typ != "OK" or not data or not data[0]:
                return [], last_uid
            uids = [int(u) for u in data[0].split()]
            # UID n:* always returns the last message even when none are truly newer;
            # drop any at/below the watermark so a re-poll adds nothing.
            uids = [u for u in uids if last_uid is None or u > last_uid]
            records: list[RawRecord] = []
            highest = last_uid
            for uid in uids:
                typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email_lib.message_from_bytes(msg_data[0][1])
                received = msg.get("Date")
                try:
                    received_iso = parsedate_to_datetime(received).isoformat() if received else None
                except (TypeError, ValueError):
                    received_iso = None
                records.append(
                    {
                        "message_id": msg.get("Message-ID", f"uid-{uid}"),
                        "thread_id": msg.get("In-Reply-To") or msg.get("Message-ID"),
                        "from": msg.get("From"),
                        "to": [a.strip() for a in (msg.get("To", "")).split(",") if a.strip()],
                        "subject": msg.get("Subject", ""),
                        "body": _message_body(msg),
                        "received_at": received_iso,
                        "labels": [],
                        "has_attachment": any(
                            part.get_filename() for part in msg.walk()
                        ),
                    }
                )
                highest = uid if highest is None else max(highest, uid)
            return records, highest
        finally:
            try:
                conn.logout()
            except OSError:
                pass


def _message_body(msg: Any) -> str:
    """Best-effort plain-text body extraction for the verbatim NAS record."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload.decode("utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode("utf-8", "replace") if payload is not None else str(msg.get_payload())


__all__ = ["EmailAdapter", "ImapMailboxClient"]
