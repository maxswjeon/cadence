"""Tests for the observability skeleton: alarms, egress log, STT stub, metrics."""

from __future__ import annotations

import pytest

from cadence.config import Settings
from cadence.obs.alarms import get_alarm_sink
from cadence.obs.egress import EgressChannel, get_egress_log
from cadence.obs.metrics import render_prometheus
from cadence.obs.stt import DagloSTTAdapter, LiveCallDisabled


def test_alarm_sink_records_and_counts() -> None:
    sink = get_alarm_sink()
    sink.fire("raw_to_cloud_violation", {"field": "x"})
    sink.fire("credential_vault_access", {"op": "get"})
    assert sink.count("raw_to_cloud_violation") == 1
    assert sink.count() == 2


def test_egress_log_records_both_channels() -> None:
    log = get_egress_log()
    log.record(EgressChannel.LLM_TEXT, content_hash="a" * 64, destination="llm", byte_len=10)
    log.record(EgressChannel.DAGLO_AUDIO, content_hash="b" * 64, destination="daglo", byte_len=20)
    assert log.count(EgressChannel.LLM_TEXT) == 1
    assert log.count(EgressChannel.DAGLO_AUDIO) == 1
    assert log.count() == 2


def test_egress_record_is_non_verbatim() -> None:
    log = get_egress_log()
    rec = log.record(EgressChannel.LLM_TEXT, content_hash="c" * 64, destination="llm")
    # Only a hash + metadata is retained, never the verbatim payload.
    assert not hasattr(rec, "text")
    assert rec.content_hash == "c" * 64


def test_daglo_stub_reads_key_but_makes_no_live_call() -> None:
    configured = DagloSTTAdapter(Settings(daglo_api_key="key-123"))
    assert configured.is_configured is True
    unconfigured = DagloSTTAdapter(Settings(daglo_api_key=None))
    assert unconfigured.is_configured is False

    # Attempting to transcribe records the (would-be) egress intent, then refuses.
    with pytest.raises(LiveCallDisabled):
        configured.transcribe("nas-audio-ref")
    assert get_egress_log().count(EgressChannel.DAGLO_AUDIO) == 1


def test_metrics_render_includes_known_series() -> None:
    get_alarm_sink().fire("replication_queue_depth", {"depth": 5})
    text = render_prometheus()
    assert 'cadence_alarm_total{name="replication_queue_depth"}' in text
    assert 'cadence_raw_egress_total{channel="llm_text"}' in text
