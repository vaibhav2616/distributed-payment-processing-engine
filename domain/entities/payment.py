"""
domain/entities/payment.py
---------------------------
PaymentEntity — a pure domain aggregate root.

Strict isolation rules:
  - NO Pydantic, NO SQLAlchemy, NO FastAPI, NO ORM imports.
  - Uses only the Python stdlib (dataclasses, datetime, uuid).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass
class PaymentEntity:
    transaction_id: str
    amount: float
    currency: str
    status: PaymentStatus = PaymentStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise ValueError("transaction_id must not be empty.")
        if self.amount <= 0:
            raise ValueError(f"amount must be positive; got {self.amount}.")
        if not self.currency or len(self.currency) != 3:
            raise ValueError(f"currency must be a 3-character ISO 4217 code; got '{self.currency}'.")
        self.currency = self.currency.upper()

    def mark_success(self) -> None:
        if self.status != PaymentStatus.PENDING:
            raise ValueError(f"Cannot mark a {self.status.value} payment as SUCCESS.")
        self.status = PaymentStatus.SUCCESS

    def mark_failed(self) -> None:
        if self.status != PaymentStatus.PENDING:
            raise ValueError(f"Cannot mark a {self.status.value} payment as FAILED.")
        self.status = PaymentStatus.FAILED

    @classmethod
    def create(cls, amount: float, currency: str, transaction_id: str | None = None) -> "PaymentEntity":
        return cls(
            transaction_id=transaction_id or str(uuid.uuid4()),
            amount=amount,
            currency=currency,
        )
