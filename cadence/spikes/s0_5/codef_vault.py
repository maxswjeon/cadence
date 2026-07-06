"""CODEF credential-vault/revoke/backoff hooks (Decision H / ADR-H, AC-8).

CODEF holds the actual bank/government credentials server-side (see Decision F's
"CODEF split" — that is the *accepted* third-party exposure, separately threat-modeled).
What Cadence itself stores is only the **CODEF Connected-ID** that references those
credentials. This module does not reimplement a secret store: it wraps the existing
NAS-only :class:`~cadence.adapters.base.CredentialVault` (concretely
:class:`~cadence.adapters.vault.FileCredentialVault`) with CODEF-specific semantics —
register/revoke by account, and a lockout backoff so a string of failed CODEF calls
degrades the adapter (per-account) rather than hammering CODEF or silently retrying
forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cadence.adapters.base import CredentialVault

_PROVIDER = "codef"


class CODEFCredentialManager:
    """Register/fetch/revoke a CODEF Connected-ID via the NAS-only credential vault."""

    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault

    def register(self, account_ref: str, connected_id: str) -> None:
        """Store ``connected_id`` for ``account_ref`` (never the underlying bank creds —
        those live server-side at CODEF by construction)."""
        self._vault.store(_PROVIDER, account_ref, {"connected_id": connected_id})

    def connected_id(self, account_ref: str) -> str:
        return self._vault.get(_PROVIDER, account_ref)["connected_id"]

    def revoke(self, account_ref: str) -> None:
        self._vault.revoke(_PROVIDER, account_ref)

    def accounts(self) -> list[str]:
        return [ref for provider, ref in self._vault.list_accounts() if provider == _PROVIDER]


class CODEFLockedOut(RuntimeError):
    """Raised when a call is attempted against an account currently in backoff lockout."""


@dataclass
class _AccountBackoffState:
    consecutive_failures: int = 0
    locked_until: datetime | None = None


@dataclass
class CODEFBackoff:
    """Per-account exponential backoff + lockout after repeated CODEF call failures.

    Degrades the *adapter* for that one account (per the risk-mitigation row in the
    consensus plan) rather than the whole CODEF integration, and rather than retrying
    indefinitely against a provider that may itself be applying its own lockout.
    """

    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(hours=1)
    trip_after: int = 5
    _accounts: dict[str, _AccountBackoffState] = field(default_factory=dict)

    def _state(self, account_ref: str) -> _AccountBackoffState:
        return self._accounts.setdefault(account_ref, _AccountBackoffState())

    def is_locked(self, account_ref: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        state = self._state(account_ref)
        return state.locked_until is not None and now < state.locked_until

    def before_call(self, account_ref: str, now: datetime | None = None) -> None:
        """Raise :class:`CODEFLockedOut` if ``account_ref`` is currently backed off."""
        if self.is_locked(account_ref, now):
            state = self._state(account_ref)
            raise CODEFLockedOut(
                f"account {account_ref!r} is locked out until "
                f"{state.locked_until.isoformat() if state.locked_until else '?'} "
                f"({state.consecutive_failures} consecutive failures)"
            )

    def record_success(self, account_ref: str) -> None:
        """A successful call clears the failure streak and any lockout."""
        self._accounts[account_ref] = _AccountBackoffState()

    def record_failure(self, account_ref: str, now: datetime | None = None) -> timedelta:
        """Record a failed call; returns the delay now in effect (may trip a lockout)."""
        now = now or datetime.now(tz=UTC)
        state = self._state(account_ref)
        state.consecutive_failures += 1
        delay = min(self.base_delay * (2 ** (state.consecutive_failures - 1)), self.max_delay)
        if state.consecutive_failures >= self.trip_after:
            state.locked_until = now + delay
        return delay

    def failure_count(self, account_ref: str) -> int:
        return self._state(account_ref).consecutive_failures


__all__ = ["CODEFCredentialManager", "CODEFLockedOut", "CODEFBackoff"]
