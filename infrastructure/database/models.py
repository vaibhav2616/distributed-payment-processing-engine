"""
infrastructure/database/models.py
-----------------------------------
SQLAlchemy ORM table definitions.
"""
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, DateTime, Text, Boolean
from datetime import datetime
import uuid


class Base(DeclarativeBase):
    pass


class PaymentModel(Base):
    """Persisted payment transaction record."""
    __tablename__ = "payments"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String, default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class OutboxEventModel(Base):
    """Transactional outbox event — published to Kafka by the relay worker."""
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)   # e.g. "Payment"
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False)     # e.g. transaction_id
    event_type: Mapped[str] = mapped_column(String, nullable=False)       # e.g. "payment.completed"
    payload: Mapped[str] = mapped_column(Text, nullable=False)            # JSON string
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
