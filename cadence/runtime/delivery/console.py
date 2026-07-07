"""Console delivery — the dev-safe default transport.

Emits the (already non-verbatim) push payload as a single structured JSON log line and
returns success. No network, no credentials — the safe default so ``python -m
cadence.runtime`` runs out of the box before any Firebase / device-token setup exists.
"""

from __future__ import annotations

import logging

from cadence.obs.logging import get_logger, log_event
from cadence.runtime.delivery import DeliveryResult, NudgeDelivery, NudgeView

_LOG = get_logger("runtime.delivery.console")


class ConsoleDelivery(NudgeDelivery):
    """Log-only delivery (structured JSON to the ``cadence`` logger)."""

    def _send(self, view: NudgeView) -> DeliveryResult:
        payload = view.to_payload()
        log_event(_LOG, logging.INFO, "nudge_delivered_console", **payload)
        return DeliveryResult(delivered=True, nudge_id=view.nudge_id, reason="sent", detail=payload)


__all__ = ["ConsoleDelivery"]
