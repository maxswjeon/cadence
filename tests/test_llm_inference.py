"""Inference-adapter tests: candidates, malformed-output safety, and the off-by-default gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.llm import LLMDeadlineInference, LLMReceptiveness, MockLLMProvider
from cadence.obs.egress import EgressChannel, get_egress_log


def _event(event_id="e1", **kw) -> Event:
    return Event(
        event_id=event_id,
        source="test",
        account_ref="acct",
        kind=kw.pop("kind", "email.message"),
        acquisition_tier=AcquisitionTier.MANUAL,
        occurred_at=kw.pop("occurred_at", datetime(2026, 7, 5, tzinfo=UTC)),
        **kw,
    )


_GOOD_JSON = (
    '{"deadlines": [{"due_at": "2026-07-15T17:00:00Z", "confidence": 0.7, '
    '"summary": "invoice due"}]}'
)


# -- happy path ------------------------------------------------------------- #


def test_inference_produces_llm_deadline_candidates() -> None:
    provider = MockLLMProvider({"invoice": _GOOD_JSON})
    infer = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True)
    hook = infer.as_hook()

    candidates = hook(_event(summary="Please pay the invoice soon"))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.origin == "inferred"
    assert c.confidence_type == "llm"
    assert c.confidence_value == 0.7
    assert c.due_at == datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    assert c.source_event_ids == ["e1"]


def test_inference_records_egress_per_call() -> None:
    provider = MockLLMProvider(default=_GOOD_JSON)
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    hook(_event(event_id="a"))
    hook(_event(event_id="b"))
    records = get_egress_log().records(EgressChannel.LLM_TEXT)
    assert len(records) == 2
    assert {r.source_event_ids for r in records} == {("a",), ("b",)}


def test_llm_candidate_flows_through_rule_extractor_reconciliation() -> None:
    # The hook plugs into RuleDeadlineExtractor and its candidates flow through the same
    # explicit-preference/divergence machinery — no special casing.
    provider = MockLLMProvider(default=_GOOD_JSON)
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    extractor = RuleDeadlineExtractor(llm_hook=hook)
    candidates = extractor.extract(_event(summary="pay the invoice"))
    assert any(c.confidence_type == "llm" for c in candidates)


# -- robustness: malformed output never crashes the pipeline ---------------- #


def test_malformed_json_yields_empty_list() -> None:
    provider = MockLLMProvider(default="this is not json at all {{{")
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    assert hook(_event()) == []


def test_json_missing_deadlines_key_yields_empty() -> None:
    provider = MockLLMProvider(default='{"something_else": 1}')
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    assert hook(_event()) == []


def test_json_fenced_in_markdown_is_still_parsed() -> None:
    provider = MockLLMProvider(default=f"```json\n{_GOOD_JSON}\n```")
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    assert len(hook(_event())) == 1


def test_item_with_unparseable_due_at_is_skipped() -> None:
    provider = MockLLMProvider(
        default='{"deadlines": [{"due_at": "not-a-date"}, {"due_at": "2026-08-01T00:00:00Z"}]}'
    )
    hook = LLMDeadlineInference(provider, model="gpt-5.5", enabled=True).as_hook()
    candidates = hook(_event())
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 8, 1, tzinfo=UTC)


def test_provider_error_is_swallowed_to_empty_list() -> None:
    class Boom(MockLLMProvider):
        def _send(self, request):  # noqa: ARG002
            raise RuntimeError("provider exploded")

    hook = LLMDeadlineInference(Boom(), model="gpt-5.5", enabled=True).as_hook()
    assert hook(_event()) == []  # logged, not raised


# -- the go-live gate: disabled by default => inert (zero LLM calls, zero egress) -- #


def test_disabled_hook_makes_zero_llm_calls_and_zero_egress() -> None:
    provider = MockLLMProvider(default=_GOOD_JSON)
    infer = LLMDeadlineInference(provider, model="gpt-5.5")  # enabled defaults to False
    assert infer.enabled is False
    hook = infer.as_hook()

    assert hook(_event()) == []
    assert provider.calls == []  # the provider was never touched
    assert get_egress_log().count(EgressChannel.LLM_TEXT) == 0


def test_default_extractor_wiring_is_inert() -> None:
    # RuleDeadlineExtractor with a disabled inference hook behaves exactly like no hook.
    provider = MockLLMProvider(default=_GOOD_JSON)
    hook = LLMDeadlineInference(provider, model="gpt-5.5").as_hook()
    extractor = RuleDeadlineExtractor(llm_hook=hook)
    assert extractor.extract(_event(summary="no dates here")) == []
    assert provider.calls == []
    assert get_egress_log().count(EgressChannel.LLM_TEXT) == 0


# -- receptiveness hook ----------------------------------------------------- #


@dataclass
class _Cand:
    confidence: float
    message_summary: str


def test_receptiveness_disabled_is_identity() -> None:
    provider = MockLLMProvider(default='{"receptiveness": 0.5}')
    hook = LLMReceptiveness(provider, model="gpt-5.5").as_hook()
    assert hook(_Cand(0.8, "nudge"), None) == 0.8
    assert provider.calls == []  # inert when disabled


def test_receptiveness_enabled_scales_confidence() -> None:
    provider = MockLLMProvider(default='{"receptiveness": 0.5}')
    hook = LLMReceptiveness(provider, model="gpt-5.5", enabled=True).as_hook()
    assert hook(_Cand(0.8, "nudge"), None) == 0.4
    assert get_egress_log().count(EgressChannel.LLM_TEXT) == 1


def test_receptiveness_malformed_falls_back_to_input_confidence() -> None:
    provider = MockLLMProvider(default="garbage")
    hook = LLMReceptiveness(provider, model="gpt-5.5", enabled=True).as_hook()
    assert hook(_Cand(0.8, "nudge"), None) == 0.8
