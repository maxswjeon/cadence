"""GitHub reference adapter — issues, pull requests, and review requests.

Fixtures-based (no live network call): :meth:`GitHubAdapter.fetch` reads raw records
from an injected list or a fixture JSON file (see ``tests/fixtures/github/``). Each
raw record carries a ``type`` discriminator (``"issue"`` | ``"pull_request"`` |
``"review_request"``) mirroring the distinct GitHub REST endpoints a live client would
poll. :meth:`GitHubAdapter.normalize` stores the **verbatim** raw record in NAS and
returns an :class:`~cadence.adapters.base.Event` carrying only structured fields, a
short non-verbatim summary, and the NAS provenance pointer — never the issue/PR body.
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


def _rest_issue_to_record(gh: dict[str, Any], repo: str) -> RawRecord:
    """Map one GitHub-REST issue object to the internal raw record :meth:`normalize` reads.

    GitHub's issues endpoint returns both issues and PRs; a PR carries a ``pull_request``
    key. Nested objects (labels, assignees, user, milestone) are flattened to the handful
    of structured fields the normalizer keeps — the issue/PR **body is never copied here**
    (it is dropped before it could reach an Event field; NAS still gets the verbatim map).
    """
    is_pr = "pull_request" in gh
    record: dict[str, Any] = {
        "type": "pull_request" if is_pr else "issue",
        "repo": repo,
        "number": gh["number"],
        "state": gh.get("state"),
        "author": (gh.get("user") or {}).get("login"),
        "title": gh.get("title", ""),
        "updated_at": gh["updated_at"],
        "html_url": gh.get("html_url"),
        "labels": [lbl["name"] for lbl in gh.get("labels", []) if isinstance(lbl, dict)],
        "assignees": [a["login"] for a in gh.get("assignees", []) if isinstance(a, dict)],
    }
    if is_pr:
        record["draft"] = gh.get("draft", False)
        record["requested_reviewers"] = [
            r["login"] for r in gh.get("requested_reviewers", []) if isinstance(r, dict)
        ]
    else:
        record["milestone_due_on"] = (gh.get("milestone") or {}).get("due_on")
    return record


@registry.register
class GitHubAdapter(Adapter):
    """Per-account GitHub adapter (issues / PRs / review requests → task candidates)."""

    provider = "github"
    acquisition_tier = AcquisitionTier.OFFICIAL_API

    def __init__(
        self,
        account_ref: str,
        *,
        vault: CredentialVault | None = None,
        nas: NASStore | None = None,
        records: list[RawRecord] | None = None,
        fixture_path: str | Path | None = None,
        http_client: httpx.Client | None = None,
        api_base: str = "https://api.github.com",
        repos: list[str] | None = None,
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self._nas = nas or NASStore()
        self._records = records
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None
        #: Injected httpx client for the live path (a test points it at a FAKE server).
        self._http = http_client
        self._api_base = api_base.rstrip("/")
        #: Repos to poll live, ``"owner/name"`` (empty → live poll yields nothing).
        self._repos = list(repos or [])

    def fetch(self) -> list[RawRecord]:
        # In production this would authenticate a REST/GraphQL client with the
        # vaulted token; fixtures never make a live call so the result is unused.
        self.credentials()
        if self._records is not None:
            return list(self._records)
        if self._fixture_path is not None:
            return json.loads(self._fixture_path.read_text())
        raise ValueError(
            "GitHubAdapter requires 'records' or 'fixture_path' (fixtures-based; no live API)"
        )

    # -- live incremental poll (REST: ?since=<ts> + conditional If-None-Match) ---- #

    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        """Fetch issues/PRs updated since the cursor for each configured repo.

        Resume state is ``{"since": <iso8601>, "etags": {repo: etag}}``: ``since`` is the
        REST ``since`` query param (updated-at watermark) and the per-repo ``ETag`` drives
        a conditional request so an unchanged repo returns ``304`` and no records. The new
        watermark is the max ``updated_at`` seen (records are requested ascending).
        """
        if self._http is None:
            raise ValueError("GitHubAdapter.poll requires an injected http_client")
        state = decode_cursor(cursor)
        since = state.get("since")
        etags: dict[str, str] = dict(state.get("etags") or {})
        token = self._token()
        events: list[Event] = []
        newest = since
        for repo in self._repos:
            records, new_etag = self._fetch_repo(repo, since, etags.get(repo), token)
            if new_etag:
                etags[repo] = new_etag
            for gh in records:
                raw = _rest_issue_to_record(gh, repo)
                events.append(self.finalize(self.normalize(raw)))
                updated = raw.get("updated_at")
                if updated and (newest is None or updated > newest):
                    newest = updated
        return events, encode_cursor({"since": newest, "etags": etags})

    def _token(self) -> str | None:
        creds = self.credentials()
        return creds.get("token") or creds.get("access_token")

    def _fetch_repo(
        self, repo: str, since: str | None, etag: str | None, token: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET one repo's updated issues/PRs; ``([], etag)`` on a ``304 Not Modified``."""
        assert self._http is not None
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if etag:
            headers["If-None-Match"] = etag
        params: dict[str, str] = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": "100",
        }
        if since:
            params["since"] = since
        resp = self._http.get(
            f"{self._api_base}/repos/{repo}/issues", params=params, headers=headers
        )
        if resp.status_code == 304:
            return [], etag
        resp.raise_for_status()
        return resp.json(), resp.headers.get("ETag")

    def normalize(self, raw: RawRecord) -> Event:
        ref = _store_verbatim(self._nas, raw)
        kind_map = {
            "issue": self._normalize_issue,
            "pull_request": self._normalize_pull_request,
            "review_request": self._normalize_review_request,
        }
        builder = kind_map.get(raw["type"])
        if builder is None:
            raise ValueError(f"unknown github fixture type: {raw['type']!r}")
        event_id, kind, occurred_at, summary, structured = builder(raw)
        return Event(
            event_id=event_id,
            source=self.provider,
            account_ref=self.account_ref,
            kind=kind,
            occurred_at=occurred_at,
            payload_hash=ref.hash,
            raw_evidence_ref=ref.id,
            summary=summary,
            confidence=0.95,
            structured=structured,
        )

    @staticmethod
    def _normalize_issue(raw: RawRecord) -> tuple[str, str, str, str, dict[str, Any]]:
        event_id = f"github:{raw['repo']}#{raw['number']}"
        structured = {
            "repo": raw["repo"],
            "number": raw["number"],
            "state": raw["state"],
            "labels": raw.get("labels", []),
            "assignee_count": len(raw.get("assignees", [])),
            "author": raw.get("author"),
            "due_on": raw.get("milestone_due_on"),
            "url": raw.get("html_url"),
        }
        summary = f"issue #{raw['number']}: {raw['title']}"
        return event_id, "github.issue", raw["updated_at"], summary, structured

    @staticmethod
    def _normalize_pull_request(raw: RawRecord) -> tuple[str, str, str, str, dict[str, Any]]:
        event_id = f"github:{raw['repo']}#{raw['number']}"
        structured = {
            "repo": raw["repo"],
            "number": raw["number"],
            "state": raw["state"],
            "draft": raw.get("draft", False),
            "requested_reviewer_count": len(raw.get("requested_reviewers", [])),
            "author": raw.get("author"),
            "url": raw.get("html_url"),
        }
        summary = f"pr #{raw['number']}: {raw['title']}"
        return event_id, "github.pull_request", raw["updated_at"], summary, structured

    @staticmethod
    def _normalize_review_request(raw: RawRecord) -> tuple[str, str, str, str, dict[str, Any]]:
        event_id = f"github:{raw['repo']}#{raw['pr_number']}:review:{raw['reviewer']}"
        structured = {
            "repo": raw["repo"],
            "pr_number": raw["pr_number"],
            "reviewer": raw["reviewer"],
            "requested_by": raw.get("requested_by"),
            "url": raw.get("html_url"),
        }
        summary = f"review requested on pr #{raw['pr_number']} for {raw['reviewer']}"
        return (
            event_id,
            "github.review_request",
            raw["requested_at"],
            summary,
            structured,
        )


__all__ = ["GitHubAdapter"]
