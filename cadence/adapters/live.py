"""Shared plumbing for the adapters' **live** (incremental) poll path.

The reference adapters (GitHub, Google Calendar, Email) each keep two fetch paths:

* the **fixtures/records** path (:meth:`Adapter.fetch` → :meth:`Adapter.emit`) used by
  tests and offline replay — no live network call, unchanged; and
* a **live** path added here: :meth:`LiveSource.poll` pulls only records *new since a
  cursor* from the real provider API, normalizes them into
  :class:`~cadence.adapters.base.Event` s, and returns the advanced cursor so the next
  poll resumes where this one stopped.

The cursor is an **opaque string** as far as the runtime poller
(:mod:`cadence.runtime.poller`) is concerned: it persists whatever string an adapter
hands back and feeds it verbatim to the next :meth:`poll`. Each adapter encodes its own
provider-native resume token inside that string via :func:`encode_cursor` /
:func:`decode_cursor` — GitHub keeps a ``since`` timestamp + per-repo ``ETag``, Google
Calendar keeps a ``syncToken``, IMAP keeps the last ``UID``.

Injectability (tests never hit the network)
-------------------------------------------
The live clients are injected into each adapter exactly like devbox's :class:`DevboxProbe`
is: GitHub/Google-Calendar take an :class:`httpx.Client` a test can point at a FAKE local
HTTP server; the IMAP mailbox (which is not HTTP) takes a :class:`MailboxClient` a test
fills with fixture messages. Real credentials/hosts are a runtime plug-in; no live call is
ever made in tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from cadence.adapters.base import Event, RawRecord


@runtime_checkable
class LiveSource(Protocol):
    """An adapter that supports incremental polling from an opaque cursor."""

    provider: str
    account_ref: str

    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        """Fetch records new since ``cursor``; return ``(events, next_cursor)``.

        ``events`` are fully normalized + dedupe-tagged (via
        :meth:`~cadence.adapters.base.Adapter.finalize`). ``next_cursor`` is the opaque
        resume token to pass back next time (``None`` only when nothing has ever been
        seen and the provider gave no token).
        """


class MailboxClient(Protocol):
    """The live-IMAP seam (the HTTP client's analog for the mailbox, which is not HTTP).

    A test fills a fake implementation with fixture messages; the runtime default
    (:class:`~cadence.adapters.email.ImapMailboxClient`) talks to a real IMAP server.
    """

    def fetch_since(self, last_uid: int | None) -> tuple[list[RawRecord], int | None]:
        """Return ``(message_records, highest_uid)`` for messages with UID > ``last_uid``.

        ``message_records`` are provider-native message dicts the email adapter
        normalizes; ``highest_uid`` is the new cursor (``None`` when the mailbox is empty
        and ``last_uid`` was ``None``).
        """


def encode_cursor(state: Mapping[str, Any]) -> str:
    """Encode an adapter's resume state into the opaque cursor string (stable ordering)."""
    return json.dumps(dict(state), sort_keys=True, default=str)


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    """Decode a cursor string back to the adapter's resume state (``{}`` if absent/bad)."""
    if not cursor:
        return {}
    try:
        obj = json.loads(cursor)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


__all__ = ["LiveSource", "MailboxClient", "encode_cursor", "decode_cursor"]
