"""Tests for the live-polling scheduler and the adapters' live fetch path.

Two layers, both hermetic (no live network, no wall-clock waits):

* **Poller mechanics** — a fake :class:`~cadence.adapters.live.LiveSource` + a fake HTTP
  poster + a temp-file :class:`~cadence.runtime.poller.CursorStore` prove the cursor
  advances across polls, a record seen twice is POSTed once (dedupe), and a failing fetch
  backs off (and resets on success).
* **Live fetch per provider** — GitHub/Google-Calendar adapters poll a FAKE local HTTP
  server (real ``httpx``, real API-shaped JSON, real ``since``/``syncToken`` incremental
  params); the Email adapter polls a fake IMAP mailbox (IMAP is not HTTP, so the mailbox
  is the injected seam). Each yields the correct Event and advances its cursor.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from cadence.adapters.base import Event
from cadence.adapters.email import EmailAdapter
from cadence.adapters.gcal import GoogleCalendarAdapter
from cadence.adapters.github import GitHubAdapter
from cadence.adapters.live import decode_cursor
from cadence.runtime.poller import CursorStore, SourcePoller
from cadence.stores.nas import NASStore

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakePoster:
    """Captures POSTed Events; mimics httpx.Response.raise_for_status()."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        self.posts.append((url, content, headers))
        return SimpleNamespace(status_code=202, raise_for_status=lambda: None)


class _FakeSource:
    """A LiveSource that returns pre-programmed (events, cursor) batches per poll."""

    provider = "github"
    account_ref = "octocat"

    def __init__(self, batches) -> None:
        self.batches = list(batches)
        self.seen_cursors: list[str | None] = []

    def poll(self, cursor):
        self.seen_cursors.append(cursor)
        if self.batches:
            return self.batches.pop(0)
        return [], cursor


class _FailingSource:
    provider = "github"
    account_ref = "octocat"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def poll(self, cursor):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom fetching")
        return [], cursor


def _event(event_id: str) -> Event:
    return Event(
        event_id=event_id, source="github", account_ref="octocat", kind="github.issue",
        summary=f"issue {event_id}",
    ).with_dedupe_id()


class _FakeGetServer:
    """Minimal GET server: a responder maps (path, query, headers) -> (status, hdrs, body)."""

    def __init__(self, responder) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                status, resp_headers, body = responder(
                    parsed.path, query, dict(self.headers.items())
                )
                data = json.dumps(body).encode()
                self.send_response(status)
                for key, val in (resp_headers or {}).items():
                    self.send_header(key, val)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# --------------------------------------------------------------------------- #
# Poller mechanics
# --------------------------------------------------------------------------- #


def test_cursor_advances_across_polls(tmp_path) -> None:
    store = CursorStore(tmp_path / "cursors.json")
    source = _FakeSource(
        [([_event("e1")], "cursor-1"), ([_event("e2")], "cursor-2")]
    )
    poster = _FakePoster()
    poller = SourcePoller(
        source, ingest_url="http://loop/ingest", http_client=poster, cursor_store=store
    )

    poller.poll_once()
    assert source.seen_cursors == [None]  # first poll starts from no cursor
    assert store.load("github:octocat")["cursor"] == "cursor-1"

    poller.poll_once()
    assert source.seen_cursors == [None, "cursor-1"]  # second poll resumes from cursor-1
    assert store.load("github:octocat")["cursor"] == "cursor-2"
    assert len(poster.posts) == 2


def test_same_record_polled_twice_is_ingested_once(tmp_path) -> None:
    store = CursorStore(tmp_path / "cursors.json")
    dup = _event("dup")
    # Same event (same dedupe_id) returned on both polls, cursor unchanged (inclusive fetch).
    source = _FakeSource([([dup], "c1"), ([_event("dup")], "c1")])
    poster = _FakePoster()
    poller = SourcePoller(
        source, ingest_url="http://loop/ingest", http_client=poster, cursor_store=store
    )

    first = poller.poll_once()
    second = poller.poll_once()

    assert len(first) == 1  # first poll posts it
    assert second == []  # second poll dedupes it away
    assert len(poster.posts) == 1  # only one POST ever hit the wire


