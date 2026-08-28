"""
application/uow.py
-------------------
Unit-of-Work abstraction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from infrastructure.database.session import async_session_maker
from infrastructure.database.repositories.payment import SqlAlchemyPaymentRepository
from infrastructure.database.repositories.outbox import SqlAlchemyOutboxRepository
from infrastructure.database.repositories.ledger import SqlAlchemyLedgerRepository
from infrastructure.external.gateway_client import gateway_client


class AbstractUnitOfWork(ABC):
    payments: object
    outbox: object
    ledger: object
    gateway: object

    @abstractmethod
    async def __aenter__(self) -> "AbstractUnitOfWork": ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(self) -> None:
        self.session_maker = async_session_maker
        self.gateway = gateway_client

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self.session_maker()
        self.payments = SqlAlchemyPaymentRepository(self.session)
        self.outbox = SqlAlchemyOutboxRepository(self.session)
        self.ledger = SqlAlchemyLedgerRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
