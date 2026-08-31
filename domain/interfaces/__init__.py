"""Domain interfaces (ports) — abstract contracts for repositories and services."""
from domain.interfaces.gateway import PaymentGatewayInterface
from domain.interfaces.ledger_repository import AbstractLedgerRepository
from domain.interfaces.outbox import AbstractOutboxRepository
from domain.interfaces.repository import AbstractPaymentRepository, AbstractRepository

__all__ = [
    "AbstractLedgerRepository",
    "AbstractOutboxRepository",
    "AbstractPaymentRepository",
    "AbstractRepository",
    "PaymentGatewayInterface",
]
