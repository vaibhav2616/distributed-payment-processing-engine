"""
domain/interfaces/repository.py
--------------------------------
Abstract repository interfaces (ports) for the domain layer.

These are pure Python abstract classes / Protocols.  The domain never
knows HOW data is stored — only WHAT operations are available.
Concrete implementations live in infrastructure/database/repositories/.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from domain.entities.payment import PaymentEntity

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    """Generic repository port — extend per aggregate root."""

    @abstractmethod
    async def add(self, entity: T) -> None:
        """Persist a new entity."""

    @abstractmethod
    async def get(self, identifier: str) -> T | None:
        """Retrieve an entity by its primary identifier. Returns None if absent."""

    @abstractmethod
    async def exists(self, identifier: str) -> bool:
        """Check existence by primary identifier without loading the full entity."""


class AbstractPaymentRepository(AbstractRepository[PaymentEntity]):
    """Port for Payment aggregate persistence operations."""

    @abstractmethod
    async def add(self, entity: PaymentEntity) -> None: ...

    @abstractmethod
    async def get(self, transaction_id: str) -> PaymentEntity | None: ...

    @abstractmethod
    async def exists(self, transaction_id: str) -> bool: ...
