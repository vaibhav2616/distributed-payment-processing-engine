"""
domain/exceptions.py
--------------------
Pure domain exception hierarchy.  NO framework imports.  NO ORM imports.
All exceptions here are raised by domain and application layers and
translated into HTTP responses by the global exception handler in
presentation/middleware/exception_handler.py.
"""


class DomainException(Exception):
    """Base class for all domain-level exceptions."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or message


# ---------------------------------------------------------------------------
# Validation / Business-rule errors
# ---------------------------------------------------------------------------

class ValidationError(DomainException):
    """Raised when a domain invariant or value-object constraint is violated."""


class DuplicateTransactionError(DomainException):
    """Raised when a transaction with the same ID already exists."""


# ---------------------------------------------------------------------------
# Not-found errors
# ---------------------------------------------------------------------------

class EntityNotFoundError(DomainException):
    """Raised when a required aggregate/entity cannot be found."""

    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} '{identifier}' not found.",
            detail=f"No {entity_type} record matching identifier '{identifier}'.",
        )
        self.entity_type = entity_type
        self.identifier = identifier


# ---------------------------------------------------------------------------
# External / infrastructure errors (raised by application layer to surface
# upstream failures without leaking infrastructure types into domain)
# ---------------------------------------------------------------------------

class ExternalServiceError(DomainException):
    """Raised when a downstream external service call fails irrecoverably."""


class PaymentGatewayError(ExternalServiceError):
    """Raised when all payment acquirers are unavailable or return errors."""


class MessagingError(DomainException):
    """Raised when an event cannot be published to the messaging broker."""
