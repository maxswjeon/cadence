"""D1 canonical store — local-canonical SQLite + Cloudflare-D1 replica (STUB).

The **local** SQLite file is the hot-path canonical store: every read/write in M1
hits it directly (no cloud on the decision path). Each committed write is also
enqueued to :class:`CloudflareD1Replica`, an **async, in-memory, no-HTTP** stub that
models the off-site durable copy and the degraded-mode read source.

Every write passes through :class:`~cadence.stores.raw_boundary.PayloadClassifier`
so verbatim raw content can never reach D1 (local *or* replica) — a raw-to-D1 write
raises :class:`~cadence.stores.raw_boundary.RawBoundaryViolation`.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import JSON as SAJSON
from sqlalchemy import String, Text, create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.config import Settings, get_settings
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import ALL_MODELS, Base
from cadence.stores.raw_boundary import (
    PayloadClassifier,
    RawBoundaryViolation,
    SchemaBoundary,
    build_schema_boundary,
)


def _row_values(instance: object) -> dict[str, object]:
    """Extract the mapped column name → value dict from an ORM instance."""
    mapper = sa_inspect(type(instance))
    return {col.key: getattr(instance, col.key) for col in mapper.columns}


def _sql_kind(sql_type: object) -> str:
    """Coarse-classify a SQLAlchemy column type for the structural boundary."""
    if isinstance(sql_type, Text):
        return "text"
    if isinstance(sql_type, String):
        return "string"
    if isinstance(sql_type, SAJSON):
        return "json"
    return "scalar"


def _schema_boundary_from_models(classifier: PayloadClassifier) -> SchemaBoundary:
    """Build the per-table column allowlist from the ORM models."""
    specs: dict[str, list[tuple[str, str, int | None]]] = {}
    for model in ALL_MODELS:
        specs[model.__tablename__] = [
            (col.key, _sql_kind(col.type), getattr(col.type, "length", None))
            for col in model.__table__.columns
        ]
    return build_schema_boundary(specs, classifier)


# --------------------------------------------------------------------------- #
# Cloudflare-D1 replica (STUB)
# --------------------------------------------------------------------------- #


@dataclass
class ReplicationOp:
    """A single queued replication operation."""

    table: str
    values: dict[str, object]


@dataclass
class CloudflareD1Replica:
    """Async off-site D1 replica — **stubbed**, no real HTTP.

    Committed local writes are enqueued here via :meth:`replicate`. The queue models
    Cloudflare's async replication; :meth:`flush` drains it (in M1 it just clears the
    queue — there is no live endpoint). Queue depth is exposed for the
    ``replication_queue_depth`` alarm.
    """

    schema: SchemaBoundary
    alarm_depth: int = 1000
    _queue: deque[ReplicationOp] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    flushed_count: int = 0

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def enqueue(self, table: str, values: Mapping[str, object]) -> None:
        """Enqueue a structured row for replication (sync; safe from any context).

        The row is re-checked against the structural raw boundary (cloud tier) before
        it is allowed into the replica queue — defense in depth against a raw-to-cloud
        leak on the way to the off-site copy.
        """
        payload = dict(values)
        self.schema.enforce_row(table, payload, tier="D1")
        with self._lock:
            self._queue.append(ReplicationOp(table=table, values=payload))
            depth = len(self._queue)
        if depth >= self.alarm_depth:
            get_alarm_sink().fire(
                "replication_queue_depth",
                {"depth": depth, "alarm_depth": self.alarm_depth},
            )

    async def replicate(self, table: str, values: Mapping[str, object]) -> None:
        """Async wrapper over :meth:`enqueue` (models Cloudflare's async replication)."""
        self.enqueue(table, values)

    async def flush(self) -> int:
        """Drain the queue (stub: no live endpoint). Returns the number drained."""
        with self._lock:
            n = len(self._queue)
            self._queue.clear()
            self.flushed_count += n
        return n

    def pending(self) -> list[ReplicationOp]:
        """Snapshot of pending replication ops (for tests/observability)."""
        return list(self._queue)


# --------------------------------------------------------------------------- #
# Local-canonical store
# --------------------------------------------------------------------------- #


class D1Store:
    """Local-canonical SQLite store with a Cloudflare-D1 replica stub.

    Parameters
    ----------
    settings:
        Cadence settings (defaults to the process settings). ``url`` overrides the
        SQLite URL (tests pass ``sqlite://`` for an in-memory database).
    """

    def __init__(self, settings: Settings | None = None, *, url: str | None = None) -> None:
        self._settings = settings or get_settings()
        self.classifier = PayloadClassifier(
            max_summary_len=self._settings.max_summary_len,
            max_structured_text_len=self._settings.max_structured_text_len,
        )
        # The structural per-table column allowlist is the real raw-boundary guarantee.
        self.schema = _schema_boundary_from_models(self.classifier)
        engine_url = url or self._settings.d1_sqlalchemy_url
        connect_args = {"check_same_thread": False} if engine_url.startswith("sqlite") else {}
        # A shared in-memory DB needs a single connection (StaticPool) to persist.
        engine_kwargs: dict[str, object] = {"future": True, "connect_args": connect_args}
        if engine_url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(engine_url, **engine_kwargs)
        self._sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.replica = CloudflareD1Replica(
            schema=self.schema,
            alarm_depth=self._settings.replication_queue_alarm_depth,
        )

    # -- schema ------------------------------------------------------------- #

    def init_schema(self) -> None:
        """Create all D1 tables (test/bootstrap path; Alembic is canonical in prod)."""
        Base.metadata.create_all(self.engine)

    @property
    def table_names(self) -> list[str]:
        return sorted(m.__tablename__ for m in ALL_MODELS)

    # -- sessions ----------------------------------------------------------- #

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session context manager (commit on success, rollback on error)."""
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- writes (raw-boundary enforced) ------------------------------------- #

    def enforce_boundary(self, instance: object) -> None:
        """Enforce the structural raw boundary over an ORM instance's column values.

        Uses the per-table column allowlist (the guarantee): only known columns, and
        free-text only on summary/enum/id/hash/label columns. Fires the
        ``raw_to_cloud_violation`` alarm on breach before re-raising.
        """
        table = type(instance).__tablename__
        try:
            self.schema.enforce_row(table, _row_values(instance), tier="D1")
        except RawBoundaryViolation as exc:
            get_alarm_sink().fire(
                "raw_to_cloud_violation",
                {"tier": exc.tier, "field": exc.field, "reason": exc.reason, "table": table},
            )
            raise

    def write(self, instance: object) -> object:
        """Persist a single ORM instance after enforcing the raw boundary.

        On success the row is also enqueued for async replication to Cloudflare D1.
        """
        self.enforce_boundary(instance)
        with self.session() as session:
            session.add(instance)
            session.flush()
            table = type(instance).__tablename__
            values = _row_values(instance)
        self._enqueue_replication(table, values)
        return instance

    def write_all(self, instances: list[object]) -> list[object]:
        """Persist several ORM instances in one transaction (all boundary-checked first)."""
        for inst in instances:
            self.enforce_boundary(inst)
        staged: list[tuple[str, dict[str, object]]] = []
        with self.session() as session:
            for inst in instances:
                session.add(inst)
            session.flush()
            for inst in instances:
                staged.append((type(inst).__tablename__, _row_values(inst)))
        for table, values in staged:
            self._enqueue_replication(table, values)
        return instances

    def _enqueue_replication(self, table: str, values: dict[str, object]) -> None:
        """Enqueue a replication op (sync; the replica models async replication itself)."""
        self.replica.enqueue(table, values)

    def replicate_instance(self, instance: object) -> None:
        """Enforce the boundary and enqueue an already-persisted row for replication.

        Used by update/merge paths (e.g. the fact-graph dedupe merge) that mutate a row
        in place rather than inserting, so those changes still reach the Cloudflare
        replica instead of silently diverging.
        """
        self.enforce_boundary(instance)
        self._enqueue_replication(type(instance).__tablename__, _row_values(instance))


__all__ = ["D1Store", "CloudflareD1Replica", "ReplicationOp"]
