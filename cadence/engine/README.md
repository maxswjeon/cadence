# Cadence Engine — Attention & Priority + Nudge Governor (M3)

The reasoning loop that turns captured D1 state into **attention-state + priority +
real-time nudges**. Pure logic over synthetic/real Facts/Tasks/Deadlines with an
**injected clock** — no devices, no live scheduler, no live LLM.

```
                 ┌─────────────────────────────────────────────────────────┐
   D1 state  ──▶ │ 1. AttentionDetector   recent device-activity Facts       │
 (facts,         │      → AttentionSnapshot(state, focus_target, dwell, conf) │
  tasks,         │ 2. PriorityEngine      open Tasks ⋈ Deadlines             │
  deadlines)     │      → PriorityView(ranked) + Misallocation finding        │
                 │ 3. NudgeGovernor       precision-first + recall floor      │
                 │      → nudge (live) / proposal (shadow), idempotent        │
                 └─────────────────────────────────────────────────────────┘
                                    ▲                         │
              AttentionEngine.evaluate(now) ── EngineTick ────┘  (the periodic tick)
```

## The loop (`AttentionEngine.evaluate(now) -> EngineTick`)

`AttentionEngine` is what a scheduler calls periodically. Each tick:

1. **Attention** (`attention.py`) — reads recent activity Facts (`device.app_usage`,
   `device.active_window`, `device.screen_context`, `browser*`) in a sliding window and
   classifies `IDLE` / `IMMERSED` / `SCATTERED` with a `focus_target`. The observation
   time is each Fact's `created_at`; only structured columns are read (raw boundary).
2. **Priority** (`priority.py`) — scores open Tasks joined with their Deadlines:
   *urgency* from time-to-`due_at` (half-life curve, overdue = max) and *importance*
   from `Task.priority` plus an explicit-deadline boost → a ranked `PriorityView`.
3. **Misallocation** (`priority.py`) — if IMMERSED on a low-priority item while a
   *materially higher-priority* item is *imminent*, emit a `Misallocation`. This is the
   canonical **"immersed in the 7-day refactor while the 1-day report is due"** signal
   (see `tests/test_engine_priority.py::test_canonical_7day_vs_1day_misallocation`).
4. **Governor** (`governor.py`) — decides whether each finding becomes a nudge, then
   runs the separate device-care rule path. Returns everything in an `EngineTick`.

Focus↔item matching is by token overlap between `focus_target` and a task's
title/summary (the "inferred task" the snapshot names). Give a task a title/summary
containing the app/context the user works it under.

### Correctness guards on the misallocation signal

The detector deliberately stays silent unless *switching would actually help*:

- **Priority-inversion guard.** It only fires when the neglected item is at least as
  *important* (`Task.priority`-derived) as the focused one. A blended-score gap driven
  purely by urgency (a low-priority item due in 2h) never pulls the user off
  higher-priority work — the engine must not degrade prioritization.
- **Genuine-immersion gate.** A single-target focus *below* `immersion_seconds` reports
  as IMMERSED but with confidence below any firing threshold, and the tick passes
  `min_focus_seconds=immersion_seconds` so a brief glance produces no finding at all.
- **Intra-run gap split.** Two same-target samples more than `max_intra_run_gap_seconds`
  apart are not one continuous run — dwell accumulates only from gap-capped intervals, so
  two glances minutes apart don't read as sustained immersion.
- **Overdue staleness.** An item overdue by more than `overdue_stale_hours` ages out of
  "imminent", so long-abandoned tasks stop generating perpetual daily nudges.
- **Explicit-over-inferred deadlines.** When a task has several deadlines, the governing
  one is the explicit non-diverging deadline (M1 convention), not merely the earliest.

## Nudge Governor

- **Precision-first + recall floor.** A per-category confidence threshold gates firing.
  The threshold can never exceed `GovernorConfig.recall_ceiling`, and any finding at/above
  `recall_floor_confidence` fires *unconditionally* — a very strong signal is never
  silently dropped. `recall_estimate() = fired / (fired + suppressed)` makes silence
  measurable; `thanks_rate()` is the precision proxy.
- **Idempotency.** One nudge per condition, deduped via `Nudge.idempotency_key`
  (hash of `{kind, focused_item, neglected_item, day}`), so re-evaluating a tick never
  double-fires. A concurrent-insert `IntegrityError` (SELECT-then-INSERT race) is treated
  as a duplicate, not a crash. Suppressions are deduped by condition-key too, so the same
  weak finding re-suppressed every tick can't drag `recall_estimate` down. `fired` /
  `suppressed` / `duplicates` are in-memory, per-process tallies.
- **Feedback.** `record_feedback(nudge_id, "thanks"|"dismiss", note?)` writes a
  `Feedback` row and moves the category threshold (Thanks lowers the bar, Dismiss raises
  it, clamped to the recall ceiling).
- **Provenance.** `Nudge` has no provenance columns, so each fired nudge links
  (`Nudge.fact_id`) to a structured `nudge` Fact carrying `source_event_ids` + confidence.
  Device-care nudges link to their originating `device.power` telemetry fact.
- **Device-care rule path.** `consider_device_care(now)` is a deterministic rule over
  `device.power` telemetry (battery ≤ threshold, not charging) — kept clearly separate
  from the inference path.

## Shadow → live graduation (ADR-A / A3)

`NudgeGovernor(d1, mode=...)` — **defaults to `mode="shadow"`** (S0.2's verdict is
`insufficient_data`; live nudges must never ship by default — callers opt in explicitly):

- `mode="shadow"` — findings become **proposals** in `governor.proposals`; **no** `Nudge`
  row is written (nothing is delivered). Use this to observe precision/recall before
  turning nudges on.
- `mode="live"` — a deliverable `Nudge` row is persisted (`delivered_at` set) and mirrored
  into `proposals`.

Graduation (shadow → live) is a deployment decision gated elsewhere (S0.2), not automated
here. Pass `mode="live"` explicitly once shadow metrics look good.

## Seams (deliberately not wired)

- **Scheduler.** `AttentionEngine.evaluate(now)` is the callable a periodic driver
  invokes; wiring the actual timer/cron is out of scope. Pass a `clock` to default `now`.
- **Ingest-triggered eval.** `AttentionEngine.on_ingest(now)` lets a high-signal ingest
  event force an immediate tick. **Off by default** (`evaluate_on_ingest=False`) — the
  periodic tick is the primary driver.
- **LLM / receptiveness.** `NudgeGovernor(receptiveness_hook=...)` is a documented seam
  (mirroring `RuleDeadlineExtractor.llm_hook`): a callable
  `(_Candidate, AttentionSnapshot|None) -> float` that can refine a candidate's confidence
  from a future receptiveness/LLM model. `None` (default) leaves rule confidence untouched;
  no live model is called in M3.

## Invariants

- Every derived row carries provenance (`source_event_ids` on attention/nudge Facts).
- The engine writes only structured/summary rows — the raw boundary holds end-to-end
  (`raw_to_cloud_violation` count stays 0; verified in the engine tests).
- All timestamps are UTC-aware; the clock is always injected, so ticks are deterministic.
