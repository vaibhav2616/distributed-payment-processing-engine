"""
003_add_full_payment_aggregate_columns

Revision ID: 003_payment_aggregate
Revises: 002_outbox
Create Date: 2026-09-29

Adds columns required by the full PaymentAggregate domain model:
  - payment_id          (new UUID primary key replacing transaction_id as PK)
  - gateway_ref         (acquirer reference, nullable)
  - failure_reason      (failure description, nullable)
  - updated_at          (last-mutated timestamp)
  - UNIQUE index on transaction_id (was already PK; now the surrogate FK handle)
  - Index on outbox_events.aggregate_id for efficient relay queries
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_payment_aggregate"
down_revision = "002_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # payments table — restructure for full PaymentAggregate model
    # ------------------------------------------------------------------

    # 1. Add payment_id UUID column (will become the new PK)
    op.add_column(
        "payments",
        sa.Column(
            "payment_id",
            sa.String(),
            nullable=True,   # temporarily nullable for the backfill below
        ),
    )

    # 2. Backfill payment_id for existing rows using the existing transaction_id
    op.execute(
        "UPDATE payments SET payment_id = transaction_id WHERE payment_id IS NULL"
    )

    # 3. Make payment_id NOT NULL now that it is backfilled
    op.alter_column("payments", "payment_id", nullable=False)

    # 4. Drop the old PK on transaction_id
    op.drop_constraint("payments_pkey", "payments", type_="primary")

    # 5. Promote payment_id to PK
    op.create_primary_key("payments_pkey", "payments", ["payment_id"])

    # 6. Add a UNIQUE index on transaction_id (now the external idempotency handle)
    op.create_unique_constraint("uq_payments_transaction_id", "payments", ["transaction_id"])
    op.create_index("ix_payments_transaction_id", "payments", ["transaction_id"])

    # 7. Add gateway_ref column
    op.add_column(
        "payments",
        sa.Column("gateway_ref", sa.String(), nullable=True),
    )

    # 8. Add failure_reason column
    op.add_column(
        "payments",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )

    # 9. Add updated_at column — defaults to created_at for existing rows
    op.add_column(
        "payments",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    op.execute("UPDATE payments SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column("payments", "updated_at", nullable=False)

    # ------------------------------------------------------------------
    # outbox_events table — add index on aggregate_id
    # ------------------------------------------------------------------
    op.create_index(
        "ix_outbox_events_aggregate_id",
        "outbox_events",
        ["aggregate_id"],
    )


def downgrade() -> None:
    # outbox_events
    op.drop_index("ix_outbox_events_aggregate_id", table_name="outbox_events")

    # payments — reverse in opposite order
    op.drop_column("payments", "updated_at")
    op.drop_column("payments", "failure_reason")
    op.drop_column("payments", "gateway_ref")
    op.drop_index("ix_payments_transaction_id", table_name="payments")
    op.drop_constraint("uq_payments_transaction_id", "payments", type_="unique")
    op.drop_constraint("payments_pkey", "payments", type_="primary")
    op.create_primary_key("payments_pkey", "payments", ["transaction_id"])
    op.drop_column("payments", "payment_id")
