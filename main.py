"""
main.py
--------
FastAPI application factory.

Middleware registration order (outermost → innermost):
  CorrelationIdMiddleware   ← sets X-Correlation-ID on every request first
  IdempotencyMiddleware     ← uses Redis; benefits from correlation tracing
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI

from core.config import settings
from core.logging import configure_logging
from presentation.api.v1.payments import router as payments_v1_router
from presentation.api.v1.webhooks import router as webhooks_v1_router
from presentation.api.v1.refunds import router as refunds_v1_router
from infrastructure.telemetry.context import TraceIdMiddleware
from presentation.middleware import (
    IdempotencyMiddleware,
    register_exception_handlers,
)
from infrastructure.messaging.kafka_producer import kafka_producer
from application.workers.outbox_relay import poll_outbox_events


def create_app() -> FastAPI:
    configure_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)

    import structlog
    logger = structlog.get_logger(__name__)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        responses={422: {"description": "Validation Error"}},
    )

    register_exception_handlers(app)

    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(TraceIdMiddleware)

    app.include_router(payments_v1_router, prefix="/api/v1")
    app.include_router(webhooks_v1_router, prefix="/api/v1")
    app.include_router(refunds_v1_router, prefix="/api/v1")

    @app.on_event("startup")
    async def startup_event() -> None:
        try:
            await kafka_producer.start()
            import os
            if os.getenv("RUN_OUTBOX_IN_PROCESS", "false").lower() == "true":
                asyncio.create_task(poll_outbox_events())
            logger.info("application_started", service=settings.PROJECT_NAME)
        except Exception as exc:
            logger.warning("infrastructure_startup_warning", error=str(exc))

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        await kafka_producer.stop()
        logger.info("application_stopped", service=settings.PROJECT_NAME)

    @app.get("/health", tags=["Health"], include_in_schema=False)
    async def health_check() -> dict:
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app


app = create_app()
