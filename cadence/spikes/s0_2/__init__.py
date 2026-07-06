"""S0.2 — inference calibration harness (backfills AC-3).

Given a labeled sample (from :mod:`cadence.spikes.s0_0`), :mod:`.calibration` computes
per-class precision/recall and a confidence-bucketed calibration curve for the
deadline/priority extractor-under-test (:class:`cadence.brain.deadlines.
RuleDeadlineExtractor`; the LLM path is a documented stub — no live LLM call here), and
:mod:`.thresholds` applies a shadow->live go/no-go promotion rule.

**Honest gating**: this harness computes real arithmetic on whatever sample it is
given. Run against the small synthetic S0.0 sample (see :mod:`.run`), it proves the
harness is correct end-to-end — it does *not* produce a real "the extractor is ready"
verdict, which needs a real captured signal log. See ``.omc/research/spikes/s0_2.md``.
"""
