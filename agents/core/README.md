# cadence-agent-core

Portable, crash-safe **capture core** shared by Cadence's device agents. The hard,
security-critical device logic — a persistent write-ahead log, content-hash dedupe IDs,
bounded-queue backpressure, envelope construction, and an mTLS transport with retry/replay —
is implemented **once** here in Rust so each platform agent (Android, Windows, …) can bind to
it via FFI instead of re-implementing it (and re-introducing its bugs).

This is the **load-bearing, buildable-and-tested** deliverable of Milestone 2 W2. It has no
OS dependency: real notification/screen/SMS capture belongs to the platform agents; this crate
provides the durability, dedupe, backpressure, and transport machinery they feed.

## What it is

| Piece | Type | Role |
|---|---|---|
| `EventEnvelope` | `envelope.rs` | On-the-wire event. Matches the brain's `Event` (`cadence/adapters/base.py`) field-for-field. **Structured fields + provenance pointers + a non-verbatim `summary` only — never verbatim raw.** |
| `AcquisitionTier` | `envelope.rs` | Trust/compliance tag; wire values match the brain's `StrEnum`. |
| `Wal` | `wal.rs` | Append-only, `fsync`-durable, crash-safe WAL with a **bounded** pending set and compaction. |
| `Transport` | `transport.rs` | Trait over `POST /ingest/event`. `HttpsTransport` = reqwest + rustls **mutual TLS**; `MockTransport` for tests. |
| `AgentCore` | `agent.rs` | Orchestrator: capture → WAL → drain with retry/backoff → ack-on-delivery. Gives **exactly-once**. |
| `CaptureSource` | `source.rs` | Read-only source trait; `TimerSource`/`FakeSource` reference impls. |

### The envelope & dedupe

The device computes `dedupe_id` as a **SHA-256 content hash of stable fields**
(`source`, `account_ref`, `event_id`, `kind`, `occurred_at`, `payload_hash`, `device_id`).
Transient fields (`ingested_at`, `summary`, `confidence`, `structured`) are excluded, so a
re-capture of the same source event hashes identically. The brain dedupes on this
(`202` new / `200` duplicate), which is what turns at-least-once delivery into exactly-once.

**Raw boundary:** the envelope never carries verbatim message/audio/screen bytes. Raw stays
device-local (NAS) and is referenced by `raw_evidence_ref` + `payload_hash`. The brain
enforces this again and returns `422` on a violation; the agent dead-letters such events.

### Ingest status mapping (matches `cadence/brain/app.py`)

| HTTP | `Outcome` | Agent action |
|---|---|---|
| `202` | `Accepted` | ack + drop from WAL |
| `200` (duplicate) | `Duplicate` | ack + drop (delivery already confirmed) |
| `503` (backpressure) | `Backpressure` | **keep buffered**, retry with backoff |
| `422` (raw-boundary) | `Rejected` | dead-letter (permanent client error) |
| network drop / other | `NetworkError` | retry with backoff |

## Build & test

```sh
cargo build
cargo test
cargo clippy --all-targets --all-features -- -D warnings
```

Tests use `MockTransport` only — **no network or TLS setup required**. The default `https`
feature compiles the real reqwest/rustls transport so it is always linted and built.

### Properties the tests prove

- **Crash-safe WAL replay, no loss** (`tests/wal_replay.rs`): un-acked events survive a
  drop-without-ack (simulated crash) and replay in order; acked events do not return.
- **Torn-tail recovery** (`tests/wal_replay.rs`): a partial trailing frame (crash mid-write /
  trailing garbage) is discarded, and every intact prior frame is still recovered; the log
  stays usable afterward.
- **Compaction is faithful & durable** (`tests/wal_replay.rs`): rewriting the log preserves the
  exact pending set and order across reopen.
- **Exactly-once under 503 + network drops** (`tests/delivery.rs`): a scripted burst of drops
  and `503`s is retried until every event is delivered; each `dedupe_id` is `Accepted` at the
  brain exactly once — no loss, no duplicate ingest.
- **Exactly-once across a crash** (`tests/delivery.rs`): a crash between the wire send and the
  WAL ack redelivers the event; the brain returns `200` (dedupe), so it is ingested exactly
  once. This is the core guarantee.
- **Persistent backpressure buffers, never drops** (`tests/delivery.rs`): a drain that exhausts
  its retry budget leaves everything safely buffered; a later drain delivers it once.
- **Dead-lettering** (`tests/delivery.rs`): a permanent `422` is dead-lettered and does not
  block the rest of the queue.
- **Bounded memory / backpressure** (`tests/backpressure.rs`): with the brain unreachable,
  capture is rejected with `QueueFull` once the bounded WAL is full; the pending set never
  exceeds capacity regardless of arrival volume. A drain relieves it and capture resumes.

`cargo test` summary: **14 passed** (3 unit + 3 backpressure + 5 delivery + 3 wal_replay);
`cargo clippy --all-targets --all-features -- -D warnings`: **clean**.

## FFI-binding intent (Android / Windows)

The crate builds a `cdylib` (see `Cargo.toml` `crate-type`) so platform agents link the same
core:

- **Android (Kotlin, W3):** a thin **JNI** layer (a `CoreBridge`) wraps `AgentCore` — the Kotlin
  services (`CadenceNotificationListenerService` etc., all read-only) map platform events to
  `EventEnvelope`s and hand them to the core; the core owns the Room-adjacent WAL, dedupe,
  backpressure, and the mTLS upload. A stable C ABI (envelope in as JSON/bytes, capture/drain
  calls out) is the intended boundary.
- **Windows (C#/.NET, W4):** the same core via **P/Invoke** (`CoreInterop`) from the .NET agent
  (`ActiveWindowCollector`, `AppUsageCollector`, …). The `cdylib` exports the C ABI.

A C-ABI FFI surface (envelope JSON marshalling + `capture`/`drain`/`pending_len` entry points)
is a follow-up once the platform skeletons need it; the Rust API here is the source of truth
those bindings wrap. `#![forbid(unsafe_code)]` holds for the pure-Rust core today; the FFI shim
will be the only `unsafe` boundary and will live behind an `ffi` feature.

## Notes / flagged decisions

- **`schema_version` is not on the wire.** The brain's `Event` uses `extra="forbid"`, so any
  unknown key (including `schema_version`) would be a `422`. The version is exposed as the
  `SCHEMA_VERSION` constant for build-time agreement with `contract/event-envelope.schema.json`
  (W1) instead. If W1 needs `schema_version` transmitted, the brain's `Event` must first accept
  it — flagged for reconciliation with workstream W1 rather than silently diverging.
- **Timestamps** are RFC 3339 (`chrono` `DateTime<Utc>`); `None` optionals are omitted from the
  JSON so the payload matches what a Python `Event` emits.
