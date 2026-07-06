# Device ↔ Brain ingest protocol (v1)

This document specifies the wire protocol device agents (Android, Windows, and any
future platform agent built on `cadence-agent-core`) use to deliver events to the
Cadence brain. The wire shape is [`event-envelope.schema.json`](./event-envelope.schema.json)
(`schema_version: "1.0.0"`); this file specifies the *behavior* around it: transport,
endpoint semantics, retry/backpressure, idempotency, and clock rules.

The brain implementation is `cadence.brain.app.create_app` (endpoint) and
`cadence.ingest.pipeline.IngestPipeline` (semantics). Where this document and the code
diverge, the code is authoritative — file a contract-version bump.

## 1. Transport: mTLS

Every device→brain call is expected over mutual TLS, terminated at a TLS-terminating
proxy in front of the brain. The brain's own choke point is `require_mtls` (see
`cadence.brain.app.create_app`):

- **Fails closed by default** (`Settings.require_mtls = True`): a request with no
  `X-Client-Cert` header is rejected with `401`.
- Today this is a **stub** — only the *presence* of `X-Client-Cert` is checked, not a
  real certificate chain/subject. Treat the header as "the proxy already did mTLS and
  is vouching for this connection," not as an application-level identity you can trust
  the contents of.
- Devices should never terminate TLS themselves and forge this header; it is only
  meaningful when set by the trusted proxy in front of the brain. In dev/test the gate
  is disabled (`require_mtls=False`) since TestClient calls don't traverse a proxy.
- A device holds a per-install client certificate (provisioned out of band, out of
  scope for this document) and presents it at the mTLS handshake; individual event
  authentication/authorization beyond "this is a trusted device channel" is not yet
  modeled — `device_id` in the envelope is a dedupe/observability tag, not an auth
  credential.

## 2. `POST /ingest/event`

Request body: exactly one JSON object conforming to `event-envelope.schema.json`.
Devices MUST send one event per request (no batching in v1).

### Response codes

| Status | Meaning | Body shape |
|---|---|---|
| `202` | Accepted — new event, ingested. | `{"accepted": true, "duplicate": false, "dedupe_id", "fact_id", "deadlines_created", "wal_offset"}` |
| `200` | Accepted — but this `dedupe_id` was already seen; no new write happened. | `{"accepted": false, "duplicate": true, "dedupe_id", "fact_id": null, ...}` |
| `503` | Backpressure — the brain's WAL buffer is full and cannot accept more events right now. | `{"accepted": false, "error": "backpressure", "detail": "..."}` |
| `422` | Rejected. Two distinct causes — see below. | see below |
| `401` | mTLS required and `X-Client-Cert` missing (see §1). | FastAPI default `HTTPException` body. |

**Both `200` and `202` mean "the brain has durably recorded this `dedupe_id` (or
already had it) — the device MAY drop it from its local WAL."** Devices MUST NOT
distinguish 200 vs 202 for WAL-retirement purposes; both are terminal-success.

### The two flavors of `422`

1. **Envelope-shape violation** (a plain FastAPI/pydantic validation error — e.g. an
   extra/unknown field, wrong type, missing required field). This is the generic
   FastAPI validation error body (`{"detail": [...]}`), raised before the event ever
   reaches `IngestPipeline`. A device sending a schema-conformant envelope will never
   hit this; it indicates a device/contract version mismatch.
2. **Raw-boundary violation** (`cadence.stores.raw_boundary.RawBoundaryViolation`) —
   the envelope was well-formed but a field (most commonly an overlong or
   content-flagged `summary`, or a `structured` value that looks like verbatim raw
   content) tripped the brain's raw-boundary classifier on write. Body:
   `{"accepted": false, "error": "raw_boundary_violation", "field": "...", "reason": "..."}`.
   **This is not retryable as-is** — the device must not resend the same envelope; it
   must either drop the offending field/event or fix how it builds `summary`/`structured`
   before generating a new envelope. Do not treat this as backpressure.

## 3. Device-side WAL, replay, and backpressure

Every device agent MUST persist an event to a local, crash-safe WAL **before**
attempting delivery — the network call is best-effort; durability is device-local
until acknowledged. (The Rust reference core, `agents/core/`, implements this WAL and
transport client; see W2.)

State machine per WAL entry:

1. **Append** the envelope to the local WAL (append-only; survives process/device
   restart).
2. **Attempt delivery** (`POST /ingest/event`).
3. On `200` or `202` → **retire** the WAL entry (safe to drop/compact).
4. On `503` (backpressure) → **keep** the entry, back off (exponential backoff with
   jitter recommended; the brain does not currently return a `Retry-After` header — a
   device-side fixed backoff ladder is fine for v1), and retry later. The device MUST
   NOT drop the entry.
