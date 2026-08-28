"""
005_add_ledger_tables

Revision ID: 005_ledger_tables
Revises: 004_payment_column_types
Create Date: 2026-09-30

Creates two new tables for the double-entry ledger domain:

  ledger_transactions
    Atomic unit of a balanced double-entry movement.
    Links back to the originating aggregate via ``reference_id`` (a plain
    indexed string, not a FK — keeps the ledger decoupled from the payments
    table so it can record non-payment movements too).

  ledger_entries
    Individual debit / credit lines owned by a ``ledger_transactions`` row.
    ``amount`` is stored as NUMERIC(18, 4) — no floating-point rounding.
    ``transaction_id`` FK uses the default RESTRICT behaviour — the database
    will raise an ``IntegrityError`` if anything attempts to delete a
    ``ledger_transactions`` row that still owns ``ledger_entries`` rows.
    Ledger entries are immutable audit records and must never be silently removed.

Indexes created:
  - ix_ledger_transactions_reference_id   (for payment↔ledger joins)
  - ix_ledger_transactions_created_at     (for time-range queries / reconciliation)
  - ix_ledger_entries_transaction_id      (for the one-to-many JOIN)
  - ix_ledger_entries_account_id          (for per-account balance queries)

No existing tables or columns are modified.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_ledger_tables"
down_revision = "004_payment_column_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ledger_transactions
    # ------------------------------------------------------------------
    op.create_table(
        "ledger_transactions",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=False),
            primary_key=True,
            nullable=False,
            comment="Stable UUID identity of the ledger transaction.",
        ),
        sa.Column(
            "reference_id",
            sa.String(),
            nullable=False,
            comment="External reference — e.g. PaymentAggregate.payment_id.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Human-readable description of the economic event.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="UTC timestamp at which the ledger transaction was recorded.",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_transactions"),
    )
    op.create_index(
        "ix_ledger_transactions_reference_id",
        "ledger_transactions",
        ["reference_id"],
    )
    op.create_index(
        "ix_ledger_transactions_created_at",
        "ledger_transactions",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # ledger_entries
    # ------------------------------------------------------------------
    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=False),
            primary_key=True,
            nullable=False,
            comment="Stable UUID identity of this ledger entry.",
        ),
        sa.Column(
            "transaction_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey(
                "ledger_transactions.id",
                name="fk_ledger_entries_transaction_id",
            ),
            nullable=False,
            comment="FK to the owning ledger_transactions row.",
        ),
        sa.Column(
            "account_id",
            sa.String(),
            nullable=False,
            comment="Account being debited (positive) or credited (negative).",
        ),
        sa.Column(
            "amount",
            sa.Numeric(precision=18, scale=4, asdecimal=True),
            nullable=False,
            comment="Entry amount as NUMERIC(18,4). Positive=debit, negative=credit.",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            comment="ISO 4217 three-letter currency code.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="UTC timestamp at which this entry was recorded.",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    )
    op.create_index(
        "ix_ledger_entries_transaction_id",
        "ledger_entries",
        ["transaction_id"],
    )
    op.create_index(
        "ix_ledger_entries_account_id",
        "ledger_entries",
        ["account_id"],
    )


def downgrade() -> None:
    # Drop child table first to satisfy FK constraint
    op.drop_index("ix_ledger_entries_account_id", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_transaction_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")

    op.drop_index("ix_ledger_transactions_created_at", table_name="ledger_transactions")
    op.drop_index("ix_ledger_transactions_reference_id", table_name="ledger_transactions")
    op.drop_table("ledger_transactions")
