"""Full-pipeline proof: devbox Events → ingest → D1 facts/deadlines, 0 raw violations.

Runs the whole :class:`DevboxAdapter` (all four collectors, fixture probe) through the
real :class:`IngestPipeline` with the rule deadline extractor and asserts the structured
payloads land as provenance-carrying facts with zero raw-to-cloud violations — the same
boundary guarantee the reference adapters meet.
"""

from __future__ import annotations

from _devbox_util import StubProbe

from cadence.adapters.devbox import (
    ContainerInfo,
    DevboxAdapter,
    DevboxCollectorConfig,
    DiskUsage,
    ProcessInfo,
    RepoDir,
    TmuxSession,
)
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.brain.facts import FactGraph
from cadence.ingest.pipeline import IngestPipeline
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Fact
from cadence.stores.nas import NASStore
from cadence.stores.raw_boundary import PayloadClassifier, Verdict

NOW = 1_720_000_000.0


def _full_probe(tmp_path) -> StubProbe:
    repo = tmp_path / "repos" / "svc"
    return StubProbe(
        now_value=NOW,
        uid=1000,
        repos=[RepoDir(repo, uid=1000)],
        git_outputs={
            repo: "# branch.head main\n# branch.ab +2 -1\n1 .M ... README.md\n? new.py\n"
        },
        mtimes={repo / "README.md": NOW - 100, repo / "new.py": NOW - 50},
        processes=[
            ProcessInfo(pid=7, uid=1000, argv=["python3", "train.py", "--k=SECRET"],
                        start_time=NOW - 900, returncode=1),
        ],
        tmux=[TmuxSession("main", attached=True, last_activity=NOW - 20, uid=1000)],
        history=[f": {int(NOW) - 40}:0;git commit -m secret", f": {int(NOW) - 10}:0;ls"],
        load=(3.0, 2.0, 1.0),
        disks={"/": DiskUsage(total=1000, used=500, free=500)},
        containers=[ContainerInfo("db", "Up 2 hours", uid=1000)],
    )


def test_devbox_events_ingest_into_d1_with_zero_raw_violations(store, settings, tmp_path) -> None:
    nas = NASStore(settings)
    cfg = DevboxCollectorConfig(repo_roots=[tmp_path], state_dir=tmp_path / "state")
    adapter = DevboxAdapter(config=cfg, probe=_full_probe(tmp_path), nas=nas)
    pipeline = IngestPipeline(
        store, fact_graph=FactGraph(store, nas), deadline_extractor=RuleDeadlineExtractor()
    )

    events = list(adapter.emit())
    kinds = {e.kind for e in events}
    assert kinds == {
        "devbox.git_status",
        "devbox.job_finished",
        "devbox.activity",
        "devbox.health",
    }

    results = pipeline.ingest_many(events)
    assert all(r.accepted for r in results)
    assert len(results) == len(events) == 4

    with store.session() as session:
        facts = session.query(Fact).all()
        assert {f.kind for f in facts} == kinds
        for fact in facts:
            # Provenance carried: every devbox fact points at a retrievable NAS blob.
            assert fact.raw_evidence_id is not None
            assert nas.exists(fact.raw_evidence_id)
            assert fact.object_label == "devbox"

    # Zero raw-to-cloud violations across the whole run.
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0

    # Belt-and-suspenders: every row queued for cloud replication is boundary-clean, and
    # no secret from any command/history/file leaked into a replicated payload.
    classifier = PayloadClassifier(
        max_summary_len=settings.max_summary_len,
        max_structured_text_len=settings.max_structured_text_len,
    )
    for op in store.replica.pending():
        for verdict in classifier.classify(op.values):
            assert verdict.verdict is Verdict.ALLOWED, (op.table, verdict)
        blob = str(op.values)
        for forbidden in ("SECRET", "train.py", "git commit", "README.md", "new.py"):
            assert forbidden not in blob


def test_devbox_adapter_registered() -> None:
    from cadence.adapters.base import AdapterRegistry

    reg = AdapterRegistry()
    reg.register(DevboxAdapter)
    assert reg.get("devbox") is DevboxAdapter


def test_devbox_events_are_deduped_on_reingest(store, settings, tmp_path) -> None:
    nas = NASStore(settings)
    cfg = DevboxCollectorConfig(repo_roots=[tmp_path], state_dir=tmp_path / "state")
    adapter = DevboxAdapter(config=cfg, probe=_full_probe(tmp_path), nas=nas)
    pipeline = IngestPipeline(store, fact_graph=FactGraph(store, nas))

    events = list(adapter.emit())
    pipeline.ingest_many(events)
    # Re-ingesting the identical events is idempotent (dedupe_id).
    second = pipeline.ingest_many(events)
    assert all(r.duplicate for r in second)
