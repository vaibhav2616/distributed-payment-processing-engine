import json
import logging
from aiokafka import AIOKafkaProducer

logger = logging.getLogger(__name__)

class KafkaProducerService:
    def __init__(self):
        self.producer = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers="localhost:9092",
            value_serializer=lambda v: json.dumps(v).encode("utf-8")
        )
        await self.producer.start()

    async def stop(self):
        if self.producer:
            await self.producer.stop()

    async def send_event(self, topic: str, message: dict):
        if not self.producer:
            logger.warning("Kafka producer is not initialized.")
            return
        await self.producer.send_and_wait(topic, message)

kafka_producer = KafkaProducerService()
