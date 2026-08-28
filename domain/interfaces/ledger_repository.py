"""
domain/interfaces/ledger_repository.py
----------------------------------------
Abstract port for the Ledger repository.

Pure Python ABC — no SQLAlchemy, no FastAPI, no Pydantic.
The concrete SqlAlchemyLedgerRepository in infrastructure/ implements this.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.entities.ledger import LedgerTransaction


class AbstractLedgerRepository(ABC):
    """
    Port for persisting double-entry ledger transactions.

    A ``LedgerTransaction`` and all its ``LedgerEntry`` objects must be
    persisted atomically within the caller's Unit-of-Work transaction.
    """

    @abstractmethod
    async def add(self, ledger_txn: LedgerTransaction) -> None:
        """
        Persist a ``LedgerTransaction`` together with all its ``LedgerEntry``
        objects inside the current database transaction.

        The repository is responsible for mapping the immutable domain
        value objects to the ORM models ``LedgerTransactionModel`` and
        ``LedgerEntryModel``.
        """
        ...

    @abstractmethod
    async def get(self, ledger_txn_id: str) -> LedgerTransaction | None:
        """Return a ``LedgerTransaction`` by its UUID, or ``None``."""
        ...

    @abstractmethod
    async def get_by_reference(self, reference_id: str) -> list[LedgerTransaction]:
        """
        Return all ``LedgerTransaction`` objects linked to a given
        ``reference_id`` (e.g. a ``PaymentAggregate.payment_id``).
        """
        ...
