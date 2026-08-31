"""
infrastructure/database/models.py
-----------------------------------
SQLAlchemy ORM table definitions.

Column-type decisions
---------------------
* ``payment_id`` / ``OutboxEventModel.id``
    Stored as native ``Uuid`` (SQLAlchemy 2.x) so PostgreSQL uses a real UUID
    column rather than a VARCHAR.  The ORM transparently marshals Python ``str``
    UUIDs to/from ``uuid.UUID`` objects; the repository adapter keeps working
    because PostgreSQL accepts both forms in comparisons.

* ``amount``
    ``Numeric(precision=18, scale=4)`` — maps to PostgreSQL ``NUMERIC(18,4)``.
    This is the only safe column type for financial amounts:
      - ``Float`` / ``DOUBLE PRECISION`` suffer from binary floating-point
        rounding errors (e.g. 0.1 + 0.2 ≠ 0.3).
      - ``Numeric`` stores values as exact decimals; SQLAlchemy returns them
        as Python ``Decimal`` objects, eliminating the ``float → Decimal``
        coercion dance that was previously done in the repository adapter.
    ``asdecimal=True`` (SQLAlchemy default for Numeric) is explicitly stated
    for documentation clarity.

* ``status``
    ``sqlalchemy.Enum`` bound to ``domain.entities.payment.PaymentStatus``
    enforces the five-state lifecycle at the database level (PostgreSQL creates
    a native ENUM type; other dialects use VARCHAR with a CHECK constraint).
    The ORM returns ``PaymentStatus`` instances directly — no ``PaymentStatus(...)``
    wrapping needed in the repository adapter.

* ``created_at`` / ``updated_at``
    ``DateTime(timezone=True)`` stores UTC timestamps with timezone info,
    preventing silent timezone stripping that bare ``DateTime`` causes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, Boolean, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domain.entities.payment import PaymentStatus


class Base(DeclarativeBase):
    pass


class PaymentModel(Base):
    """
    Persisted payment transaction record — the ORM mirror of ``PaymentAggregate``.

    Type mapping summary:

    ============== ========================= =============================
    Domain field   Python type               PostgreSQL column type
    ============== ========================= =============================
    payment_id     str (UUID)                UUID (native)
    transaction_id str                       VARCHAR  (UNIQUE INDEX)
    amount         Decimal                   NUMERIC(18, 4)
    currency       str                       VARCHAR(3)
    status         PaymentStatus (Enum)      ENUM('PENDING',…)
    gateway_ref    str | None                VARCHAR
    failure_reason str | None                TEXT
    created_at     datetime (UTC-aware)      TIMESTAMPTZ
    updated_at     datetime (UTC-aware)      TIMESTAMPTZ
    ============== ========================= =============================
    """

    __tablename__ = "payments"

    # ------------------------------------------------------------------
    # Primary key — stable aggregate identity
    # ------------------------------------------------------------------
    payment_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),          # as_uuid=False → Python str, not uuid.UUID
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="Stable aggregate UUID assigned by the domain layer.",
    )

    # ------------------------------------------------------------------
    # External / merchant idempotency handle
    # ------------------------------------------------------------------
    transaction_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        index=True,
        comment="Merchant-provided transaction reference; unique per payment.",
    )

    # ------------------------------------------------------------------
    # Amount — Numeric(18, 4) for exact decimal storage
    # ------------------------------------------------------------------
    amount: Mapped[sa.Numeric] = mapped_column(
        Numeric(precision=18, scale=4, asdecimal=True),
        nullable=False,
        comment="Payment amount stored as NUMERIC(18,4) — no floating-point rounding.",
    )

    # ------------------------------------------------------------------
    # Currency — ISO 4217 three-letter code
    # ------------------------------------------------------------------
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="INR",
        comment="ISO 4217 three-letter currency code.",
    )

    amount_refunded: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2),
        nullable=False,
        default=Decimal("0.00"),
        comment="Total amount refunded so far. Cannot exceed amount.",
    )

    # ------------------------------------------------------------------
    # Status — DB-enforced ENUM, bound to PaymentStatus domain type
    # ------------------------------------------------------------------
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="paymentstatus",       # PostgreSQL ENUM type name in the DB
            values_callable=lambda e: [m.value for m in e],
            create_constraint=True,
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
        comment="Payment lifecycle state — enforced by DB ENUM constraint.",
    )

    # ------------------------------------------------------------------
    # Gateway & failure metadata
    # ------------------------------------------------------------------
    gateway_ref: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Acquirer reference number returned on authorisation.",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable failure description when status=FAILED.",
    )

    # ------------------------------------------------------------------
    # Timestamps — timezone-aware to prevent UTC stripping
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp at which the payment was created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of the last state change.",
    )


class OutboxEventModel(Base):
    """Transactional outbox event — published to Kafka by the relay worker."""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)   # e.g. "Payment"
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)       # e.g. "payment.captured"
    payload: Mapped[str] = mapped_column(Text, nullable=False)            # JSON string
    status: Mapped[str] = mapped_column(String, default="PENDING", nullable=False, index=True)
    retry_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class LedgerTransactionModel(Base):
    """
    ORM mirror of ``domain.entities.ledger.LedgerTransaction``.

    Represents an atomic, balanced double-entry transaction.  One
    ``LedgerTransactionModel`` owns many ``LedgerEntryModel`` rows via the
    ``entries`` relationship.  The ``reference_id`` column links back to the
    ``PaymentModel.payment_id`` that caused this ledger movement (e.g. on
    capture or refund), but is intentionally a plain indexed string rather
    than a FK so that the ledger can also record non-payment movements
    without requiring a ``payments`` row to exist.

    Type mapping:

    ============ ==================== ====================
    Field        Python type          PostgreSQL column
    ============ ==================== ====================
    id           str (UUID)           UUID (PK)
    reference_id str                  VARCHAR (INDEX)
    description  str                  TEXT
    created_at   datetime (UTC)       TIMESTAMPTZ
    entries      list[LedgerEntry]    — (ORM relationship)
    ============ ==================== ====================
    """

    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="Stable UUID identity of the ledger transaction.",
    )
    # Soft-link back to the originating aggregate (payment_id, refund_id, etc.)
    # Kept as a plain string so the ledger remains decoupled from the payments table.
    reference_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment="External reference linking this transaction to a domain aggregate "
                "(e.g. PaymentAggregate.payment_id).",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="Human-readable description of the economic event.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
        comment="UTC timestamp at which the ledger transaction was recorded.",
    )

    # One-to-many: a transaction owns its entries
    entries: Mapped[list["LedgerEntryModel"]] = relationship(
        "LedgerEntryModel",
        back_populates="transaction",
        lazy="selectin",        # eager-load entries with the transaction by default
        order_by="LedgerEntryModel.created_at",
    )


class LedgerEntryModel(Base):
    """
    ORM mirror of ``domain.entities.ledger.LedgerEntry``.

    A single immutable debit or credit line within a ``LedgerTransactionModel``.
    Positive ``amount`` = debit; negative ``amount`` = credit.

    Type mapping:

    ============== ==================== =============================
    Field          Python type          PostgreSQL column
    ============== ==================== =============================
    id             str (UUID)           UUID (PK)
    transaction_id str (FK)             UUID → ledger_transactions.id
    account_id     str                  VARCHAR (INDEX)
    amount         Decimal              NUMERIC(18, 4)
    currency       str                  VARCHAR(3)
    created_at     datetime (UTC)       TIMESTAMPTZ
    ============== ==================== =============================
    """

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="Stable UUID identity of this ledger entry.",
    )
    transaction_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("ledger_transactions.id"),
        nullable=False,
        index=True,
        comment="FK to the owning LedgerTransactionModel.",
    )
    account_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment="Account being debited (positive) or credited (negative).",
    )
    # Positive = debit, negative = credit — matches LedgerEntry.amount convention
    amount: Mapped[sa.Numeric] = mapped_column(
        Numeric(precision=18, scale=4, asdecimal=True),
        nullable=False,
        comment="Entry amount as NUMERIC(18,4). Positive=debit, negative=credit.",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        comment="ISO 4217 three-letter currency code.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp at which this entry was recorded.",
    )

    # Many-to-one back-reference
    transaction: Mapped["LedgerTransactionModel"] = relationship(
        "LedgerTransactionModel",
        back_populates="entries",
    )
