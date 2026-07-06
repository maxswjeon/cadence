"""Cadence — self-hosted personal-assistant brain.

Milestone 1 foundation spine. See ``.omc/plans/cadence-milestone-1-foundation.md``
for the authoritative scope and the architecture invariants this package upholds:

* **D1 raw boundary** — the canonical structured store (D1) never holds verbatim
  raw content. See :mod:`cadence.stores.raw_boundary`.
* **Local-canonical D1** — hot-path reads/writes hit local SQLite; the Cloudflare-D1
  replica is async and off the hot path. See :mod:`cadence.stores.d1`.
* **Provenance** — every derived fact carries source-event IDs, a NAS evidence
  pointer, confidence, expiration, and feedback history. See :mod:`cadence.brain.facts`.
"""

__version__ = "0.1.0"
