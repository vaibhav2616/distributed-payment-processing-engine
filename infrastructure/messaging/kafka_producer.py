"""
infrastructure/messaging/kafka_producer.py
-------------------------------------------
Async Kafka producer service — uses structlog for structured logging.
"""
from __future__ import annotations

import json

import structlog
from aiokafka import AIOKafkaProducer

from core.config import settings

logger = structlog.get_logger(__name__)


class KafkaProducerService:
    def __init__(self) -> None:
        self.producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        await self.producer.start()
        logger.info("kafka_producer_started", servers=settings.KAFKA_BOOTSTRAP_SERVERS)

    async def stop(self) -> None:
        if self.producer:
            await self.producer.stop()
            logger.info("kafka_producer_stopped")

    async def send_event(self, topic: str, message: dict) -> None:
        if not self.producer:
            logger.warning("kafka_producer_not_initialized", topic=topic)
            return
        await self.producer.send_and_wait(topic, message)
        logger.debug("kafka_event_sent", topic=topic)


kafka_producer = KafkaProducerService()
