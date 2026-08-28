"""SQLAlchemy concrete implementation of AbstractPaymentRepository."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.interfaces.repository import AbstractPaymentRepository
from infrastructure.database.models import PaymentModel


class SqlAlchemyPaymentRepository(AbstractPaymentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_model(entity: PaymentAggregate) -> PaymentModel:
        return PaymentModel(
            payment_id=entity.payment_id,
            transaction_id=entity.transaction_id,
            amount=entity.amount,          # Decimal → Numeric(18,4) column directly
            currency=entity.currency,
            status=entity.status,          # PaymentStatus → Enum column directly
            gateway_ref=entity.gateway_ref,
            failure_reason=entity.failure_reason,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    @staticmethod
    def _to_entity(model: PaymentModel) -> PaymentAggregate:
        return PaymentAggregate(
            payment_id=model.payment_id,
            transaction_id=model.transaction_id,
            amount=model.amount,           # Numeric column → Decimal directly
            currency=model.currency,
            status=model.status,           # Enum column → PaymentStatus directly
            gateway_ref=model.gateway_ref,
            failure_reason=model.failure_reason,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def add(self, entity: PaymentAggregate) -> None:
        self._session.add(self._to_model(entity))

    async def update(self, entity: PaymentAggregate) -> None:
        """
        Merge an updated ``PaymentAggregate`` back into the session.

        ``session.merge()`` loads the existing row by PK (``payment_id``),
        applies the new field values, and schedules an UPDATE for the next
        flush/commit.  This is safe to call inside the same transaction that
        locked the row via ``get_stale_pending()``.
        """
        await self._session.merge(self._to_model(entity))

    async def get(self, transaction_id: str) -> PaymentAggregate | None:
        stmt = select(PaymentModel).where(PaymentModel.transaction_id == transaction_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def get_by_payment_id(self, payment_id: str) -> PaymentAggregate | None:
        stmt = select(PaymentModel).where(PaymentModel.payment_id == payment_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def exists(self, transaction_id: str) -> bool:
        return await self.get(transaction_id) is not None

    async def get_stale_pending_ids(
        self,
        older_than_seconds: int = 300,
        batch_size: int = 50,
    ) -> list[str]:
        """
        Return ``payment_id`` strings of stale PENDING payments.

        Intentionally **unlocked** — no ``FOR UPDATE``, no open transaction
        required.  The caller reads these IDs, then does expensive network I/O
        (gateway.verify_status) with zero DB connection held.  Each ID is
        re-locked individually in ``lock_pending_by_id()`` once a definitive
        gateway response has been obtained.

        Only ``payment_id`` (the PK) is fetched — no full model hydration.
        """
        staleness_cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        stmt = (
            select(PaymentModel.payment_id)   # scalar column — minimal data transfer
            .where(
                PaymentModel.status == PaymentStatus.PENDING,
                PaymentModel.created_at < staleness_cutoff,
            )
            .order_by(PaymentModel.created_at.asc())  # oldest first
            .limit(batch_size)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def lock_pending_by_id(
        self,
        payment_id: str,
    ) -> PaymentAggregate | None:
        """
        Acquire a ``SELECT ... FOR UPDATE NOWAIT`` lock on a single payment row
        inside the caller's active transaction.

        NOWAIT semantics
        ----------------
        If the row is already locked by another session (another reconciler pod
        that processed the same ID from the unlocked sweep), PostgreSQL raises
        an error immediately rather than blocking.  We catch ``OperationalError``
        and return ``None`` so the reconciler skips this payment gracefully.

        Stale-state guard
        -----------------
        The orchestrator or another reconciler pod may have resolved this payment
        while we were executing the gateway network call.  After acquiring the
        lock we confirm the status is still ``PENDING``; if not, we return ``None``
        and the caller rolls back without making any state change.
        """
        try:
            stmt = (
                select(PaymentModel)
                .where(PaymentModel.payment_id == payment_id)
                .with_for_update(nowait=True)   # raises immediately if row is locked
            )
            result = await self._session.execute(stmt)
            model = result.scalar_one_or_none()
        except OperationalError:
            # Row is locked by another transaction — skip without blocking.
            return None

        if model is None:
            return None

        # Stale-state guard: only return the entity if it is still PENDING.
        if model.status != PaymentStatus.PENDING:
            return None

        return self._to_entity(model)
