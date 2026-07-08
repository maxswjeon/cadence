"""device registry (M8 / B1 mTLS enrollment)

Frozen, explicit DDL for the ``device`` table — the status-based trust registry for the
mTLS trust fabric. Mirrors :class:`cadence.stores.models.Device` column-for-column.

Timestamp columns use ``sa.DateTime(timezone=True)`` (wrapped by ``UTCDateTime`` at
runtime), matching 0001. Unique constraint / index: ``device.public_key_fingerprint``
(idempotent re-enroll); index on ``device.status`` (registry filtering).

Revision ID: 0002_device_registry
Revises: 0001_initial
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_device_registry"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("public_key_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("key_provenance", sa.String(length=32), nullable=False),
        sa.Column("pop_method", sa.String(length=16), nullable=True),
        sa.Column("pop_proof", sa.Text(), nullable=True),
        sa.Column("cert_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("attestation_ref", sa.String(length=255), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("device", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_device_public_key_fingerprint"),
            ["public_key_fingerprint"],
            unique=True,
        )
        batch_op.create_index(batch_op.f("ix_device_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("device", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_status"))
        batch_op.drop_index(batch_op.f("ix_device_public_key_fingerprint"))
    op.drop_table("device")
