# Cadence

Cadence is a self-hosted personal attention & context guardian: a brain that ingests events from your accounts and devices, turns them into provenance-tagged facts and deadlines, and (in later milestones) nudges you when your attention drifts from what actually matters. This repo currently holds **Milestone 1: Foundation** — the runnable spine the rest of Cadence is built on, with no live credentials, no recording, and no live-inference engine required to pass its tests.

## What Milestone 1 actually contains

Built and tested (132 tests green):

- **Source Adapter framework** (`cadence/adapters/base.py`) — the `Adapter` ABC (`fetch`/`normalize`/`emit`), the provenance-tagged `Event` schema, `AcquisitionTier` tagging, and an `AdapterRegistry` for per-account instances.
- **Reference adapters, fixtures-based** (`cadence/adapters/github.py`, `cadence/adapters/gcal.py`, `cadence/adapters/email.py`) — GitHub (issues/PRs/review-requests), Google Calendar (events/attendees), and email (messages). Each stores the verbatim raw record in NAS and emits an `Event` carrying only structured fields + a non-verbatim summary. Exercised against recorded fixtures (`tests/fixtures/{github,google_calendar,email}/`) in `tests/test_adapters_reference.py` — no live network call.
- **NAS-only credential vault** (`cadence/adapters/vault.py`) — encrypted-at-rest (stub cipher), per-account scoped, revocable; positively validates its directory resolves inside the configured NAS trust boundary (`settings.nas_root`, following symlinks), chmods the vault dir `0700` and each credential file `0600`, and refuses to start with the checked-in default master key when `CADENCE_ENV=prod`.
- **D1 canonical store** (`cadence/stores/d1.py`, `cadence/stores/models.py`) — a local SQLite database that is the hot-path canonical store for `calendar_event`, `task`, `deadline`, `person`, `place`, `source_account`, `fact`, `nudge`, `feedback`, `sync_session`, plus an in-memory `CloudflareD1Replica` stub (async, no live HTTP) modeling the durable off-site replica. Timestamps use a `UTCDateTime` type decorator so reads always come back timezone-aware UTC even on SQLite; `source_account(provider, account_ref)` and `place.label` carry unique constraints.
- **Raw-boundary enforcement** (`cadence/stores/raw_boundary.py`) — two layers: a structural per-table column allowlist (`SchemaBoundary`, built from the ORM metadata) that is the real guarantee — a write may reference only known columns, and free-text is accepted only on columns explicitly typed as summary/enum/id/hash/label — plus a heuristic `PayloadClassifier` (name denylist, separator-tolerant + Luhn-checked account/card numbers, base64-blob detection) as defense-in-depth. Enforced on every `D1Store.write`/`write_all` call and again when a row is enqueued to the Cloudflare replica.
- **NAS + R2 stores** (`cadence/stores/nas.py`, `cadence/stores/r2.py`) — content-addressed local blob directories (raw evidence vs. derived blobs) plus a `TieringRouter` that keeps RAW → NAS, DERIVED_BLOB → R2, STRUCTURED → D1.
- **Provenance fact graph** (`cadence/brain/facts.py`) — `FactGraph.assert_fact` writes NAS evidence + a deduped, provenance-carrying D1 row (`source_event_ids`, confidence, NAS pointer, expiration, feedback history).
- **Typed-row projection** (`cadence/brain/projection.py`) — beyond the generic `Fact`, a pluggable `ProjectionRegistry` maps known event kinds (`calendar.event`, `github.issue`/`pull_request`/`review_request`, `email.message`) into first-class `calendar_event`/`task` rows, with idempotent get-or-create resolution of `source_account`/`place` entities (safe under concurrent inserts via unique-constraint retry).
- **Ingestion pipeline** (`cadence/ingest/pipeline.py`) — a `WALBuffer` with backpressure, cross-device `dedupe_id` handling, projection, and routing into the fact graph and (via a pluggable `DeadlineExtractor`) into `deadline` rows. The whole per-event critical section is serialized behind an instance lock so concurrent FastAPI requests can't interleave.
- **FastAPI brain** (`cadence/brain/app.py`) — `POST /ingest/event`, `GET /healthz`, `GET /metrics`. The per-app `require_mtls` dependency **fails closed** by default (`settings.require_mtls=True`): a request missing the `X-Client-Cert` header a TLS-terminating proxy would set is rejected with 401. It's still a stub (header presence only, no real certificate verification), but an accidental prod deploy can't silently accept unauthenticated callers; a `RawBoundaryViolation` on ingest returns 422.
- **Deadline parser** (`cadence/brain/deadlines.py`) — `DeadlineExtractor` ABC + `DeadlineCandidate`, plus a concrete `RuleDeadlineExtractor` (rule/heuristic, no LLM call) that the FastAPI app wires in live: it pulls an explicit due date off `Event.structured` when present, pattern-matches an inferred deadline out of `Event.summary` (ISO/slash/month-name/Korean dates, `D-N` countdowns, "due"/"by"/"마감" keyword phrases), and reconciles the two per the explicit-over-inferred convention — flagging `divergence_flag` when they disagree on calendar date. It exposes an `llm_hook` seam for a future LLM-backed inference pass; none is implemented in M1.
- **Observability skeleton** (`cadence/obs/`) — structured JSON logging, an alarm sink (`raw_to_cloud_violation`, `credential_vault_access`, `replication_queue_depth`), a raw-content egress ledger with the two sanctioned channels (`llm_text`, `daglo_audio`), a Prometheus-format `/metrics` renderer, and a provider-agnostic STT interface with a Daglo adapter stub that reads `DAGLO_API_KEY` but refuses to make any live call.
- **Alembic migration** (`alembic/versions/0001_initial_schema.py`) — a frozen, explicit-DDL snapshot of the D1 schema (not autogenerated from live model state, so it stays reproducible independent of future model changes). `alembic/env.py` calls `Settings.ensure_dirs()` before resolving the DB URL, so `alembic upgrade head` works on a clean checkout with no manual setup.

