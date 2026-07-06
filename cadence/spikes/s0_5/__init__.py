"""S0.5 — compliance technical controls (Decisions D + E + H + I).

Implements the *technical* controls a real human location/co-presence/recording
collection, or a real CODEF credential enablement, must pass through — as code
interfaces + tests, self-reviewed (no counsel; user decision #3). See
``.omc/research/spikes/s0_5.md`` for the controls checklist and honest gating.

Modules:

* ``recording_gate`` — participant-context confirm + cancellable-countdown state
  machine (Decision D2) with the non-participant abort/purge hook wired in.
* ``indicator`` — visible/audible recording-state indicator + one-tap stop.
* ``purge`` — the abort/purge hook contract + the retention/destruction (TTL) job.
* ``audit`` — per-trigger audit log (distinct from, and referencing, the raw-content
  egress log in ``cadence.obs.egress`` — this log is trigger *lifecycle*, not content).
* ``presence_gate`` — the BLE owner-presence power-gate interface (Decision I,
  default-OFF) + the single-subject (owner-only) voiceprint-enrollment invariant.
* ``codef_vault`` — CODEF credential-vault/revoke/backoff hooks, built on top of the
  existing NAS-only ``CredentialVault`` rather than a new secret store.
"""

from __future__ import annotations
