"""Live-polling scheduler — drives the source adapters' incremental (live) path.

Where the fixtures/records path replays a fixed batch, :class:`SourcePoller` is the
**runtime** step that keeps an adapter ingesting: every ``interval_seconds`` it asks a
:class:`~cadence.adapters.live.LiveSource` for records *new since a persisted cursor*,
POSTs each resulting :class:`~cadence.adapters.base.Event` to the brain's ``/ingest/event``
endpoint, then persists the advanced cursor so the next poll — even after a restart —
resumes exactly where this one stopped. It is the network-facing sibling of devbox's
``main()`` loop, and mirrors its two guarantees:

* **Resilience.** Any per-poll failure (a provider fetch error, a POST error) is caught,
  logged, and backed off; the loop never crashes. :meth:`SourcePoller.poll_once` is the
  deterministic unit a test drives directly.
* **Incremental + idempotent.** The persisted cursor drives incremental fetches, and a
  small persisted set of recently-seen ``dedupe_id`` s means a record returned twice
  (e.g. GitHub's ``since`` is inclusive) is POSTed once. The ingest endpoint dedupes again,
  so this is an optimization, not the correctness boundary.

Auth posture
------------
The poller runs on the same host as the brain and POSTs to a loopback ingest URL, so by
default it sends **no** ``X-Client-Cert`` header and relies on the loopback exemption in
:func:`cadence.brain.app.enforce_mtls` (``settings.trust_loopback_ingest``). A
``cert_header`` may still be supplied for a proxied/remote deployment.

Injectability
-------------
The clock, sleep, HTTP client, and cursor store are all injected, so a test drives polls
deterministically against fakes with no real network call or wall-clock wait.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from cadence.adapters.base import CredentialVault, Event
from cadence.adapters.live import LiveSource
from cadence.config import Settings, get_settings
from cadence.obs.logging import get_logger, log_event

if TYPE_CHECKING:
    import httpx

_LOG = get_logger("runtime.poller")

#: How many recent dedupe ids to retain per account (bounds the persisted state).
DEFAULT_SEEN_CAP = 512


class HttpPoster(Protocol):
    """The minimal HTTP seam the poller needs to reach ``/ingest/event`` (httpx.Client)."""

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> Any: ...


# --------------------------------------------------------------------------- #
# Cursor persistence
# --------------------------------------------------------------------------- #


class CursorStore:
    """Per-account poll state (``cursor`` + recent ``seen`` dedupe ids) on disk.

    One JSON file holds every account's state keyed by ``"{provider}:{account_ref}"``, so
    restarts resume incrementally. Writes are atomic (temp file + ``os.replace``) so a
    crash mid-write cannot corrupt the file. Injectable: a test points ``path`` at a temp
    file (or swaps in any object exposing ``load``/``save``).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_all(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def load(self, key: str) -> dict[str, Any]:
        state = self._read_all().get(key)
        return state if isinstance(state, dict) else {}

    def save(self, key: str, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        allstate = self._read_all()
        allstate[key] = state
        tmp = self.path.with_name(f"{self.path.name}.tmp-{id(state)}")
        tmp.write_text(json.dumps(allstate, sort_keys=True, default=str))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #


class SourcePoller:
    """Drives one live adapter: :meth:`poll_once` (deterministic) + :meth:`run` (loop)."""

    def __init__(
        self,
        source: LiveSource,
        *,
        ingest_url: str,
        http_client: HttpPoster,
        cursor_store: CursorStore,
        interval_seconds: float = 300.0,
        cert_header: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        initial_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 300.0,
        seen_cap: int = DEFAULT_SEEN_CAP,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
        self.source = source
        self.ingest_url = ingest_url
        self._http = http_client
        self._store = cursor_store
        self.interval_seconds = interval_seconds
        self._cert_header = cert_header
        self._clock = clock  # reserved for future scheduling; keeps parity with scheduler
        self._sleep = sleep
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._seen_cap = seen_cap
        self._key = f"{source.provider}:{source.account_ref}"
        self._stopped = False

    # -- deterministic single poll ----------------------------------------- #

    def poll_once(self) -> list[Event]:
        """Fetch new records, POST each un-seen Event, persist the advanced cursor.

        Returns the Events actually POSTed (already deduped). Raises on a fetch or POST
        failure **without** persisting — so the cursor never advances past records that
        did not make it to ingest; :meth:`run` catches the raise and backs off.
        """
        state = self._store.load(self._key)
        cursor = state.get("cursor")
        seen_list: list[str] = [s for s in state.get("seen", []) if isinstance(s, str)]
        seen: set[str] = set(seen_list)

        events, next_cursor = self.source.poll(cursor)

        posted: list[Event] = []
        for event in events:
            dedupe_id = event.dedupe_id or event.with_dedupe_id().dedupe_id
            if dedupe_id in seen:
                continue
            self._post(event)
            seen.add(dedupe_id)
            seen_list.append(dedupe_id)
            posted.append(event)

        self._store.save(
            self._key,
            {"cursor": next_cursor, "seen": seen_list[-self._seen_cap :]},
        )
        if posted:
            log_event(
                _LOG, 20, "poller.ingested",
                provider=self.source.provider, account_ref=self.source.account_ref,
                count=len(posted),
            )
        return posted

    def _post(self, event: Event) -> None:
        headers = {"Content-Type": "application/json"}
        if self._cert_header:
            headers["X-Client-Cert"] = self._cert_header
        resp = self._http.post(
            self.ingest_url, content=event.model_dump_json().encode("utf-8"), headers=headers
        )
        # httpx.Response exposes raise_for_status(); a fake may omit it.
        raise_for_status = getattr(resp, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()

    # -- resilient loop ----------------------------------------------------- #

    def stop(self) -> None:
        self._stopped = True

    def run(self, *, once: bool = False) -> None:
        """Poll forever (``once=True`` → a single poll), backing off on failure.

        A raised poll never kills the loop: it is logged and the next attempt waits an
        exponentially-growing backoff (reset on the next success). This is the runtime
        entrypoint; tests drive :meth:`poll_once` directly.
        """
        backoff = self._initial_backoff
        while not self._stopped:
            try:
                self.poll_once()
                backoff = self._initial_backoff
                wait = self.interval_seconds
            except Exception as exc:  # noqa: BLE001 - a bad poll must never kill the loop
                log_event(
                    _LOG, 40, "poller.poll_failed",
                    provider=self.source.provider, account_ref=self.source.account_ref,
                    error=type(exc).__name__, backoff_seconds=round(backoff, 1),
                )
                wait = backoff
                backoff = min(backoff * 2, self._max_backoff)
            if once:
                return
            self._sleep(wait)


# --------------------------------------------------------------------------- #
# Config-driven construction (OFF by default: no accounts / no creds → no pollers)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PollerAccountConfig:
    """One live-poll account. ``provider`` selects the adapter; the rest are its opts."""

    provider: str  # "github" | "google_calendar" | "email"
    account_ref: str
    interval_seconds: float = 300.0
    repos: tuple[str, ...] = ()  # github: "owner/name" repos to poll
    calendar_id: str = "primary"  # google_calendar
    api_base: str | None = None  # override the provider's default API base


@dataclass(frozen=True)
class PollerRuntimeConfig:
    """The poller block of the runtime config. Empty ``accounts`` → no pollers run."""

    accounts: tuple[PollerAccountConfig, ...] = ()
    #: Where pollers POST Events. Defaults (in the runtime) to the loopback ingest URL.
    ingest_url: str | None = None
    #: Optional X-Client-Cert for a proxied/remote deployment; None → loopback exemption.
    cert_header: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> PollerRuntimeConfig:
        """Parse ``CADENCE_POLLER_ACCOUNTS`` (JSON list) + related vars; empty by default.

        Example ``CADENCE_POLLER_ACCOUNTS`` value::

            [{"provider": "github", "account_ref": "octocat",
              "repos": ["octocat/hello-world"], "interval_seconds": 120}]
        """
        raw = env.get("CADENCE_POLLER_ACCOUNTS", "").strip()
        accounts: list[PollerAccountConfig] = []
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"CADENCE_POLLER_ACCOUNTS must be a JSON list of accounts: {exc}"
                ) from exc
            for item in parsed:
                accounts.append(
                    PollerAccountConfig(
                        provider=item["provider"],
                        account_ref=item["account_ref"],
                        interval_seconds=float(item.get("interval_seconds", 300.0)),
                        repos=tuple(item.get("repos", ())),
                        calendar_id=item.get("calendar_id", "primary"),
                        api_base=item.get("api_base"),
                    )
                )
        return cls(
            accounts=tuple(accounts),
            ingest_url=env.get("CADENCE_POLLER_INGEST_URL") or None,
            cert_header=env.get("CADENCE_CLIENT_CERT") or None,
        )


def _build_live_adapter(
    account: PollerAccountConfig,
    *,
    vault: CredentialVault,
    http_client: httpx.Client,
    creds: dict[str, Any],
) -> LiveSource:
    """Construct the live-configured adapter for one account (vault creds already resolved)."""
    from cadence.adapters.email import EmailAdapter, ImapMailboxClient
    from cadence.adapters.gcal import GoogleCalendarAdapter
    from cadence.adapters.github import GitHubAdapter

    ref = account.account_ref
    if account.provider == "github":
        return GitHubAdapter(
            ref, vault=vault, http_client=http_client,
            api_base=account.api_base or "https://api.github.com",
            repos=list(account.repos),
        )
    if account.provider == "google_calendar":
        return GoogleCalendarAdapter(
            ref, vault=vault, http_client=http_client,
            api_base=account.api_base or "https://www.googleapis.com/calendar/v3",
            calendar_id=account.calendar_id,
        )
    if account.provider == "email":
        mailbox = ImapMailboxClient(
            host=creds["host"],
            username=creds["username"],
            password=creds["password"],
            mailbox=creds.get("mailbox", "INBOX"),
            port=int(creds.get("port", 993)),
            use_ssl=bool(creds.get("use_ssl", True)),
        )
        return EmailAdapter(ref, vault=vault, mailbox=mailbox)
    raise ValueError(f"unknown poller provider {account.provider!r}")


def build_source_pollers(
    config: PollerRuntimeConfig,
    *,
    ingest_url: str,
    cursor_store: CursorStore,
    settings: Settings | None = None,
    vault: CredentialVault | None = None,
    http_client: httpx.Client | None = None,
) -> list[SourcePoller]:
    """Build a poller per configured account **that has vault credentials**.

    Honest, OFF-by-default posture: no configured accounts → ``[]``; an account whose
    credentials are absent from the vault is skipped with a log line (never a silent or
    crashing start). ``ingest_url`` is the default POST target; an account/config
    ``ingest_url`` override on ``config`` wins when set.
    """
    if not config.accounts:
        return []
    settings = settings or get_settings()
    if vault is None:
        from cadence.adapters.vault import FileCredentialVault

        vault = FileCredentialVault(settings)
    if http_client is None:
        import httpx

        http_client = httpx.Client(timeout=30.0)

    target = config.ingest_url or ingest_url
    pollers: list[SourcePoller] = []
    for account in config.accounts:
        try:
            creds = vault.get(account.provider, account.account_ref)
        except KeyError:
            log_event(
                _LOG, 30, "poller.skipped_no_credentials",
                provider=account.provider, account_ref=account.account_ref,
            )
            continue
        try:
            source = _build_live_adapter(
                account, vault=vault, http_client=http_client, creds=creds
            )
        except (KeyError, ValueError) as exc:
            log_event(
                _LOG, 40, "poller.skipped_bad_config",
                provider=account.provider, account_ref=account.account_ref, error=str(exc),
            )
            continue
        pollers.append(
            SourcePoller(
                source,
                ingest_url=target,
                http_client=http_client,
                cursor_store=cursor_store,
                interval_seconds=account.interval_seconds,
                cert_header=config.cert_header,
            )
        )
    log_event(_LOG, 20, "poller.built", count=len(pollers))
    return pollers


__all__ = [
    "SourcePoller",
    "CursorStore",
    "HttpPoster",
    "DEFAULT_SEEN_CAP",
    "PollerAccountConfig",
    "PollerRuntimeConfig",
    "build_source_pollers",
]