## What is deliberately excluded / gated

Per `.omc/plans/cadence-milestone-1-foundation.md` and `.omc/plans/cadence-consensus-plan.md`, this milestone builds **only** the foundation spine. Explicitly **not** built here:

- Office Raspberry Pi capture node (Decision I — counsel-recommended, presence-gated design; not started).
- Any recording/audio capture or VAD (co-presence, meetings, phone calls) — gated behind the S0.5 compliance-controls step. The only audio-related code in this repo is the interface-only Daglo STT stub in `cadence/obs/stt.py`, which never makes a live call.
- CODEF financial/government data integration — gated behind S0.5.
- Live inference/nudging (the Nudge Governor, confidence-calibrated priority inference) — gated behind S0.2 calibration. M1 ships a rule/heuristic deadline extractor (no LLM), not the full inference engine.
- Device agents (Android, Windows, watch).
- VM surface (Instagram Stories sweep) and co-presence/speaker-ID sensing.

See `.omc/plans/cadence-consensus-plan.md` for the full architecture rationale (Decisions A–I) and `.omc/plans/cadence-milestone-1-foundation.md` for this milestone's exact scope and acceptance criteria.

## Quickstart

```bash
pip install -e ".[dev]"

# Apply the D1 schema (creates var/d1.sqlite by default; see cadence/config.py).
# alembic/env.py calls Settings.ensure_dirs() first, so this works on a clean checkout.
alembic upgrade head

# Run the test suite
pytest

# Lint
ruff check .

# Run the FastAPI brain (dev)
uvicorn cadence.brain.app:create_app --factory --reload
```

Configuration is environment-based (prefix `CADENCE_`, optional `.env` file) — see `cadence/config.py` for every setting (storage-tier paths, the `nas_root` trust boundary, `env` for prod fail-closed gates, Cloudflare replica placeholders, vault master key, `require_mtls`, Daglo placeholder, raw-boundary length limits).

## Architecture (built spine)

```
 ┌───────────────── Source Adapters (per-account) ─────────────────┐
 │  Adapter ABC: fetch() → normalize() → emit()                     │
 │  GitHub · Google Calendar · Email  (fixtures-based, no live call) │
 │  common Event schema (provenance-tagged) + AcquisitionTier       │
 │  CredentialVault (NAS-only, encrypted-at-rest, nas_root-checked) │
 └───────────────────────────┬───────────────────────────────────────┘
                              │ Event (structured + NAS pointer, never raw)
 ┌────────────────────────────▼──────────────────────────────────────┐
 │ Ingestion (FastAPI POST /ingest/event, mTLS-checked, fail-closed)  │
 │   WALBuffer (backpressure) → dedupe_id → … (serialized per-event)  │
 └───────────┬───────────────────┬─────────────────────┬─────────────┘
             │                   │                      │
     ┌───────▼────────┐  ┌───────▼─────────┐  ┌──────────▼────────────┐
     │  FactGraph      │  │  Projection      │  │ RuleDeadlineExtractor │
     │  (assert_fact,  │  │  (typed rows:    │  │ (explicit + inferred, │
     │   dedup+merge)  │  │  calendar_event, │  │  no LLM; reconciled)  │
     │                 │  │  task)           │  │                       │
     └───────┬─────────┘  └────────┬─────────┘  └───────────┬───────────┘
             │ fact row            │ typed rows              │ deadline rows
             ▼                     ▼                         ▼
 ┌──────────────────────────── D1 (LOCAL-CANONICAL SQLite) ─────────────────────────┐
 │ calendar_event · task · deadline · person · place · source_account ·             │
 │ fact · nudge · feedback · sync_session                                           │
 │ Every write passes SchemaBoundary (structural per-table column allowlist) +      │
 │ PayloadClassifier (heuristic backstop) — no verbatim content, ever.              │
 └───────────────────┬───────────────────────────────────────┬──────────────────────┘
                      │ async enqueue (boundary re-checked)      │ raw_evidence_id/hash
             ┌────────▼─────────────┐                 ┌─────────▼─────────┐
             │ Cloudflare D1 replica│                 │      NAS          │
             │ (STUB — in-memory    │                 │ (raw evidence,    │
             │  queue, no HTTP)     │                 │  content-addressed│
             └───────────────────────┘                │  local blob dir)  │
                                                        └─────────┬─────────┘
                                                                  │ derived blobs only
                                                        ┌─────────▼─────────┐
                                                        │        R2         │
                                                        │ (derived blobs —  │
                                                        │  stub local dir)  │
                                                        └────────────────────┘

 Observability: alarm sink (raw_to_cloud_violation · credential_vault_access ·
 replication_queue_depth) + raw-content egress log, two sanctioned channels:
   llm_text     — raw text to the user-configured LLM provider
   daglo_audio  — raw audio to Daglo STT (interface-only in M1; no live call)
```

**Invariants this diagram encodes** (see `AGENTS.md` for the full list): all hot-path reads/writes hit the local SQLite D1, never the cloud replica; D1 and R2 hold structured/derived data only — raw evidence lives solely in NAS, referenced by opaque id/hash, enforced by the structural `SchemaBoundary` allowlist; credentials live only in the NAS-only vault, positively boundary-checked against `nas_root`; every raw-content egress goes through one of exactly two logged channels; the mTLS ingest gate fails closed by default.
