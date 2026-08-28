"""
presentation/api/payments.py
------------------------------
Payments REST API router (presentation layer).

Pydantic request/response models live here only.
Domain entities and use-case result types are *mapped* here — never leaked.

Endpoints:
  POST /api/v1/payments/           → CreatePaymentUseCase (legacy compat)
  POST /api/v1/payments/process    → ProcessPaymentUseCase (full lifecycle)
  GET  /api/v1/payments/{id}       → retrieve by transaction_id
"""
from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from application.uow import AbstractUnitOfWork, SqlAlchemyUnitOfWork
from application.use_cases.create_payment import CreatePaymentUseCase
from application.use_cases.process_payment import (
    ProcessPaymentCommand,
    ProcessPaymentResult,
    ProcessPaymentUseCase,
)
from domain.entities.payment import PaymentAggregate
from domain.exceptions import (
    DuplicateTransactionError,
    InvalidStateTransitionError,
    PaymentGatewayError,
)
from infrastructure.cache.redis import redis_client

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/payments", tags=["Payments"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_uow() -> AbstractUnitOfWork:
    """Dependency provider for the Unit of Work."""
    return SqlAlchemyUnitOfWork()


# ---------------------------------------------------------------------------
# Pydantic schemas (presentation-layer only — zero domain leakage)
# ---------------------------------------------------------------------------


class PaymentCreateRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, description="Unique transaction identifier")
    amount: float = Field(..., gt=0, description="Payment amount — must be positive")
    currency: str = Field("INR", min_length=3, max_length=3, description="ISO 4217 currency code")


class ProcessPaymentRequest(BaseModel):
    """Request schema for the full payment processing endpoint."""

    transaction_id: str = Field(
        ...,
        min_length=1,
        description="Unique merchant transaction reference (idempotency key at the domain level).",
    )
    amount: str = Field(
        ...,
        description=(
            "Payment amount as a decimal string (e.g. '100.00') "
            "to avoid floating-point precision loss."
        ),
        examples=["100.00", "1999.99"],
    )
    currency: str = Field(
        "INR",
        min_length=3,
        max_length=3,
        description="ISO 4217 three-letter currency code.",
        examples=["INR", "USD", "EUR"],
    )


class PaymentResponse(BaseModel):
    transaction_id: str
    amount: float
    currency: str
    status: str
    created_at: datetime


class ProcessPaymentResponse(BaseModel):
    """Response schema for the full lifecycle payment processing endpoint."""

    payment_id: str = Field(..., description="Stable aggregate UUID assigned by the system.")
    transaction_id: str = Field(..., description="Merchant-provided transaction reference.")
    amount: str = Field(..., description="Settled amount as a decimal string.")
    currency: str = Field(..., description="ISO 4217 currency code.")
    status: str = Field(
        ...,
        description="Final payment status: PENDING | AUTHORIZED | CAPTURED | FAILED | REFUNDED.",
    )
    gateway_ref: str | None = Field(None, description="Acquirer reference number, if available.")
    failure_reason: str | None = Field(None, description="Human-readable failure description.")


# ---------------------------------------------------------------------------
# Endpoint: POST /api/v1/payments/  (legacy create — backward-compat)
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment (legacy)",
    description=(
        "Initiates a payment through the configured gateway. "
        "Idempotent when X-Idempotency-Key is provided. "
        "Use POST /process for the full lifecycle endpoint."
    ),
)
async def create_payment(
    payload: PaymentCreateRequest,
    uow: AbstractUnitOfWork = Depends(get_uow),
) -> PaymentResponse:
    entity = PaymentAggregate.create(
        amount=payload.amount,
        currency=payload.currency,
        transaction_id=payload.transaction_id,
    )
    use_case = CreatePaymentUseCase(uow)
    result: PaymentAggregate = await use_case.execute(entity)
    return PaymentResponse(
        transaction_id=result.transaction_id,
        amount=float(result.amount),
        currency=result.currency,
        status=result.status.value,
        created_at=result.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoint: POST /api/v1/payments/process  (full lifecycle — primary endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/process",
    response_model=ProcessPaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Process a payment (full lifecycle)",
    description=(
        "Executes the full payment lifecycle: PENDING → AUTHORIZED → CAPTURED (or FAILED). "
        "Idempotent: supply X-Idempotency-Key to safely retry on network failures. "
        "All domain events (PaymentInitiated, PaymentAuthorized, PaymentCaptured / PaymentFailed) "
        "are written to the transactional outbox and relayed to Kafka asynchronously."
    ),
    responses={
        201: {"description": "Payment processed successfully."},
        409: {"description": "Duplicate transaction_id — already processed."},
        502: {"description": "Payment gateway unavailable — all acquirers failed."},
    },
)
async def process_payment(
    payload: ProcessPaymentRequest,
    request: Request,
    uow: AbstractUnitOfWork = Depends(get_uow),
) -> ProcessPaymentResponse:
    """
    Full payment processing endpoint.

    Headers consumed:
      X-Idempotency-Key   → safe replay for the same payment attempt
      X-Correlation-ID    → distributed trace ID (injected by CorrelationIdMiddleware)
    """
    idempotency_key: str = request.headers.get("X-Idempotency-Key", "")
    correlation_id: str = getattr(request.state, "correlation_id", "")

    command = ProcessPaymentCommand(
        amount=payload.amount,
        currency=payload.currency,
        transaction_id=payload.transaction_id,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )

    use_case = ProcessPaymentUseCase(uow=uow, redis=redis_client)

    try:
        result: ProcessPaymentResult = await use_case.execute(command)
    except DuplicateTransactionError as exc:
        logger.warning(
            "api_duplicate_transaction",
            transaction_id=payload.transaction_id,
            detail=exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.detail,
        ) from exc
    except PaymentGatewayError as exc:
        logger.error(
            "api_gateway_error",
            transaction_id=payload.transaction_id,
            detail=exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.detail,
        ) from exc
    except InvalidStateTransitionError as exc:
        # Defensive: signals a domain logic bug — surface as 500
        logger.critical(
            "api_invalid_state_transition",
            transaction_id=payload.transaction_id,
            detail=exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal payment processing error. Please contact support.",
        ) from exc

    return ProcessPaymentResponse(
        payment_id=result.payment_id,
        transaction_id=result.transaction_id,
        amount=result.amount,
        currency=result.currency,
        status=result.status,
        gateway_ref=result.gateway_ref,
        failure_reason=result.failure_reason,
    )


# ---------------------------------------------------------------------------
# Endpoint: GET /api/v1/payments/{transaction_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{transaction_id}",
    response_model=ProcessPaymentResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve payment by transaction ID",
    description="Fetches the current state of a payment by its merchant transaction reference.",
    responses={
        200: {"description": "Payment found."},
        404: {"description": "Payment not found."},
    },
)
async def get_payment(
    transaction_id: str,
    uow: AbstractUnitOfWork = Depends(get_uow),
) -> ProcessPaymentResponse:
    async with uow as active_uow:
        payment: PaymentAggregate | None = await active_uow.payments.get(transaction_id)

    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment with transaction_id='{transaction_id}' not found.",
        )

    return ProcessPaymentResponse(
        payment_id=payment.payment_id,
        transaction_id=payment.transaction_id,
        amount=str(payment.amount),
        currency=payment.currency,
        status=payment.status.value,
        gateway_ref=payment.gateway_ref,
        failure_reason=payment.failure_reason,
    )
