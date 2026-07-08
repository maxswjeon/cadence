"""Real-transport tests for the Cloudflare D1 replica and R2 store.

No live Cloudflare: every HTTP call is intercepted by an :class:`httpx.MockTransport`
that records the request and returns a programmed response. Exercises the request
shape (URL + auth header + ``{sql, params}`` body / SigV4), retry-then-success on a
transient 5xx, the raw-boundary re-check still blocking a raw row before it is ever
sent, the queue-depth alarm, R2's derived-only gate, and the honest off-by-default
behavior (no creds → no network call).
"""

from __future__ import annotations

import json

import httpx
import pytest

from cadence.config import Settings
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.d1 import D1Store, ReplicationError
from cadence.stores.models import Fact, SourceAccount
from cadence.stores.r2 import Artifact, R2Store, Tier, TieringRouter
from cadence.stores.raw_boundary import RawBoundaryViolation

# --------------------------------------------------------------------------- #
# Settings helpers
# --------------------------------------------------------------------------- #


def _base_kwargs(tmp_path) -> dict:
    return {
        "data_dir": tmp_path / "data",
        "d1_path": tmp_path / "d1.sqlite",
        "nas_dir": tmp_path / "nas",
        "r2_dir": tmp_path / "r2",
        "vault_dir": tmp_path / "vault",
        "vault_master_key": "test-master-key",
        "require_mtls": False,
    }


def _d1_settings(tmp_path) -> Settings:
    """Settings with the Cloudflare D1 replica fully configured (real transport on)."""
    return Settings(
        **_base_kwargs(tmp_path),
        cloudflare_account_id="acct-123",
        cloudflare_d1_database_id="db-abc",
        cloudflare_api_token="cf-token-xyz",
        cloudflare_d1_url="https://fake-cf.example.com/client/v4",
        replication_backoff_base=0.0,  # no real sleeps in tests
    )


def _r2_settings(tmp_path) -> Settings:
    """Settings with R2 fully configured (real transport on)."""
    return Settings(
        **_base_kwargs(tmp_path),
        r2_bucket="derived-blobs",
        r2_access_key_id="AKIAFAKE",
        r2_secret_access_key="secret-fake",
        r2_endpoint="https://fake-r2.example.com",
    )


# --------------------------------------------------------------------------- #
# Cloudflare D1 replica — real HTTP transport
# --------------------------------------------------------------------------- #


async def test_d1_flush_posts_correct_url_auth_and_body(tmp_path) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": [], "success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = D1Store(_d1_settings(tmp_path), url="sqlite://", replica_client=client)
    store.init_schema()

    store.write(SourceAccount(provider="github", account_ref="octocat"))
    sent = await store.replica.flush()
    await client.aclose()

    assert sent == 1
    assert store.replica.queue_depth == 0
    req = captured[0]
    assert str(req.url) == (
        "https://fake-cf.example.com/client/v4/accounts/acct-123/d1/database/db-abc/query"
    )
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer cf-token-xyz"
    body = json.loads(req.content)
    assert body["sql"].startswith("INSERT INTO source_account (")
    assert body["sql"].count("?") == len(body["params"])
    assert "github" in body["params"]


async def test_d1_flush_retries_on_5xx_then_succeeds(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"errors": ["overloaded"]})
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = D1Store(_d1_settings(tmp_path), url="sqlite://", replica_client=client)
    store.init_schema()

    store.write(SourceAccount(provider="p", account_ref="a"))
    sent = await store.replica.flush()
    await client.aclose()

    assert calls["n"] == 2  # one 503, one retry that succeeded
    assert sent == 1
    assert store.replica.queue_depth == 0