5. On network failure/timeout → **keep** the entry, back off, retry. Indistinguishable
   from (4) from the device's perspective; treat both as "try again later."
6. On `422` raw-boundary violation → **retire** the WAL entry but surface/log the
   rejection locally (see §2) — resending verbatim will fail identically.
7. On `422` envelope-shape violation → **retire** the WAL entry and surface a
   contract-version-mismatch diagnostic; resending verbatim will fail identically.
8. **Startup replay**: on process/device restart, the WAL is replayed from its oldest
   un-retired entry, in append order, before any new events are captured and appended.
   Replay must be idempotent — safe to re-attempt an entry that was actually delivered
   but whose ack was lost (the `dedupe_id` makes a redundant redelivery a `200`, not a
   duplicate fact).

The brain's own `WALBuffer` (`cadence.ingest.pipeline.WALBuffer`) is a bounded,
in-memory, per-process append log that backstops a single `IngestPipeline` instance
(`max_depth`, default `10_000`); it is not yet the durable, offset-tracked, replicated
WAL described in the milestone-2 architecture note. `503` from the brain today means
"this process's in-memory buffer is momentarily full," not a durable server-side queue
overflow — but devices should treat it identically either way (back off, keep local
WAL entry).

## 4. Idempotency (`dedupe_id`)

Every event MUST resolve to exactly one `dedupe_id`, which the brain uses as the sole
cross-device idempotency key (`IngestPipeline.ingest`, in-memory `_seen` set in v1).

- A device MAY set `dedupe_id` itself (e.g. a content hash it already computed for its
  own WAL). If set, the brain uses it verbatim and does **not** recompute it
  (`Event.with_dedupe_id()` only fills an unset `dedupe_id`).
- If omitted, the brain derives `dedupe_id = sha256("{source}|{account_ref}|{event_id}")`.
  This means: **the same logical event observed on two different devices dedupes to
  the same fact** as long as `source`, `account_ref`, and `event_id` agree — this is
  the mechanism that makes "seen on phone + laptop" collapse to one fact. Devices that
  supply their own `dedupe_id` must preserve this same-source-same-account-same-event
  collision property, or cross-device dedupe silently breaks.
- `event_id` therefore MUST be stable and reproducible for the same underlying source
  item (e.g. a notification's `(package, notification key, post time)` tuple, not a
  random UUID minted per capture attempt) — a random `event_id` defeats dedupe on
  every retry, not just across devices.

## 5. Clock / timestamp rules

- All timestamps in the envelope (`occurred_at`, `ingested_at`) are **UTC**,
  ISO-8601 (`...Z` or an explicit `+00:00` offset — the schema's `date-time` format
  accepts either, but devices SHOULD emit `Z`).
- `occurred_at` is the source event's own time (e.g. a notification's post time); set
  it to `null`/omit only when genuinely unknown.
- `ingested_at` SHOULD be omitted by devices in v1 — the brain stamps it with its own
  receipt time (`Event.ingested_at` default factory). If a device does set it, it must
  be a real UTC instant (never `null` — omit the field entirely instead of sending
  `null`, since the underlying type is non-nullable).
- Devices are not assumed to have a trustworthy clock; no ordering or freshness
  guarantee is derived from `occurred_at`/`ingested_at` in v1 — they are informational
  only, not used for dedupe or sequencing.

## 6. Ordering guarantees

**None, across devices or sources.** `IngestPipeline.ingest` serializes the whole
dedupe-check → WAL-append → fact/projection/deadline-write critical section behind a
single lock, so concurrent requests to one brain instance never interleave — but that
only guarantees each individual ingest is atomic, not that events are processed in any
particular relative order. Whichever request reaches the brain first "wins" the fact
write for a given `dedupe_id`; later duplicates (from a slower device, a WAL replay, or
a retried delivery) become `200`s. Devices MUST NOT depend on causal/relative ordering
across their own retries or across other devices — if ordering of a sequence of events
matters, encode that ordering as data (e.g. a sequence number or timestamp in
`structured`), not as reliance on delivery order.

## 7. Non-negotiable invariants carried by this contract

- **No verbatim raw content** ever appears in an envelope — only structured fields,
  provenance pointers (`raw_evidence_ref`, `payload_hash`), and a bounded non-verbatim
  `summary`. Verbatim evidence stays device-local/NAS; see the raw-boundary policy in
  `cadence/stores/raw_boundary.py`.
- **No secrets** in any field — `account_ref` is an opaque handle, never a credential.
- Every envelope carries `acquisition_tier`, so the brain can apply tier-appropriate
  trust/compliance handling downstream.
