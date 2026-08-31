"""add refund support

Revision ID: 007_refund_support
Revises: 006_add_outbox_status
Create Date: 2026-10-08 20:00:00
"""
from alembic import op
import sqlalchemy as sa
from decimal import Decimal

revision = "007_refund_support"
down_revision = "006_add_outbox_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add PARTIALLY_REFUNDED to paymentstatus enum if postgresql
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_REFUNDED'")

    op.add_column(
        "payments",
        sa.Column(
            "amount_refunded",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )


def downgrade() -> None:
    op.drop_column("payments", "amount_refunded")
