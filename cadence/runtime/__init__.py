"""Cadence runtime spine — the layer that actually *runs* the brain.

The engine, governor, ingest pipeline, and LLM providers are all libraries; nothing
in the lower layers drives a loop or pushes a nudge to a phone. This package is the
runnable assembly:

* :class:`~cadence.runtime.scheduler.TickScheduler` — the periodic driver that calls
  :meth:`~cadence.engine.engine.AttentionEngine.evaluate` and hands each **live** nudge
  to delivery. A raised exception in one tick is caught and logged; the loop never dies.
* :mod:`cadence.runtime.delivery` — the push transports (console/mock/FCM) and the
  non-verbatim :class:`~cadence.runtime.delivery.NudgeView` payload (title + body from
  the nudge summary, ``Thanks!``/``Dismiss`` action buttons, ``nudge_id``, category).
* :class:`~cadence.runtime.service.CadenceRuntime` — composes ``create_app`` (ingest)
  + a feedback route + the scheduler + delivery + optional (gated) LLM hooks into one
  service, driven by :class:`~cadence.runtime.service.RuntimeConfig`.

Honesty posture (see ``README.md``): delivery defaults to console; FCM push needs real
Firebase creds + a device token; only LIVE nudges are ever delivered (shadow proposals
are skipped); the LLM stays off unless explicitly enabled + onboarded.
"""

from __future__ import annotations

from cadence.runtime.scheduler import TickScheduler
from cadence.runtime.service import CadenceRuntime, RuntimeConfig, build_delivery

__all__ = ["CadenceRuntime", "RuntimeConfig", "TickScheduler", "build_delivery"]
