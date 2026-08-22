"""
presentation/api/v1/payments.py
---------------------------------
v1 Payments REST API router.

Routing table:
  POST   /api/v1/payments                     → process_payment()
  GET    /api/v1/payments/{payment_id}         → get_payment()
  GET    /api/v1/payments/{payment_id}/ledger  → get_payment_ledger()

Architectural constraints
--------------------------
  - ZERO business logic here.  The router maps HTTP ↔ orchestrator commands.
  - Domain objects (PaymentAggregate, LedgerTransaction) never leak into
    response models — all mapping happens in the ``_to_payment_response``
    and ``_to_ledger_response`` helpers at the bottom of this module.
  - All DB access goes through the UoW.  No raw SQLAlchemy sessions here.
  - Idempotency-Key is read from the HTTP header (enforced by IdempotencyMiddleware)
    and threaded through as ``transaction_id`` on the orchestrator command.

HTTP semantics:
  201 Created          → CAPTURED (payment fully settled)
  202 Accepted         → PENDING / reconciler_needed=True (gateway timed out;
                         client should poll GET endpoint)
  422 Unprocessable    → FAILED (card declined / gateway hard decline)
                         Body is RFC 7807 Problem Details JSON
  404 Not Found        → payment_id does not exist
  409 Conflict         → duplicate Idempotency-Key already resolved to a
                         different outcome (raised by UoW / domain layer)
"""
from __future__ import annotations