def test_backoff_on_failing_fetch_grows_then_resets(tmp_path) -> None:
    store = CursorStore(tmp_path / "cursors.json")
    waits: list[float] = []

    # Always-failing source: prove the backoff sequence 5 -> 10 -> 20.
    always = _FailingSource(fail_times=99)
    poller = SourcePoller(
        always, ingest_url="http://loop/ingest", http_client=_FakePoster(),
        cursor_store=store, interval_seconds=60.0,
        initial_backoff_seconds=5.0, max_backoff_seconds=300.0,
    )

    def stop_after_three(w: float) -> None:
        waits.append(w)
        if len(waits) >= 3:
            poller.stop()

    poller._sleep = stop_after_three  # noqa: SLF001 - inject deterministic sleep
    poller.run()
    assert waits == [5.0, 10.0, 20.0]

    # A success between failures resets the backoff to the interval.
    waits2: list[float] = []
    flaky = _FailingSource(fail_times=1)  # fail once, then succeed forever
    poller2 = SourcePoller(
        flaky, ingest_url="http://loop/ingest", http_client=_FakePoster(),
        cursor_store=CursorStore(tmp_path / "c2.json"), interval_seconds=60.0,
        initial_backoff_seconds=5.0,
    )

    def stop_after_two(w: float) -> None:
        waits2.append(w)
        if len(waits2) >= 2:
            poller2.stop()

    poller2._sleep = stop_after_two  # noqa: SLF001
    poller2.run()
    assert waits2 == [5.0, 60.0]  # backoff (fail), then interval (success reset)


def test_poller_survives_post_failure_without_advancing_cursor(tmp_path) -> None:
    store = CursorStore(tmp_path / "cursors.json")

    class _BoomPoster:
        def post(self, url, *, content, headers):
            return SimpleNamespace(
                raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("503"))
            )

    source = _FakeSource([([_event("e1")], "cursor-1")])
    poller = SourcePoller(
        source, ingest_url="http://loop/ingest", http_client=_BoomPoster(),
        cursor_store=store,
    )

    # poll_once raises on the POST failure; the cursor was NOT persisted, so the next
    # poll re-fetches from the old cursor (ingest is the idempotency boundary).
    import pytest

    with pytest.raises(RuntimeError):
        poller.poll_once()
    assert store.load("github:octocat") == {}


# --------------------------------------------------------------------------- #
# GitHub live fetch (fake HTTP server, real httpx, since/ETag)
# --------------------------------------------------------------------------- #


def _github_issue() -> dict:
    return {
        "number": 42, "state": "open", "title": "Fix login bug",
        "updated_at": "2026-07-02T09:00:00Z",
        "html_url": "https://github.com/octocat/hello-world/issues/42",
        "user": {"login": "mona"}, "labels": [{"name": "bug"}],
        "assignees": [{"login": "octocat"}], "milestone": {"due_on": "2026-07-15"},
        "body": "SECRET verbatim body that must never leak",
    }


def _github_pr() -> dict:
    return {
        "number": 43, "state": "open", "title": "Add rate limiting",
        "updated_at": "2026-07-03T12:00:00Z",
        "html_url": "https://github.com/octocat/hello-world/pull/43",
        "user": {"login": "octocat"}, "labels": [],
        "pull_request": {"url": "..."}, "draft": False,
        "requested_reviewers": [{"login": "hubot"}],
        "body": "PR body verbatim",
    }


def test_github_live_poll_fetches_and_advances_since(settings) -> None:
    def responder(path, query, headers):
        assert path == "/repos/octocat/hello-world/issues"
        if headers.get("If-None-Match") == '"etag-1"':
            return 304, {}, {}
        return 200, {"ETag": '"etag-1"'}, [_github_issue(), _github_pr()]

    import httpx

    with _FakeGetServer(responder) as srv:
        client = httpx.Client()
        adapter = GitHubAdapter(
            "octocat", nas=NASStore(settings), http_client=client,
            api_base=srv.base, repos=["octocat/hello-world"],
        )
        events, cursor = adapter.poll(None)
        # Second poll carries the ETag -> 304 -> nothing new, cursor unchanged.
        events2, cursor2 = adapter.poll(cursor)
        client.close()

    assert [e.kind for e in events] == ["github.issue", "github.pull_request"]
    issue = events[0]
    assert issue.summary == "issue #42: Fix login bug"
    assert issue.structured["repo"] == "octocat/hello-world"
    assert issue.structured["due_on"] == "2026-07-15"
    assert events[1].structured["requested_reviewer_count"] == 1
    # No verbatim body leaked into any public Event field.
    blob = json.dumps([e.model_dump(mode="json") for e in events])
    assert "verbatim" not in blob

    state = decode_cursor(cursor)
    assert state["since"] == "2026-07-03T12:00:00Z"  # max updated_at
    assert state["etags"]["octocat/hello-world"] == '"etag-1"'
    assert events2 == []
    assert decode_cursor(cursor2)["since"] == "2026-07-03T12:00:00Z"


# --------------------------------------------------------------------------- #
# Google Calendar live fetch (fake HTTP server, syncToken)
# --------------------------------------------------------------------------- #


