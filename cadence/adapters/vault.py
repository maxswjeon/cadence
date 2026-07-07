"""NAS-only credential vault (authenticated-encryption at rest).

Concrete :class:`~cadence.adapters.base.CredentialVault` that stores per-account
secrets as encrypted files **on the local NAS directory only** — never D1, never R2,
never LLM egress. Secrets are per-account scoped and individually revocable; every
access fires the ``credential_vault_access`` alarm.

Records are sealed with **AES-256-GCM** (v2 envelope). The 32-byte AES key is derived
from ``settings.vault_master_key`` via **HKDF-SHA256**; every write uses a fresh random
96-bit nonce, and the record's slot ``(provider, account_ref)`` is bound as AAD so a
ciphertext copied into another slot fails to decrypt. Only the v2 envelope is accepted:
the historical v1 stub (an unauthenticated-slot SHA-256-keystream-XOR record) was
dev-only and is now **rejected** on read rather than migrated — reading it would reopen a
slot-relocation downgrade, and no real v1 vaults exist to migrate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cadence.adapters.base import CredentialVault
from cadence.config import DEFAULT_VAULT_MASTER_KEY, Settings, get_settings
from cadence.obs.alarms import get_alarm_sink

# Belt-and-suspenders tripwire only — the authoritative check is
# _assert_within_nas_root below, which positively validates the vault dir resolves
# inside settings.nas_root rather than just blocklisting known cloud-tier substrings.
_FORBIDDEN_CLOUD_MARKERS = ("r2_blobs", "d1.sqlite")

#: Providers whose secrets are *real, minted* live credentials (OAuth tokens / API keys)
#: rather than M1 placeholders. Persisting one of these under the publicly-known
#: DEFAULT_VAULT_MASTER_KEY is refused in **every** env (not just prod): a checked-in key
#: derives a publicly-known AES key, so a real ChatGPT-OAuth / API-key login must set
#: CADENCE_VAULT_MASTER_KEY first. (Placeholder providers stay allowed in dev.)
_LIVE_CRED_PROVIDERS = frozenset({"chatgpt_oauth", "openai_api"})

#: HKDF ``info`` (domain-separation label) for the v2 AES-256-GCM key.
_HKDF_INFO = b"cadence-vault-v2"

#: Current on-disk envelope version and AEAD identifier.
_ENVELOPE_VERSION = 2
_ENVELOPE_ALG = "AES-256-GCM"


class FileCredentialVault(CredentialVault):
    """File-backed, NAS-only credential vault (AES-256-GCM authenticated encryption)."""

    def __init__(self, settings: Settings | None = None, *, base_dir: Path | None = None) -> None:
        self._settings = settings or get_settings()
        is_default_key = self._settings.vault_master_key == DEFAULT_VAULT_MASTER_KEY
        if self._settings.env == "prod" and is_default_key:
            raise ValueError(
                "refusing to start: CADENCE_ENV=prod with the default vault_master_key; "
                "set CADENCE_VAULT_MASTER_KEY to a real secret before running in prod"
            )
        self.base_dir = Path(base_dir) if base_dir is not None else self._settings.vault_dir
        self._assert_within_nas_root(self.base_dir, self._settings.nas_root)
        self._assert_not_cloud(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)
        # The v2 AES key is an HKDF-derived subkey, so the raw master key is never used
        # directly as the cipher key.
        master_key = self._settings.vault_master_key.encode("utf-8")
        self._aead = AESGCM(self._derive_key(master_key))
        # Guards store/revoke read-modify-write of the sidecar index against
        # concurrent callers; cred/index files themselves are written atomically
        # (temp file + os.replace) so a torn write can never corrupt them.
        self._lock = threading.Lock()

    # -- guards ------------------------------------------------------------- #

    @staticmethod
    def _assert_within_nas_root(path: Path, nas_root: Path | None) -> None:
        """Positively enforce the vault stays inside the NAS trust boundary.

        Resolves both paths (following symlinks) so a vault dir that *is*, or sits
        behind, a symlink escaping ``nas_root`` (e.g. into a cloud-synced folder) is
        rejected rather than silently accepted.
        """
        if nas_root is None:
            return
        resolved = path.resolve()
        root = nas_root.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"credential vault path {path!r} resolves to {resolved}, which escapes "
                f"the NAS trust boundary {root!r}; the vault is NAS-only and must never "
                "be reachable via a cloud-synced or otherwise external location"
            )

    @staticmethod
    def _assert_not_cloud(path: Path) -> None:
        """Fail fast if the vault is ever pointed at a known cloud-tier location."""
        lowered = str(path).lower()
        for marker in _FORBIDDEN_CLOUD_MARKERS:
            if marker in lowered:
                raise ValueError(
                    f"credential vault path {path!r} resolves under a cloud tier "
                    f"('{marker}'); the vault is NAS-only and must never touch D1/R2"
                )

    def _slug(self, provider: str, account_ref: str) -> str:
        basis = f"{provider}|{account_ref}".encode()
        return hashlib.sha256(basis).hexdigest()

    def _path(self, provider: str, account_ref: str) -> Path:
        return self.base_dir / f"{self._slug(provider, account_ref)}.cred"

    # -- crypto (AES-256-GCM, v2 envelope) ---------------------------------- #

    @staticmethod
    def _derive_key(master_key: bytes) -> bytes:
        """Derive the 32-byte AES-256 key from the master key via HKDF-SHA256."""
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO)
        return hkdf.derive(master_key)

    @staticmethod
    def _aad(provider: str, account_ref: str) -> bytes:
        """Additional authenticated data binding a ciphertext to its slot.

        Uses an **injective** JSON encoding rather than ``f"{provider}:{account_ref}"``:
        a plain ``:`` join collides — ``("a", "b:c")`` and ``("a:b", "c")`` would map to
        the same AAD, letting a ciphertext be opened under a different slot. JSON escaping
        of the two elements makes the encoding unambiguous.
        """
        return json.dumps([provider, account_ref], separators=(",", ":")).encode("utf-8")

    def _encrypt(self, plaintext: bytes, provider: str, account_ref: str) -> bytes:
        """Seal ``plaintext`` into a versioned JSON envelope (bytes) for the given slot."""
        nonce = os.urandom(12)  # 96-bit random nonce, one per record write
        ct = self._aead.encrypt(nonce, plaintext, self._aad(provider, account_ref))
        envelope = {
            "v": _ENVELOPE_VERSION,
            "alg": _ENVELOPE_ALG,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
        return json.dumps(envelope).encode("utf-8")

    @staticmethod
    def _parse_v2_envelope(blob: bytes) -> dict[str, Any] | None:
        """Return the parsed v2 envelope, or ``None`` if ``blob`` is a legacy v1 record.

        Legacy v1 records are raw ``salt+mac+ct`` bytes, which almost never decode as
        the JSON object a v2 envelope is, so a failed/foreign parse means "treat as v1".
        """
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(obj, dict) and obj.get("v") == _ENVELOPE_VERSION:
            return obj
        return None

    def _decrypt_v2(self, envelope: dict[str, Any], provider: str, account_ref: str) -> bytes:
        """Open a v2 AES-256-GCM envelope; raise on tamper / wrong key / wrong slot."""
        try:
            nonce = base64.b64decode(envelope["nonce"])
            ct = base64.b64decode(envelope["ct"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("vault entry is malformed (corrupt envelope)") from exc
        try:
            return self._aead.decrypt(nonce, ct, self._aad(provider, account_ref))
        except InvalidTag as exc:
            raise ValueError(
                "vault entry failed integrity check (wrong key, tampered, or wrong slot)"
            ) from exc

    # -- atomic file I/O ------------------------------------------------------ #

    def _atomic_write(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        """Write via temp-file + ``os.replace`` so a crash mid-write can't corrupt ``path``.

        The temp file is created with its restrictive ``mode`` already applied (via
        ``os.open`` + ``O_CREAT|O_EXCL``) rather than written-then-chmod'd, so the secret
        never briefly exists at the process umask. ``O_EXCL`` also prevents clobbering a
        pre-existing temp file (the pid+tid suffix already makes collisions unlikely).
        """
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            os.unlink(tmp)
            raise
        os.replace(tmp, path)

    # -- CredentialVault contract ------------------------------------------ #

    def store(self, provider: str, account_ref: str, secret: dict[str, Any]) -> None:
        if (
            provider in _LIVE_CRED_PROVIDERS
            and self._settings.vault_master_key == DEFAULT_VAULT_MASTER_KEY
        ):
            raise ValueError(
                f"refusing to persist live '{provider}' credentials under the default "
                "vault_master_key: a checked-in key derives a publicly-known AES key. "
                "Set CADENCE_VAULT_MASTER_KEY to a real secret before authenticating."
            )
        get_alarm_sink().fire(
            "credential_vault_access",
            {"op": "store", "provider": provider, "account_ref": account_ref},
        )
        path = self._path(provider, account_ref)
        self._assert_not_cloud(path)
        with self._lock:
            self._atomic_write(
                path, self._encrypt(json.dumps(secret).encode("utf-8"), provider, account_ref)
            )
            self._update_index(provider, account_ref, add=True)

    def get(self, provider: str, account_ref: str) -> dict[str, Any]:
        get_alarm_sink().fire(
            "credential_vault_access",
            {"op": "get", "provider": provider, "account_ref": account_ref},
        )
        path = self._path(provider, account_ref)
        if not path.exists():
            raise KeyError(f"no credential for {provider}/{account_ref}")
        blob = path.read_bytes()
        envelope = self._parse_v2_envelope(blob)
        if envelope is None:
            # Not a v2 envelope: a legacy v1 stub (or otherwise foreign) record. The v1
            # stub authenticated only salt+ct, not the slot, so honoring it would let a
            # blob be relocated into another slot's file and returned as that slot's
            # secret. Reject rather than migrate — the v1 vault was dev-only.
            raise ValueError(
                "vault entry is not a v2 AES-256-GCM record; legacy/unrecognized formats "
                "are rejected (re-onboard the credential to store it under the new cipher)"
            )
        plaintext = self._decrypt_v2(envelope, provider, account_ref)
        return json.loads(plaintext.decode("utf-8"))

    def revoke(self, provider: str, account_ref: str) -> None:
        get_alarm_sink().fire(
            "credential_vault_access",
            {"op": "revoke", "provider": provider, "account_ref": account_ref},
        )
        path = self._path(provider, account_ref)
        with self._lock:
            path.unlink(missing_ok=True)
            self._update_index(provider, account_ref, add=False)

    def list_accounts(self) -> list[tuple[str, str]]:
        # Slugs are opaque; we track (provider, account_ref) in a sidecar index.
        index = self.base_dir / "index.json"
        if not index.exists():
            return []
        data = json.loads(index.read_text())
        return [(p, a) for p, a in data]

    def _update_index(self, provider: str, account_ref: str, add: bool) -> None:
        """Read-modify-write the sidecar index. Caller must hold ``self._lock``."""
        index = self.base_dir / "index.json"
        current = {
            tuple(x) for x in (json.loads(index.read_text()) if index.exists() else [])
        }
        if add:
            current.add((provider, account_ref))
        else:
            current.discard((provider, account_ref))
        self._atomic_write(index, json.dumps(sorted(current)).encode("utf-8"))


__all__ = ["FileCredentialVault"]
