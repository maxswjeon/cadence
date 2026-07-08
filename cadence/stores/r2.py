"""Cloudflare R2 derived-blob store + tiering router.

R2 holds **derived** blobs only (transcripts, cue-frames, summaries) — never raw
evidence. The distinction is by explicit **tier tag**, not content scanning: a
transcript is a derived artifact and legitimately lives in R2, whereas raw audio is
RAW and must stay in NAS.

:class:`R2Store` is **off by default**: unconfigured it writes to a local directory
(``settings.r2_dir``) and makes no network call; once an R2 bucket + access
key/secret + endpoint are configured it PUTs/GETs derived blobs over the
S3-compatible API (AWS SigV4) through an injectable :class:`httpx.Client`. Either way
it is derived-only — the DERIVED marker gate lives in the router, below.

The :class:`TieringRouter` enforces the routing invariant:

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
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from cadence.config import Settings, get_settings
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.nas import BlobRef, NASStore
from cadence.stores.raw_boundary import RawBoundaryViolation

#: R2's S3-compatible API always signs against this region.
R2_REGION = "auto"
R2_SERVICE = "s3"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class R2Error(RuntimeError):
    """An R2 object operation failed at the transport/HTTP layer."""


class Tier(StrEnum):
    """The storage tier an artifact belongs to."""

    RAW = "raw"
    DERIVED_BLOB = "derived_blob"
    STRUCTURED = "structured"


def sigv4_headers(
    *,
    method: str,
    url: str,
    payload: bytes,
    access_key: str,
    secret_key: str,
    now: datetime,
    region: str = R2_REGION,
    service: str = R2_SERVICE,
) -> dict[str, str]:
    """Compute AWS SigV4 auth headers for an R2 (S3-compatible) request.

    Returns the ``Authorization`` header plus the ``x-amz-*`` headers that were signed;
    the caller must send them verbatim. Only the small header set we actually use is
    signed (host + the two ``x-amz`` headers), which is sufficient for R2 object PUT/GET.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = quote(parsed.path or "/", safe="/~")
    canonical_qs = parsed.query  # our object keys carry no query string
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest() if payload else _EMPTY_SHA256

    signed = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_qs, canonical_headers, signed_headers, payload_hash]
    )

    algorithm = "AWS4-HMAC-SHA256"
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [algorithm, amzdate, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode(), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }


class R2Store:
    """Content-addressed blob store for **derived** artifacts.

    **Off by default:** with no bucket + access key/secret + endpoint configured
    (:attr:`configured`) this writes to ``settings.r2_dir`` locally and makes **no**
    network call. Once configured, blobs go to R2 over the S3-compatible API (SigV4)
    through an **injectable** :class:`httpx.Client` (tests pass a fake transport).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_dir: Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.base_dir = Path(base_dir) if base_dir is not None else self._settings.r2_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._bucket = self._settings.r2_bucket
        self._access_key = self._settings.r2_access_key_id
        self._secret_key = self._settings.r2_secret_access_key
        self._endpoint = self._resolve_endpoint()
        self._client = client
        self._owns_client = client is None

    def _resolve_endpoint(self) -> str | None:
        if self._settings.r2_endpoint:
            return self._settings.r2_endpoint.rstrip("/")
        if self._settings.cloudflare_account_id:
            return f"https://{self._settings.cloudflare_account_id}.r2.cloudflarestorage.com"
        return None

    @property
    def configured(self) -> bool:
        """True only when a real R2 bucket + credentials + endpoint are all present."""
        return bool(
            self._bucket and self._access_key and self._secret_key and self._endpoint
        )

    # -- local-directory path (stub / unconfigured) ------------------------- #

    def _path_for(self, digest: str) -> Path:
        shard = self.base_dir / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        return shard / digest

    # -- R2 object key + URL ------------------------------------------------ #

    @staticmethod
    def _key_for(digest: str) -> str:
        # Content-addressed key, sharded like the local layout for parity.
        return f"{digest[:2]}/{digest}"

    def _object_url(self, digest: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{self._key_for(digest)}"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _signed_request(self, method: str, digest: str, payload: bytes) -> httpx.Response:
        url = self._object_url(digest)
        headers = sigv4_headers(
            method=method,
            url=url,
            payload=payload,
            access_key=self._access_key,  # type: ignore[arg-type]
            secret_key=self._secret_key,  # type: ignore[arg-type]
            now=datetime.now(tz=UTC),
        )
        try:
            return self._http().request(method, url, content=payload or None, headers=headers)
        except httpx.HTTPError as exc:
            raise R2Error(f"R2 {method} failed: {exc}") from exc

    def put_derived(self, data: bytes) -> BlobRef:
        """Store a derived blob and return its content-addressed reference."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if not self.configured:
            path = self._path_for(digest)
            if not path.exists():
                path.write_bytes(data)
            return BlobRef(id=digest, hash=digest, byte_len=len(data))
        resp = self._signed_request("PUT", digest, data)
        if resp.status_code >= 300:
            raise R2Error(f"R2 PUT rejected: {resp.status_code}")
        return BlobRef(id=digest, hash=digest, byte_len=len(data))

    def get(self, ref: BlobRef | str) -> bytes:
        digest = ref.id if isinstance(ref, BlobRef) else ref
        if not self.configured:
            return self._path_for(digest).read_bytes()
        resp = self._signed_request("GET", digest, b"")
        if resp.status_code >= 300:
            raise R2Error(f"R2 GET rejected: {resp.status_code}")
        return resp.content

    def exists(self, ref: BlobRef | str) -> bool:
        digest = ref.id if isinstance(ref, BlobRef) else ref
        if not self.configured:
            return self._path_for(digest).exists()
        resp = self._signed_request("HEAD", digest, b"")
        return resp.status_code < 300

    def close(self) -> None:
        """Close the HTTP client if this store created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


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


__all__ = ["Tier", "R2Store", "R2Error", "Artifact", "TieringRouter", "sigv4_headers"]
