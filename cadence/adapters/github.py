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
    ) -> None:
        super().__init__(account_ref, vault=vault)
        self._nas = nas or NASStore()
        self._records = records
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None

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
