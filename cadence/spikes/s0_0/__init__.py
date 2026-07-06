"""S0.0 — capture harness + bootstrap labeling (enabler spike).

Accumulates :class:`~cadence.adapters.base.Event` objects (from the existing
GitHub/Google Calendar/Email adapters + their fixtures, or synthetic Events) into a
time-ordered :class:`~cadence.spikes.s0_0.harness.SignalLog`, and provides a **one-time
bootstrap** gold-labeling schema/helper (:mod:`cadence.spikes.s0_0.labeling`) so a
labeled sample can be built for the S0.2 calibration harness.

This is bootstrap plumbing, not ongoing task maintenance: it produces a static labeled
sample once, the same way a human would hand-review a signal log once to seed
calibration — it does not maintain a live, growing labeling queue (the no-list
mandate applies here too).
"""
