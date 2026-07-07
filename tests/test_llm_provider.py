"""Provider-core tests: TokenSet round-trip, Responses-API parsing, egress auditing."""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.llm import LLMRequest, MockLLMProvider, TokenSet
from cadence.llm.provider import extract_output_text
from cadence.obs.egress import EgressChannel, get_egress_log

# -- TokenSet (the shared vault credential schema) -------------------------- #


def test_tokenset_vault_roundtrip() -> None:
    ts = TokenSet(
        access_token="acc",
        refresh_token="ref",
        id_token="idt",
        account_id="acct-1",
        expires_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )
    assert TokenSet.from_vault_dict(ts.to_vault_dict()) == ts


def test_tokenset_from_vault_dict_naive_expiry_becomes_utc() -> None:
    ts = TokenSet.from_vault_dict(
        {"access_token": "a", "expires_at": "2026-07-10T00:00:00"}
    )
    assert ts.expires_at == datetime(2026, 7, 10, tzinfo=UTC)


def test_tokenset_from_vault_dict_missing_expiry_is_none() -> None:
    ts = TokenSet.from_vault_dict({"access_token": "a"})
    assert ts.expires_at is None
    assert ts.refresh_token is None


# -- Responses-API payload parsing ------------------------------------------ #


def test_extract_output_text_prefers_convenience_field() -> None:
    assert extract_output_text({"output_text": "hello"}) == "hello"


def test_extract_output_text_walks_output_array() -> None:
    payload = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": "a"},
                    {"type": "output_text", "text": "b"},
                ]
            }
        ]
    }
    assert extract_output_text(payload) == "ab"


def test_extract_output_text_tolerates_garbage() -> None:
    assert extract_output_text({"weird": 1}) == ""
    assert extract_output_text({"output": "not-a-list"}) == ""


# -- Egress auditing (base complete() records every call BEFORE it leaves) --- #


def test_mock_provider_records_egress_per_call() -> None:
    provider = MockLLMProvider({"ping": '{"ok": true}'})
    log = get_egress_log()
    assert log.count(EgressChannel.LLM_TEXT) == 0

    provider.complete(LLMRequest(model="m", input="ping", source_event_ids=("e1",)))
    provider.complete(LLMRequest(model="m", input="ping", source_event_ids=("e2",)))

    records = log.records(EgressChannel.LLM_TEXT)
    assert len(records) == 2
    assert records[0].channel is EgressChannel.LLM_TEXT
    assert records[0].destination == "mock"
    assert records[0].source_event_ids == ("e1",)
    assert records[0].byte_len > 0


def test_egress_record_is_a_hash_not_verbatim() -> None:
    provider = MockLLMProvider()
    secret = "SENSITIVE-RAW-CONTENT-42"
    provider.complete(LLMRequest(model="m", input=secret))
    rec = get_egress_log().records(EgressChannel.LLM_TEXT)[0]
    # The ledger stores a hash + metadata, never the verbatim payload.
    assert secret not in rec.content_hash
    assert len(rec.content_hash) == 64  # sha256 hex


def test_mock_provider_keyed_replies_and_call_capture() -> None:
    provider = MockLLMProvider({"weather": "sunny"}, default="unknown")
    assert provider.complete(LLMRequest(model="m", input="the weather today")).text == "sunny"
    assert provider.complete(LLMRequest(model="m", input="something else")).text == "unknown"
    assert [c.input for c in provider.calls] == ["the weather today", "something else"]
