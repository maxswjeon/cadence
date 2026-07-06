"""Tests for the data-layer hardening fix wave.

Covers: UTC datetime round-trip, structural raw-boundary allowlist, broadened
account/card/base64 detection + substring name-lint, explicit reproducible migration,
fact-merge replication, and the R2 derived-artifact marker.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from cadence.brain.facts import FactGraph, FactInput
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Base, CalendarEvent, Fact, Place, SourceAccount
from cadence.stores.r2 import Artifact, Tier, TieringRouter
from cadence.stores.raw_boundary import (
    PayloadClassifier,
    RawBoundaryViolation,
    Verdict,
    lint_columns,
)

EXPECTED_TABLES = {
    "calendar_event", "task", "deadline", "person", "place", "source_account",
    "fact", "nudge", "feedback", "sync_session",
}


# --------------------------------------------------------------------------- #
# [HIGH] tz round-trip
# --------------------------------------------------------------------------- #


def test_datetime_roundtrip_reattaches_utc(store) -> None:
    naive = datetime(2026, 7, 10, 9, 0, 0)  # naive → assumed UTC
    aware = datetime(2026, 7, 10, 10, 30, tzinfo=UTC)
    ev = CalendarEvent(title="Standup", starts_at=naive, ends_at=aware)
    store.write(ev)
    with store.session() as session:
        got = session.get(CalendarEvent, ev.id)
        assert got.starts_at.tzinfo == UTC
        assert got.ends_at.tzinfo == UTC
        assert got.created_at.tzinfo == UTC
        # value preserved (naive interpreted as UTC)
        assert got.starts_at == datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
        assert got.ends_at == aware


# --------------------------------------------------------------------------- #
# [HIGH] structural raw-boundary allowlist
# --------------------------------------------------------------------------- #


def test_schema_boundary_rejects_unknown_column(store) -> None:
    with pytest.raises(RawBoundaryViolation) as exc:
        store.schema.enforce_row("fact", {"kind": "k", "not_a_real_column": "x"})
    assert exc.value.field == "not_a_real_column"


def test_schema_boundary_rejects_unknown_table(store) -> None:
    with pytest.raises(RawBoundaryViolation):
        store.schema.enforce_row("secret_table", {"x": "y"})


def test_freetext_on_scalar_column_rejected(store) -> None:
    # priority is an Integer (SCALAR) column — free-text is a violation.
    with pytest.raises(RawBoundaryViolation):
        store.schema.enforce_row("task", {"priority": "definitely free text here"})


def test_label_column_is_length_bounded(store) -> None:
    # object_label is a LABEL column (255): 300 chars of verbatim is now rejected
    # (previously slipped through the 280-char generic gate on a non-denylisted name).
    with pytest.raises(RawBoundaryViolation):
        store.write(Fact(kind="k", dedupe_key="dk-long", object_label="x" * 300))
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1


def test_summary_column_still_allows_bounded_summary(store) -> None:
    store.write(Fact(kind="k", dedupe_key="dk-ok", summary="a short non-verbatim summary"))
    with store.session() as session:
        assert session.query(Fact).count() == 1


# --------------------------------------------------------------------------- #
# [MED] account-number / card / base64 / name-lint
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "4111.1111.1111.1111",   # dot-separated card (previously slipped through)
        "4111 1111 1111 1111",   # space-separated
        "4111-1111-1111-1111",   # dash-separated
        "1002345678901",         # bare 13-digit run
    ],
)
def test_account_card_numbers_rejected(value) -> None:
    c = PayloadClassifier()
    assert c.classify_field("summary", value).verdict is Verdict.REJECTED


def test_luhn_valid_card_rejected() -> None:
    c = PayloadClassifier()
    # 4111 1111 1111 1111 is a Luhn-valid test PAN.
    assert c.classify_field("object_label", "4111111111111111").verdict is Verdict.REJECTED


def test_base64_blob_rejected() -> None:
    c = PayloadClassifier()
    blob = base64.b64encode(b"raw verbatim evidence bytes " * 4).decode()
    assert len(blob) >= 64
    assert c.classify_field("summary", blob).verdict is Verdict.REJECTED


def test_short_ordinary_text_still_allowed() -> None:
    c = PayloadClassifier()
    assert c.classify_field("summary", "Met Alice about the Q3 roadmap").verdict is Verdict.ALLOWED


def test_name_lint_substring_bypass_caught() -> None:
    # emailbody / messagebody tokenize to one non-denylisted token; substring match
    # on high-risk tokens (body/chat/transcript/...) now catches them.
    flagged = lint_columns("t", ["emailbody", "messagebody", "chatlog", "id", "summary"])
    assert set(flagged) == {"emailbody", "messagebody", "chatlog"}


def test_account_ref_style_column_not_falsely_flagged() -> None:
    # 'account_ref' must NOT be flagged (opaque handle, not an account number) — the
    # substring set deliberately excludes 'account'/'email'/'message'.
    assert lint_columns("source_account", ["account_ref", "provider"]) == []


# --------------------------------------------------------------------------- #
# [HIGH] explicit, reproducible migration
# --------------------------------------------------------------------------- #


def _migrated_columns(db_path) -> dict[str, set[str]]:
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    return {
        t: {c["name"] for c in insp.get_columns(t)}
        for t in insp.get_table_names()
        if t != "alembic_version"
    }


def test_migration_is_explicit_not_create_all() -> None:
    text = open("alembic/versions/0001_initial_schema.py").read()
    assert "create_all" not in text
    assert "op.create_table(" in text


def test_migration_column_parity_and_reproducible(tmp_path, monkeypatch) -> None:
    from alembic.config import Config

    from alembic import command
    from cadence.config import get_settings

    db = tmp_path / "m.sqlite"
    monkeypatch.setenv("CADENCE_D1_PATH", str(db))
    get_settings.cache_clear()
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        migrated = _migrated_columns(db)
        assert set(migrated) == EXPECTED_TABLES

        # Column-for-column parity with the ORM metadata (create_all).
        ref_eng = create_engine("sqlite://")
        Base.metadata.create_all(ref_eng)
        rinsp = inspect(ref_eng)
        reference = {t: {c["name"] for c in rinsp.get_columns(t)} for t in rinsp.get_table_names()}
        assert migrated == reference

        # Reproducible: full down then up again works.
        command.downgrade(cfg, "base")
        assert "fact" not in inspect(create_engine(f"sqlite:///{db}")).get_table_names()
        command.upgrade(cfg, "head")
        assert set(_migrated_columns(db)) == EXPECTED_TABLES
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# unique constraints
# --------------------------------------------------------------------------- #


def test_source_account_provider_ref_unique(store) -> None:
    store.write(SourceAccount(provider="github", account_ref="octocat"))
    with pytest.raises(IntegrityError):
        store.write(SourceAccount(provider="github", account_ref="octocat"))


def test_place_label_unique(store) -> None:
    store.write(Place(label="HQ"))
    with pytest.raises(IntegrityError):
        store.write(Place(label="HQ"))


# --------------------------------------------------------------------------- #
# [MED] fact-merge replication
# --------------------------------------------------------------------------- #


def test_fact_merge_reaches_replica(store) -> None:
    graph = FactGraph(store)
    graph.assert_fact(FactInput(kind="k", subject_id="s", source_event_ids=["e1"]))
    depth_after_insert = store.replica.queue_depth
    assert depth_after_insert == 1
    # Re-assert the same fact → merge branch. Must ALSO enqueue a replica op.
    graph.assert_fact(FactInput(kind="k", subject_id="s", source_event_ids=["e2"]))
    assert store.replica.queue_depth == depth_after_insert + 1
    op = store.replica.pending()[-1]
    assert op.table == "fact"
    assert set(op.values["source_event_ids"]) == {"e1", "e2"}


# --------------------------------------------------------------------------- #
# [MED] R2 derived-artifact marker + non-NAS guard
# --------------------------------------------------------------------------- #


def test_r2_route_without_derived_marker_is_blocked(settings) -> None:
    router = TieringRouter(settings=settings)
    with pytest.raises(RawBoundaryViolation):
        router.route(Artifact(tier=Tier.DERIVED_BLOB, data=b"blob"))  # derived defaults False
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1


def test_r2_route_with_derived_marker_ok(settings) -> None:
    router = TieringRouter(settings=settings)
    ref = router.route(Artifact(tier=Tier.DERIVED_BLOB, data=b"transcript", derived=True))
    assert router.r2.exists(ref)
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0


def test_store_derived_blob_requires_marker(settings) -> None:
    router = TieringRouter(settings=settings)
    with pytest.raises(RawBoundaryViolation):
        router.store_derived_blob(b"blob")  # missing marker
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1
