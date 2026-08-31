"""add idempotency_key unique constraint

Revision ID: 008_idempotency_key_unique
Revises: 007_refund_support
Create Date: 2026-10-09 17:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "008_idempotency_key_unique"
down_revision = "007_refund_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add column as nullable first to allow backfilling existing rows
    op.add_column("payments", sa.Column("idempotency_key", sa.String(), nullable=True))

    # 2. Backfill idempotency_key from transaction_id for any existing records
    op.execute("UPDATE payments SET idempotency_key = transaction_id WHERE idempotency_key IS NULL")

    # 3. Enforce NOT NULL
    op.alter_column("payments", "idempotency_key", nullable=False)

    # 4. Enforce strict database-level UNIQUE constraint and index
    op.create_unique_constraint("uq_payments_idempotency_key", "payments", ["idempotency_key"])
    op.create_index("ix_payments_idempotency_key", "payments", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index("ix_payments_idempotency_key", table_name="payments")
    op.drop_constraint("uq_payments_idempotency_key", "payments", type_="unique")
    op.drop_column("payments", "idempotency_key")