async def test_d1_flush_persistent_5xx_raises_and_keeps_op_queued(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": ["down"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = D1Store(_d1_settings(tmp_path), url="sqlite://", replica_client=client)
    store.init_schema()

    store.write(SourceAccount(provider="p", account_ref="a"))
    with pytest.raises(ReplicationError):
        await store.replica.flush()
    await client.aclose()

    # The durable queue survives: the un-acked op stays for a later retry.
    assert store.replica.queue_depth == 1


async def test_d1_raw_boundary_blocks_row_and_never_sends(tmp_path) -> None:
    """A verbatim-carrying row is rejected before enqueue — no HTTP call is ever made."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = D1Store(_d1_settings(tmp_path), url="sqlite://", replica_client=client)
    store.init_schema()

    fact = Fact(kind="note", dedupe_key="k1", summary="acct 1002 3456 7890 12")
    with pytest.raises(RawBoundaryViolation):
        store.write(fact)

    # Nothing queued, nothing sent even after a flush.
    assert store.replica.queue_depth == 0
    sent = await store.replica.flush()
    await client.aclose()
    assert sent == 0
    assert captured == []
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1


async def test_d1_queue_depth_alarm_fires_past_threshold(tmp_path) -> None:
    settings = Settings(**_base_kwargs(tmp_path), replication_queue_alarm_depth=2)
    store = D1Store(settings, url="sqlite://")
    store.init_schema()

    store.write(SourceAccount(provider="p", account_ref="a0"))
    assert get_alarm_sink().count("replication_queue_depth") == 0
    store.write(SourceAccount(provider="p", account_ref="a1"))  # depth hits 2
    assert get_alarm_sink().count("replication_queue_depth") == 1


async def test_d1_off_by_default_makes_no_http_call(tmp_path) -> None:
    """With no Cloudflare creds, flush drains in memory and never touches the client."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(**_base_kwargs(tmp_path))  # no cloudflare_* set
    store = D1Store(settings, url="sqlite://", replica_client=client)
    store.init_schema()

    assert store.replica.configured is False
    store.write(SourceAccount(provider="p", account_ref="a"))
    sent = await store.replica.flush()
    await client.aclose()

    assert sent == 1  # drained in memory
    assert store.replica.queue_depth == 0
    assert captured == []  # honest default: no network call


# --------------------------------------------------------------------------- #
# Cloudflare R2 — real S3-compatible transport
# --------------------------------------------------------------------------- #


async def test_r2_put_uses_correct_path_and_sigv4_auth(tmp_path) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r2 = R2Store(_r2_settings(tmp_path), client=client)
    assert r2.configured is True

    data = b"a derived transcript blob"
    ref = r2.put_derived(data)
    r2.close()

    req = captured[0]
    assert req.method == "PUT"
    key = f"{ref.hash[:2]}/{ref.hash}"
    assert req.url.path == f"/derived-blobs/{key}"
    assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKE/")
    assert "x-amz-date" in req.headers
    assert "x-amz-content-sha256" in req.headers


async def test_r2_get_roundtrips_over_http(tmp_path) -> None:
    blob = b"derived summary bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=blob)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r2 = R2Store(_r2_settings(tmp_path), client=client)
    ref = r2.put_derived(blob)
    assert r2.get(ref) == blob
    r2.close()


def test_r2_router_refuses_non_derived_and_raw(tmp_path) -> None:
    """The derived-marker gate stays: a non-derived (or raw) R2 write is refused."""
    settings = Settings(**_base_kwargs(tmp_path))
    router = TieringRouter(settings=settings)

    with pytest.raises(RawBoundaryViolation):
        router.store_derived_blob(b"blob", derived=False)
    with pytest.raises(RawBoundaryViolation):
        router.route(Artifact(tier=Tier.DERIVED_BLOB, data=b"blob", derived=False))
    with pytest.raises(RawBoundaryViolation):
        router.guard_cloud_target(Tier.RAW, "R2")
    assert get_alarm_sink().count("raw_to_cloud_violation") == 3


def test_r2_off_by_default_makes_no_http_call(tmp_path) -> None:
    """With no R2 creds, put_derived writes locally and never touches the client."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(**_base_kwargs(tmp_path))  # no r2_* set
    r2 = R2Store(settings, client=client)
    assert r2.configured is False

    ref = r2.put_derived(b"local derived blob")
    r2.close()

    assert captured == []  # honest default: no network call
    assert r2.exists(ref)  # written to the local directory
    assert r2.get(ref) == b"local derived blob"
