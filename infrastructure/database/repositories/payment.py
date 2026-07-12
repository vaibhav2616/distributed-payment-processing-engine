"""SQLAlchemy concrete implementation of AbstractPaymentRepository."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.payment import PaymentEntity, PaymentStatus
from domain.interfaces.repository import AbstractPaymentRepository
from infrastructure.database.models import PaymentModel


class SqlAlchemyPaymentRepository(AbstractPaymentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_model(entity: PaymentEntity) -> PaymentModel:
        return PaymentModel(
            transaction_id=entity.transaction_id,
            amount=entity.amount,
            currency=entity.currency,
            status=entity.status.value,
            created_at=entity.created_at,
        )

    @staticmethod
    def _to_entity(model: PaymentModel) -> PaymentEntity:
        return PaymentEntity(
            transaction_id=model.transaction_id,
            amount=model.amount,
            currency=model.currency,
            status=PaymentStatus(model.status),
            created_at=model.created_at,
        )

    async def add(self, entity: PaymentEntity) -> None:
        self._session.add(self._to_model(entity))

    async def get(self, transaction_id: str) -> PaymentEntity | None:
        stmt = select(PaymentModel).where(PaymentModel.transaction_id == transaction_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def exists(self, transaction_id: str) -> bool:
        return await self.get(transaction_id) is not None
