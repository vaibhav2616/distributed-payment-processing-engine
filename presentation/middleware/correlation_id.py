"""
presentation/middleware/correlation_id.py
------------------------------------------
Correlation ID middleware for distributed trace visibility.

Behaviour:
  1. On each incoming request, reads the ``X-Correlation-ID`` header.
  2. If absent, generates a new UUID4 correlation ID.
  3. Stores the ID on ``request.state.correlation_id`` (available to
     exception handlers and route handlers).
  4. Injects the ID into the structlog ContextVar so every log line
     emitted during the request lifecycle automatically carries it.
  5. Echoes the ID back in the ``X-Correlation-ID`` response header so
     clients / API gateways can correlate logs end-to-end.

Propagation across async boundaries (Redis, Kafka):
  The ID is stored in a contextvars.ContextVar, which is correctly
  propagated by asyncio across await boundaries within the same task.
  When spawning a new asyncio.Task (e.g. outbox relay), copy the
  context explicitly:
      ctx = contextvars.copy_context()
      asyncio.get_event_loop().run_in_executor(None, ctx.run, fn)
  Or use asyncio.create_task() — it automatically copies the current
  context in Python 3.7+.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from core.logging import correlation_id_ctx

logger = structlog.get_logger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that manages the ``X-Correlation-ID`` lifecycle.

    Mount order matters — this middleware should be added BEFORE other
    middlewares so the correlation_id is available to all subsequent
    middleware and handlers.

    Example (in create_app):
        app.add_middleware(CorrelationIdMiddleware)
        app.add_middleware(IdempotencyMiddleware)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # 1. Resolve correlation ID
        correlation_id: str = (
            request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
        )

        # 2. Store on request state for handlers / exception handlers
        request.state.correlation_id = correlation_id

        # 3. Bind into structlog ContextVar — auto-injected on every log line
        token = correlation_id_ctx.set(correlation_id)

        # 4. Also bind into structlog's contextvars store (belt-and-suspenders)
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        try:
            logger.debug(
                "request_started",
                method=request.method,
                path=request.url.path,
                correlation_id=correlation_id,
            )
            response: Response = await call_next(request)
        finally:
            # 5. Reset ContextVar to avoid leaking into unrelated tasks
            correlation_id_ctx.reset(token)
            structlog.contextvars.clear_contextvars()

        # 6. Echo the correlation ID in the response headers
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
