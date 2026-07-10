"""
domain/interfaces/repository.py
--------------------------------
Abstract repository interfaces (ports) for the domain layer.
Pure Python abstract classes — no SQLAlchemy, no FastAPI, no Pydantic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from domain.entities.payment import PaymentEntity

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    @abstractmethod
    async def add(self, entity: T) -> None: ...

    @abstractmethod
    async def get(self, identifier: str) -> T | None: ...

    @abstractmethod
    async def exists(self, identifier: str) -> bool: ...


class AbstractPaymentRepository(AbstractRepository[PaymentEntity]):
    @abstractmethod
    async def add(self, entity: PaymentEntity) -> None: ...

    @abstractmethod
    async def get(self, transaction_id: str) -> PaymentEntity | None: ...

    @abstractmethod
    async def exists(self, transaction_id: str) -> bool: ...
