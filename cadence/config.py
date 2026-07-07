"""Environment-based configuration for Cadence.

All configuration is read from environment variables (prefix ``CADENCE_``) or an
optional ``.env`` file. Nothing here performs live network I/O; secrets such as
``DAGLO_API_KEY`` are placeholders in Milestone 1 (no live call is ever made).

The three storage tiers each get a local path in M1:

* ``d1_path``   — local-canonical SQLite file (the "D1").
* ``nas_dir``   — raw-evidence blob directory (NAS stub).
* ``r2_dir``    — derived-blob directory (Cloudflare R2 stub).
* ``vault_dir`` — credential vault directory (NAS-only, never cloud).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The insecure, checked-in vault master key. Only acceptable in dev/test — see
#: ``Settings.env`` and :class:`cadence.adapters.vault.FileCredentialVault`, which
#: refuses to start with this key when ``env == "prod"``.
DEFAULT_VAULT_MASTER_KEY = "cadence-dev-master-key-do-not-use-in-prod"


class Settings(BaseSettings):
    """Cadence runtime settings.

    Read from environment (prefix ``CADENCE_``). Example::

        CADENCE_D1_PATH=/var/cadence/d1.sqlite
        CADENCE_DAGLO_API_KEY=...        # placeholder in M1, unused
    """

    model_config = SettingsConfigDict(
        env_prefix="CADENCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage tiers (local paths in M1) ---------------------------------
    data_dir: Path = Field(
        default=Path("var"),
        description="Base directory for local runtime data.",
    )
    d1_path: Path = Field(
        default=Path("var/d1.sqlite"),
        description="Local-canonical SQLite file (the D1 hot path).",
    )
    nas_dir: Path = Field(
        default=Path("var/nas_blobs"),
        description="NAS raw-evidence blob dir (raw never leaves except sanctioned egress).",
    )
    r2_dir: Path = Field(
        default=Path("var/r2_blobs"),
        description="Cloudflare R2 derived-blob directory (stub); derived-only, never raw.",
    )
    vault_dir: Path = Field(
        default=Path("var/vault"),
        description="Credential-vault directory. NAS-only, encrypted-at-rest stub, NEVER cloud.",
    )
    nas_root: Path | None = Field(
        default=None,
        description=(
            "Trust-boundary root every local storage directory must resolve under. "
            "The credential vault positively validates against this (not just a "
            "blocklist) so it can never be pointed at a cloud-synced folder. Left "
            "unset, it defaults to the common ancestor of the other local storage "
            "directories (see _default_nas_root)."
        ),
    )

    # --- Cloudflare D1 replica (STUB — no real HTTP in M1) ------------------
    cloudflare_d1_url: str | None = Field(
        default=None,
        description="Cloudflare-D1 replica endpoint. Unused in M1; replica writes queue in memory.",
    )
    cloudflare_account_id: str | None = Field(default=None)
    cloudflare_api_token: str | None = Field(default=None)
    replication_queue_alarm_depth: int = Field(
        default=1000,
        description="Replication-queue depth that trips the replication_queue_depth alarm.",
    )

    # --- Deployment environment ---------------------------------------------
    env: str = Field(
        default="dev",
        description=(
            "Deployment environment ('dev'|'test'|'prod'). Gates fail-closed checks — "
            "e.g. the vault refuses to start with the default vault_master_key when "
            "this is 'prod'."
        ),
    )

    # --- Vault crypto (STUB) -----------------------------------------------
    vault_master_key: str = Field(
        default=DEFAULT_VAULT_MASTER_KEY,
        description="Master key for the vault stub. Replace with an OS/HW keystore in prod.",
    )

    # --- STT / audio (interface only in M1; no recording/capture) ----------
    daglo_api_key: str | None = Field(
        default=None,
        description="Daglo (daglo.ai) STT API key placeholder. Key TBD; NO live call in M1.",
    )
    daglo_endpoint: str = Field(
        default="https://apis.daglo.ai/stt/v1",
        description="Daglo STT endpoint (not contacted in M1).",
    )

    # --- mTLS gate (STUB — no real cert verification in M1) -----------------
    require_mtls: bool = Field(
        default=True,
        description=(
            "Fail-closed gate for the /ingest/event mTLS choke point (see "
            "cadence.brain.app.create_app). True (default) rejects any request "
            "missing the client-cert proof a TLS-terminating proxy would set; "
            "set False only for local dev/tests where mTLS is not terminated."
        ),
    )
    trust_loopback_ingest: bool = Field(
        default=True,
        description=(
            "Same-host pollers are trusted: when require_mtls is on and a request "
            "carries no X-Client-Cert header, it is allowed only if it originates "
            "from loopback (127.0.0.1 / ::1). A properly-configured mTLS proxy still "
            "forwards the client-cert header for remote devices, so remote callers "
            "keep going through the cert path and never benefit from this exemption. "
            "Set False to require the cert header even for same-host callers."
        ),
    )

    # --- LLM inference (M4; OFF by default, shadow-gated until S0.2) --------
    llm_provider: str = Field(
        default="none",
        description=(
            "Which LLM transport to use: 'none' (default, no inference), 'openai_api' "
            "(API key — the supported, pay-per-token default), or 'chatgpt_oauth' "
            "(opt-in, gray-zone, may break, message-count limits). See cadence/llm/README.md."
        ),
    )
    llm_model: str = Field(
        default="gpt-5.5",
        description="Model id sent in the Responses-API request body.",
    )
    llm_enabled: bool = Field(
        default=False,
        description=(
            "Go-live opt-in for the inference hooks. False (default) keeps llm_hook / "
            "receptiveness_hook inert (no LLM calls, no egress) even if a provider is "
            "configured. Enabling still runs the governor in shadow mode until S0.2 passes."
        ),
    )
    openai_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for the API-key Responses endpoint ('/responses' is appended).",
    )

    # --- ChatGPT-OAuth block (Codex defaults; all env-overridable) ----------
    chatgpt_oauth_client_id: str = Field(
        default="app_EMoamEEZ73f0CkXaXp7hrann",
        description="Public Codex OAuth client_id presented to the ChatGPT backend.",
    )
    chatgpt_oauth_issuer: str = Field(
        default="https://auth.openai.com",
        description="OAuth issuer (authorize/token/revoke live under here).",
    )
    chatgpt_oauth_scopes: str = Field(
        default="openid profile email offline_access api.connectors.read api.connectors.invoke",
        description="Space-separated OAuth scopes requested during onboarding.",
    )
    chatgpt_oauth_originator: str = Field(
        default="codex_cli_rs",
        description="'originator' header/param identifying the client to the ChatGPT backend.",
    )
    chatgpt_base: str = Field(
        default="https://chatgpt.com/backend-api/codex",
        description="ChatGPT Codex backend base URL ('/responses' is appended for inference).",
    )
    chatgpt_oauth_redirect_port: int = Field(
        default=1455,
        description="Loopback redirect port for the onboarding PKCE flow (fallback 1457).",
    )
    chatgpt_oauth_redirect_port_fallback: int = Field(
        default=1457,
        description="Fallback loopback port tried when chatgpt_oauth_redirect_port is taken.",
    )

    # --- Raw-boundary tuning -----------------------------------------------
    max_summary_len: int = Field(
        default=500,
        description="Max length of a non-verbatim summary field allowed into D1.",
    )
    max_structured_text_len: int = Field(
        default=280,
        description="Max free-text structured value length before it is treated as verbatim.",
    )

    def ensure_dirs(self) -> None:
        """Create the local storage directories if they do not yet exist."""
        for path in (self.data_dir, self.nas_dir, self.r2_dir, self.vault_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.d1_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def d1_sqlalchemy_url(self) -> str:
        """SQLAlchemy URL for the local-canonical SQLite store."""
        return f"sqlite:///{self.d1_path}"

    @model_validator(mode="after")
    def _default_nas_root(self) -> Settings:
        """Fill an unset ``nas_root`` from the shared ancestor of the *other* storage dirs.

        This is what makes the vault's containment check meaningful without extra
        configuration: in the default (and test) layout every non-vault storage tier
        lives under one local root, so the vault must resolve under it too. Deliberately
        excludes ``vault_dir`` from the candidates used to compute the boundary — if it
        were included, the derived root would always trivially contain it (the common
        ancestor of a set that includes X is always an ancestor-or-equal of X), which
        would make the containment check a no-op. Operators who explicitly set
        ``nas_root`` (e.g. to a real NAS mount) get that boundary enforced instead.
        """
        if self.nas_root is None:
            candidates = (self.data_dir, self.nas_dir, self.r2_dir, self.d1_path.parent)
            common = os.path.commonpath([str(p.resolve()) for p in candidates])
            self.nas_root = Path(common)
        return self

    @model_validator(mode="after")
    def _require_https_endpoints(self) -> Settings:
        """Reject a plaintext ``http://`` OAuth/inference endpoint (env-overridable → hostile).

        ``chatgpt_oauth_issuer``, ``chatgpt_base``, and ``openai_api_base`` carry the auth
        code / refresh token / API key. If an attacker (or a typo) sets one to
        ``http://…`` the secret would leave in cleartext, so we require ``https://`` —
        exempting loopback (``localhost`` / ``127.0.0.1`` / ``::1``) so the fake-server
        tests and local dev proxies still work.
        """
        for name in ("chatgpt_oauth_issuer", "chatgpt_base", "openai_api_base"):
            url = getattr(self, name)
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https":
                continue
            if parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
                continue
            raise ValueError(
                f"{name} must use https:// (got {url!r}); only loopback hosts may use "
                "http. A plaintext endpoint would exfiltrate the OAuth code / token."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
