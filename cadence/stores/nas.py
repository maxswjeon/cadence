"""NAS raw-evidence blob store (local-directory STUB).

The NAS is the **only** place verbatim raw evidence lives (chat DBs, audio, raw
location, screenshots, OCR). It is content-addressed: :meth:`NASStore.put` returns a
:class:`BlobRef` whose ``id``/``hash`` is the opaque handle stored in D1 — D1 never
holds the bytes themselves. In M1 this is a local directory under ``settings.nas_dir``.

Raw bytes never leave here except via the two sanctioned egress channels
(see :mod:`cadence.obs.egress`); the NAS is never replicated to cloud.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cadence.config import Settings, get_settings


@dataclass(frozen=True)
class BlobRef:
    """An opaque, content-addressed reference to a stored blob."""

    id: str
    hash: str
    byte_len: int

    @property
    def uri(self) -> str:
        return f"nas://{self.id}"


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data`` (used as the content address)."""
    return hashlib.sha256(data).hexdigest()


class NASStore:
    """Content-addressed local blob store for raw evidence."""

    def __init__(self, settings: Settings | None = None, *, base_dir: Path | None = None) -> None:
        self._settings = settings or get_settings()
        self.base_dir = Path(base_dir) if base_dir is not None else self._settings.nas_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        # shard by first two hex chars to avoid huge flat dirs
        shard = self.base_dir / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        return shard / digest

    def put(self, data: bytes) -> BlobRef:
        """Store raw ``data`` and return its content-addressed :class:`BlobRef`."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        digest = sha256_hex(data)
        path = self._path_for(digest)
        if not path.exists():
            path.write_bytes(data)
        return BlobRef(id=digest, hash=digest, byte_len=len(data))

    def get(self, ref: BlobRef | str) -> bytes:
        """Retrieve raw bytes by :class:`BlobRef` or digest string."""
        digest = ref.id if isinstance(ref, BlobRef) else ref
        return self._path_for(digest).read_bytes()

    def exists(self, ref: BlobRef | str) -> bool:
        digest = ref.id if isinstance(ref, BlobRef) else ref
        return self._path_for(digest).exists()


__all__ = ["NASStore", "BlobRef", "sha256_hex"]
