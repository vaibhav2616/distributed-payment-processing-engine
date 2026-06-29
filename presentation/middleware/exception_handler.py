"""
presentation/middleware/exception_handler.py
---------------------------------------------
RFC 7807 Problem Details — global exception handler.

Registers on the FastAPI application and translates every exception class
into a uniform Problem Details JSON response.

Spec reference: https://www.rfc-editor.org/rfc/rfc7807

Response shape:
  {
    "type":       "https://<base>/errors/<slug>",
    "title":      "Human-readable summary",
    "status":     <HTTP status code>,
    "detail":     "Contextual explanation",
    "instance":   "/request/path",
    "correlation_id": "<X-Correlation-ID value>"  # extension field
  }

Content-Type: application/problem+json
"""
from __future__ import annotations

import traceback
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette import status as http_status

HTTP_400_BAD_REQUEST = http_status.HTTP_400_BAD_REQUEST
HTTP_404_NOT_FOUND = http_status.HTTP_404_NOT_FOUND
HTTP_409_CONFLICT = http_status.HTTP_409_CONFLICT
try:
    # Starlette ≥ 0.36 renamed the constant; avoid triggering the deprecation
    # warning by preferring the new name and only falling back via an integer.
    HTTP_422_UNPROCESSABLE_ENTITY: int = http_status.HTTP_422_UNPROCESSABLE_CONTENT  # type: ignore[attr-defined]
except AttributeError:
    HTTP_422_UNPROCESSABLE_ENTITY = 422
HTTP_500_INTERNAL_SERVER_ERROR = http_status.HTTP_500_INTERNAL_SERVER_ERROR
HTTP_502_BAD_GATEWAY = http_status.HTTP_502_BAD_GATEWAY
HTTP_503_SERVICE_UNAVAILABLE = http_status.HTTP_503_SERVICE_UNAVAILABLE

from domain.exceptions import (
    DomainException,
    DuplicateTransactionError,
    EntityNotFoundError,
    ExternalServiceError,
    MessagingError,
    PaymentGatewayError,
    ValidationError as DomainValidationError,
)

logger = structlog.get_logger(__name__)

_TYPE_BASE = "https://api.example.com/errors"


def _problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    type_slug: str = "internal-error",
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build an RFC 7807-compliant Problem Details response."""
    correlation_id: str = getattr(request.state, "correlation_id", "")
    body: dict[str, Any] = {
        "type": f"{_TYPE_BASE}/{type_slug}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if correlation_id:
        body["correlation_id"] = correlation_id
    if extra:
        body.update(extra)

    return JSONResponse(
        content=body,
        status_code=status,
        media_type="application/problem+json",
    )


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

async def _handle_domain_validation_error(
    request: Request, exc: DomainValidationError
) -> JSONResponse:
    logger.warning("domain_validation_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_400_BAD_REQUEST,
        title="Domain Validation Error",
        detail=exc.detail,
        type_slug="domain-validation-error",
    )


async def _handle_duplicate_transaction(
    request: Request, exc: DuplicateTransactionError
) -> JSONResponse:
    logger.warning("duplicate_transaction_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_409_CONFLICT,
        title="Duplicate Transaction",
        detail=exc.detail,
        type_slug="duplicate-transaction",
    )


async def _handle_entity_not_found(
    request: Request, exc: EntityNotFoundError
) -> JSONResponse:
    logger.info(
        "entity_not_found",
        entity_type=exc.entity_type,
        identifier=exc.identifier,
    )
    return _problem_response(
        request,
        status=HTTP_404_NOT_FOUND,
        title="Entity Not Found",
        detail=exc.detail,
        type_slug="entity-not-found",
    )


async def _handle_payment_gateway_error(
    request: Request, exc: PaymentGatewayError
) -> JSONResponse:
    logger.error("payment_gateway_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_502_BAD_GATEWAY,
        title="Payment Gateway Unavailable",
        detail=exc.detail,
        type_slug="payment-gateway-error",
    )


async def _handle_external_service_error(
    request: Request, exc: ExternalServiceError
) -> JSONResponse:
    logger.error("external_service_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_503_SERVICE_UNAVAILABLE,
        title="External Service Unavailable",
        detail=exc.detail,
        type_slug="external-service-error",
    )


async def _handle_messaging_error(
    request: Request, exc: MessagingError
) -> JSONResponse:
    logger.error("messaging_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_503_SERVICE_UNAVAILABLE,
        title="Messaging Service Error",
        detail=exc.detail,
        type_slug="messaging-error",
    )


async def _handle_domain_exception(
    request: Request, exc: DomainException
) -> JSONResponse:
    """Catch-all for any unclassified DomainException subclass."""
    logger.error("unclassified_domain_error", detail=exc.detail)
    return _problem_response(
        request,
        status=HTTP_400_BAD_REQUEST,
        title="Business Rule Violation",
        detail=exc.detail,
        type_slug="business-rule-violation",
    )


async def _handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI/Pydantic request body validation failures → 422."""
    errors = exc.errors()
    detail = "; ".join(
        f"{' → '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
        for e in errors
    )
    logger.warning("request_validation_error", errors=errors)
    return _problem_response(
        request,
        status=HTTP_422_UNPROCESSABLE_ENTITY,
        title="Request Validation Failed",
        detail=detail,
        type_slug="request-validation-error",
        extra={"validation_errors": errors},
    )


