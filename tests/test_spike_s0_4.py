"""Tests for the S0.4 co-presence spike (`cadence/spikes/s0_4/`).

Two halves, matching the honest gating in the spike itself:

* Place/fusion (RUNNABLE): the fusion scorer + ROC/AUC compute correctly on a
  synthetic labeled dataset.
* Speaker-ID scaffold (GATED on real audio): the diarize -> embed -> match
  control flow and the participant-gate / single-subject-voiceprint structure
  behave correctly with stubbed embeddings. No real audio is used or claimed.
"""

from __future__ import annotations

import pytest

from cadence.spikes.s0_4.fusion import (
    BluetoothSignal,
    CalendarSignal,
    CoPresenceSignals,
    FusionWeights,
    GeofenceSignal,
    bluetooth_confidence,
    calendar_confidence,
    compute_roc,
    fuse_signals,
    generate_synthetic_dataset,
    geofence_confidence,
)
from cadence.spikes.s0_4.speaker_id import (
    CoPresenceSpeakerPipeline,
    ParticipantGate,
    ParticipantGateClosed,
    ResemblyzerEmbeddingExtractor,
    SpeakerIdMatcher,
    StubDiarizer,
    StubEmbeddingExtractor,
    SyntheticAudioClip,
    Voiceprint,
    VoiceprintStore,
    cosine_similarity,
    seeded_unit_vector,
)

# --------------------------------------------------------------------------- #
# Place/fusion (RUNNABLE)
# --------------------------------------------------------------------------- #


def test_geofence_confidence_bounds():
    assert geofence_confidence(GeofenceSignal(same_place=False)) == 0.0
    assert geofence_confidence(GeofenceSignal(same_place=True, distance_m=0.0)) == 1.0
    mid = geofence_confidence(GeofenceSignal(same_place=True, distance_m=25.0), radius_m=50.0)
    assert 0.0 < mid < 1.0


def test_calendar_confidence_bounds():
    assert calendar_confidence(CalendarSignal(is_attendee=False, overlap_frac=1.0)) == 0.0
    assert calendar_confidence(CalendarSignal(is_attendee=True, overlap_frac=0.75)) == 0.75


def test_bluetooth_confidence_bounds():
    assert bluetooth_confidence(BluetoothSignal(rssi_dbm=None)) == 0.0
    assert bluetooth_confidence(BluetoothSignal(rssi_dbm=-50.0)) == 1.0
    assert bluetooth_confidence(BluetoothSignal(rssi_dbm=-95.0)) == 0.0
    mid = bluetooth_confidence(BluetoothSignal(rssi_dbm=-75.0))
    assert 0.0 < mid < 1.0


def test_fuse_signals_strong_evidence_scores_high():
    strong = CoPresenceSignals(
        geofence=GeofenceSignal(same_place=True, distance_m=1.0),
        calendar=CalendarSignal(is_attendee=True, overlap_frac=1.0),
        bluetooth=BluetoothSignal(rssi_dbm=-45.0),
    )
    assert fuse_signals(strong) > 0.9


def test_fuse_signals_absent_evidence_scores_low():
    absent = CoPresenceSignals(
        geofence=GeofenceSignal(same_place=False),
        calendar=CalendarSignal(is_attendee=False),
        bluetooth=BluetoothSignal(rssi_dbm=None),
    )
    assert fuse_signals(absent) < 0.1


def test_fuse_signals_one_strong_signal_dominates_weak_ones():
    """Log-odds pooling: one very strong signal should outweigh two absent ones,
    unlike a plain average which would drag the score toward 1/3."""
    mixed = CoPresenceSignals(
        geofence=GeofenceSignal(same_place=True, distance_m=0.0),
        calendar=CalendarSignal(is_attendee=False),
        bluetooth=BluetoothSignal(rssi_dbm=None),
    )
    assert fuse_signals(mixed, FusionWeights(geofence=3.0, calendar=1.0, bluetooth=1.0)) > 0.6


def test_generate_synthetic_dataset_balanced_and_reproducible():
    a = generate_synthetic_dataset(n=200, seed=42)
    b = generate_synthetic_dataset(n=200, seed=42)
    assert len(a) == 200
    assert a == b  # deterministic given the same seed
    assert sum(1 for s in a if s.co_present) == 100  # exactly half, per generator contract


