"""
domain/events/payment_events.py
--------------------------------
Immutable domain event contracts for the Payment bounded context.

Design principles:
  - ZERO framework leakage: stdlib only (dataclasses, datetime, uuid).
  - All fields are set at construction and are never mutated (frozen=True).
  - Every event carries mandatory envelope metadata:
      event_id        → globally unique event identity (UUID v4 string)
      correlation_id  → distributed trace handle propagated from the HTTP layer
      occurred_at     → UTC wall-clock timestamp of when the event was raised
      aggregate_id    → the PaymentAggregate.payment_id this event belongs to
      event_type      → canonical dotted string used as the Kafka topic stem
  - aggregate_id is the stable identity that links outbox rows ↔ Kafka messages.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Envelope base — never instantiated directly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainEvent:
    """
    Base class for all domain events.

    Subclasses must declare *event_type* as a ClassVar string literal.
    The envelope fields (event_id, correlation_id, occurred_at) are
    auto-populated by the base __init_subclass__ defaults.
    """

    aggregate_id: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default="")
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Resolved by each concrete subclass — acts as the routing key.
    event_type: str = field(default="domain.event", init=False)

    def with_correlation_id(self, correlation_id: str) -> "DomainEvent":
        """
        Returns a new instance of this event enriched with a correlation_id.

        Because the dataclass is frozen we use object.__setattr__ for the one
        controlled mutation point in the envelope infrastructure.
        """
        # We create a dict copy, override correlation_id, and rebuild.
        d = {
            f.name: getattr(self, f.name)
            for f in self.__dataclass_fields__.values()  # type: ignore[attr-defined]
            if f.init
        }
        d["correlation_id"] = correlation_id
        return self.__class__(**d)

    def to_dict(self) -> dict:
        """Serialisable representation for the transactional outbox payload."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at.isoformat(),
            **self._payload(),
        }

    def _payload(self) -> dict:
        """Override in subclasses to add event-specific fields to the dict."""
        return {}


# ---------------------------------------------------------------------------
# Concrete payment domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentInitiated(DomainEvent):
    """
    Raised when a new PaymentAggregate is created in PENDING state.

    Published to Kafka topic: ``payment-initiated``
    """

    transaction_id: str = ""
    amount: str = "0.00"   # Decimal serialised as string — no float precision loss
    currency: str = ""
    event_type: str = field(default="payment.initiated", init=False)

    def _payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "amount": self.amount,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class PaymentAuthorized(DomainEvent):
    """
    Raised when the acquiring bank successfully authorises the payment.

    Published to Kafka topic: ``payment-authorized``
    """

    transaction_id: str = ""
    gateway_ref: str = ""
    event_type: str = field(default="payment.authorized", init=False)

    def _payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "gateway_ref": self.gateway_ref,
        }


@dataclass(frozen=True)
class PaymentCaptured(DomainEvent):
    """
    Raised when funds are settled (capture confirmed by the acquirer).

    Published to Kafka topic: ``payment-captured``
    """

    transaction_id: str = ""
    amount: str = "0.00"
    currency: str = ""
    gateway_ref: str = ""
    event_type: str = field(default="payment.captured", init=False)

    def _payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "amount": self.amount,
            "currency": self.currency,
            "gateway_ref": self.gateway_ref,
        }


@dataclass(frozen=True)
class PaymentFailed(DomainEvent):
    """
    Raised when a payment transitions to FAILED state.

    Published to Kafka topic: ``payment-failed``
    """

    transaction_id: str = ""
    reason: str = ""
    event_type: str = field(default="payment.failed", init=False)

    def _payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PaymentRefunded(DomainEvent):
    """
    Raised when a previously CAPTURED payment is fully refunded.

    Published to Kafka topic: ``payment-refunded``
    """

    transaction_id: str = ""
    amount: str = "0.00"
    currency: str = ""
    event_type: str = field(default="payment.refunded", init=False)

    def _payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "amount": self.amount,
            "currency": self.currency,
        }
