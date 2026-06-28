from abc import ABC, abstractmethod
from infrastructure.database.session import async_session_maker

class AbstractUnitOfWork(ABC):
    @abstractmethod
    async def __aenter__(self): pass
    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb): pass
    @abstractmethod
    async def commit(self): pass
    @abstractmethod
    async def rollback(self): pass

class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(self):
        self.session_maker = async_session_maker

    async def __aenter__(self):
        self.session = self.session_maker()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        await self.session.close()

    async def commit(self):
        await self.session.commit()

    async def rollback(self):
        await self.session.rollback()
