"""SQLAlchemy 2.0 ORM models for the D1 canonical schema.

These tables are **structured/derived only** — they uphold the D1 raw boundary
(see :mod:`cadence.stores.raw_boundary`). No column stores verbatim raw content;
verbatim evidence lives in NAS and is referenced here by ``raw_evidence_id`` /
``raw_evidence_hash``. The set of tables is Cadence-owned and fixed for M1:

    calendar_event, task, deadline, person, place, source_account,
    fact, nudge, feedback, sync_session

Every derived row that represents an inference carries provenance columns:
``source_event_ids`` (JSON list), ``raw_evidence_id``/``raw_evidence_hash``,
``confidence_value`` + ``confidence_type``, and (where applicable) ``expires_at``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Timezone-aware current UTC timestamp (single source for row timestamps)."""
    return datetime.now(tz=UTC)


class UTCDateTime(TypeDecorator):
    """A timezone-aware ``DateTime`` that normalizes to UTC on write and read.

    SQLite (and some drivers) drop ``tzinfo`` on round-trip, returning naive datetimes.
    This decorator guarantees a full round-trip contract: values are stored as
    aware-UTC and re-hydrated with ``tzinfo=UTC`` on read, so ``read_back.tzinfo`` is
    always :data:`datetime.UTC`. Naive inputs are assumed to already be UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for all D1 models."""

    type_annotation_map = {dict: JSON, list: JSON}


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` to a model."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProvenanceMixin:
    """Provenance columns for any derived/inferred row.

    ``summary`` is a **short non-verbatim** derived summary bounded by the raw
    boundary; it is stored as TEXT but the payload classifier rejects verbatim
    content and over-long values on the write path.
    """

    confidence_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_event_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    raw_evidence_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_evidence_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Accounts / entities
# --------------------------------------------------------------------------- #


class SourceAccount(TimestampMixin, Base):
    """A per-provider, per-account source instance (e.g. one GitHub login).

    Holds only an **opaque** account reference/label — never credentials. Secrets
    live in the NAS-only credential vault (see :mod:`cadence.adapters.base`).
    """

    __tablename__ = "source_account"
    __table_args__ = (
        UniqueConstraint("provider", "account_ref", name="uq_source_account_provider_ref"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    acquisition_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    display_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Person(TimestampMixin, Base):
    """A resolved person entity. Handles are opaque references, not raw messages."""

    __tablename__ = "person"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    canonical_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    handles: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Place(TimestampMixin, Base):
    """A resolved place entity (structured coordinates + label)."""

    __tablename__ = "place"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)


# --------------------------------------------------------------------------- #
# Schedule / work items
# --------------------------------------------------------------------------- #


class CalendarEvent(ProvenanceMixin, TimestampMixin, Base):
    """A normalized calendar event (Cadence *is* the calendar; sources import into this)."""

    __tablename__ = "calendar_event"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    source_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_account.id"), nullable=True, index=True
    )
    source_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    place_id: Mapped[str | None] = mapped_column(ForeignKey("place.id"), nullable=True)


class Task(ProvenanceMixin, TimestampMixin, Base):
    """A work item derived from a source event (issue/PR/email/etc.)."""

    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    source_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_account.id"), nullable=True, index=True
    )
    source_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Deadline(ProvenanceMixin, TimestampMixin, Base):
    """A due date/time attached to a task or calendar event.

    ``origin`` distinguishes an ``explicit`` source-provided deadline from an
    ``inferred`` (extracted) one; ``divergence_flag`` marks explicit-vs-inferred
    disagreement per the deadline-parser contract. Explicit is preferred over
    inferred by convention; see :mod:`cadence.brain.deadlines`.
    """

    __tablename__ = "deadline"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), nullable=True, index=True)
    calendar_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("calendar_event.id"), nullable=True, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default="explicit", nullable=False)
    divergence_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- #
# Provenance fact graph
# --------------------------------------------------------------------------- #


class Fact(ProvenanceMixin, TimestampMixin, Base):
    """A node in the provenance fact graph.

    Every fact carries provenance (via :class:`ProvenanceMixin`). ``dedupe_key`` is
    a stable content key used to collapse duplicate derivations; ``superseded_by``
    points at a newer fact that replaces this one. Feedback history is stored in the
    :class:`Feedback` table keyed by ``fact_id``.
    """

    __tablename__ = "fact"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    predicate: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(ForeignKey("fact.id"), nullable=True)

    feedback: Mapped[list[Feedback]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )


class Nudge(TimestampMixin, Base):
    """A proactive nudge derived from a fact. ``idempotency_key`` dedupes delivery."""

    __tablename__ = "nudge"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    fact_id: Mapped[str | None] = mapped_column(ForeignKey("fact.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Feedback(TimestampMixin, Base):
    """User feedback on a fact or nudge (confirm/reject/snooze/edit) with a weight."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    fact_id: Mapped[str | None] = mapped_column(ForeignKey("fact.id"), nullable=True, index=True)
    nudge_id: Mapped[str | None] = mapped_column(ForeignKey("nudge.id"), nullable=True, index=True)
    signal: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    note_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    fact: Mapped[Fact | None] = relationship(back_populates="feedback")


class SyncSession(TimestampMixin, Base):
    """An append-only ingest/reconciliation session for a device + source account."""

    __tablename__ = "sync_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_account.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    events_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_wal_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


#: All ORM model classes, in dependency order (useful for schema lint + tests).
ALL_MODELS = (
    SourceAccount,
    Person,
    Place,
    CalendarEvent,
    Task,
    Deadline,
    Fact,
    Nudge,
    Feedback,
    SyncSession,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "ProvenanceMixin",
    "SourceAccount",
    "Person",
    "Place",
    "CalendarEvent",
    "Task",
    "Deadline",
    "Fact",
    "Nudge",
    "Feedback",
    "SyncSession",
    "ALL_MODELS",
    "utcnow",
    "UTCDateTime",
]
