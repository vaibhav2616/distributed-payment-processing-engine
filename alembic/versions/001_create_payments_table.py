"""create payments table

Revision ID: 001_payments
Revises:
Create Date: 2025-05-28 11:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = "001_payments"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("transaction_id"),
    )

def downgrade() -> None:
    op.drop_table("payments")
