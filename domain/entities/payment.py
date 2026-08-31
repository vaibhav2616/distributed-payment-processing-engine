"""
domain/entities/payment.py
---------------------------
PaymentAggregate — the authoritative aggregate root for the payment bounded context.

Design principles enforced here:
  - ZERO framework leakage: no Pydantic, no SQLAlchemy, no FastAPI, no ORM.
  - Pure Python stdlib only: dataclasses, datetime, uuid, enum, decimal.
  - All business invariants and state-transition guardrails live here.
  - The aggregate root is the *only* entry-point for mutating payment state.
  - Domain events are collected in-memory and dispatched by the application layer.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.events.payment_events import DomainEvent


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------


class Currency(str):
    """
    ISO 4217 three-letter currency code value object.
    Immutable and self-validating.
    """

    def __new__(cls, value: str) -> "Currency":
        if not value or len(value) != 3 or not value.isalpha():
            raise ValueError(
                f"Currency must be a 3-letter ISO 4217 alphabetic code; got '{value}'."
            )
        return super().__new__(cls, value.upper())


class Money:
    """
    Immutable value object representing a monetary amount.
    Uses Decimal internally to avoid floating-point arithmetic errors.
    """

    __slots__ = ("_amount", "_currency")

    def __init__(self, amount: Decimal | float | str, currency: str) -> None:
        try:
            decimal_amount = Decimal(str(amount))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid monetary amount: '{amount}'.") from exc

        if decimal_amount <= Decimal("0"):
            raise ValueError(
                f"Monetary amount must be strictly positive; got {decimal_amount}."
            )

        self._amount: Decimal = decimal_amount.quantize(Decimal("0.01"))
        self._currency: Currency = Currency(currency)

    @property
    def amount(self) -> Decimal:
        return self._amount

    @property
    def currency(self) -> Currency:
        return self._currency

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._amount == other._amount and self._currency == other._currency

    def __repr__(self) -> str:  # pragma: no cover
        return f"Money(amount={self._amount}, currency={self._currency})"


# ---------------------------------------------------------------------------
# Lifecycle State Machine
# ---------------------------------------------------------------------------


class PaymentStatus(str, Enum):
    """
    Strict lifecycle states for a payment transaction.

    Legal transition graph:
        PENDING  ──authorize──► AUTHORIZED ──capture──► CAPTURED
                 ──fail──────►  FAILED
        AUTHORIZED ──fail──────► FAILED
        CAPTURED   ──refund────► REFUNDED
        FAILED     [terminal — no outbound transitions]
        REFUNDED   [terminal — no outbound transitions]
    """

    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


# Adjacency map: current_state → set of reachable next states
_ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset(
        {PaymentStatus.AUTHORIZED, PaymentStatus.FAILED}
    ),
    PaymentStatus.AUTHORIZED: frozenset(
        {PaymentStatus.CAPTURED, PaymentStatus.FAILED}
    ),
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
    PaymentStatus.FAILED: frozenset(),    # terminal
    PaymentStatus.REFUNDED: frozenset(),  # terminal
}


# ---------------------------------------------------------------------------
# Aggregate Root
# ---------------------------------------------------------------------------


@dataclass
class PaymentAggregate:
    """
    Payment Aggregate Root.

    Encapsulates the full payment lifecycle and enforces all domain invariants.
    All state mutations go through the public command methods; raw field
    assignment is intentionally not used after construction.

    Attributes:
        payment_id      Aggregate identity (UUID string).
        transaction_id  External idempotency / correlation handle.
        amount          Gross amount in the specified currency (Decimal).
        currency        ISO 4217 currency code.
        status          Current lifecycle state.
        gateway_ref     Acquirer reference returned on authorisation.
        failure_reason  Human-readable failure reason when status == FAILED.
        amount_refunded Total amount refunded so far (Decimal).
        created_at      UTC wall-clock timestamp at creation.
        updated_at      UTC wall-clock timestamp of the last state change.
        _events         In-memory list of uncommitted domain events.
    """

    payment_id: str
    transaction_id: str
    amount: Decimal
    currency: str
    status: PaymentStatus = PaymentStatus.PENDING
    gateway_ref: str | None = None
    failure_reason: str | None = None
    amount_refunded: Decimal = field(default_factory=lambda: Decimal("0.00"))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _events: list["DomainEvent"] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    # Construction guard
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.payment_id:
            raise ValueError("payment_id must not be empty.")
        if not self.transaction_id:
            raise ValueError("transaction_id must not be empty.")
        # Coerce and validate amount
        try:
            self.amount = Decimal(str(self.amount)).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid amount: '{self.amount}'.") from exc
        if self.amount <= Decimal("0"):
            raise ValueError(f"amount must be strictly positive; got {self.amount}.")
        # Coerce and validate currency
        self.currency = Currency(self.currency)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        amount: Decimal | float | str,
        currency: str,
        transaction_id: str | None = None,
        payment_id: str | None = None,
    ) -> "PaymentAggregate":
        """
        Factory method: creates a new PaymentAggregate in PENDING state and
        registers a PaymentInitiated domain event.
        """
        # Import deferred inside factory to avoid top-level circular import.
        from domain.events.payment_events import PaymentInitiated

        now = datetime.now(timezone.utc)
        aggregate = cls(
            payment_id=payment_id or str(uuid.uuid4()),
            transaction_id=transaction_id or str(uuid.uuid4()),
            amount=Decimal(str(amount)),
            currency=currency,
            status=PaymentStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        aggregate._record(
            PaymentInitiated(
                aggregate_id=aggregate.payment_id,
                transaction_id=aggregate.transaction_id,
                amount=str(aggregate.amount),
                currency=aggregate.currency,
            )
        )
        return aggregate

    # ------------------------------------------------------------------
    # State transition commands
    # ------------------------------------------------------------------

    def authorize(self, gateway_ref: str) -> None:
        """
        Marks the payment as AUTHORIZED by the acquiring bank.

        Only valid from ``PENDING`` state. Attempting to authorize from any
        other state raises a ``DomainException``.

        Args:
            gateway_ref: Unique acquirer reference returned by the gateway.

        Raises:
            InvalidStateTransitionError (DomainException): When the current
                state is not ``PENDING``. Callers may catch ``DomainException``
                as the stable base type.
        """
        from domain.events.payment_events import PaymentAuthorized

        self._assert_transition(PaymentStatus.AUTHORIZED)
        self.gateway_ref = gateway_ref
        self.status = PaymentStatus.AUTHORIZED
        self.updated_at = datetime.now(timezone.utc)
        self._record(
            PaymentAuthorized(
                aggregate_id=self.payment_id,
                transaction_id=self.transaction_id,
                gateway_ref=gateway_ref,
            )
        )

    def capture(self) -> None:
        """
        Marks the payment as CAPTURED (funds settled).

        Only valid from ``AUTHORIZED`` state. A payment that is still
        ``PENDING``, or that has already ``FAILED`` / ``REFUNDED``, cannot be
        captured — doing so raises a ``DomainException``.

        Raises:
            InvalidStateTransitionError (DomainException): When the current
                state is not ``AUTHORIZED``. Callers may catch ``DomainException``
                as the stable base type.
        """
        from domain.events.payment_events import PaymentCaptured

        self._assert_transition(PaymentStatus.CAPTURED)
        self.status = PaymentStatus.CAPTURED
        self.updated_at = datetime.now(timezone.utc)
        self._record(
            PaymentCaptured(
                aggregate_id=self.payment_id,
                transaction_id=self.transaction_id,
                amount=str(self.amount),
                currency=self.currency,
                gateway_ref=self.gateway_ref or "",
            )
        )

    def fail(self, reason: str) -> None:
        """
        Marks the payment as FAILED.

        Valid from ``PENDING`` or ``AUTHORIZED`` states only. A payment that
        has already reached a terminal state (``CAPTURED``, ``FAILED``, or
        ``REFUNDED``) cannot be failed — doing so raises a ``DomainException``.

        Args:
            reason: Human-readable description of the failure cause.

        Raises:
            InvalidStateTransitionError (DomainException): When the current
                state is ``CAPTURED``, ``FAILED``, or ``REFUNDED``. Callers
                may catch ``DomainException`` as the stable base type.
        """
        from domain.events.payment_events import PaymentFailed

        self._assert_transition(PaymentStatus.FAILED)
        self.failure_reason = reason
        self.status = PaymentStatus.FAILED
        self.updated_at = datetime.now(timezone.utc)
        self._record(
            PaymentFailed(
                aggregate_id=self.payment_id,
                transaction_id=self.transaction_id,
                reason=reason,
            )
        )

    def process_refund(self, amount: Decimal) -> None:
        """
        Processes a partial or full refund against a CAPTURED or PARTIALLY_REFUNDED payment.

        Args:
            amount: The amount to refund.

        Raises:
            InvalidStateTransitionError: If the payment is not in a valid state for refund.
            InvalidRefundAmountError: If the requested amount exceeds the remaining refundable balance.
        """
        from domain.events.payment_events import PaymentRefunded
        from domain.exceptions import InvalidRefundAmountError

        # Only allow refunds if currently CAPTURED or PARTIALLY_REFUNDED
        if self.status not in (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED):
            self._assert_transition(PaymentStatus.REFUNDED) # this will raise appropriately

        try:
            amount_decimal = Decimal(str(amount)).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid refund amount: '{amount}'.") from exc

        if amount_decimal <= Decimal("0"):
            raise ValueError(f"Refund amount must be strictly positive; got {amount_decimal}.")

        remaining_refundable = self.amount - self.amount_refunded
        if amount_decimal > remaining_refundable:
            raise InvalidRefundAmountError(
                requested=str(amount_decimal),
                max_allowed=str(remaining_refundable)
            )

        self.amount_refunded += amount_decimal
        
        if self.amount_refunded == self.amount:
            self.status = PaymentStatus.REFUNDED
        else:
            self.status = PaymentStatus.PARTIALLY_REFUNDED
            
        self.updated_at = datetime.now(timezone.utc)
        
        self._record(
            PaymentRefunded(
                aggregate_id=self.payment_id,
                transaction_id=self.transaction_id,
                amount=str(amount_decimal),
                currency=self.currency,
            )
        )

    def refund(self) -> None:
        """
        Legacy full refund method.
        Marks a CAPTURED payment as REFUNDED completely.
        """
        self.process_refund(self.amount - self.amount_refunded)

    def fail_refund(self, amount: Decimal) -> None:
        """
        Compensating transaction method for when a refund definitively fails at the gateway.
        Reverts the amount_refunded and updates status based on the new total.
        """
        try:
            amount_decimal = Decimal(str(amount)).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid refund amount: '{amount}'.") from exc

        if amount_decimal <= Decimal("0"):
            raise ValueError(f"Refund amount must be strictly positive; got {amount_decimal}.")

        self.amount_refunded -= amount_decimal
        
        # Ensure we don't drop below 0
        if self.amount_refunded < Decimal("0"):
            self.amount_refunded = Decimal("0")

        if self.amount_refunded == Decimal("0"):
            self.status = PaymentStatus.CAPTURED
        else:
            self.status = PaymentStatus.PARTIALLY_REFUNDED
            
        self.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Domain event collection
    # ------------------------------------------------------------------

    def collect_events(self) -> list["DomainEvent"]:
        """
        Returns and clears the list of uncommitted domain events.
        Called by the application layer immediately before persisting outbox
        entries so that events are never lost and never double-published.
        """
        events, self._events = self._events, []
        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record(self, event: "DomainEvent") -> None:
        """Appends an event to the internal uncommitted event list."""
        self._events.append(event)

    def _assert_transition(self, target: PaymentStatus) -> None:
        """
        Guards every state mutation.

        Raises:
            InvalidStateTransitionError: When the requested transition is not
                                         permitted by the state machine graph.
        """
        from domain.exceptions import InvalidStateTransitionError

        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            raise InvalidStateTransitionError(
                from_state=self.status,
                to_state=target,
                payment_id=self.payment_id,
            )

    # ------------------------------------------------------------------
    # Backward-compatibility shim (for existing PaymentEntity consumers)
    # ------------------------------------------------------------------

    @property
    def transaction_id_pk(self) -> str:
        """Primary key alias used by the SQLAlchemy repository adapter."""
        return self.transaction_id


# ---------------------------------------------------------------------------
# Legacy alias — keeps existing infrastructure adapters working without changes
# ---------------------------------------------------------------------------

#: Backward-compatibility alias. All new code should reference ``PaymentAggregate``
#: directly. ``PaymentStatus_Legacy`` has been removed — use ``PaymentStatus``
#: which contains the canonical five-state lifecycle.
PaymentEntity = PaymentAggregate
