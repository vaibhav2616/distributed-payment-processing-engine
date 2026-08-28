"""
004_align_payment_model_column_types

Revision ID: 004_payment_column_types
Revises: 003_payment_aggregate
Create Date: 2026-09-29

Aligns the ``payments`` table column types with the authoritative SQLAlchemy
model in ``infrastructure/database/models.py``:

1. ``amount``    Float  → NUMERIC(18, 4)
   Eliminates floating-point rounding errors for all stored monetary values.
   Existing ``FLOAT`` data is safe to cast — PostgreSQL ``ALTER COLUMN … USING
   amount::numeric(18,4)`` performs an exact lossless conversion for any value
   that was stored via Python's default ``float`` representation.

2. ``status``    VARCHAR(20) → paymentstatus ENUM
   Creates a PostgreSQL native ENUM type ``paymentstatus`` containing the five
   canonical lifecycle values, then alters the column to use it.
   Existing rows contain plain string values that match the enum labels, so the
   ``USING status::paymentstatus`` cast is safe.

3. ``created_at`` / ``updated_at``    TIMESTAMP → TIMESTAMPTZ
   Adds timezone awareness to prevent silent UTC stripping on DST boundaries.

All other columns, constraints, and indexes are unchanged.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_payment_column_types"
down_revision = "003_payment_aggregate"
branch_labels = None
depends_on = None

# The five values from domain.entities.payment.PaymentStatus
_STATUS_VALUES = ("PENDING", "AUTHORIZED", "CAPTURED", "FAILED", "REFUNDED")
_ENUM_NAME = "paymentstatus"
_ENUM_TYPE = sa.Enum(*_STATUS_VALUES, name=_ENUM_NAME)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create the PostgreSQL ENUM type (idempotent guard via checkfirst)
    # ------------------------------------------------------------------
    _ENUM_TYPE.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # 2. amount: Float → NUMERIC(18, 4)
    # ------------------------------------------------------------------
    op.alter_column(
        "payments",
        "amount",
        type_=sa.Numeric(precision=18, scale=4, asdecimal=True),
        existing_type=sa.Float(),
        postgresql_using="amount::numeric(18,4)",
        nullable=False,
    )

    # ------------------------------------------------------------------
    # 3. status: VARCHAR(20) → paymentstatus ENUM
    # ------------------------------------------------------------------
    op.alter_column(
        "payments",
        "status",
        type_=_ENUM_TYPE,
        existing_type=sa.String(20),
        postgresql_using=f"status::{_ENUM_NAME}",
        nullable=False,
    )

    # ------------------------------------------------------------------
    # 4. created_at / updated_at: TIMESTAMP → TIMESTAMPTZ
    # ------------------------------------------------------------------
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "payments",
            col,
            type_=sa.DateTime(timezone=True),
            existing_type=sa.DateTime(),
            nullable=False,
        )

    # Also upgrade outbox_events.created_at
    op.alter_column(
        "outbox_events",
        "created_at",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(),
        nullable=False,
    )


def downgrade() -> None:
    # Reverse outbox_events timestamp
    op.alter_column(
        "outbox_events",
        "created_at",
        type_=sa.DateTime(),
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )

    # Reverse payments timestamps
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "payments",
            col,
            type_=sa.DateTime(),
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )

    # Reverse status: ENUM → VARCHAR(20)
    op.alter_column(
        "payments",
        "status",
        type_=sa.String(20),
        existing_type=_ENUM_TYPE,
        postgresql_using="status::varchar",
        nullable=False,
    )
    _ENUM_TYPE.drop(op.get_bind(), checkfirst=True)

    # Reverse amount: NUMERIC → Float
    op.alter_column(
        "payments",
        "amount",
        type_=sa.Float(),
        existing_type=sa.Numeric(precision=18, scale=4, asdecimal=True),
        postgresql_using="amount::float",
        nullable=False,
    )
