"""Service assembly — composes the whole runnable brain in one process.

:class:`CadenceRuntime` wires together, from a :class:`RuntimeConfig`:

* the D1 store + :class:`~cadence.engine.governor.NudgeGovernor` (mode from config) +
  :class:`~cadence.engine.engine.AttentionEngine`,
* the FastAPI app from :func:`~cadence.brain.app.create_app` (ingest) **plus** a feedback
  route ``POST /nudge/{nudge_id}/feedback`` that calls
  :meth:`~cadence.engine.governor.NudgeGovernor.record_feedback` on the *same* governor,
* a :class:`~cadence.runtime.scheduler.TickScheduler` driving the engine into delivery,
* optional, already-gated LLM hooks — a ``deadline_llm_hook`` wired into the ingest
  pipeline's :class:`~cadence.brain.deadlines.RuleDeadlineExtractor` and a
  ``receptiveness_hook`` wired into the governor — each attached **only when non-None**.
* an optional ``deadline_calibration_source`` wired into the governor's S0.2 shadow→live
  gate (:meth:`~cadence.engine.governor.NudgeGovernor.deadline_go_no_go`) for
  deadline-derived nudges. ``None`` (default) means no data feed is wired — production D1
  has no gold-labeled deadline sample table yet, so the gate always reads
  ``"insufficient_data"`` and those nudges stay shadow even in ``live`` mode; wiring a real
  feed (a D1-backed shadow-event/gold-label source) is the remaining runtime step (see
  ``.omc/plans/cadence-production-hardening-plan.md`` A3).

The LLM hooks are produced by the ``cadence.llm.factory`` (built by a sibling task); this
module never enables inference on its own — it accepts whatever the factory hands it, which
is ``None`` unless the operator explicitly onboarded and enabled a provider.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from cadence.adapters.base import Event  # noqa: F401 - re-exported context for callers
from cadence.brain.app import build_device_verifier, create_app, enforce_mtls
from cadence.brain.deadlines import DeadlineCandidate, RuleDeadlineExtractor
from cadence.config import Settings, get_settings
from cadence.devices.verify import DeviceVerifier
from cadence.engine.engine import AttentionEngine
from cadence.engine.governor import NudgeGovernor, ProposedNudge  # noqa: F401
from cadence.ingest.pipeline import IngestPipeline
from cadence.obs.logging import get_logger
from cadence.runtime.delivery import NudgeDelivery
from cadence.runtime.delivery.console import ConsoleDelivery
from cadence.runtime.delivery.mock import MockDelivery
from cadence.runtime.poller import (
    CursorStore,
    PollerRuntimeConfig,
    SourcePoller,
    build_source_pollers,
)
from cadence.runtime.scheduler import TickScheduler
from cadence.spikes.s0_2.calibration import CalibrationReport
from cadence.stores.d1 import D1Store

_LOG = get_logger("runtime.service")

#: Type of the ingest-side LLM hook the factory produces (Event -> deadline candidates).
DeadlineLLMHook = Callable[[Event], list[DeadlineCandidate]]
#: Type of the governor-side receptiveness hook (candidate, snapshot -> refined confidence).
ReceptivenessHook = Callable[..., float]
#: Type of the governor-side S0.2 gate data source (-> the current calibration report, or
#: None if no sample is available yet).
DeadlineCalibrationSource = Callable[[], CalibrationReport | None]


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime knobs. Defaults are the honest, safe posture: shadow + console + LLM off."""

    governor_mode: str = "shadow"
    interval_seconds: float = 60.0
    delivery_provider: str = "console"  # "console" | "mock" | "fcm"
    fcm_project_id: str | None = None
    #: Comma-separated device tokens for the FCM provider (a runtime plug-in point).
    fcm_device_tokens: tuple[str, ...] = ()
    #: HTTP bind address. Defaults to loopback — a TLS-terminating proxy (which sets the
    #: mTLS client-cert header) is expected to face the network; set 0.0.0.0 to expose
    #: the ingest API directly (dev/LAN only).
    http_host: str = "127.0.0.1"
    #: HTTP listen port. Default 3245 (0x0CAD — "Cadence"); a distinctive, uncommon port
    #: that avoids the common 8000/8080 collisions and sits below the ephemeral range.
    http_port: int = 3245
    #: Live-poll accounts. Empty by default → no pollers start (honest OFF posture).
    poller: PollerRuntimeConfig = field(default_factory=PollerRuntimeConfig)

    @property
    def ingest_url(self) -> str:
        """The loopback ingest URL a same-host poller POSTs to (see the loopback exemption)."""
        return f"http://{self.http_host}:{self.http_port}/ingest/event"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Read the runtime knobs from ``CADENCE_*`` environment variables.

        Bad values raise a :class:`ValueError` that names the offending variable and shows
        the value, rather than a bare ``could not convert string to float`` from deep in
        the stack.
        """
        env = env if env is not None else os.environ
        tokens = env.get("CADENCE_FCM_DEVICE_TOKENS", "")
        return cls(
            governor_mode=_parse_governor_mode(env.get("CADENCE_GOVERNOR_MODE", "shadow")),
            interval_seconds=_parse_interval(env.get("CADENCE_TICK_INTERVAL_SECONDS", "60")),
            delivery_provider=env.get("CADENCE_DELIVERY_PROVIDER", "console"),
            fcm_project_id=env.get("CADENCE_FCM_PROJECT_ID") or None,
            fcm_device_tokens=tuple(t.strip() for t in tokens.split(",") if t.strip()),
            http_host=env.get("CADENCE_HTTP_HOST", "127.0.0.1"),
            http_port=_parse_port(env.get("CADENCE_HTTP_PORT", "3245")),
            poller=PollerRuntimeConfig.from_env(dict(env)),
        )


def _parse_interval(raw: str) -> float:
    """Parse ``CADENCE_TICK_INTERVAL_SECONDS`` into a positive float or raise clearly."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"CADENCE_TICK_INTERVAL_SECONDS must be a positive number, got {raw!r}"
        ) from None
    if value <= 0:
        raise ValueError(
            f"CADENCE_TICK_INTERVAL_SECONDS must be a positive number, got {raw!r}"
        )
    return value


