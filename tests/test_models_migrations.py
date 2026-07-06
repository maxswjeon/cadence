"""Tests for the D1 schema models, the schema raw-boundary lint, and Alembic migrations."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from cadence.stores.models import ALL_MODELS, Base
from cadence.stores.raw_boundary import lint_columns

EXPECTED_TABLES = {
    "calendar_event",
    "task",
    "deadline",
    "person",
    "place",
    "source_account",
    "fact",
    "nudge",
    "feedback",
    "sync_session",
}


def test_all_expected_tables_present() -> None:
    assert {m.__tablename__ for m in ALL_MODELS} == EXPECTED_TABLES


def test_create_all_produces_expected_tables() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    got = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= got


def test_no_model_column_violates_raw_boundary() -> None:
    """Schema lint: no D1 column name may imply verbatim raw content."""
    offenders: dict[str, list[str]] = {}
    for model in ALL_MODELS:
        cols = [c.key for c in model.__table__.columns]
        bad = lint_columns(model.__tablename__, cols)
        if bad:
            offenders[model.__tablename__] = bad
    assert offenders == {}, f"raw-looking columns found: {offenders}"


def test_alembic_upgrade_matches_models(tmp_path, monkeypatch) -> None:
    """`alembic upgrade head` on a fresh DB yields the same tables as the models."""
    from alembic.config import Config

    from alembic import command
    from cadence.config import get_settings

    # Nested, non-existent parent dir: exercises env.py creating the DB dir on a
    # clean checkout (regression guard — SQLite can't open a file in a missing dir).
    db_path = tmp_path / "var" / "nested" / "migrated.sqlite"
    assert not db_path.parent.exists()
    monkeypatch.setenv("CADENCE_D1_PATH", str(db_path))
    get_settings.cache_clear()
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{db_path}")
        got = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= got
        assert "alembic_version" in got
    finally:
        get_settings.cache_clear()
