# AGENTS.md — Cadence contributor/agent guide

This file is for anyone (human or agent) extending Cadence's Milestone 1 foundation spine. Read `README.md` first for what's built; this file covers the invariants you must uphold, how to extend the code, and what not to touch.

## Architecture invariants (non-negotiable)

These are enforced in code and by tests — do not weaken them to make a feature "just work."

1. **D1/R2 raw boundary — no verbatim raw content in D1 or R2.**
   D1 (`cadence/stores/d1.py`) and R2 (`cadence/stores/r2.py`) may hold only structured/derived fields, source-event IDs, hashes, confidence values, timestamps, and short non-verbatim summaries. Two layers enforce this (`cadence/stores/raw_boundary.py`):
   - **`SchemaBoundary`** — the real guarantee. Built from the ORM metadata (`_schema_boundary_from_models` in `cadence/stores/d1.py`), it knows every D1 column and the *shape* of value each may carry (`ColumnKind`: `SCALAR`/`JSON`/`ID`/`HASH`/`ENUM`/`LABEL`/`SUMMARY`). A write may reference only known columns, and free-text is accepted only on columns typed `LABEL`/`SUMMARY`/`ENUM` — a raw `Text` column with no such typing, or an unknown column entirely, is rejected. `D1Store.enforce_boundary` calls `SchemaBoundary.enforce_row` on every write; `CloudflareD1Replica.enqueue` calls it again before queuing.
   - **`PayloadClassifier`** — a heuristic backstop for callers with no table context: a field-name denylist (`_RAW_FIELD_TOKENS` plus substring matches like `emailbody`), separator-tolerant + Luhn-checked account/card-number detection, and base64-blob detection.
   A violation raises `RawBoundaryViolation` and fires the `raw_to_cloud_violation` alarm. See `tests/test_raw_boundary.py`, `tests/test_data_layer_hardening.py`, and the end-to-end check in `tests/test_e2e.py`. If you add a column, it is picked up automatically by `SchemaBoundary` from the model metadata — pick a name/type that reflects its real shape (don't declare a raw-text column and expect the boundary to catch it after the fact).

2. **Local-canonical D1 — no cloud on the hot path.**
   Every read/write in M1 hits the **local** SQLite file (`Settings.d1_path` / `D1Store.engine`) directly. The `CloudflareD1Replica` in `cadence/stores/d1.py` is written to asynchronously (`replicate`/`enqueue`) and modeled as a durable off-site copy / degraded-mode read source — it is never on the decision path. Do not add code that reads from or blocks on the replica during normal ingestion.

3. **Provenance on every derived fact.**
   Anything asserted into the fact graph (`cadence/brain/facts.py`) must carry `source_event_ids`, a provenance pointer (`raw_evidence_id`/`raw_evidence_hash` into NAS), a `confidence_value` + `confidence_type`, and (where applicable) `expires_at`. `FactGraph.assert_fact` dedupes on a stable `dedupe_key` and **merges** provenance into an existing fact rather than duplicating it — preserve that merge behavior if you touch this path. Feedback history lives in the separate `feedback` table, not inline on the fact. The same discipline extends to typed projected rows (`cadence/brain/projection.py`): every projector attaches `_provenance(event)` (confidence, source-event ids, NAS pointer, summary) to the row it emits.

4. **Credential vault is NAS-only — never cloud.**
   `CredentialVault` (`cadence/adapters/base.py`) implementations must store secrets only under a NAS-local path. `FileCredentialVault` (`cadence/adapters/vault.py`) **positively** validates its base directory resolves (after following symlinks) inside `settings.nas_root` — the NAS trust boundary, which defaults to the common ancestor of `data_dir`/`nas_dir`/`r2_dir`/`d1_path.parent` (see `Settings._default_nas_root`) — plus a belt-and-suspenders substring blocklist (`r2_blobs`, `d1.sqlite`). It also refuses to start when `CADENCE_ENV=prod` and `vault_master_key` is still the checked-in `DEFAULT_VAULT_MASTER_KEY`. The vault directory is chmod'd `0700` and every credential file `0600`; writes go through `_atomic_write` (temp file + `os.replace`) so a crash mid-write can't corrupt a credential or the sidecar index. Every access fires `credential_vault_access`. Secrets are per-account scoped and individually revocable (`revoke()`); do not add a bulk-secret-dump path.

5. **Exactly two raw-content egress channels.**
   Per Decision E, raw content may leave the machine only via `EgressChannel.LLM_TEXT` (raw text to the user-configured LLM) or `EgressChannel.DAGLO_AUDIO` (raw audio to Daglo STT) — both logged in `cadence/obs/egress.py` (`RawEgressLog`), keyed by content hash, never verbatim payload. `cadence/obs/stt.py`'s `DagloSTTAdapter` records the would-be egress event but then raises `LiveCallDisabled` — M1 makes no live STT call. Do not add a third silent egress path; any new raw-content-leaving-the-machine code must record through `get_egress_log()`.

## The `Adapter` contract — adding a new source

The framework is in `cadence/adapters/base.py`; `github.py`, `gcal.py`, and `email.py` are the reference implementations to copy the pattern from. To add a new source:

1. Subclass `Adapter`, set `provider: str` (concrete, not `"abstract"`) and `acquisition_tier: AcquisitionTier`.
2. Implement `fetch(self) -> Iterable[RawRecord]` — pull raw records. The reference adapters accept either an injected `records` list or a `fixture_path` JSON file (see `tests/fixtures/{github,google_calendar,email}/`) and raise if neither is given — no live network call is required or made.
3. Implement `normalize(self, raw) -> Event` — convert one raw record into a provenance-tagged `Event`. **Store the verbatim raw record in NAS yourself** (the reference adapters use a shared `_store_verbatim` helper that JSON-serializes the record deterministically and calls `nas.put`) and set `raw_evidence_ref`/`payload_hash` on the `Event` from the returned `BlobRef`; only structured fields, a non-verbatim `summary`, and provenance pointers flow onward. The `Event` model is `extra="forbid"` — you cannot smuggle a `raw_body`-style field through it.
4. If the event kind should produce a first-class typed row (not just a generic `Fact`), register a projector in `cadence/brain/projection.py`'s `default_projection_registry()` — see `project_calendar_event`/`project_task`/`project_email_task` for the pattern (conservative: only project when the mapping is clean, e.g. `project_email_task` only fires when `structured["actionable"]` is true).
5. Leave `emit()` as-is unless your source has different streaming semantics — the default `fetch → normalize → tag acquisition_tier → dedupe_id → yield` orchestration is what the pipeline expects.
6. Register the class with `@registry.register` so `AdapterRegistry.create(provider, account_ref, vault=...)` can build per-account instances.
7. Resolve credentials via `self.credentials()`, which reads from the injected NAS-only `CredentialVault` — never pass secrets around in the clear beyond the adapter instance.

## The `DeadlineExtractor` / `RuleDeadlineExtractor`

`cadence/brain/deadlines.py` defines the contract: `DeadlineExtractor.extract(event) -> list[DeadlineCandidate]`, pure functions over an `Event` (no I/O, no reaching into raw NAS evidence beyond what the `Event` exposes). `DeadlineCandidate.origin` is `"explicit"` or `"inferred"`; the convention — **prefer explicit-source deadlines over inferred, and flag divergence** (`divergence_flag`) rather than silently overriding — is implemented by `RuleDeadlineExtractor._reconcile`. `RuleDeadlineExtractor` is wired live in `cadence/brain/app.py`'s `create_app` lifespan (not `NullDeadlineExtractor`, which remains available for tests/spine-only use). It reads an explicit due date off `Event.structured` (`_EXPLICIT_STRUCTURED_KEYS`) and separately pattern-matches an inferred one out of `Event.summary` only (never raw NAS evidence). To attach a future LLM-backed inference pass, pass `llm_hook: Callable[[Event], list[DeadlineCandidate]]` to `RuleDeadlineExtractor.__init__` — its candidates flow through the same reconciliation as the rule-based pass. No `llm_hook` is implemented in M1.

## Module map

```
cadence/
  adapters/
    base.py        Adapter ABC, Event schema, AcquisitionTier, AdapterRegistry, CredentialVault ABC
    vault.py        FileCredentialVault (NAS-only, nas_root-checked, encrypted-at-rest stub)
    github.py       GitHub reference adapter (issues/PRs/review-requests → task candidates)
    gcal.py         Google Calendar reference adapter (events/attendees → calendar_event)
    email.py        Email reference adapter (messages → task/deadline candidates)
  brain/
    app.py           FastAPI app factory: POST /ingest/event, GET /healthz, GET /metrics,
                      require_mtls (fails closed by default)
    facts.py         FactGraph — provenance fact graph write/dedupe path
    projection.py    ProjectionRegistry — typed calendar_event/task rows alongside the Fact
    deadlines.py     DeadlineExtractor contract, NullDeadlineExtractor, RuleDeadlineExtractor
  ingest/
    pipeline.py      IngestPipeline, WALBuffer (backpressure), dedupe, projection + deadline routing,
                      per-event lock for concurrent-request safety
  stores/
    models.py        SQLAlchemy models: calendar_event, task, deadline, person, place,
                      source_account, fact, nudge, feedback, sync_session; UTCDateTime type decorator
    d1.py             D1Store (local-canonical) + CloudflareD1Replica (async stub);
                      builds the SchemaBoundary from ORM metadata
    raw_boundary.py  SchemaBoundary (structural allowlist) + PayloadClassifier (heuristic backstop)
    nas.py           NASStore — content-addressed raw-evidence blob dir
    r2.py            R2Store + TieringRouter — content-addressed derived-blob dir + tier routing
  obs/
    logging.py       Structured JSON logging (never logs verbatim raw content)
    alarms.py        AlarmSink: raw_to_cloud_violation, credential_vault_access, replication_queue_depth
    egress.py        RawEgressLog — the two sanctioned raw-content egress channels
    stt.py           STTProvider ABC + DagloSTTAdapter (interface-only, no live call)
    metrics.py       Prometheus-text renderer for /metrics
  config.py           Settings (env prefix CADENCE_) — storage-tier paths, nas_root trust boundary,
                      env (prod fail-closed gates), require_mtls, replica/vault/Daglo placeholders
alembic/
  env.py               Calls Settings.ensure_dirs() before resolving the DB URL (clean-checkout safe)
  versions/0001_initial_schema.py   Frozen, explicit-DDL D1 schema snapshot (not autogenerated)
tests/                One test module per component, plus tests/fixtures/{github,google_calendar,email}/
                       for the reference adapters; conftest.py provides isolated tmp-path settings +
                       an in-memory D1 store per test, and resets the alarm/egress singletons between tests
```

## Running tests and lint

```bash
pip install -e ".[dev]"
pytest                 # 132 tests as of this writing; testpaths = ["tests"]
ruff check .            # see [tool.ruff] in pyproject.toml (line-length 100, py311 target)
alembic upgrade head    # apply the D1 schema to var/d1.sqlite (path from cadence/config.py);
                        # works on a clean checkout — env.py creates the dirs first
```

`pytest.ini_options` in `pyproject.toml` turns on `asyncio_mode = "auto"` and errors on any `DeprecationWarning` raised from `cadence.*` — don't introduce one and silence it instead of fixing it.

## Do NOT build here / gated features

The following are explicitly out of scope for the foundation spine and gated by later milestones (`.omc/plans/cadence-consensus-plan.md`, Phase 0 / S0.2 / S0.5):

- **Office Raspberry Pi capture node** (Decision I) — presence-gated design with an unresolved legal risk acceptance (counsel recommended); do not implement any part of its capture pipeline here.
- **Any recording/audio capture, VAD, or speaker-ID on live audio** — gated behind S0.5 compliance controls. `cadence/obs/stt.py` is interface-only by design; do not make `DagloSTTAdapter.transcribe` actually call out.
- **CODEF financial/government data integration** — gated behind S0.5; no credentials, no live calls.
- **Live inference / confidence-calibrated priority inference / the Nudge Governor** — gated behind S0.2 calibration. `RuleDeadlineExtractor` is rule/heuristic only (no LLM call); its `llm_hook` seam exists but do not wire a real inference engine into it without going through the S0.2 gate.
- **Device agents** (Android, Windows, watch) — not part of this repo's scope.
- **Real mTLS certificate verification** — `require_mtls` in `cadence/brain/app.py` fails closed on missing `X-Client-Cert` but only checks header *presence*, not a real certificate; it's the designated choke point for later, not something to half-implement now.
- **A real Cloudflare-D1 HTTP client** — `CloudflareD1Replica` is an in-memory, no-HTTP stub; don't add live network calls to it without also handling the failover/lease semantics in Decision C (out of scope for M1).
- **VM surface (Instagram Stories sweep) and co-presence/speaker-ID sensing** — not started.

When in doubt about whether something belongs in this milestone, check `.omc/plans/cadence-milestone-1-foundation.md`'s task list and the "Explicitly EXCLUDED" line at its top before building.
