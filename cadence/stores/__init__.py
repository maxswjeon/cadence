"""Cadence storage tiers.

* :mod:`cadence.stores.d1` — local-canonical SQLite (the "D1") + Cloudflare-D1 replica stub.
* :mod:`cadence.stores.models` — SQLAlchemy ORM models for the D1 schema.
* :mod:`cadence.stores.raw_boundary` — payload classifier enforcing the D1 raw boundary.
* :mod:`cadence.stores.nas` — local raw-evidence blob store (NAS stub).
* :mod:`cadence.stores.r2` — derived-blob store (Cloudflare R2 stub) + tiering router.
"""
