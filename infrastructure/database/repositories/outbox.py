"""Outbox repository adapter — appends domain events to the transactional outbox table."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import OutboxEventModel


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: str,
    ) -> None:
        event = OutboxEventModel(
            id=str(uuid.uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            status="PENDING",
            retry_count=0,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(event)

    async def get_pending_batch(self, batch_size: int = 100) -> list[OutboxEventModel]:
        """Fetch a batch of pending events using SELECT FOR UPDATE SKIP LOCKED."""
        stmt = (
            select(OutboxEventModel)
            .where(OutboxEventModel.status == "PENDING")
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
