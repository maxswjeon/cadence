"""Tick scheduler — the periodic driver that actually runs the engine loop.

:class:`TickScheduler` calls :meth:`~cadence.engine.engine.AttentionEngine.evaluate`
every ``interval_seconds`` and hands each **live** nudge in the resulting tick to a
:class:`~cadence.runtime.delivery.NudgeDelivery`. It is a background thread with a stop
event, so it composes with the FastAPI service in one process.

Two guarantees:

* **Resilience.** A raised exception anywhere in a tick — the engine, a delivery — is
  caught and logged; the loop continues to the next interval. One bad tick never kills
  the driver. :meth:`tick_once` is the deterministic, exception-swallowing unit a test
  drives directly with an explicit ``now``.
* **Live-only delivery.** Only nudges with a persisted ``nudge_id`` (live mode) are
  delivered; shadow proposals are skipped here (and the delivery ABC guards it again).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from cadence.engine.engine import AttentionEngine, EngineTick
from cadence.obs.logging import get_logger, log_event
from cadence.runtime.delivery import NudgeDelivery
from cadence.stores.models import utcnow

_LOG = get_logger("runtime.scheduler")


class TickScheduler:
    """Periodic engine driver with resilient ticks and live-only delivery."""

    def __init__(
        self,
        engine: AttentionEngine,
        delivery: NudgeDelivery,
        *,
        interval_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
        self.engine = engine
        self.delivery = delivery
        self.interval_seconds = interval_seconds
        #: Source of ``now`` for the periodic loop (defaults to wall-clock UTC). Injecting
        #: it makes the loop's time deterministic; :meth:`tick_once` also takes an explicit
        #: ``now`` that overrides it.
        self.clock = clock or utcnow
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- deterministic single tick ----------------------------------------- #

    def tick_once(self, now: datetime | None = None) -> EngineTick | None:
        """Run exactly one tick and deliver its live nudges. Never raises.

        A failure in the engine returns ``None`` (logged); a failure delivering one nudge
        is logged and the remaining nudges are still attempted. This is what the periodic
        loop calls, and what tests drive with a fixed ``now``.
        """
        moment = now if now is not None else self.clock()
        try:
            tick = self.engine.evaluate(moment)
        except Exception as exc:  # noqa: BLE001 - a bad tick must never kill the loop
            log_event(
                _LOG, logging.ERROR, "tick_evaluate_failed",
                error=type(exc).__name__, now=moment.isoformat(),
            )
            return None

        for nudge in tick.nudges:
            if nudge.nudge_id is None:
                # Shadow proposal — not deliverable. (The delivery ABC also guards this.)
                continue
            try:
                self.delivery.deliver(nudge)
            except Exception as exc:  # noqa: BLE001 - one failed push can't drop the rest
                log_event(
                    _LOG, logging.ERROR, "tick_delivery_failed",
                    nudge_id=nudge.nudge_id, kind=nudge.kind, error=type(exc).__name__,
                )
        return tick

    # -- lifecycle ---------------------------------------------------------- #

    def _run(self) -> None:
        log_event(_LOG, logging.INFO, "scheduler_started", interval_seconds=self.interval_seconds)
        while not self._stop.is_set():
            self.tick_once(self.clock())
            # Interruptible sleep: ``stop()`` wakes it immediately.
            self._stop.wait(self.interval_seconds)
        log_event(_LOG, logging.INFO, "scheduler_stopped")

    def start(self) -> None:
        """Start the background tick loop (idempotent while already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cadence-tick", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the loop to stop and join the thread (idempotent).

        If the join times out (a tick is still in flight), the handle is **kept**, not
        dropped: nulling a still-running thread would let a later ``start()`` — which
        treats ``_thread is None`` as "not running" — spawn a *second* loop alongside the
        first. Keeping the live handle makes that ``start()`` a no-op until the tick
        actually finishes.
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            log_event(
                _LOG, logging.WARNING, "scheduler_stop_timeout",
                timeout_seconds=timeout,
            )
            return
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


__all__ = ["TickScheduler"]
