import asyncio
from fastapi import FastAPI
from core.config import settings
from presentation.api.payments import router as payments_router
from presentation.middleware.idempotency import IdempotencyMiddleware
from infrastructure.messaging.kafka_producer import kafka_producer
from infrastructure.workers.outbox_relay import poll_outbox_events

def create_app() -> FastAPI:
    app = FastAPI(title=settings.PROJECT_NAME, version="1.0.0")
    app.add_middleware(IdempotencyMiddleware)
    app.include_router(payments_router)

    @app.on_event("startup")
    async def startup_event():
        try:
            await kafka_producer.start()
            asyncio.create_task(poll_outbox_events())
        except Exception as e:
            print(f"Infrastructure startup warning: {e}")

    @app.on_event("shutdown")
    async def shutdown_event():
        await kafka_producer.stop()

    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app

app = create_app()
