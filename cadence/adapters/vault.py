"""NAS-only credential vault (encrypted-at-rest STUB).

Concrete :class:`~cadence.adapters.base.CredentialVault` that stores per-account
secrets as encrypted files **on the local NAS directory only** — never D1, never R2,
never LLM egress. Secrets are per-account scoped and individually revocable; every
access fires the ``credential_vault_access`` alarm.

.. warning::
   The encryption here is a **stub** (a SHA-256 keystream XOR), adequate for M1's
   no-live-creds testing. Production must use a real KMS/HW keystore (age/sops or an
   OS keychain). It is intentionally *not* production-grade crypto.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any

from cadence.adapters.base import CredentialVault
from cadence.config import DEFAULT_VAULT_MASTER_KEY, Settings, get_settings
from cadence.obs.alarms import get_alarm_sink

# Belt-and-suspenders tripwire only — the authoritative check is
# _assert_within_nas_root below, which positively validates the vault dir resolves
# inside settings.nas_root rather than just blocklisting known cloud-tier substrings.
_FORBIDDEN_CLOUD_MARKERS = ("r2_blobs", "d1.sqlite")


def _keystream(key: bytes, salt: bytes, n: int) -> bytes:
    """Deterministic SHA-256 keystream (STUB cipher, not for production)."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        block = hashlib.sha256(key + salt + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:n])


class FileCredentialVault(CredentialVault):
    """File-backed, NAS-only credential vault (encrypted-at-rest stub)."""

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
        self._master_key = self._settings.vault_master_key.encode("utf-8")
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

    # -- crypto (stub) ------------------------------------------------------ #

    def _encrypt(self, plaintext: bytes) -> bytes:
        # Random per write — a deterministic (plaintext-derived) salt would make
        # identical secrets produce identical ciphertext, leaking equality.
        salt = os.urandom(16)
        ks = _keystream(self._master_key, salt, len(plaintext))
        ct = bytes(a ^ b for a, b in zip(plaintext, ks, strict=True))
        mac = hmac.new(self._master_key, salt + ct, hashlib.sha256).digest()
        return salt + mac + ct

    def _decrypt(self, blob: bytes) -> bytes:
        salt, mac, ct = blob[:16], blob[16:48], blob[48:]
        expected = hmac.new(self._master_key, salt + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("vault entry failed integrity check (wrong key or tampered)")
        ks = _keystream(self._master_key, salt, len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks, strict=True))

    # -- atomic file I/O ------------------------------------------------------ #

    def _atomic_write(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        """Write via temp-file + ``os.replace`` so a crash mid-write can't corrupt ``path``."""
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    # -- CredentialVault contract ------------------------------------------ #

    def store(self, provider: str, account_ref: str, secret: dict[str, Any]) -> None:
        get_alarm_sink().fire(
            "credential_vault_access",
            {"op": "store", "provider": provider, "account_ref": account_ref},
        )
        path = self._path(provider, account_ref)
        self._assert_not_cloud(path)
        with self._lock:
            self._atomic_write(path, self._encrypt(json.dumps(secret).encode("utf-8")))
            self._update_index(provider, account_ref, add=True)

    def get(self, provider: str, account_ref: str) -> dict[str, Any]:
        get_alarm_sink().fire(
            "credential_vault_access",
            {"op": "get", "provider": provider, "account_ref": account_ref},
        )
        path = self._path(provider, account_ref)
        if not path.exists():
            raise KeyError(f"no credential for {provider}/{account_ref}")
        return json.loads(self._decrypt(path.read_bytes()).decode("utf-8"))

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
