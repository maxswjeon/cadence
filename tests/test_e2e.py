"""End-to-end foundation test (AC-7 analog).

Seed fixture events through an adapter → ingest → assert D1 rows are correct, raw
evidence lives only in NAS (referenced by hash), and there are **zero** raw-to-cloud
violations across the whole run (local D1 rows *and* everything queued to the replica).
"""

from __future__ import annotations

from cadence.adapters.base import AcquisitionTier, Adapter, Event
from cadence.brain.facts import FactGraph
from cadence.ingest.pipeline import IngestPipeline
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Fact
from cadence.stores.nas import NASStore
from cadence.stores.raw_boundary import PayloadClassifier, Verdict

# A fixture "inbox": each record has a verbatim body (raw) + structured fields.
FIXTURES = [
    {"id": "gh-1", "title": "Fix login", "body": "The login page throws a 500 on submit."},
    {"id": "gh-2", "title": "Update deps", "body": "Bump fastapi and sqlalchemy to latest."},
    {"id": "gh-3", "title": "Write docs", "body": "Document the ingest endpoint."},
]


class _FixtureGithubAdapter(Adapter):
    """Minimal fixture adapter: stores verbatim body in NAS, emits structured Events."""

    provider = "github_fixture"
    acquisition_tier = AcquisitionTier.OFFICIAL_API

    def __init__(self, account_ref: str, nas: NASStore) -> None:
        super().__init__(account_ref)
        self._nas = nas

    def fetch(self):
        return list(FIXTURES)

    def normalize(self, raw) -> Event:
        # Verbatim body goes to NAS; only the pointer + a summary flow onward.
        ref = self._nas.put(raw["body"].encode("utf-8"))
        return Event(
            event_id=raw["id"],
            source=self.provider,
            account_ref=self.account_ref,
            kind="github.issue",
            summary=f"issue: {raw['title']}",
            payload_hash=ref.hash,
            raw_evidence_ref=ref.id,
            confidence=0.95,
        )


def test_e2e_seed_ingest_zero_raw_to_cloud_violations(store, settings) -> None:
    nas = NASStore(settings)
    adapter = _FixtureGithubAdapter("octocat", nas)
    pipeline = IngestPipeline(store, fact_graph=FactGraph(store, nas))

    results = pipeline.ingest_many(list(adapter.emit()))
    assert all(r.accepted for r in results)
    assert len({r.fact_id for r in results}) == 3

    # D1 has exactly the 3 structured facts, each with a NAS pointer + summary.
    with store.session() as session:
        facts = session.query(Fact).all()
        assert len(facts) == 3
        for fact in facts:
            assert fact.raw_evidence_id is not None
            assert fact.summary and fact.summary.startswith("issue:")
            # The verbatim body is NOT in D1 — only its hash pointer is.
            assert fact.raw_evidence_id == fact.raw_evidence_hash
            # ...and the verbatim body IS retrievable from NAS.
            assert nas.exists(fact.raw_evidence_id)

    # Invariant: zero raw-to-cloud violations fired during the whole run.
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0

    # Belt-and-suspenders: every row queued for cloud replication is boundary-clean.
    classifier = PayloadClassifier(
        max_summary_len=settings.max_summary_len,
        max_structured_text_len=settings.max_structured_text_len,
    )
    for op in store.replica.pending():
        for verdict in classifier.classify(op.values):
            assert verdict.verdict is Verdict.ALLOWED, (op.table, verdict)
