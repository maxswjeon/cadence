"""D1 canonical store — local-canonical SQLite + Cloudflare-D1 replica.

The **local** SQLite file is the hot-path canonical store: every read/write in M1
hits it directly (no cloud on the decision path). Each committed write is also
enqueued to :class:`CloudflareD1Replica`, an **async, durable-queue** replica that
models the off-site durable copy and the degraded-mode read source.

The replica is **off by default**: with no ``account_id``/``database_id``/``api_token``
configured it keeps its in-memory queue and makes **no HTTP call** (an honest stub).
Once configured, :meth:`CloudflareD1Replica.flush` drains the queue by POSTing each row
to the Cloudflare D1 query API behind an **injectable** async HTTP client (default
:class:`httpx.AsyncClient`; tests pass a fake transport), retrying transient failures
with exponential backoff and leaving un-acked rows on the durable queue.

Every write passes through :class:`~cadence.stores.raw_boundary.PayloadClassifier`
so verbatim raw content can never reach D1 (local *or* replica) — a raw-to-D1 write
raises :class:`~cadence.stores.raw_boundary.RawBoundaryViolation`. The structural
boundary is re-checked in :meth:`CloudflareD1Replica.enqueue`, **before** a row can
enter the queue or be sent, so a raw row is blocked and never transmitted off-site.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

import httpx
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

#: Documented default Cloudflare API base; overridable via ``cloudflare_d1_url``.
DEFAULT_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


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


class ReplicationError(RuntimeError):
    """A replication op could not be delivered to the Cloudflare D1 replica.

    Raised by :meth:`CloudflareD1Replica.flush` when an op still fails after the retry
    budget (transient 5xx/network) or hits a non-transient rejection (4xx). The failed
    op stays at the head of the durable queue so a later flush can retry it.
    """


@dataclass
class ReplicationOp:
    """A single queued replication operation."""

    table: str
    values: dict[str, object]


def _to_param(value: object) -> object:
    """Coerce a column value to a JSON-serializable D1 query parameter.

    D1 params are null / number / string. Structured Python values (datetimes, enums,
    decimals, uuids, JSON lists/dicts) are normalized deterministically. Bytes should
    never reach here (the raw boundary blocks them) — if one does, refuse rather than
    silently smuggle a blob off-site.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _to_param(value.value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ReplicationError("binary value may not be replicated to D1 (store raw in NAS)")
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value if not isinstance(value, set) else sorted(value), default=str)
    return str(value)


def _op_to_query(op: ReplicationOp) -> dict[str, object]:
    """Build the ``{sql, params}`` body for a single INSERT.

    Table/column names come from our own ORM models (never caller input), so they are
    safe to interpolate; all values go through ``?`` placeholders as bound params.
    """
    cols = list(op.values.keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {op.table} ({col_list}) VALUES ({placeholders})"
    params = [_to_param(op.values[c]) for c in cols]
    return {"sql": sql, "params": params}


@dataclass
class CloudflareD1Replica:
    """Async off-site D1 replica with a durable queue and real HTTP flush.

    Committed local writes are enqueued here via :meth:`replicate`/:meth:`enqueue`. The
    queue models Cloudflare's async replication; :meth:`flush` drains it. Queue depth is
    exposed for the ``replication_queue_depth`` alarm.

    **Off by default:** unless ``account_id``, ``database_id`` and ``api_token`` are all
    set (:attr:`configured`), :meth:`flush` drains the queue in memory and makes **no**
    HTTP call. Once configured, each op is POSTed to the Cloudflare D1 query API through
    :attr:`client` (default :class:`httpx.AsyncClient`), retried on transient failure,
    and only removed from the durable queue once the replica acks it.
    """

    schema: SchemaBoundary
    alarm_depth: int = 1000
    account_id: str | None = None
    database_id: str | None = None
    api_token: str | None = None
    api_base: str = DEFAULT_CLOUDFLARE_API_BASE
    max_retries: int = 3
    backoff_base: float = 0.5
    timeout: float = 30.0
    client: httpx.AsyncClient | None = None
    _queue: deque[ReplicationOp] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _owns_client: bool = False
    flushed_count: int = 0

    @property
    def configured(self) -> bool:
        """True only when a real D1 endpoint + credentials are all present."""
        return bool(self.account_id and self.database_id and self.api_token)

    @property
    def query_url(self) -> str:
        """The Cloudflare D1 query endpoint for the configured database."""
        base = (self.api_base or DEFAULT_CLOUDFLARE_API_BASE).rstrip("/")
        return f"{base}/accounts/{self.account_id}/d1/database/{self.database_id}/query"

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def enqueue(self, table: str, values: Mapping[str, object]) -> None:
        """Enqueue a structured row for replication (sync; safe from any context).

        The row is re-checked against the structural raw boundary (cloud tier) before
        it is allowed into the replica queue — defense in depth against a raw-to-cloud
        leak on the way to the off-site copy. A violation raises before the row is
        queued, so it is never sent.
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
        """Drain the queue, returning the number of ops delivered.

        Unconfigured (no endpoint/credentials) this drains in memory and makes no HTTP
        call. Configured, it POSTs each op to the D1 query API in order; an op is removed
        from the durable queue only once acked. A persistent failure raises
        :class:`ReplicationError` with the failed op left at the head for a later retry.
        """
        if not self.configured:
            with self._lock:
                n = len(self._queue)
                self._queue.clear()
                self.flushed_count += n
            return n

        sent = 0
        while True:
            with self._lock:
                op = self._queue[0] if self._queue else None
            if op is None:
                break
            await self._send_op(op)  # raises on persistent failure; op stays queued
            with self._lock:
                if self._queue and self._queue[0] is op:
                    self._queue.popleft()
                self.flushed_count += 1
            sent += 1
        return sent

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self.client

    async def _send_op(self, op: ReplicationOp) -> None:
        """POST one op to the D1 query API, retrying transient failures with backoff."""
        body = _op_to_query(op)
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            try:
                resp = await self._http().post(self.query_url, json=body, headers=headers)
            except httpx.HTTPError as exc:  # network/transport failure — transient
                if attempt >= self.max_retries:
                    raise ReplicationError(f"D1 replica request failed: {exc}") from exc
                await self._backoff(attempt)
                attempt += 1
                continue
            if resp.status_code >= 500:  # server-side — transient, retry
                if attempt >= self.max_retries:
                    raise ReplicationError(
                        f"D1 replica flush failed after {attempt} retries: {resp.status_code}"
                    )
                await self._backoff(attempt)
                attempt += 1
                continue
            if resp.status_code >= 400:  # client-side — non-transient, surface it
                raise ReplicationError(f"D1 replica rejected op: {resp.status_code}")
            return

    async def _backoff(self, attempt: int) -> None:
        delay = self.backoff_base * (2**attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        """Close the HTTP client if this replica created it."""
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
            self._owns_client = False

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

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        url: str | None = None,
        replica_client: httpx.AsyncClient | None = None,
    ) -> None:
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
            account_id=self._settings.cloudflare_account_id,
            database_id=self._settings.cloudflare_d1_database_id,
            api_token=self._settings.cloudflare_api_token,
            api_base=self._settings.cloudflare_d1_url or DEFAULT_CLOUDFLARE_API_BASE,
            max_retries=self._settings.replication_max_retries,
            backoff_base=self._settings.replication_backoff_base,
            client=replica_client,
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


__all__ = [
    "D1Store",
    "CloudflareD1Replica",
    "ReplicationOp",
    "ReplicationError",
    "DEFAULT_CLOUDFLARE_API_BASE",
]
