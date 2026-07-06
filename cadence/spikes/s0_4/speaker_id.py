"""Speaker-ID pipeline scaffold (S0.4b — interface + owner-only voiceprint, GATED).

Scaffolds the **office-Pi** diarization -> embedding -> voiceprint-match
interface from Decision I (see ``.omc/plans/cadence-consensus-plan.md``,
Decision I / S0.4), with its two load-bearing structural properties encoded
as code, not just prose:

1. **Participant gate as a hard precondition for CAPTURE, not retention.**
   :class:`ParticipantGate` models the BLE owner-presence hardware gate: the
   pipeline refuses to run at all (:class:`ParticipantGateClosed`) unless the
   owner is confirmed present. Discarding audio afterwards is not the control
   — refusing to run the pipeline before diarization even starts is (per
   ``.omc/research/korea-office-mic-legal-risk.md``: "discard/metadata-only
   does NOT cure interception").
2. **Single-subject (owner-only) voiceprint.** :class:`VoiceprintStore`
   deliberately exposes only ``enroll_owner``/``owner_voiceprint`` — there is
   no "enroll a third party" method. Every non-owner speaker is reported as
   ``"unknown_speaker"``, never matched or named, which keeps this pipeline
   out of the separate PIPA Sec.23 (biometric data) exposure that matching a
   non-consenting third party's voiceprint would create.

**Honest gating (do NOT read this module as an accuracy claim):** there is no
real audio, no microphone, and no diarization/embedding library running here.
:class:`StubDiarizer`/:class:`StubEmbeddingExtractor` operate on a small
synthetic per-"speaker" feature seed (:class:`SyntheticAudioClip`), purely so
the interface's control flow (gate -> diarize -> embed -> match) can be
exercised end to end without any audio dependency. Real speaker-ID accuracy —
far-field degradation, diarization DER, false-accept/-reject rates — is
**GATED on real audio** (a real office mic + the S0.5 compliance-controls
gate, per ``.omc/plans/cadence-phase0-spikes.md``) and is not measured here.
Real backends (Resemblyzer, sherpa-onnx) are OPTIONAL dependencies; see
:class:`ResemblyzerEmbeddingExtractor` below for the seam they plug into,
mirroring the interface-only-stub convention already used for STT in
``cadence.obs.stt.DagloSTTAdapter``.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class ParticipantGateClosed(RuntimeError):
    """Raised when the BLE owner-presence gate is closed: no capture happens.

    This is the "presence = participation" gate from Decision I. It must be
    checked *before* any diarization/embedding runs — a closed gate means the
    mic never should have produced audio in the first place, not that audio
    was captured and then discarded.
    """


@dataclass
class ParticipantGate:
    """BLE owner-presence hardware gate (Decision I). ``owner_present=False`` -> refuse to run."""

    owner_present: bool

    def check(self) -> None:
        if not self.owner_present:
            raise ParticipantGateClosed(
                "owner-presence BLE gate is closed; the office-Pi pipeline must not "
                "run (mic is hardware-off, not merely 'discard after capture')."
            )


# --------------------------------------------------------------------------- #
# Diarization / embedding / voiceprint contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpeakerSegment:
    """One diarized time segment with an anonymous, clip-local cluster label.

    ``cluster_label`` (e.g. ``"spk_0"``) is NOT an identity — diarization only
    clusters segments by voice similarity within one clip; identity resolution
    (owner vs unknown) is a separate step done by :class:`SpeakerIdMatcher`.
    """

    start_s: float
    end_s: float
    cluster_label: str


@dataclass(frozen=True)
class Voiceprint:
    """A speaker-embedding vector, either enrolled (the owner's) or extracted from a segment."""

    label: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class SpeakerIdResult:
    """Outcome of matching one diarized segment's embedding against the owner voiceprint."""

    cluster_label: str
    identity: str  # "owner" | "unknown_speaker"
    matched_owner: bool
    similarity: float | None


class Diarizer(ABC):
    """Contract: audio clip -> anonymous speaker segments (no identity resolution)."""

    @abstractmethod
    def diarize(self, audio: Any) -> list[SpeakerSegment]:
        """Return the diarized segments found in ``audio``."""


class EmbeddingExtractor(ABC):
    """Contract: (audio clip, segment) -> a speaker-embedding :class:`Voiceprint`."""

    @abstractmethod
    def embed(self, audio: Any, segment: SpeakerSegment) -> Voiceprint:
        """Extract a speaker embedding for one diarized segment."""


# --------------------------------------------------------------------------- #
# Owner-only voiceprint store (single-subject by construction)
# --------------------------------------------------------------------------- #


class VoiceprintStore:
    """Holds **at most one** enrolled voiceprint: the owner's.

    Deliberately has no "enroll a contact/third party" method. This is a
    structural guard, not just a policy note: there is nothing in this class's
    interface that could be called to build a non-owner voiceprint, which is
    what keeps the office-Pi pipeline out of the PIPA Sec.23 (third-party
    biometric data) exposure flagged in Decision I. Broader multi-contact
    voiceprint enrollment (for off-site co-presence attribution, with each
    contact's explicit consent) is a distinct, out-of-scope feature tracked
    under Decision D / the Phase-3 people graph — not this office-Pi scaffold.
    """

    def __init__(self) -> None:
        self._owner: Voiceprint | None = None

    def enroll_owner(self, voiceprint: Voiceprint) -> None:
        self._owner = voiceprint

    def owner_voiceprint(self) -> Voiceprint | None:
        return self._owner


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise ValueError("voiceprint vectors must be the same length")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SpeakerIdMatcher:
    """Matches a segment's embedding against the (sole) enrolled owner voiceprint.

    Never matches against anything else — there is no non-owner voiceprint to
    match against by construction of :class:`VoiceprintStore`. A non-owner
    speaker always comes back ``"unknown_speaker"``.
    """

    def __init__(self, store: VoiceprintStore, *, match_threshold: float = 0.75) -> None:
        self._store = store
        self._threshold = match_threshold

    def match(self, segment_embedding: Voiceprint) -> SpeakerIdResult:
        owner = self._store.owner_voiceprint()
        if owner is None:
            return SpeakerIdResult(
                cluster_label=segment_embedding.label,
                identity="unknown_speaker",
                matched_owner=False,
                similarity=None,
            )
        similarity = cosine_similarity(owner.vector, segment_embedding.vector)
        matched = similarity >= self._threshold
        return SpeakerIdResult(
            cluster_label=segment_embedding.label,
            identity="owner" if matched else "unknown_speaker",
            matched_owner=matched,
            similarity=similarity,
        )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class CoPresenceSpeakerPipeline:
    """Wires gate -> diarize -> embed -> match. The gate check runs first, always."""

    def __init__(
        self,
        gate: ParticipantGate,
        diarizer: Diarizer,
        embedder: EmbeddingExtractor,
        matcher: SpeakerIdMatcher,
    ) -> None:
        self._gate = gate
        self._diarizer = diarizer
        self._embedder = embedder
        self._matcher = matcher

    def run(self, audio: Any) -> list[SpeakerIdResult]:
        """Run the full pipeline on ``audio``; raises if the owner-presence gate is closed."""
        self._gate.check()
        results = []
        for segment in self._diarizer.diarize(audio):
            embedding = self._embedder.embed(audio, segment)
            results.append(self._matcher.match(embedding))
        return results


# --------------------------------------------------------------------------- #
# Stub backends (no audio dependency; deterministic synthetic embeddings)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SyntheticAudioClip:
    """Placeholder audio representation — NOT real audio.

    Real backends would consume actual waveform bytes; this spike environment
    has no microphone/audio, so the stub backends instead consume one integer
    "feature seed" per synthetic speaker, letting the pipeline's control flow
    be exercised deterministically without any audio library.
    """

    speaker_feature_seeds: tuple[int, ...]
    duration_s: float = 10.0


class StubDiarizer(Diarizer):
    """Deterministic fake diarizer: one equal-length segment per seed in the clip."""

    def diarize(self, audio: SyntheticAudioClip) -> list[SpeakerSegment]:
        n = len(audio.speaker_feature_seeds)
        if n == 0:
            return []
        seg_len = audio.duration_s / n
        return [
            SpeakerSegment(start_s=i * seg_len, end_s=(i + 1) * seg_len, cluster_label=f"spk_{i}")
            for i in range(n)
        ]


class StubEmbeddingExtractor(EmbeddingExtractor):
    """Deterministic fake embedding: a feature seed maps to a fixed pseudo-random unit vector.

    Same seed -> same vector (so re-embedding "the owner's" seed reliably
    cosine-matches an owner voiceprint enrolled from that same seed in tests);
    different seeds -> near-orthogonal vectors (so they do not spuriously match).
    """

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    def embed(self, audio: SyntheticAudioClip, segment: SpeakerSegment) -> Voiceprint:
        idx = int(segment.cluster_label.rsplit("_", 1)[-1])
        seed = audio.speaker_feature_seeds[idx]
        return Voiceprint(label=segment.cluster_label, vector=seeded_unit_vector(seed, self._dim))


def seeded_unit_vector(seed: int, dim: int) -> tuple[float, ...]:
    """A deterministic, seed-keyed pseudo-random unit vector (stand-in for a real embedding)."""
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return tuple(x / norm for x in raw)


# --------------------------------------------------------------------------- #
# Optional real backend (NOT installed/exercised here — shows the seam only)
# --------------------------------------------------------------------------- #


class ResemblyzerEmbeddingExtractor(EmbeddingExtractor):
    """Optional real backend wrapping Resemblyzer's speaker-embedding model.

    Mirrors the interface-only-stub convention of ``cadence.obs.stt.DagloSTTAdapter``:
    the class exists to document the exact seam a later milestone plugs a real
    embedding model into, but it is not exercised by this spike's tests (no
    real audio here) and refuses to run without the optional dependency.
    """

    def __init__(self) -> None:
        try:
            import resemblyzer  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "resemblyzer is an optional dependency and is not installed; the real "
                "embedding backend cannot run without it (and without real audio input)."
            ) from exc

    def embed(self, audio: Any, segment: SpeakerSegment) -> Voiceprint:
        raise NotImplementedError(
            "real audio embedding is GATED on real audio input (see module docstring); "
            "not implemented in this spike."
        )


__all__ = [
    "ParticipantGateClosed",
    "ParticipantGate",
    "SpeakerSegment",
    "Voiceprint",
    "SpeakerIdResult",
    "Diarizer",
    "EmbeddingExtractor",
    "VoiceprintStore",
    "cosine_similarity",
    "SpeakerIdMatcher",
    "CoPresenceSpeakerPipeline",
    "SyntheticAudioClip",
    "StubDiarizer",
    "StubEmbeddingExtractor",
    "seeded_unit_vector",
    "ResemblyzerEmbeddingExtractor",
]
