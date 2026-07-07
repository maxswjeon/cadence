"""``python -m cadence.runtime`` — run the composed brain.

Reads the runtime knobs from the environment (:meth:`RuntimeConfig.from_env`), asks the
LLM factory for the (gated) inference hooks, composes :class:`CadenceRuntime`, starts the
tick scheduler, and serves the FastAPI app.

Serving HTTP needs an ASGI server (``uvicorn``); if it is not installed the scheduler
still runs and delivers nudges, and a clear log line explains how to enable the ingest +
feedback HTTP surface. Everything the loop does works without it — only the inbound HTTP
API needs the server.
"""

from __future__ import annotations

import logging

from cadence.config import get_settings
from cadence.obs.logging import get_logger, log_event
from cadence.runtime.service import CadenceRuntime, RuntimeConfig

_LOG = get_logger("runtime.main")


def _build_llm_hooks(settings):
    """Ask the LLM factory for the gated inference hooks (both ``None`` unless enabled)."""
    try:
        from cadence.llm.factory import build_deadline_llm_hook, build_receptiveness_hook
    except ImportError:
        log_event(_LOG, logging.INFO, "llm_factory_unavailable")
        return None, None
    from cadence.adapters.vault import FileCredentialVault

    vault = FileCredentialVault(settings)
    return (
        build_deadline_llm_hook(settings, vault),
        build_receptiveness_hook(settings, vault),
    )


def main() -> None:
    settings = get_settings()
    config = RuntimeConfig.from_env()
    deadline_hook, receptiveness_hook = _build_llm_hooks(settings)

    runtime = CadenceRuntime(
        config=config,
        settings=settings,
        deadline_llm_hook=deadline_hook,
        receptiveness_hook=receptiveness_hook,
    )
    log_event(
        _LOG, logging.INFO, "runtime_composed",
        governor_mode=config.governor_mode,
        interval_seconds=config.interval_seconds,
        delivery_provider=config.delivery_provider,
        llm_enabled=deadline_hook is not None or receptiveness_hook is not None,
        http_host=config.http_host,
        http_port=config.http_port,
    )
    runtime.start()
    try:
        _serve(runtime)
    finally:
        runtime.stop()


def _serve(runtime: CadenceRuntime) -> None:
    try:
        import uvicorn
    except ImportError:
        log_event(
            _LOG, logging.WARNING, "uvicorn_unavailable",
            hint="install uvicorn to serve the ingest + feedback HTTP API; "
            "the tick scheduler is running in the meantime",
        )
        runtime.scheduler._stop.wait()  # noqa: SLF001 - block until stop() is signalled
        return
    uvicorn.run(
        runtime.app,
        host=runtime.config.http_host,
        port=runtime.config.http_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
