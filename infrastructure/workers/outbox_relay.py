"""
infrastructure/workers/outbox_relay.py
----------------------------------------
Transactional Outbox relay worker — structlog with per-event context binding.
"""
from __future__ import annotations

import asyncio
import json

import structlog
from sqlalchemy import select

from infrastructure.database.session import async_session_maker
from infrastructure.database.models import OutboxEventModel
from infrastructure.messaging.kafka_producer import kafka_producer

logger = structlog.get_logger(__name__)

_POLL_INTERVAL_SECONDS = 2
_BATCH_SIZE = 50


async def poll_outbox_events() -> None:
    logger.info("outbox_relay_started", poll_interval=_POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    stmt = (
                        select(OutboxEventModel)
                        .where(OutboxEventModel.processed == False)  # noqa: E712
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                    result = await session.execute(stmt)
                    events = result.scalars().all()
                    for event in events:
                        with structlog.contextvars.bound_contextvars(
                            outbox_event_id=event.id,
                            aggregate_type=event.aggregate_type,
                            aggregate_id=event.aggregate_id,
                            event_type=event.event_type,
                        ):
                            try:
                                topic = event.event_type.replace(".", "-")
                                await kafka_producer.send_event(topic, json.loads(event.payload))
                                event.processed = True
                                session.add(event)
                                logger.info("outbox_event_relayed", topic=topic)
                            except Exception as inner_exc:
                                logger.error("outbox_event_publish_failed", error=str(inner_exc))
                                break
        except Exception as exc:
            logger.error("outbox_relay_poll_error", error=str(exc))
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