async def _handle_pydantic_validation_error(
    request: Request, exc: PydanticValidationError
) -> JSONResponse:
    errors = exc.errors()
    detail = "; ".join(
        f"{' → '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
        for e in errors
    )
    logger.warning("pydantic_validation_error", errors=errors)
    return _problem_response(
        request,
        status=HTTP_422_UNPROCESSABLE_ENTITY,
        title="Data Validation Failed",
        detail=detail,
        type_slug="data-validation-error",
        extra={"validation_errors": errors},
    )


async def _handle_unhandled_exception(
    request: Request, exc: Exception
) -> JSONResponse:
    """Last-resort handler — never leaks internal details to the client."""
    logger.error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        traceback=traceback.format_exc(),
    )
    return _problem_response(
        request,
        status=HTTP_500_INTERNAL_SERVER_ERROR,
        title="Internal Server Error",
        detail="An unexpected error occurred. Please try again later.",
        type_slug="internal-server-error",
    )


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_exception_handlers(app: FastAPI) -> None:
    """
    Attach all RFC 7807 exception handlers to the FastAPI application.
    Call this in create_app() before including routers.

    Handler precedence (most-specific first):
      DuplicateTransactionError  →  409
      EntityNotFoundError        →  404
      DomainValidationError      →  400
      PaymentGatewayError        →  502
      MessagingError             →  503
      ExternalServiceError       →  503
      DomainException (base)     →  400
      RequestValidationError     →  422
      PydanticValidationError    →  422
      Exception (catch-all)      →  500
    """
    app.add_exception_handler(DuplicateTransactionError, _handle_duplicate_transaction)   # type: ignore[arg-type]
    app.add_exception_handler(EntityNotFoundError, _handle_entity_not_found)              # type: ignore[arg-type]
    app.add_exception_handler(DomainValidationError, _handle_domain_validation_error)     # type: ignore[arg-type]
    app.add_exception_handler(PaymentGatewayError, _handle_payment_gateway_error)         # type: ignore[arg-type]
    app.add_exception_handler(MessagingError, _handle_messaging_error)                    # type: ignore[arg-type]
    app.add_exception_handler(ExternalServiceError, _handle_external_service_error)       # type: ignore[arg-type]
    app.add_exception_handler(DomainException, _handle_domain_exception)                  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)   # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, _handle_pydantic_validation_error) # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unhandled_exception)                     # type: ignore[arg-type]
