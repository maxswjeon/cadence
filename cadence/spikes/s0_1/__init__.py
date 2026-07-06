"""S0.1 — read-state safety probe for accessibility-scraped Korean messengers.

See ``protocol.py`` for the per-app probe state machine + forbidden-action assertion
layer, and ``simulate.py`` for the device-free simulated probe run. Real on-device
empirical verdicts are gated — see ``.omc/research/spikes/s0_1.md``.
"""

from __future__ import annotations