def _gcal_item() -> dict:
    return {
        "id": "evt_1", "summary": "Team sync", "status": "confirmed",
        "description": "SECRET agenda verbatim",
        "start": {"dateTime": "2026-07-08T15:00:00Z"},
        "end": {"dateTime": "2026-07-08T15:30:00Z"},
        "location": "Zoom", "organizer": {"email": "alice@example.com"},
        "attendees": [
            {"email": "alice@example.com", "responseStatus": "accepted"},
            {"email": "bob@example.com", "responseStatus": "needsAction"},
        ],
    }


def test_gcal_live_poll_fetches_and_advances_sync_token(settings) -> None:
    def responder(path, query, headers):
        assert path == "/calendars/primary/events"
        if query.get("syncToken") == "tok-1":
            return 200, {}, {"items": [], "nextSyncToken": "tok-2"}
        return 200, {}, {"items": [_gcal_item()], "nextSyncToken": "tok-1"}

    import httpx

    with _FakeGetServer(responder) as srv:
        client = httpx.Client()
        adapter = GoogleCalendarAdapter(
            "me", nas=NASStore(settings), http_client=client, api_base=srv.base,
        )
        events, cursor = adapter.poll(None)
        events2, cursor2 = adapter.poll(cursor)
        client.close()

    assert len(events) == 1
    evt = events[0]
    assert evt.kind == "calendar.event"
    assert evt.summary == "event: Team sync"
    assert evt.structured["attendee_count"] == 2
    assert evt.structured["accepted_count"] == 1
    assert "verbatim" not in json.dumps(evt.model_dump(mode="json"))
    assert decode_cursor(cursor)["sync_token"] == "tok-1"
    assert events2 == []
    assert decode_cursor(cursor2)["sync_token"] == "tok-2"


# --------------------------------------------------------------------------- #
# Email live fetch (fake IMAP mailbox, last UID)
# --------------------------------------------------------------------------- #


class _FakeMailbox:
    def __init__(self, messages) -> None:
        self.messages = messages  # list[(uid, record)]

    def fetch_since(self, last_uid):
        new = [m for (uid, m) in self.messages if last_uid is None or uid > last_uid]
        highest = max((uid for uid, _ in self.messages), default=last_uid)
        return new, highest


def _email_record(mid: str) -> dict:
    return {
        "message_id": mid, "thread_id": "t1", "from": "manager@example.com",
        "to": ["me@example.com"], "subject": "Review the Q3 budget",
        "body": "SECRET verbatim body", "received_at": "2026-07-04T09:15:00Z",
        "labels": ["inbox"], "has_attachment": True,
    }


def test_email_live_poll_fetches_and_advances_uid(settings) -> None:
    mailbox = _FakeMailbox([(10, _email_record("msg-100")), (11, _email_record("msg-101"))])
    adapter = EmailAdapter("me", nas=NASStore(settings), mailbox=mailbox)

    events, cursor = adapter.poll(None)
    events2, cursor2 = adapter.poll(cursor)

    assert [e.kind for e in events] == ["email.message", "email.message"]
    assert events[0].summary == "email: Review the Q3 budget"
    assert "verbatim" not in json.dumps([e.model_dump(mode="json") for e in events])
    assert decode_cursor(cursor)["last_uid"] == 11
    assert events2 == []  # nothing newer than UID 11
    assert decode_cursor(cursor2)["last_uid"] == 11


# --------------------------------------------------------------------------- #
# End-to-end: adapter live fetch -> SourcePoller -> POST to ingest
# --------------------------------------------------------------------------- #


def test_poller_drives_github_adapter_end_to_end(settings, tmp_path) -> None:
    def responder(path, query, headers):
        if headers.get("If-None-Match") == '"etag-1"':
            return 304, {}, {}
        return 200, {"ETag": '"etag-1"'}, [_github_issue(), _github_pr()]

    import httpx

    with _FakeGetServer(responder) as srv:
        client = httpx.Client()
        adapter = GitHubAdapter(
            "octocat", nas=NASStore(settings), http_client=client,
            api_base=srv.base, repos=["octocat/hello-world"],
        )
        poster = _FakePoster()
        poller = SourcePoller(
            adapter, ingest_url="http://127.0.0.1:3245/ingest/event",
            http_client=poster, cursor_store=CursorStore(tmp_path / "c.json"),
        )
        first = poller.poll_once()
        second = poller.poll_once()  # 304 -> nothing new
        client.close()

    assert len(first) == 2
    assert second == []
    assert len(poster.posts) == 2
    # POSTs carry no X-Client-Cert (loopback exemption); body is a serialized Event.
    url, content, headers = poster.posts[0]
    assert url == "http://127.0.0.1:3245/ingest/event"
    assert "X-Client-Cert" not in headers
    assert json.loads(content)["kind"] == "github.issue"
