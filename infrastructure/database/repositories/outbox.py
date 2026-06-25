"""
infrastructure/database/repositories/outbox.py
-----------------------------------------------
Outbox repository adapter — appends domain events to the outbox table.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

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
        """Append an outbox event that the relay worker will publish to Kafka."""
        event = OutboxEventModel(
            id=str(uuid.uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            processed=False,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(event)
