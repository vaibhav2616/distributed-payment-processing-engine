"""
application/workers/outbox_relay.py
-----------------------------------
Transactional Outbox relay worker with telemetry and poison pill circuit breaker.
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
_MAX_RETRIES = 5


async def poll_outbox_events() -> None:
    logger.info("outbox_relay_started", poll_interval=_POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session_maker() as session:
                async with session.begin():
                    stmt = (
                        select(OutboxEventModel)
                        .where(OutboxEventModel.status == "PENDING")
                        .order_by(OutboxEventModel.created_at.asc())
                        .limit(_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                    result = await session.execute(stmt)
                    events = result.scalars().all()

                    for event in events:
                        payload_dict = json.loads(event.payload)
                        trace_id = payload_dict.get("trace_id", "unknown")
                        
                        with structlog.contextvars.bound_contextvars(
                            trace_id=trace_id,
                            outbox_event_id=event.id,
                            aggregate_type=event.aggregate_type,
                            aggregate_id=event.aggregate_id,
                            event_type=event.event_type,
                            retry_count=event.retry_count,
                        ):
                            if event.retry_count >= _MAX_RETRIES:
                                logger.critical("outbox_event_poison_pill", reason="max_retries_exceeded")
                                event.status = "DLQ"
                                session.add(event)
                                continue

                            try:
                                topic = event.event_type.replace(".", "-")
                                await kafka_producer.send_event(topic, payload_dict)
                                
                                # Success
                                event.status = "PROCESSED"
                                session.add(event)
                                logger.info("outbox_event_relayed", topic=topic)

                            except Exception as inner_exc:
                                # In a real implementation we would catch specific aiokafka errors:
                                # e.g. from aiokafka.errors import KafkaConnectionError, RecordTooLargeError
                                exc_name = inner_exc.__class__.__name__
                                
                                # Assume ValueError or RecordTooLargeError are terminal
                                if exc_name in ("RecordTooLargeError", "ValueError", "TypeError", "KafkaConfigurationError"):
                                    logger.critical("outbox_event_terminal_error", error=str(inner_exc), exc_type=exc_name)
                                    event.status = "DLQ"
                                else:
                                    logger.warning("outbox_event_transient_error", error=str(inner_exc), exc_type=exc_name)
                                    event.retry_count += 1
                                    # Leave status as PENDING
                                
                                session.add(event)
                                # Do NOT break the batch! Continue to the next record.

        except Exception as exc:
            logger.error("outbox_relay_poll_error", error=str(exc))
        
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


class OutboxRelayWorker:
    """Transactional Outbox relay worker that streams events to Redpanda/Kafka."""

    def __init__(
        self,
        poll_interval: int = _POLL_INTERVAL_SECONDS,
        batch_size: int = _BATCH_SIZE,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.max_retries = max_retries

    async def run(self) -> None:
        logger.info("outbox_relay_worker_starting", poll_interval=self.poll_interval)
        # Retry connecting to Redpanda/Kafka until cluster is ready
        while True:
            try:
                await kafka_producer.start()
                break
            except Exception as exc:
                logger.warning("kafka_producer_wait_retry", error=str(exc))
                await asyncio.sleep(2)

        try:
            await poll_outbox_events()
        finally:
            await kafka_producer.stop()


if __name__ == "__main__":
    worker = OutboxRelayWorker()
    asyncio.run(worker.run())
