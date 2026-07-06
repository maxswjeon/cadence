"""Cloudflare R2 derived-blob store (local-directory STUB) + tiering router.

R2 holds **derived** blobs only (transcripts, cue-frames, summaries) — never raw
evidence. The distinction is by explicit **tier tag**, not content scanning: a
transcript is a derived artifact and legitimately lives in R2, whereas raw audio is
RAW and must stay in NAS. The :class:`TieringRouter` enforces the routing invariant:

    RAW            → NAS   (never cloud)
    DERIVED_BLOB   → R2
    STRUCTURED     → D1

The router does **not** trust the caller's tier tag for a cloud destination: every
non-NAS route passes through :meth:`TieringRouter.guard_cloud_target`, and an R2 write
must carry an explicit **derived-artifact marker** (``Artifact.derived``). A RAW artifact
directed at R2/D1, or an R2 write lacking the derived marker, is a raw-to-cloud
violation — the router fires the ``raw_to_cloud_violation`` alarm and raises
:class:`~cadence.stores.raw_boundary.RawBoundaryViolation`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cadence.config import Settings, get_settings
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.nas import BlobRef, NASStore
from cadence.stores.raw_boundary import RawBoundaryViolation


class Tier(StrEnum):
    """The storage tier an artifact belongs to."""

    RAW = "raw"
    DERIVED_BLOB = "derived_blob"
    STRUCTURED = "structured"


class R2Store:
    """Content-addressed local blob store for **derived** artifacts (R2 stub)."""

    def __init__(self, settings: Settings | None = None, *, base_dir: Path | None = None) -> None:
        self._settings = settings or get_settings()
        self.base_dir = Path(base_dir) if base_dir is not None else self._settings.r2_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        shard = self.base_dir / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        return shard / digest

    def put_derived(self, data: bytes) -> BlobRef:
        """Store a derived blob and return its content-addressed reference."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest)
        if not path.exists():
            path.write_bytes(data)
        return BlobRef(id=digest, hash=digest, byte_len=len(data))

    def get(self, ref: BlobRef | str) -> bytes:
        digest = ref.id if isinstance(ref, BlobRef) else ref
        return self._path_for(digest).read_bytes()

    def exists(self, ref: BlobRef | str) -> bool:
        digest = ref.id if isinstance(ref, BlobRef) else ref
        return self._path_for(digest).exists()


@dataclass
class Artifact:
    """A tier-tagged artifact to be routed to storage.

    ``derived`` is the explicit derived-artifact marker required for an R2 write; a raw
    producer never sets it, so a RAW blob mislabeled ``DERIVED_BLOB`` is still rejected.
    """

    tier: Tier
    data: bytes | None = None
    instance: object | None = None
    derived: bool = False


class TieringRouter:
    """Routes artifacts to the correct tier and enforces the raw-to-cloud invariant."""

    def __init__(
        self,
        *,
        nas: NASStore | None = None,
        r2: R2Store | None = None,
        d1: object | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.nas = nas or NASStore(settings)
        self.r2 = r2 or R2Store(settings)
        # D1 is optional here to avoid a hard import cycle; injected by the pipeline.
        self.d1 = d1

    def store_raw(self, data: bytes) -> BlobRef:
        """Store raw evidence in NAS (the only legal destination for RAW)."""
        return self.nas.put(data)

    def store_derived_blob(self, data: bytes, *, derived: bool = False) -> BlobRef:
        """Store a derived blob in R2. Requires the explicit derived-artifact marker.

        An R2 write lacking ``derived=True`` is treated as an untrusted (possibly raw)
        write: it fires the ``raw_to_cloud_violation`` alarm and is rejected.
        """
        if not derived:
            get_alarm_sink().fire(
                "raw_to_cloud_violation",
                {"tier": "R2", "reason": "R2 write lacking a derived-artifact marker"},
            )
            raise RawBoundaryViolation(
                field="<blob>",
                reason="R2 write requires an explicit derived-artifact marker",
                tier="R2",
            )
        return self.r2.put_derived(data)

    def route(self, artifact: Artifact) -> object:
        """Route ``artifact`` to its tier's store, blocking raw→cloud misroutes.

        Any non-NAS destination is guarded independently of the caller's tier tag.
        """
        if artifact.tier is Tier.RAW:
            if artifact.data is None:
                raise ValueError("RAW artifact requires bytes in .data")
            return self.store_raw(artifact.data)
        if artifact.tier is Tier.DERIVED_BLOB:
            # Cloud destination (R2): guard the tier + require the derived marker.
            self.guard_cloud_target(artifact.tier, "R2")
            if artifact.data is None:
                raise ValueError("DERIVED_BLOB artifact requires bytes in .data")
            return self.store_derived_blob(artifact.data, derived=artifact.derived)
        if artifact.tier is Tier.STRUCTURED:
            # Cloud destination (D1): guard the tier; D1Store enforces the raw boundary.
            self.guard_cloud_target(artifact.tier, "D1")
            if self.d1 is None:
                raise RuntimeError("no D1 store injected for STRUCTURED routing")
            return self.d1.write(artifact.instance)
        raise ValueError(f"unknown tier {artifact.tier!r}")

    def guard_cloud_target(self, tier: Tier, target: str) -> None:
        """Raise if a RAW artifact is directed at a cloud tier (R2/D1).

        Used by callers that decide a target independently of :meth:`route`.
        """
        if tier is Tier.RAW and target in ("R2", "D1"):
            get_alarm_sink().fire(
                "raw_to_cloud_violation",
                {"tier": target, "reason": "attempted to route RAW artifact to cloud tier"},
            )
            raise RawBoundaryViolation(
                field="<blob>",
                reason="RAW artifact may only be stored in NAS, never R2/D1",
                tier=target,
            )


__all__ = ["Tier", "R2Store", "Artifact", "TieringRouter"]
