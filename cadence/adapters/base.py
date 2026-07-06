"""Source-adapter framework.

Defines the contract every source adapter (GitHub, Google Calendar, Gmail, …)
implements, plus the provenance-tagged :class:`Event` schema, the acquisition-tier
tagging enum, a per-provider :data:`registry`, and the NAS-only
:class:`CredentialVault`.

Adapter contract (implemented by the reference-adapter wave)
------------------------------------------------------------
An adapter is instantiated **per account** and declares its
:attr:`Adapter.acquisition_tier`. It implements three methods:

* ``fetch() -> Iterable[RawRecord]`` — pull raw records from the source. In tests this
  reads recorded fixtures; **no live network call** is required. Raw bytes/records are
  the adapter's own type (``RawRecord = Any``).
* ``normalize(raw) -> Event`` — convert **one** raw record into a provenance-tagged
  :class:`Event`. The adapter is responsible for putting any verbatim raw evidence in
  NAS and setting ``raw_evidence_ref``/``payload_hash`` — the Event that flows onward
  carries only structured fields, a non-verbatim summary, and provenance pointers.
* ``emit() -> Iterator[Event]`` — orchestrates ``fetch`` → ``normalize`` (default
  implementation provided; override only if streaming semantics differ).

Deadline-extractor hook point
-----------------------------
The deadline parser (built in a later wave) attaches at
:class:`cadence.brain.deadlines.DeadlineExtractor`. The ingestion pipeline calls the
configured extractor on each normalized Event to derive ``deadline`` rows; adapters do
**not** parse deadlines themselves — they only produce Events.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

RawRecord = Any


# --------------------------------------------------------------------------- #
# Acquisition tiers
# --------------------------------------------------------------------------- #


class AcquisitionTier(StrEnum):
    """How a source's data was acquired — drives trust/compliance handling downstream."""

    OFFICIAL_API = "official_api"        # first-party API with an OAuth/token grant
    OAUTH = "oauth"                      # OAuth-scoped read (e.g. Google Calendar)
    USER_TOKEN = "user_token"            # user-supplied token/session (e.g. Discord)
    FILE_IMPORT = "file_import"          # imported export/log file
    NOTIFICATION_WAL = "notification_wal"  # device notification WAL capture
    SCRAPE_NONROOT = "scrape_nonroot"    # non-root on-device scrape
    MANUAL = "manual"                    # manually entered
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Provenance-tagged Event schema
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(tz=UTC)


class Event(BaseModel):
    """A normalized, provenance-tagged source event.

    Events are the single currency flowing from adapters → ingestion → fact graph.
    The ``structured`` payload holds normalized fields destined for D1 rows and must
    be raw-boundary clean (it is enforced again at the D1 write). Verbatim raw content
    is **not** carried here — only ``raw_evidence_ref`` (NAS id/hash) + ``payload_hash``.
    """

    model_config = {"frozen": False, "extra": "forbid"}

    event_id: str = Field(description="Stable id of the source event (opaque).")
    source: str = Field(description="Provider name, e.g. 'github', 'google_calendar', 'gmail'.")
    account_ref: str = Field(description="Opaque per-account reference (never a credential).")
    acquisition_tier: AcquisitionTier = AcquisitionTier.UNKNOWN
    kind: str = Field(description="Event kind, e.g. 'github.issue', 'calendar.event'.")

    occurred_at: datetime | None = None
    ingested_at: datetime = Field(default_factory=_now)
    device_id: str | None = Field(default=None, description="Originating device (dedupe input).")
    dedupe_id: str | None = Field(default=None, description="Cross-device idempotency key.")

    payload_hash: str | None = Field(default=None, description="SHA-256 of the raw payload.")
    raw_evidence_ref: str | None = Field(default=None, description="NAS blob id/hash for raw.")
    summary: str | None = Field(default=None, description="Short NON-verbatim summary.")
    confidence: float | None = None

    structured: dict[str, Any] = Field(
        default_factory=dict, description="Normalized structured fields for D1."
    )

    def with_dedupe_id(self) -> Event:
        """Return a copy with ``dedupe_id`` filled from source+account+event if unset."""
        if self.dedupe_id:
            return self
        basis = f"{self.source}|{self.account_ref}|{self.event_id}".encode()
        self.dedupe_id = hashlib.sha256(basis).hexdigest()
        return self


