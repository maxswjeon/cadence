"""Mock delivery — records deliveries in memory for tests.

Captures every delivered :class:`~cadence.runtime.delivery.NudgeView` so a test can assert
the exact payload (title/body, ``Thanks!``/``Dismiss`` actions, ``nudge_id``, category)
that would have gone to a phone. No network.
"""

from __future__ import annotations

from cadence.runtime.delivery import DeliveryResult, NudgeDelivery, NudgeView


class MockDelivery(NudgeDelivery):
    """Delivery that records each :class:`NudgeView` instead of sending it."""

    def __init__(self) -> None:
        #: Views handed to :meth:`_send`, in delivery order.
        self.delivered: list[NudgeView] = []

    def _send(self, view: NudgeView) -> DeliveryResult:
        self.delivered.append(view)
        return DeliveryResult(
            delivered=True, nudge_id=view.nudge_id, reason="sent", detail=view.to_payload()
        )

    @property
    def payloads(self) -> list[dict]:
        """The recorded deliveries as transport-neutral payload dicts."""
        return [v.to_payload() for v in self.delivered]


__all__ = ["MockDelivery"]
