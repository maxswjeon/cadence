"""initial D1 schema

Frozen, explicit DDL for the Cadence-owned D1 schema (a reproducible t0 snapshot):

    source_account, person, place, calendar_event, task, deadline,
    fact, nudge, feedback, sync_session

Timestamp columns use ``sa.DateTime(timezone=True)`` — the runtime models wrap this in
a UTC-normalizing ``UTCDateTime`` type decorator, but the on-disk DDL is identical, so
the migration stays self-contained and independent of evolving model code. Unique
constraints: ``source_account(provider, account_ref)``, ``place.label``,
``fact.dedupe_key``, ``nudge.idempotency_key``.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fact",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("predicate", sa.String(length=128), nullable=True),
        sa.Column("object_label", sa.String(length=255), nullable=True),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.String(length=64), nullable=True),
        sa.Column("confidence_value", sa.Float(), nullable=True),
        sa.Column("confidence_type", sa.String(length=32), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=True),
        sa.Column("raw_evidence_id", sa.String(length=128), nullable=True),
        sa.Column("raw_evidence_hash", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["superseded_by"], ["fact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("fact", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_fact_dedupe_key"), ["dedupe_key"], unique=True)
        batch_op.create_index(batch_op.f("ix_fact_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_fact_subject_id"), ["subject_id"], unique=False)

    op.create_table(
        "person",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=True),
        sa.Column("handles", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "place",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label"),
    )

    op.create_table(
        "source_account",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("account_ref", sa.String(length=255), nullable=False),
        sa.Column("acquisition_tier", sa.String(length=32), nullable=False),
        sa.Column("display_label", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "account_ref", name="uq_source_account_provider_ref"),
    )
    with op.batch_alter_table("source_account", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_source_account_provider"), ["provider"], unique=False
        )

    op.create_table(
        "calendar_event",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_account_id", sa.String(length=64), nullable=True),
        sa.Column("source_event_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("place_id", sa.String(length=64), nullable=True),
        sa.Column("confidence_value", sa.Float(), nullable=True),
        sa.Column("confidence_type", sa.String(length=32), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=True),
        sa.Column("raw_evidence_id", sa.String(length=128), nullable=True),
        sa.Column("raw_evidence_hash", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["place.id"]),
        sa.ForeignKeyConstraint(["source_account_id"], ["source_account.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("calendar_event", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_calendar_event_source_account_id"), ["source_account_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_calendar_event_source_event_id"), ["source_event_id"], unique=False
        )

    op.create_table(
        "nudge",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("fact_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("message_summary", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["fact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("nudge", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_nudge_fact_id"), ["fact_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_nudge_idempotency_key"), ["idempotency_key"], unique=True
        )

    op.create_table(
        "sync_session",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("source_account_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("events_ingested", sa.Integer(), nullable=False),
        sa.Column("last_wal_offset", sa.Integer(), nullable=True),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_account_id"], ["source_account.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_session", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_session_device_id"), ["device_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_session_source_account_id"), ["source_account_id"], unique=False
        )

    op.create_table(
        "task",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_account_id", sa.String(length=64), nullable=True),
        sa.Column("source_event_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("confidence_value", sa.Float(), nullable=True),
        sa.Column("confidence_type", sa.String(length=32), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=True),
        sa.Column("raw_evidence_id", sa.String(length=128), nullable=True),
        sa.Column("raw_evidence_hash", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_account_id"], ["source_account.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_task_source_account_id"), ["source_account_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_task_source_event_id"), ["source_event_id"], unique=False
        )

    op.create_table(
        "deadline",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("calendar_event_id", sa.String(length=64), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("divergence_flag", sa.Boolean(), nullable=False),
        sa.Column("confidence_value", sa.Float(), nullable=True),
        sa.Column("confidence_type", sa.String(length=32), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=True),
        sa.Column("raw_evidence_id", sa.String(length=128), nullable=True),
        sa.Column("raw_evidence_hash", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["calendar_event_id"], ["calendar_event.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("deadline", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_deadline_calendar_event_id"), ["calendar_event_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_deadline_task_id"), ["task_id"], unique=False)

    op.create_table(
        "feedback",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("fact_id", sa.String(length=64), nullable=True),
        sa.Column("nudge_id", sa.String(length=64), nullable=True),
        sa.Column("signal", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("note_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["fact.id"]),
        sa.ForeignKeyConstraint(["nudge_id"], ["nudge.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_feedback_fact_id"), ["fact_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_feedback_nudge_id"), ["nudge_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_feedback_nudge_id"))
        batch_op.drop_index(batch_op.f("ix_feedback_fact_id"))
    op.drop_table("feedback")

    with op.batch_alter_table("deadline", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deadline_task_id"))
        batch_op.drop_index(batch_op.f("ix_deadline_calendar_event_id"))
    op.drop_table("deadline")

    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_source_event_id"))
        batch_op.drop_index(batch_op.f("ix_task_source_account_id"))
    op.drop_table("task")

    with op.batch_alter_table("sync_session", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_session_source_account_id"))
        batch_op.drop_index(batch_op.f("ix_sync_session_device_id"))
    op.drop_table("sync_session")

    with op.batch_alter_table("nudge", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_nudge_idempotency_key"))
        batch_op.drop_index(batch_op.f("ix_nudge_fact_id"))
    op.drop_table("nudge")

    with op.batch_alter_table("calendar_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_calendar_event_source_event_id"))
        batch_op.drop_index(batch_op.f("ix_calendar_event_source_account_id"))
    op.drop_table("calendar_event")

    with op.batch_alter_table("source_account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_source_account_provider"))
    op.drop_table("source_account")

    op.drop_table("place")
    op.drop_table("person")

    with op.batch_alter_table("fact", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fact_subject_id"))
        batch_op.drop_index(batch_op.f("ix_fact_kind"))
        batch_op.drop_index(batch_op.f("ix_fact_dedupe_key"))
    op.drop_table("fact")
