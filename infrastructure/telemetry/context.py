"""
infrastructure/telemetry/context.py
-----------------------------------
OpenTelemetry trace context propagation and structlog binding.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-Id"


class TraceIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware to intercept incoming requests and ensure a trace ID exists.
    Binds the trace ID to structlog contextvars for the duration of the request.
    """
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id: str = request.headers.get(TRACE_ID_HEADER) or str(uuid.uuid4())
        
        # Save on request state just in case other middlewares need it
        request.state.trace_id = trace_id
        
        # Bind to structlog context variables for log propagation
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        
        try:
            response: Response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
            
        response.headers[TRACE_ID_HEADER] = trace_id
        return response


def get_current_trace_id() -> str:
    """
    Helper to extract the current trace_id from structlog contextvars.
    Returns a new UUID4 if not found (e.g. running outside request context).
    """
    ctx = structlog.contextvars.get_contextvars()
    return ctx.get("trace_id") or str(uuid.uuid4())
