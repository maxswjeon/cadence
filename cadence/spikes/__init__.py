"""Phase-0 feasibility spikes (`.omc/plans/cadence-phase0-spikes.md`).

Spike code de-risks decisions before the Milestone-1+ build; it is not itself part of
the production spine (`cadence.adapters`, `cadence.brain`, `cadence.ingest`,
`cadence.stores`) and nothing here is imported by it. Each spike gets its own
subpackage (``s0_0``, ``s0_1``, ..., ``s0_5``) plus a report under
``.omc/research/spikes/<id>.md``.

**One deliberate exception (M7/A3):** `cadence.engine.governor` imports
`cadence.spikes.s0_2.thresholds.evaluate_go_no_go` to wire the already-built S0.2
calibration gate into live nudge delivery (see
``.omc/plans/cadence-production-hardening-plan.md`` A3). S0.2 stops being a spike-only
artifact at that point; everything else in this package still holds to the rule above.
"""