# --------------------------------------------------------------------------- #
# Adapter ABC + registry
# --------------------------------------------------------------------------- #


class Adapter(ABC):
    """Base class for per-account source adapters.

    Subclasses set the class attributes :attr:`provider` and :attr:`acquisition_tier`
    and implement :meth:`fetch` and :meth:`normalize`. Instantiate one adapter per
    account (``account_ref``); the credential handle is resolved from the NAS-only
    :class:`CredentialVault`, never passed around in the clear beyond this object.
    """

    provider: str = "abstract"
    acquisition_tier: AcquisitionTier = AcquisitionTier.UNKNOWN

    def __init__(self, account_ref: str, *, vault: CredentialVault | None = None) -> None:
        self.account_ref = account_ref
        self._vault = vault

    # -- credential access (NAS-only vault) -------------------------------- #

    def credentials(self) -> dict[str, Any]:
        """Resolve this account's secret from the vault (NAS-only, audited)."""
        if self._vault is None:
            return {}
        return self._vault.get(self.provider, self.account_ref)

    # -- contract ----------------------------------------------------------- #

    @abstractmethod
    def fetch(self) -> Iterable[RawRecord]:
        """Pull raw records from the source (fixtures in tests; no live call required)."""

    @abstractmethod
    def normalize(self, raw: RawRecord) -> Event:
        """Convert one raw record into a provenance-tagged :class:`Event`."""

    def emit(self) -> Iterator[Event]:
        """Default orchestration: fetch → normalize → tag dedupe id → yield."""
        for raw in self.fetch():
            event = self.normalize(raw)
            if event.acquisition_tier is AcquisitionTier.UNKNOWN:
                event.acquisition_tier = self.acquisition_tier
            yield event.with_dedupe_id()


class AdapterRegistry:
    """Registry mapping provider name → :class:`Adapter` subclass; builds per-account instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[Adapter]] = {}

    def register(self, adapter_cls: type[Adapter]) -> type[Adapter]:
        """Register an adapter class (usable as a decorator)."""
        provider = adapter_cls.provider
        if provider in ("", "abstract"):
            raise ValueError(f"adapter {adapter_cls!r} must set a concrete 'provider'")
        self._adapters[provider] = adapter_cls
        return adapter_cls

    def get(self, provider: str) -> type[Adapter]:
        return self._adapters[provider]

    def create(
        self, provider: str, account_ref: str, *, vault: CredentialVault | None = None
    ) -> Adapter:
        """Instantiate a per-account adapter for ``provider``."""
        return self._adapters[provider](account_ref, vault=vault)

    def providers(self) -> list[str]:
        return sorted(self._adapters)


#: Process-wide adapter registry the reference adapters register into.
registry = AdapterRegistry()


# --------------------------------------------------------------------------- #
# Credential vault (NAS-only, encrypted-at-rest STUB, NEVER cloud)
# --------------------------------------------------------------------------- #


class CredentialVault(ABC):
    """Per-account secret store. **NAS-only** — implementations must never write to D1/R2.

    Secrets are per-account scoped and individually revocable. Every access fires the
    ``credential_vault_access`` alarm for auditability.
    """

    @abstractmethod
    def store(self, provider: str, account_ref: str, secret: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def get(self, provider: str, account_ref: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def revoke(self, provider: str, account_ref: str) -> None:
        ...

    @abstractmethod
    def list_accounts(self) -> list[tuple[str, str]]:
        ...


__all__ = [
    "AcquisitionTier",
    "Event",
    "Adapter",
    "AdapterRegistry",
    "registry",
    "CredentialVault",
    "RawRecord",
]