def test_compute_roc_shape_and_auc_on_synthetic_data():
    samples = generate_synthetic_dataset(n=400, seed=7)
    roc = compute_roc(samples)

    assert roc.n_pos == 200
    assert roc.n_neg == 200
    assert roc.points[0] == (0.0, 0.0)
    assert roc.points[-1] == (1.0, 1.0)
    # fpr/tpr must each be monotonically non-decreasing along the sorted-by-score walk
    for (x0, y0), (x1, y1) in zip(roc.points, roc.points[1:], strict=False):
        assert x1 >= x0
        assert y1 >= y0

    # Real number on synthetic data: the fusion must beat random guessing (0.5) by a
    # wide margin, but the generator's deliberate label-noise overlap means it should
    # not be a trivial 1.0 either.
    assert 0.6 < roc.auc < 1.0


# --------------------------------------------------------------------------- #
# Speaker-ID scaffold (GATED on real audio — stubbed embeddings only)
# --------------------------------------------------------------------------- #


def test_participant_gate_closed_blocks_the_entire_pipeline():
    """Closed gate must refuse before diarization runs at all (capture-time gate,
    not a post-hoc discard)."""
    gate = ParticipantGate(owner_present=False)
    store = VoiceprintStore()
    pipeline = CoPresenceSpeakerPipeline(
        gate=gate,
        diarizer=StubDiarizer(),
        embedder=StubEmbeddingExtractor(),
        matcher=SpeakerIdMatcher(store),
    )
    with pytest.raises(ParticipantGateClosed):
        pipeline.run(SyntheticAudioClip(speaker_feature_seeds=(1, 2)))


def test_participant_gate_open_runs_pipeline_end_to_end():
    gate = ParticipantGate(owner_present=True)
    store = VoiceprintStore()
    pipeline = CoPresenceSpeakerPipeline(
        gate=gate,
        diarizer=StubDiarizer(),
        embedder=StubEmbeddingExtractor(),
        matcher=SpeakerIdMatcher(store),
    )
    results = pipeline.run(SyntheticAudioClip(speaker_feature_seeds=(1, 2, 3)))
    assert len(results) == 3
    assert {r.cluster_label for r in results} == {"spk_0", "spk_1", "spk_2"}


def test_voiceprint_store_matches_owner_seed_and_rejects_others():
    store = VoiceprintStore()
    owner_seed = 101
    store.enroll_owner(Voiceprint(label="owner", vector=seeded_unit_vector(owner_seed, 16)))
    matcher = SpeakerIdMatcher(store)

    audio = SyntheticAudioClip(speaker_feature_seeds=(owner_seed, 999))
    diarizer = StubDiarizer()
    embedder = StubEmbeddingExtractor()
    segments = diarizer.diarize(audio)

    owner_result = matcher.match(embedder.embed(audio, segments[0]))
    stranger_result = matcher.match(embedder.embed(audio, segments[1]))

    assert owner_result.identity == "owner"
    assert owner_result.matched_owner is True
    assert stranger_result.identity == "unknown_speaker"
    assert stranger_result.matched_owner is False


def test_speaker_id_matcher_unknown_speaker_when_nothing_enrolled():
    store = VoiceprintStore()  # nothing enrolled
    matcher = SpeakerIdMatcher(store)
    embedding = Voiceprint(label="spk_0", vector=seeded_unit_vector(5, 16))
    result = matcher.match(embedding)
    assert result.identity == "unknown_speaker"
    assert result.similarity is None


def test_voiceprint_store_has_no_third_party_enrollment_method():
    """Structural regression guard for Decision I: single-subject (owner-only)
    voiceprint store must not grow a way to enroll a non-owner voiceprint."""
    store = VoiceprintStore()
    assert not hasattr(store, "enroll_contact")
    assert not hasattr(store, "enroll_other")
    assert not hasattr(store, "enroll")


def test_cosine_similarity_identical_vectors_is_one():
    v = seeded_unit_vector(3, 8)
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_rejects_mismatched_length():
    with pytest.raises(ValueError, match="same length"):
        cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0))


def test_resemblyzer_backend_raises_when_unavailable():
    """resemblyzer is not a project dependency; the real backend must refuse to
    construct rather than silently no-op, mirroring cadence.obs.stt's stub pattern."""
    try:
        import resemblyzer  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="optional dependency"):
            ResemblyzerEmbeddingExtractor()
    else:
        pytest.skip("resemblyzer is installed in this environment; gating path not exercised")
