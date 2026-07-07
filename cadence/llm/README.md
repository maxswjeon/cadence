# `cadence/llm` — live LLM inference core

Wires a real LLM into the brain's inference **seams** — the deadline extractor's
`llm_hook` and the nudge governor's `receptiveness_hook` — behind a provider abstraction
that speaks the OpenAI **Responses API**. Nothing here goes live on its own (see the gate
below).

## Posture — read this first

| Provider | Status | Auth | Notes |
|---|---|---|---|
| **`openai_api`** (`OpenAIAPIProvider`) | **Supported default. Use this.** | `Authorization: Bearer sk-...` | Pay-per-token, no ToS issue, no rate-limit surprises. |
| `chatgpt_oauth` (`ChatGPTOAuthProvider`) | **Opt-in · gray-zone · may break** | ChatGPT-account OAuth `access_token` | Reaches `chatgpt.com/backend-api/codex` by presenting the **public Codex `client_id` + `originator`** the way the Codex CLI does. Unsupported for non-Codex use, **may break without notice, ToS-gray, subject to message-count rate limits.** |

Every endpoint, `client_id`, `originator`, and inference URL is **configuration with a
documented default** (`cadence.config.Settings`, env-overridable via `CADENCE_*`) — never a
silent hardcode. The onboarding flow (worker m4-2) prints the gray-zone warning and
requires explicit confirmation before running the ChatGPT-OAuth login.

## The go-live gate — nothing fires automatically

Inference is **built and wired but OFF by default.** Three independent conditions must all
hold before a single token leaves the box:

1. **A configured, authenticated provider** — `llm_provider != "none"` and a credential in
   the NAS vault.
2. **The explicit opt-in flag** — `llm_enabled=True`. With `enabled=False` (the default),
   `LLMDeadlineInference.as_hook()` / `LLMReceptiveness.as_hook()` return an **inert no-op
   that never touches the provider** (zero LLM calls, zero egress). A test asserts this.
3. **Shadow until S0.2** — even enabled, the governor runs the receptiveness hook in
   **shadow mode** until the S0.2 calibration spike passes (it currently returns
   `insufficient_data`).

## Components

- `provider.py` — `LLMProvider` ABC (`complete(LLMRequest) -> LLMResponse`), the
  `LLMRequest`/`LLMResponse` Responses-API shapes, the shared **`TokenSet`** vault
  credential schema (co-owned with onboarding), and the **`EgressGuard`** that records
  **every** raw-content call into `RawEgressLog(EgressChannel.LLM_TEXT)` — a hash + byte
  length + provenance, never verbatim — from the base `complete()` so no provider can skip
  the audit.
- `openai_api.py` — the supported API-key transport.
- `chatgpt_oauth.py` — the gray-zone transport. Reads `TokenSet` from the vault;
  **proactive refresh** (near expiry) **+ refresh-on-401**, re-persisting rotated tokens;
  the actual `grant_type=refresh_token` call is an injected `refresh_callable` owned by the
  onboarding module. Handles `missing_codex_entitlement` with a clear error.
- `mock.py` — deterministic, network-free provider for tests (still egress-audited).
- `inference.py` — `LLMDeadlineInference` (→ `llm_hook`) and `LLMReceptiveness`
  (→ `receptiveness_hook`). Strict-JSON prompts over **non-verbatim** Event fields
  (`summary` + `structured`, never raw NAS evidence). **Malformed model output can never
  crash the pipeline** — bad JSON → `[]` (deadlines) / unchanged confidence
  (receptiveness), logged.

## Raw boundary & secrets

- The LLM is one of the **two** sanctioned raw-content egress channels (Decision E); every
  call is audited via `RawEgressLog`.
- Tokens and API keys live **only** in the NAS-only `FileCredentialVault` — never D1, never
  R2, never logs. The egress audit records a content hash and headers are never logged.