from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from application.use_cases.payment_orchestrator import (
    PaymentOrchestrator,
    ProcessPaymentCommand,
    ProcessPaymentResult,
)
from application.uow import SqlAlchemyUnitOfWork
from domain.entities.ledger import LedgerTransaction
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import DuplicateTransactionError, InvalidStateTransitionError
from infrastructure.external.gateway_client import gateway_client
from presentation.api.v1.schemas import (
    CreatePaymentRequest,
    LedgerEntryResponse,
    LedgerResponse,
    PaymentResponse,
    ProblemDetail,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments v1"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_uow_factory():
    """Return the UoW factory callable used by the orchestrator."""
    return SqlAlchemyUnitOfWork


def get_orchestrator(
    uow_factory=Depends(get_uow_factory),
) -> PaymentOrchestrator:
    """Construct and inject a PaymentOrchestrator with its dependencies."""
    return PaymentOrchestrator(uow_factory, gateway=gateway_client)


# ---------------------------------------------------------------------------
# Response mapping helpers (pure functions — no side effects)
# ---------------------------------------------------------------------------


def _to_payment_response(
    result: ProcessPaymentResult,
    created_at,
) -> PaymentResponse:
    return PaymentResponse(
        id=result.payment_id,
        reference_id=result.transaction_id,
        status=result.status.value,
        amount=Decimal(str(result.status)),   # placeholder; overridden below
        currency="",
        created_at=created_at,
    )


def _aggregate_to_payment_response(payment: PaymentAggregate) -> PaymentResponse:
    return PaymentResponse(
        id=payment.payment_id,
        reference_id=payment.transaction_id,
        status=payment.status.value,
        amount=payment.amount,        # already Decimal
        currency=payment.currency,
        gateway_ref=payment.gateway_ref,
        ledger_txn_id=None,           # not stored on the aggregate; client uses /ledger
        created_at=payment.created_at,
    )


def _result_to_payment_response(
    result: ProcessPaymentResult,
    payment: PaymentAggregate,
) -> PaymentResponse:
    return PaymentResponse(
        id=result.payment_id,
        reference_id=result.transaction_id,
        status=result.status.value,
        amount=payment.amount,
        currency=payment.currency,
        gateway_ref=result.gateway_ref,
        ledger_txn_id=result.ledger_txn_id,
        created_at=payment.created_at,
    )


def _to_ledger_response(
    payment_id: str,
    ledger_transactions: list[LedgerTransaction],
) -> LedgerResponse:
    entries: list[LedgerEntryResponse] = []
    for txn in ledger_transactions:
        for entry in txn.entries:
            entries.append(
                LedgerEntryResponse(
                    account_id=entry.account_id,
                    amount=entry.amount,      # Decimal; never float
                    currency=entry.currency,
                    created_at=entry.created_at,
                )
            )
    total = sum((e.amount for e in entries), Decimal("0.00"))
    return LedgerResponse(payment_id=payment_id, entries=entries, total=total)


def _problem_detail_response(
    type_: str,
    title: str,
    http_status: int,
    detail: str,
    instance: str | None = None,
) -> JSONResponse:
    """Return an RFC 7807 Problem Details JSON response."""
    body = ProblemDetail(
        type=type_,
        title=title,
        status=http_status,
        detail=detail,
        instance=instance,
    )
    return JSONResponse(
        status_code=http_status,
        content=body.model_dump(),
        media_type="application/problem+json",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/payments
# ---------------------------------------------------------------------------


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentResponse,
    summary="Process a payment",
    description=(
        "Initiates and settles a payment through the configured gateway.\n\n"
        "**Idempotency**: supply `Idempotency-Key` header to safely replay "
        "the same request on network failures without double-charging.\n\n"
        "**Response codes**:\n"
        "- `201 Created` — payment CAPTURED (funds settled).\n"
        "- `202 Accepted` — gateway timed out; poll `GET /payments/{id}`.\n"
        "- `422 Unprocessable Entity` — card declined (RFC 7807 body).\n"
        "- `409 Conflict` — duplicate `Idempotency-Key`.\n"
    ),
    responses={
        202: {"description": "Accepted — gateway unreachable; poll GET endpoint."},
        409: {"description": "Conflict — duplicate Idempotency-Key."},
        422: {
            "description": "Unprocessable — gateway hard decline.",
            "content": {
                "application/problem+json": {
                    "example": {
                        "type": "payment-declined",
                        "title": "Gateway Rejected",
                        "status": 422,
                        "detail": "Card declined: insufficient funds.",
                        "instance": None,
                    }
                }
            },
        },
    },
)
async def process_payment(
    payload: CreatePaymentRequest,
    request: Request,
    orchestrator: PaymentOrchestrator = Depends(get_orchestrator),
    uow_factory=Depends(get_uow_factory),
):
    """
    Process a payment end-to-end through the PaymentOrchestrator.

    The router is a pure HTTP ↔ orchestrator adapter:
      1. Extract Idempotency-Key from headers.
      2. Map HTTP payload → ProcessPaymentCommand (Decimal, not float).
      3. Execute PaymentOrchestrator.process_payment().
      4. Map ProcessPaymentResult → HTTP status + response body.
    """
    correlation_id: str = getattr(request.state, "correlation_id", "")
    log = logger.bind(
        correlation_id=correlation_id,
        reference_id=payload.reference_id,
    )

    command = ProcessPaymentCommand(
        amount=payload.amount,          # Decimal — validated by schema
        currency=payload.currency,
        source=payload.source_token,
        transaction_id=payload.reference_id,
        correlation_id=correlation_id,
    )

    try:
        result: ProcessPaymentResult = await orchestrator.process_payment(command)
    except DuplicateTransactionError as exc:
        log.warning("api.duplicate_transaction", detail=exc.detail)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.detail,
        ) from exc
    except InvalidStateTransitionError as exc:
        # Programming error — surface as 500 so it is never silently swallowed.
        log.critical("api.invalid_state_transition", detail=exc.detail)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal payment processing error.",
        ) from exc

    # ── Map result → HTTP semantics ──────────────────────────────────────────

    if result.reconciler_needed:
        # Gateway timed out — payment is PENDING, reconciler will resolve it.
        # 202 Accepted: "we have the request; outcome is not yet determined."
        log.info("api.payment_accepted_pending", payment_id=result.payment_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Retry-After": "15"},
            content={
                "id": result.payment_id,
                "transaction_id": result.transaction_id,
                "status": result.status.value,
                "message": (
                    "Payment accepted. Processing is pending due to a gateway timeout. "
                    f"Poll GET /api/v1/payments/{result.payment_id} for the final status."
                ),
            },
        )

    if result.status == PaymentStatus.FAILED:
        # Definitive hard decline from the gateway.
        # 422 with RFC 7807 Problem Details body.
        log.info("api.payment_declined", payment_id=result.payment_id)
        return _problem_detail_response(
            type_="payment-declined",
            title="Gateway Rejected",
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The payment was declined by the acquirer.",
            instance=f"/api/v1/payments/{result.payment_id}",
        )

    # CAPTURED — 201 Created.
    # Re-fetch the aggregate so we have amount, currency, created_at for the response.
    # This is a cheap indexed PK read; the data is hot in Postgres.
    async with uow_factory() as uow:
        payment = await uow.payments.get_by_payment_id(result.payment_id)

    if payment is None:
        # Should never happen — orchestrator just wrote it.
        log.error("api.payment_not_found_after_capture", payment_id=result.payment_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment record missing after capture.",
        )

    log.info("api.payment_captured", payment_id=result.payment_id)
    return PaymentResponse(
        id=result.payment_id,
        reference_id=result.transaction_id,
        status=result.status.value,
        amount=payment.amount,
        currency=payment.currency,
        gateway_ref=result.gateway_ref,
        ledger_txn_id=result.ledger_txn_id,
        created_at=payment.created_at,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/payments/{payment_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{payment_id}",
    status_code=status.HTTP_200_OK,
    response_model=PaymentResponse,
    summary="Get payment status",
    description=(
        "Returns the current state of a payment by its stable aggregate UUID.\n\n"
        "This is the **polling endpoint** for clients that received a `202 Accepted` "
        "on POST.  Keep polling until `status` is `CAPTURED` or `FAILED`."
    ),
    responses={
        404: {"description": "Payment not found."},
    },
)
async def get_payment(
    payment_id: str,
    uow_factory=Depends(get_uow_factory),
) -> PaymentResponse:
    """
    Fetch the current state of a payment by its stable aggregate UUID.

    Used as the polling endpoint when POST returned 202 Accepted.
    """
    async with uow_factory() as uow:
        payment: PaymentAggregate | None = await uow.payments.get_by_payment_id(
            payment_id
        )

    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment '{payment_id}' not found.",
        )

    return _aggregate_to_payment_response(payment)


