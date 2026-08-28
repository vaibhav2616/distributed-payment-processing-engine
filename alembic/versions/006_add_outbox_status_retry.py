"""
006_add_outbox_status_retry

Revision ID: 006_add_outbox_status
Revises: 005_ledger_tables
Create Date: 2026-10-05

Adds status (PENDING, PROCESSED, DLQ) and retry_count columns to outbox_events.
Drops the processed column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision = "006_add_outbox_status"
down_revision = "005_ledger_tables"
branch_labels = None
depends_on = None

def upgrade() -> None:
    # We must handle the existing 'processed' column by converting its state to 'status'
    # False -> 'PENDING', True -> 'PROCESSED'
    
    op.add_column("outbox_events", sa.Column("status", sa.String(length=10), nullable=True))
    op.add_column("outbox_events", sa.Column("retry_count", sa.Integer(), nullable=True))
    
    # Backfill
    op.execute("UPDATE outbox_events SET status = 'PROCESSED' WHERE processed = true")
    op.execute("UPDATE outbox_events SET status = 'PENDING' WHERE processed = false")
    op.execute("UPDATE outbox_events SET retry_count = 0")
    
    # Set NOT NULL
    op.alter_column("outbox_events", "status", existing_type=sa.String(length=10), nullable=False)
    op.alter_column("outbox_events", "retry_count", existing_type=sa.Integer(), nullable=False)
    
    # Drop old column
    op.drop_column("outbox_events", "processed")

def downgrade() -> None:
    op.add_column("outbox_events", sa.Column("processed", sa.Boolean(), nullable=True))
    
    # Revert state
    op.execute("UPDATE outbox_events SET processed = true WHERE status = 'PROCESSED'")
    op.execute("UPDATE outbox_events SET processed = false WHERE status != 'PROCESSED'")
    
    op.alter_column("outbox_events", "processed", existing_type=sa.Boolean(), nullable=False)
    
    op.drop_column("outbox_events", "retry_count")
    op.drop_column("outbox_events", "status")
