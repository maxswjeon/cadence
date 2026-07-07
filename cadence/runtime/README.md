# Cadence runtime (`cadence.runtime`)

The layer that actually **runs** the brain. The engine, governor, ingest pipeline, and LLM
providers are libraries — nothing below this package drives a loop or pushes a nudge to a
phone. This package composes them into a runnable service:

```
events ──▶ IngestPipeline ──▶ D1 state
                                  │
             TickScheduler ──▶ AttentionEngine.evaluate(now) ──▶ NudgeGovernor
             (every interval)                                        │
                                        live nudge (nudge_id set) ───┤
                                                                     ▼
                                                            NudgeDelivery.deliver
                                                        (console | mock | FCM push)
                                                                     │
                          phone taps Thanks!/Dismiss ───────────────┘
                                     │
                POST /nudge/{nudge_id}/feedback ──▶ governor.record_feedback (adjusts threshold)
```

## Run it

```bash
python -m cadence.runtime
```

By default this is fully safe: the governor runs in **shadow** mode (proposes nudges,
persists nothing, delivers nothing), delivery is **console** (structured log lines, no
network), and the **LLM is off**. Serving the HTTP API (ingest + feedback) needs an ASGI
server — install `uvicorn` and it is served on `0.0.0.0:8000`; without it the tick
scheduler still runs and a log line explains how to enable HTTP.

## Config knobs (environment)

| Env var | Default | Meaning |
|---|---|---|
| `CADENCE_GOVERNOR_MODE` | `shadow` | `shadow` (propose only) or `live` (persist + deliver). Even in `live`, inference stays shadow-gated until S0.2. |
| `CADENCE_TICK_INTERVAL_SECONDS` | `60` | Seconds between engine ticks. |
| `CADENCE_DELIVERY_PROVIDER` | `console` | `console` (log), `mock` (in-memory, tests), or `fcm` (real push). |
| `CADENCE_FCM_PROJECT_ID` | — | Firebase project id (required for `fcm`). |
| `CADENCE_FCM_DEVICE_TOKENS` | — | Comma-separated device registration tokens. |
| `CADENCE_LLM_ENABLED` / `CADENCE_LLM_PROVIDER` | `false` / `none` | Inference go-live (see `cadence/llm/README.md`); the runtime wires the factory hooks only when it returns non-`None`. |

Standard `CADENCE_*` storage/env settings (D1 path, vault dir, `env`, mTLS) come from
`cadence.config.Settings`.

## What is delivered, and what is not

- **Only LIVE nudges are pushed.** A shadow-mode `ProposedNudge` has `nudge_id is None`;
  the scheduler skips it, and `NudgeDelivery.deliver` guards it again. Shadow proposals are
  never delivered.
- **No raw evidence in a push.** The `NudgeView` payload is built only from the nudge's
  derived, non-verbatim `message_summary` + category + ids, plus the `Thanks!` / `Dismiss`
  action buttons carrying the `nudge_id`. `source_event_ids` and confidence never leave.
  (A derived nudge payload is therefore *not* a raw-egress channel — but never put raw
  evidence in one.)
- **Resilience.** A raised exception in any tick (engine or a delivery) is caught and
  logged; the loop continues. `TickScheduler.tick_once(now)` is the deterministic unit.

## Delivery transports

- `ConsoleDelivery` (default) — one structured JSON log line per nudge. No creds.
- `MockDelivery` — records `NudgeView`s in memory for tests.
- `FCMDelivery` — real FCM **HTTP v1** (`POST …/v1/projects/{project}/messages:send`) with
  an OAuth2 bearer token minted from a Firebase **service-account JSON** in the NAS vault
  (`provider="fcm"`). Handles a rejected token (401 → re-mint next tick) and a dead device
  token (`UNREGISTERED`/`NOT_FOUND` → pruned via the `on_unregister` callback). The request
  *shape* is tested against a fake local server; **real sending is a runtime plug-in point**.

## Remaining plug-in points to go fully live

1. **Real FCM credentials** — store the Firebase service-account JSON in the vault under
   `provider="fcm"`, set a real `CADENCE_VAULT_MASTER_KEY` (the vault refuses live creds
   under the default key), and install `cryptography` (the RS256 signer used to mint the
   OAuth token). Token minting is injectable (`access_token_provider`) if you prefer to
   supply tokens another way.
2. **A device token** — register the phone and set `CADENCE_FCM_DEVICE_TOKENS` (or wire a
   dynamic token source into `FCMDelivery(device_tokens=…)`).
3. **LLM onboarding** — onboard a provider (`python -m cadence.onboarding …`) and set
   `CADENCE_LLM_ENABLED=true` + `CADENCE_LLM_PROVIDER=…`. The factory then returns the
   deadline/receptiveness hooks the runtime wires in; otherwise they stay `None` (off).
4. **S0.2 calibration data** — governor `live` mode + enabled inference remain shadow-gated
   by the S0.2 precision/recall gate until that data clears it.
