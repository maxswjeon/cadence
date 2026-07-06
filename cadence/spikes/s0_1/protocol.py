"""Read-state safety probe protocol (Decision B / AC-2).

Invariant #1 (Principle 1 of the consensus plan) is that capture must **never** mutate
source state on the user's device — for accessibility-scraped messengers the one thing
that must never happen is the scrape silently marking a message/room "read" (or
triggering any other mutating side effect) on behalf of the user.

This module is the **protocol**, runnable without a device:

* :class:`MessengerApp` — the five apps gated individually (Claude M3: each has a
  distinct accessibility tree + read-state semantics, so passing KakaoTalk's probe
  does NOT imply LINE/KakaoWork/LINE Works/JANDI/Naver pass theirs).
* :data:`FORBIDDEN_ACTIONS` — the enumerated mutating-API assertion layer, per app.
* :class:`ControlAccountConfig` — the device/OS/app-version + control-account matrix.
* :class:`ControlObservation` — the two observation channels (control-account
  unread-indicator watch + outbound read-event capture).
* :class:`MessengerProbe` — the per-app state machine: OBSERVE_ONLY (default; import
  or notification-only, no accessibility scraping) -> PROBING -> SCRAPING_ENABLED, with
  **auto-disable-on-failure** back to a terminal ``DISABLED_SAFETY_TRIP`` the moment a
  forbidden action fires or a control observation shows an unexpected read-state
  change. Failure never silently falls back to a mutating path — it escalates by
  staying (or returning) at the safe, non-mutating state.

The on-device empirical run — actually walking this protocol against a real phone with
real KakaoTalk/LINE/etc. app versions — is **REQUIRED before Decision B graduates any
app past OBSERVE_ONLY** and is explicitly out of scope here (no device in this
environment). See :mod:`cadence.spikes.s0_1.simulate` for what *is* runnable now: the
state machine, the assertion layer, and a simulated/mock probe run against a fake
accessibility driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from cadence.obs.logging import get_logger, log_event


class MessengerApp(StrEnum):
    """The accessibility-scraped Korean messengers, each gated independently."""

    KAKAOTALK = "kakaotalk"
    LINE = "line"
    KAKAOWORK = "kakaowork"
    LINE_WORKS = "line_works"
    JANDI = "jandi"
    NAVER = "naver"  # Naver Band/Cafe


#: The enumerated forbidden mutating APIs, per app, asserted against on every
#: accessibility action taken during a scrape session. These are accessibility-node
#: action/click targets that are known (from each app's UI) to mark something read,
#: dismiss a notification, or otherwise mutate source state. This list is a *starting*
#: enumeration to probe against on-device — expanding it (never shrinking it silently)
#: is part of the re-probe cadence when an app updates its UI.
FORBIDDEN_ACTIONS: dict[MessengerApp, frozenset[str]] = {
    MessengerApp.KAKAOTALK: frozenset(
        {"open_chat_room", "mark_as_read", "clear_notification", "dismiss_unread_badge"}
    ),
    MessengerApp.LINE: frozenset(
        {"open_chat_room", "mark_as_read", "clear_notification", "send_read_receipt"}
    ),
    MessengerApp.KAKAOWORK: frozenset(
        {"open_channel", "mark_as_read", "clear_notification", "acknowledge_mention"}
    ),
    MessengerApp.LINE_WORKS: frozenset(
        {"open_channel", "mark_as_read", "clear_notification", "send_read_receipt"}
    ),
    MessengerApp.JANDI: frozenset({"open_topic", "mark_as_read", "clear_notification"}),
    MessengerApp.NAVER: frozenset({"open_post", "mark_as_read", "clear_notification"}),
}


class ForbiddenActionError(RuntimeError):
    """Raised when a scrape session attempts (or observes) a banned mutating action."""


@dataclass(frozen=True)
class ControlAccountConfig:
    """One entry of the device/OS/app-version + control-account matrix (per app).

    A real on-device run needs at least one row per (app, app_version) combination it
    intends to scrape: a **sender** control account (sends known messages on a known
    schedule) and a **recipient** control account (the account being scraped) whose
    unread state is independently observable — e.g. from a second device/session that
    never touches the scraped account.
    """

    app: MessengerApp
    device_os_version: str
    app_version: str
    sender_control_account: str
    recipient_control_account: str


@dataclass(frozen=True)
class ControlObservation:
    """One round of the two observation channels for a probe/scrape action.

    * ``recipient_unread_before`` / ``after`` — channel (a): the control recipient's
      unread indicator, watched independently of the scrape. It flipping to "read" as a
      side effect of scraping (rather than the recipient's own action) is a failure.
    * ``outbound_read_events_observed`` — channel (b): outbound network/read-event
      capture. Any non-zero count during a scrape of an unread-negative-control message
      is a failure (the scrape must never itself emit a read receipt/ack).
    * ``unread_negative_control`` — whether this observation targets a message that was
      deliberately left unread (the negative control) vs. one already read (baseline).
    """

    recipient_unread_before: bool
    recipient_unread_after: bool
    outbound_read_events_observed: int
    unread_negative_control: bool

    @property
    def is_safe(self) -> bool:
        """True iff nothing about this observation indicates a read-state mutation."""
        if self.outbound_read_events_observed > 0:
            return False
        went_from_unread_to_read = self.recipient_unread_before and not self.recipient_unread_after
        if self.unread_negative_control and went_from_unread_to_read:
            # The negative-control message was unread before and must still be unread
            # after a scrape that never opened/touched it on the recipient's behalf.
            return False
        return True


class ProbeState(StrEnum):
    """Per-app probe lifecycle. Only ``SCRAPING_ENABLED`` permits live accessibility
    scraping; every other state means "stay on notification/import-only capture."
    """

    OBSERVE_ONLY = "observe_only"
    PROBING = "probing"
    SCRAPING_ENABLED = "scraping_enabled"
    DISABLED_SAFETY_TRIP = "disabled_safety_trip"


@dataclass
class ProbeLogEntry:
    at: datetime
    from_state: ProbeState
    to_state: ProbeState
    reason: str


class MessengerProbe:
    """The auto-disable-on-failure state machine for one messenger app.

    Transitions (see module docstring): a failed probe or a safety trip **never**
    silently leaves the app on a scraping-capable state — it lands back on
    ``OBSERVE_ONLY`` (probe failure, retry later) or on the terminal
    ``DISABLED_SAFETY_TRIP`` (safety violation during/after scraping — requires a
    fresh, explicit re-probe to clear).
    """

    def __init__(self, app: MessengerApp, *, reprobe_interval_days: int = 14) -> None:
        self.app = app
        self.reprobe_interval_days = reprobe_interval_days
        self.state = ProbeState.OBSERVE_ONLY
        self.probed_app_version: str | None = None
        self.last_probed_at: datetime | None = None
        self.history: list[ProbeLogEntry] = []
        self._logger = get_logger("spikes.s0_1")

    def _transition(self, to_state: ProbeState, reason: str) -> None:
        entry = ProbeLogEntry(datetime.now(tz=UTC), self.state, to_state, reason)
        self.history.append(entry)
        log_event(
            self._logger,
            20,  # logging.INFO
            f"s0_1_probe:{self.app.value}:{entry.from_state.value}->{to_state.value}",
            app=self.app.value,
            from_state=entry.from_state.value,
            to_state=to_state.value,
            reason=reason,
        )
        self.state = to_state

    def start_probe(self, config: ControlAccountConfig) -> None:
        """Begin probing (only valid from ``OBSERVE_ONLY`` or after a stale re-probe)."""
        if config.app is not self.app:
            raise ValueError(f"config is for {config.app!r}, probe is for {self.app!r}")
        if self.state not in (ProbeState.OBSERVE_ONLY,):
            raise ValueError(f"cannot start a probe from state {self.state!r}")
        self._transition(ProbeState.PROBING, f"probe started ({config.app_version})")
        self.probed_app_version = config.app_version
        self.last_probed_at = datetime.now(tz=UTC)

    def record_observation(self, observation: ControlObservation) -> None:
        """Feed one observation round into the in-progress probe.

        An unsafe observation trips the probe immediately — even mid-probe, before it
        ever reached ``SCRAPING_ENABLED`` — because "escalate, never silently fall back
        to a mutating path" applies during probing too.
        """
        if self.state not in (ProbeState.PROBING, ProbeState.SCRAPING_ENABLED):
            raise ValueError(f"no active probe/scrape session in state {self.state!r}")
        if not observation.is_safe:
            reason = (
                f"unsafe observation: "
                f"outbound_read_events={observation.outbound_read_events_observed}, "
                f"unread_before={observation.recipient_unread_before}, "
                f"unread_after={observation.recipient_unread_after}"
            )
            self._transition(ProbeState.DISABLED_SAFETY_TRIP, reason)

    def assert_action_allowed(self, action: str) -> None:
        """Assert ``action`` is not a forbidden mutating API for this app.

        Raises :class:`ForbiddenActionError` **and** trips the probe to
        ``DISABLED_SAFETY_TRIP`` in the same call — the assertion layer and the
        auto-disable state machine are one mechanism, not two independent checks that
        could drift out of sync.
        """
        if action in FORBIDDEN_ACTIONS.get(self.app, frozenset()):
            if self.state is not ProbeState.DISABLED_SAFETY_TRIP:
                self._transition(
                    ProbeState.DISABLED_SAFETY_TRIP, f"forbidden action attempted: {action!r}"
                )
            raise ForbiddenActionError(
                f"{self.app.value}: action {action!r} is a forbidden mutating API"
            )

    def pass_probe(self) -> None:
        """Graduate PROBING -> SCRAPING_ENABLED. Only valid with a clean probe history."""
        if self.state is not ProbeState.PROBING:
            raise ValueError(f"cannot pass a probe from state {self.state!r}")
        self._transition(ProbeState.SCRAPING_ENABLED, f"probe passed ({self.probed_app_version})")

    def fail_probe(self, reason: str) -> None:
        """Probe failed cleanly (no safety trip, just didn't reach a passing bar).

        Lands back on ``OBSERVE_ONLY`` — the safe default — rather than any scraping
        state. This is the "escalate, don't silently fall back" path for an inconclusive
        or failing probe run.
        """
        if self.state is not ProbeState.PROBING:
            raise ValueError(f"cannot fail a probe from state {self.state!r}")
        self._transition(ProbeState.OBSERVE_ONLY, f"probe failed: {reason}")

    def due_for_reprobe(self, now: datetime, current_app_version: str) -> bool:
        """Whether a re-probe is due: app updated, or the cadence interval elapsed."""
        if self.state is not ProbeState.SCRAPING_ENABLED:
            return False
        if self.probed_app_version != current_app_version:
            return True
        if self.last_probed_at is None:
            return True
        return (now - self.last_probed_at).days >= self.reprobe_interval_days

    def force_reprobe(self, reason: str) -> None:
        """Pull a ``SCRAPING_ENABLED`` app back to ``PROBING`` for a scheduled re-probe.

        Scraping is suspended (not ``SCRAPING_ENABLED``) for the duration — an app
        update means the accessibility tree may have shifted, so the old pass is not
        trusted until re-confirmed.
        """
        if self.state is not ProbeState.SCRAPING_ENABLED:
            raise ValueError(f"cannot re-probe from state {self.state!r}")
        self._transition(ProbeState.PROBING, f"re-probe triggered: {reason}")


__all__ = [
    "MessengerApp",
    "FORBIDDEN_ACTIONS",
    "ForbiddenActionError",
    "ControlAccountConfig",
    "ControlObservation",
    "ProbeState",
    "ProbeLogEntry",
    "MessengerProbe",
]
