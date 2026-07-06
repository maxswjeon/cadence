"""Provider-agnostic speech-to-text (STT) interface + Daglo adapter STUB.

This is the **only** audio-related scaffolding permitted in M1: an interface plus a
stub adapter. No recording/capture and **no live call** happen here. When the audio
pipeline is unlocked (post-S0.5), a real Daglo call would:

1. read raw audio from NAS by id/hash,
2. record a :attr:`~cadence.obs.egress.EgressChannel.DAGLO_AUDIO` egress event, then
3. POST to Daglo and return a transcript (which is a *derived* artifact → R2, not D1).

In M1 :meth:`DagloSTTAdapter.transcribe` refuses to run (raises
:class:`LiveCallDisabled`) so no bytes ever leave the machine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from cadence.config import Settings, get_settings
from cadence.obs.egress import EgressChannel, get_egress_log


class LiveCallDisabled(RuntimeError):
    """Raised when live STT is attempted while it is disabled (always, in M1)."""


@dataclass(frozen=True)
class Transcript:
    """A derived STT result. Verbatim text is a *derived* artifact bound for R2/NAS.

    ``text`` is populated only by a real provider; the M1 stub never returns it.
    """

    audio_ref: str
    language: str
    text: str
    provider: str
    confidence: float | None = None


class STTProvider(ABC):
    """Provider-agnostic STT contract. Implementations must be swappable."""

    name: str = "abstract"

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether the provider has the config (e.g. API key) it needs to run."""

    @abstractmethod
    def transcribe(self, audio_ref: str, *, language: str = "ko") -> Transcript:
        """Transcribe raw audio referenced by NAS id/hash. May record an egress event."""


class DagloSTTAdapter(STTProvider):
    """Daglo (daglo.ai) STT adapter — **STUB**. Reads ``DAGLO_API_KEY``; no live call."""

    name = "daglo"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.daglo_api_key)

    def transcribe(self, audio_ref: str, *, language: str = "ko") -> Transcript:
        """M1 stub: records the (would-be) egress intent, then refuses to call out.

        No network I/O happens. The egress ledger entry documents that, *were this
        live*, raw audio would leave via the sanctioned Daglo channel.
        """
        get_egress_log().record(
            EgressChannel.DAGLO_AUDIO,
            content_hash=audio_ref,
            destination=self._settings.daglo_endpoint,
            source_event_ids=(),
        )
        raise LiveCallDisabled(
            "Daglo STT is interface-only in M1 (audio capture/recording is excluded); "
            "no live call is made."
        )


__all__ = ["STTProvider", "DagloSTTAdapter", "Transcript", "LiveCallDisabled"]