def _parse_port(raw: str) -> int:
    """Parse ``CADENCE_HTTP_PORT`` into a valid TCP port (1-65535) or raise clearly."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"CADENCE_HTTP_PORT must be an integer 1-65535, got {raw!r}") from None
    if not 1 <= value <= 65535:
        raise ValueError(f"CADENCE_HTTP_PORT must be an integer 1-65535, got {raw!r}")
    return value


def _parse_governor_mode(raw: str) -> str:
    """Validate ``CADENCE_GOVERNOR_MODE`` is ``shadow`` or ``live`` or raise clearly."""
    if raw not in ("shadow", "live"):
        raise ValueError(
            f"CADENCE_GOVERNOR_MODE must be 'shadow' or 'live', got {raw!r}"
        )
    return raw


class FeedbackBody(BaseModel):
    """Body of ``POST /nudge/{nudge_id}/feedback``."""

    kind: Literal["thanks", "dismiss"]
    note: str | None = None


def build_delivery(
    config: RuntimeConfig,
    *,
    settings: Settings | None = None,
) -> NudgeDelivery:
    """Construct the configured delivery transport.

    ``console`` (default) and ``mock`` are dependency-free. ``fcm`` builds a real
    :class:`~cadence.runtime.delivery.fcm.FCMDelivery` whose OAuth token is minted from the
    service-account JSON in the NAS vault — a live plug-in point (needs the credential + a
    device token + the ``cryptography`` signer).
    """
    provider = config.delivery_provider
    if provider == "console":
        return ConsoleDelivery()
    if provider == "mock":
        return MockDelivery()
    if provider == "fcm":
        return _build_fcm_delivery(config, settings or get_settings())
    raise ValueError(f"unknown delivery provider {provider!r} (console|mock|fcm)")


def mutable_device_tokens(
    initial: Sequence[str],
) -> tuple[Callable[[], list[str]], Callable[[str], None]]:
    """A live device-token pool: a getter and a pruner over one mutable list.

    The getter is passed to :class:`FCMDelivery` as ``device_tokens`` (resolved each send)
    and the pruner as ``on_unregister``, so a token FCM reports ``UNREGISTERED`` is dropped
    from the pool and not re-sent on the next tick.
    """
    pool = list(initial)

    def get() -> list[str]:
        return list(pool)

    def prune(dead: str) -> None:
        try:
            pool.remove(dead)
        except ValueError:
            pass  # already pruned (e.g. two nudges hit the same dead token in one batch)

    return get, prune


def _build_fcm_delivery(config: RuntimeConfig, settings: Settings) -> NudgeDelivery:
    # Imported here so the FCM/vault dependency chain is only pulled in when actually used.
    from cadence.adapters.vault import FileCredentialVault
    from cadence.runtime.delivery.fcm import FCMDelivery, ServiceAccountTokenSource

    if not config.fcm_project_id:
        raise ValueError("delivery provider 'fcm' requires CADENCE_FCM_PROJECT_ID")
    vault = FileCredentialVault(settings)
    token_source = ServiceAccountTokenSource.from_vault(vault)
    get_tokens, prune_token = mutable_device_tokens(config.fcm_device_tokens)
    return FCMDelivery(
        project_id=config.fcm_project_id,
        device_tokens=get_tokens,
        access_token_provider=token_source,
        on_unregister=prune_token,
    )


class CadenceRuntime:
    """The composed, runnable brain: ingest + feedback API + scheduler + delivery."""

    def __init__(
        self,
        *,
        config: RuntimeConfig | None = None,
        settings: Settings | None = None,
        store: D1Store | None = None,
        delivery: NudgeDelivery | None = None,
        deadline_llm_hook: DeadlineLLMHook | None = None,
        receptiveness_hook: ReceptivenessHook | None = None,
        deadline_calibration_source: DeadlineCalibrationSource | None = None,
        pollers: Sequence[SourcePoller] | None = None,
        device_verifier: DeviceVerifier | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.settings = settings or get_settings()
        if store is None:
            self.settings.ensure_dirs()
            store = D1Store(self.settings)
            store.init_schema()
        self.store = store

        # Real mTLS cert verification for BOTH the ingest and feedback routes, over the
        # same D1. Built only when the gate is on (require_mtls); a pure-dev/test runtime
        # leaves it None and a present X-Client-Cert then fails closed (see enforce_mtls).
        if device_verifier is not None:
            self.verifier: DeviceVerifier | None = device_verifier
        elif self.settings.require_mtls:
            self.verifier = build_device_verifier(self.settings, store)
        else:
            self.verifier = None

        # One governor instance is shared by the engine (writes nudges) and the feedback
        # route (adjusts the very thresholds those nudges fired against).
        self.governor = NudgeGovernor(
            store,
            mode=self.config.governor_mode,
            receptiveness_hook=receptiveness_hook,
            deadline_calibration_source=deadline_calibration_source,
        )
        self.engine = AttentionEngine(store, self.governor)

        # Wire the LLM deadline hook only when the factory actually produced one.
        extractor = RuleDeadlineExtractor(llm_hook=deadline_llm_hook)
        self.pipeline = IngestPipeline(store, deadline_extractor=extractor)

        self.delivery = delivery or build_delivery(self.config, settings=self.settings)
        self.scheduler = TickScheduler(
            self.engine, self.delivery, interval_seconds=self.config.interval_seconds
        )
        self.app = self._build_app()

        # Live-polling sources: injected in tests, else built from config. Empty by
        # default — no configured accounts (or no vault creds) means no pollers start.
        if pollers is not None:
            self.pollers: list[SourcePoller] = list(pollers)
        else:
            cursor_store = CursorStore(self.settings.data_dir / "poller_cursors.json")
            self.pollers = build_source_pollers(
                self.config.poller,
                ingest_url=self.config.ingest_url,
                cursor_store=cursor_store,
                settings=self.settings,
            )
        self._poller_threads: list[threading.Thread] = []

    def _build_app(self) -> FastAPI:
        app = create_app(pipeline=self.pipeline, settings=self.settings, verifier=self.verifier)
        governor = self.governor
        settings = self.settings

        def require_mtls(request: Request) -> None:
            # Same fail-closed choke point as the ingest route, including the loopback
            # exemption for same-host pollers and the SAME DeviceVerifier (real cert
            # verification) — read off app.state so the lifespan-built one is shared.
            enforce_mtls(request, settings, app.state.verifier)

        @app.post("/nudge/{nudge_id}/feedback")
        def nudge_feedback(
            nudge_id: str,
            body: FeedbackBody,
            _: None = Depends(require_mtls),
        ) -> dict:
            try:
                governor.record_feedback(nudge_id, body.kind, body.note)
            except ValueError as exc:
                # record_feedback raises on an unknown nudge_id (kind is Literal-validated).
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"ok": True, "nudge_id": nudge_id, "kind": body.kind}

        return app

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        """Start the tick scheduler and one background thread per live poller.

        The HTTP app is served separately. Pollers are OFF unless accounts were
        configured (and their credentials exist), so this spawns nothing by default.
        """
        self.scheduler.start()
        self._poller_threads = []
        for poller in self.pollers:
            thread = threading.Thread(
                target=poller.run,
                name=f"cadence-poller-{poller.source.provider}",
                daemon=True,
            )
            thread.start()
            self._poller_threads.append(thread)

    def stop(self) -> None:
        for poller in self.pollers:
            poller.stop()
        self.scheduler.stop()


__all__ = [
    "CadenceRuntime",
    "FeedbackBody",
    "RuntimeConfig",
    "build_delivery",
    "mutable_device_tokens",
]
