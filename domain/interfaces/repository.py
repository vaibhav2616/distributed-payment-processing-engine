"""
domain/interfaces/repository.py
--------------------------------
Abstract repository interfaces (ports) for the domain layer.
Pure Python abstract classes — no SQLAlchemy, no FastAPI, no Pydantic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from domain.entities.payment import PaymentAggregate

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    @abstractmethod
    async def add(self, entity: T) -> None: ...

    @abstractmethod
    async def get(self, identifier: str) -> T | None: ...

    @abstractmethod
    async def exists(self, identifier: str) -> bool: ...


class AbstractPaymentRepository(AbstractRepository[PaymentAggregate]):
    @abstractmethod
    async def add(self, entity: PaymentAggregate) -> None: ...

    @abstractmethod
    async def get(self, transaction_id: str) -> PaymentAggregate | None: ...

    @abstractmethod
    async def get_by_payment_id(self, payment_id: str) -> PaymentAggregate | None: ...

    @abstractmethod
    async def exists(self, transaction_id: str) -> bool: ...

    @abstractmethod
    async def update(self, entity: PaymentAggregate) -> None:
        """
        Persist mutations to an already-persisted ``PaymentAggregate``.

        Unlike ``add()``, which inserts a new row, ``update()`` merges changes
        back into an existing row identified by ``entity.payment_id``.
        """
        ...

    @abstractmethod
    async def get_stale_pending_ids(
        self,
        older_than_seconds: int = 300,
        batch_size: int = 50,
    ) -> list[str]:
        """
        Return the ``payment_id`` values of PENDING payments whose
        ``created_at`` is older than ``older_than_seconds`` seconds ago.

        This is an **unlocked** read — no transaction, no ``FOR UPDATE``.
        It is safe to execute outside any UoW context and will not hold
        any database connections open while the caller performs network I/O.
        """
        ...

    @abstractmethod
    async def lock_pending_by_id(
        self,
        payment_id: str,
    ) -> PaymentAggregate | None:
        """
        Fetch a single payment by ``payment_id`` inside the current session's
        transaction using ``SELECT ... FOR UPDATE NOWAIT``.

        Returns ``None`` in two cases:
          - The row does not exist.
          - The row is already locked by another transaction (NOWAIT raises
            immediately; the caller should skip this payment and move on).
          - The payment status is no longer ``PENDING`` (resolved by the
            orchestrator or another reconciler pod while we were doing the
            network call — the caller must check and skip).

        Must be called inside an active UoW transaction.  The lock is
        released when the caller commits or rolls back.
        """
        ...
