"""
domain/exceptions.py
--------------------
Pure domain exception hierarchy.  NO framework imports.  NO ORM imports.
"""


class DomainException(Exception):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or message


class ValidationError(DomainException):
    """Raised when a domain invariant or value-object constraint is violated."""


class DuplicateTransactionError(DomainException):
    """Raised when a transaction with the same ID already exists."""


class EntityNotFoundError(DomainException):
    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} '{identifier}' not found.",
            detail=f"No {entity_type} record matching identifier '{identifier}'.",
        )
        self.entity_type = entity_type
        self.identifier = identifier


class ExternalServiceError(DomainException):
    """Raised when a downstream external service call fails irrecoverably."""


class PaymentGatewayError(ExternalServiceError):
    """Raised when all payment acquirers are unavailable or return errors."""


class MessagingError(DomainException):
    """Raised when an event cannot be published to the messaging broker."""


class InvalidStateTransitionError(DomainException):
    """
    Raised when an attempt is made to transition a PaymentAggregate to a state
    that is not reachable from its current state.

    Example:
        Cannot transition from CAPTURED → AUTHORIZED.
    """

    def __init__(self, from_state: object, to_state: object, payment_id: str) -> None:
        super().__init__(
            f"Invalid state transition: {from_state} → {to_state} "
            f"for payment '{payment_id}'.",
            detail=(
                f"The payment '{payment_id}' is currently in state '{from_state}' "
                f"which does not allow a transition to '{to_state}'."
            ),
        )
        self.from_state = from_state
        self.to_state = to_state
        self.payment_id = payment_id


class IdempotentRequestError(DomainException):
    """
    Raised (or caught) by the application layer when the Redis idempotency
    store already contains a result for the given idempotency key.

    Handlers should return the previously-cached result rather than
    re-executing the use case.
    """


class LedgerImbalanceError(DomainException):
    """
    Raised by ``LedgerTransaction.verify_balance()`` when the sum of all
    ``LedgerEntry`` amounts does not equal exactly ``Decimal('0.00')``.

    This signals a violation of the double-entry accounting invariant:
    every debit must have an equal and opposite credit.

    Attributes:
        transaction_id  The ``LedgerTransaction.id`` that failed validation.
        reference       The external reference (e.g. ``PaymentAggregate.payment_id``).
        imbalance       The non-zero net sum as a ``Decimal`` — useful for diagnostics.
    """

    def __init__(self, transaction_id: str, reference: str, imbalance: object) -> None:
        super().__init__(
            f"Ledger transaction '{transaction_id}' (ref: '{reference}') is not balanced. "
            f"Net imbalance: {imbalance}.",
            detail=(
                f"The sum of all LedgerEntry amounts for transaction '{transaction_id}' "
                f"must equal Decimal('0.00'). Got {imbalance} instead. "
                "Ensure every debit entry has a corresponding credit entry of the same magnitude."
            ),
        )
        self.transaction_id = transaction_id
        self.reference = reference
        self.imbalance = imbalance

