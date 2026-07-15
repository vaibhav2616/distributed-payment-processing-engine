"""
presentation/middleware/correlation_id.py
------------------------------------------
Correlation ID middleware for distributed trace visibility.
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
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id: str = (
            request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
        )
        request.state.correlation_id = correlation_id
        token = correlation_id_ctx.set(correlation_id)
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
            correlation_id_ctx.reset(token)
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
