import asyncio
import json
import logging
from sqlalchemy import select
from infrastructure.database.session import async_session_maker
from infrastructure.database.models import OutboxEventModel
from infrastructure.messaging.kafka_producer import kafka_producer

logger = logging.getLogger(__name__)

async def poll_outbox_events():
    """Background loop that relays outbox events to Kafka with at-least-once guarantees."""
    while True:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    stmt = (
                        select(OutboxEventModel)
                        .where(OutboxEventModel.processed == False)
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(50)
                        .with_for_update(skip_locked=True)
                    )
                    result = await session.execute(stmt)
                    events = result.scalars().all()
                    for event in events:
                        try:
                            topic = event.event_type.replace(".", "-")
                            await kafka_producer.send_event(topic, json.loads(event.payload))
                            event.processed = True
                            session.add(event)
                            logger.info(f"Relayed outbox event {event.id} to topic {topic}")
                        except Exception as e:
                            logger.error(f"Failed to publish outbox event {event.id}: {e}")
                            break
        except Exception as e:
            logger.error(f"Outbox relay worker error: {e}")
        await asyncio.sleep(2)
