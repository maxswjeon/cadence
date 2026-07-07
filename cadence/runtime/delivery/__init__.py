"""Nudge delivery — the push transports and the non-verbatim payload they carry.

A :class:`NudgeDelivery` turns a governor :class:`~cadence.engine.governor.ProposedNudge`
into a phone notification. Two invariants live in the ABC so no transport can break them:

* **Only LIVE nudges are delivered.** A ``ProposedNudge`` from *shadow* mode has
  ``nudge_id is None`` — :meth:`NudgeDelivery.deliver` skips it (and logs) before any
  transport runs. Shadow proposals are never pushed.
* **No raw evidence in a payload.** The push :class:`NudgeView` is built only from the
  nudge's derived, non-verbatim ``message_summary`` + category + ids — the same raw
  boundary that governs D1/logs. ``source_event_ids`` and confidence never leave here.

The payload always carries the two feedback affordances the product promises — a
``Thanks!`` and a ``Dismiss`` action button, each tagged with the ``nudge_id`` so the
tap can call back to ``POST /nudge/{nudge_id}/feedback``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cadence.engine.governor import ProposedNudge
from cadence.obs.logging import get_logger, log_event

_LOG = get_logger("runtime.delivery")

#: Stable action ids — these map straight onto the ``kind`` the feedback route expects
#: (``POST /nudge/{nudge_id}/feedback {"kind": ...}``), so a tapped button round-trips
#: to :meth:`~cadence.engine.governor.NudgeGovernor.record_feedback` with no translation.
ACTION_THANKS = "thanks"
ACTION_DISMISS = "dismiss"

#: Short, non-verbatim per-category headings. The *body* is the nudge's derived summary;
#: the title is a fixed, content-free label so no evidence leaks into the notification
#: heading. Unknown categories fall back to a neutral product title.
_CATEGORY_TITLES = {
    "attention.misallocation": "A higher priority may be slipping",
    "device.care": "Your device needs a moment",
}
_DEFAULT_TITLE = "Cadence"


@dataclass(frozen=True)
class NudgeAction:
    """One notification action button (``Thanks!`` / ``Dismiss``)."""

    #: Feedback kind sent back to the governor when tapped.
    action: str
    #: Human-facing button label.
    title: str


@dataclass(frozen=True)
class NudgeView:
    """A push-ready, non-verbatim view of a live nudge.

    Everything here is derived (title label, summary body, category, ids) — deliberately
    no ``source_event_ids``, no confidence, no raw evidence. :meth:`to_payload` is the
    transport-neutral dict every delivery serializes from.
    """

    nudge_id: str
    category: str
    priority: int
    title: str
    body: str
    actions: tuple[NudgeAction, ...]

    def to_payload(self) -> dict:
        """Transport-neutral push payload (title/body + action buttons + callback ids)."""
        return {
            "nudge_id": self.nudge_id,
            "category": self.category,
            "priority": self.priority,
            "title": self.title,
            "body": self.body,
            "actions": [{"action": a.action, "title": a.title} for a in self.actions],
        }


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a :meth:`NudgeDelivery.deliver` call."""

    delivered: bool
    nudge_id: str | None
    #: ``"shadow"`` (skipped, not live), ``"sent"``, ``"unregistered"``, ``"error"``, …
    reason: str | None = None
    detail: dict = field(default_factory=dict)


def build_view(nudge: ProposedNudge) -> NudgeView:
    """Map a **live** ``ProposedNudge`` to its non-verbatim push :class:`NudgeView`.

    Raises ``ValueError`` on a shadow nudge (``nudge_id is None``): building a payload for
    an undeliverable proposal is a bug, and the ABC guards against it before we get here.
    """
    if nudge.nudge_id is None:
        raise ValueError("cannot build a delivery view for a shadow nudge (nudge_id is None)")
    title = _CATEGORY_TITLES.get(nudge.kind, _DEFAULT_TITLE)
    return NudgeView(
        nudge_id=nudge.nudge_id,
        category=nudge.kind,
        priority=nudge.priority,
        title=title,
        body=nudge.message_summary,
        actions=(
            NudgeAction(action=ACTION_THANKS, title="Thanks!"),
            NudgeAction(action=ACTION_DISMISS, title="Dismiss"),
        ),
    )


class NudgeDelivery(ABC):
    """Base push transport. Enforces the live-only + non-verbatim invariants.

    Subclasses implement :meth:`_send` (the actual transport over a :class:`NudgeView`).
    :meth:`deliver` is a template method: it drops shadow nudges up front, so a shadow
    proposal can never reach a transport regardless of how the caller drives it.
    """

    def deliver(self, nudge: ProposedNudge) -> DeliveryResult:
        """Deliver a **live** nudge; skip (and log) a shadow proposal."""
        if nudge.nudge_id is None:
            log_event(
                _LOG,
                logging.DEBUG,
                "nudge_delivery_skipped_shadow",
                kind=nudge.kind,
                idempotency_key=nudge.idempotency_key,
            )
            return DeliveryResult(delivered=False, nudge_id=None, reason="shadow")
        return self._send(build_view(nudge))

    @abstractmethod
    def _send(self, view: NudgeView) -> DeliveryResult:
        """Transmit ``view`` to the device(s) and return the result."""


__all__ = [
    "ACTION_DISMISS",
    "ACTION_THANKS",
    "DeliveryResult",
    "NudgeAction",
    "NudgeDelivery",
    "NudgeView",
    "build_view",
]
