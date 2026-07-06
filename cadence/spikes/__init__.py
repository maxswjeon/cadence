"""Phase-0 feasibility spikes (`.omc/plans/cadence-phase0-spikes.md`).

Spike code de-risks decisions before the Milestone-1+ build; it is not itself part of
the production spine (`cadence.adapters`, `cadence.brain`, `cadence.ingest`,
`cadence.stores`) and nothing here is imported by it. Each spike gets its own
subpackage (``s0_0``, ``s0_1``, ..., ``s0_5``) plus a report under
``.omc/research/spikes/<id>.md``.
"""
