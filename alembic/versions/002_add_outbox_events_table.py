"""add outbox_events table

Revision ID: 002_outbox
Revises: 001_payments
Create Date: 2025-06-03 17:15:00
"""
from alembic import op
import sqlalchemy as sa

revision = "002_outbox"
down_revision = "001_payments"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("aggregate_type", sa.String(), nullable=False),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_events_processed_created_at", "outbox_events", ["processed", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_outbox_events_processed_created_at", table_name="outbox_events")
    op.drop_table("outbox_events")