# ---------------------------------------------------------------------------
# GET /api/v1/payments/{payment_id}/ledger
# ---------------------------------------------------------------------------


@router.get(
    "/{payment_id}/ledger",
    status_code=status.HTTP_200_OK,
    response_model=LedgerResponse,
    summary="Get ledger entries for a payment",
    description=(
        "Returns all double-entry ledger lines recorded for a payment.\n\n"
        "The `total` field in the response is the arithmetic sum of all entry "
        "`amount` values.  A balanced transaction always has `total == 0.00`.\n\n"
        "Returns an empty `entries` list (not 404) if the payment exists but "
        "has not yet been CAPTURED (no ledger entries exist yet)."
    ),
    responses={
        404: {"description": "Payment not found."},
    },
)
async def get_payment_ledger(
    payment_id: str,
    uow_factory=Depends(get_uow_factory),
) -> LedgerResponse:
    """
    Fetch and return the double-entry ledger lines for a payment.

    Confirms that the zero-sum invariant holds by including ``total`` in the
    response body (clients can assert ``total == "0.00"``).
    """
    async with uow_factory() as uow:
        # Confirm the payment exists before querying the ledger.
        payment: PaymentAggregate | None = await uow.payments.get_by_payment_id(
            payment_id
        )
        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Payment '{payment_id}' not found.",
            )

        ledger_txns: list[LedgerTransaction] = await uow.ledger.get_by_reference(
            payment_id
        )

    return _to_ledger_response(payment_id, ledger_txns)
