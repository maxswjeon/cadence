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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
