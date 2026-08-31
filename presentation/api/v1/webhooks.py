"""
presentation/api/v1/webhooks.py
-------------------------------
Webhook receiver API router for asynchronous payment gateway notifications.

Endpoints:
  POST /api/v1/webhooks/gateway
    - Authenticated via HMAC SHA-256 (verify_webhook_signature)
    - Concurrency-safe: SELECT FOR UPDATE NOWAIT
    - Resolves payments to CAPTURED or FAILED
    - Returns 200 OK on success or if already resolved
    - Returns 409 Conflict if row is locked (signals gateway to retry)
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from application.uow import SqlAlchemyUnitOfWork
from application.use_cases.webhook_orchestrator import (
    WebhookOrchestrator,
    WebhookResult,
)
from domain.exceptions import ConcurrentUpdateException, EntityNotFoundError
from presentation.api.v1.dependencies import verify_webhook_signature

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks v1"])


class GatewayWebhookRequest(BaseModel):
    """Schema for inbound payment gateway webhook payload."""
    model_config = ConfigDict(extra="ignore")

    reference_id: str = Field(
        ...,
        description="Gateway reference or payment reference ID",
        examples=["gw_ref_12345", "pay_uuid_67890"],
    )
    status: str = Field(
        ...,
        description="Outcome of the transaction: CAPTURED or FAILED",
        examples=["CAPTURED", "FAILED"],
    )


def get_uow_factory():
    """Unit-of-work dependency factory."""
    return SqlAlchemyUnitOfWork


def get_webhook_orchestrator(
    uow_factory=Depends(get_uow_factory),
) -> WebhookOrchestrator:
    """Inject WebhookOrchestrator use-case."""
    return WebhookOrchestrator(uow_factory=uow_factory)


@router.post(
    "/gateway",
    status_code=status.HTTP_200_OK,
    summary="Receive gateway webhook",
    description=(
        "Asynchronous callback endpoint for payment gateways.\n\n"
        "Security: Protected by HMAC SHA-256 signature verification.\n"
        "Concurrency: Uses SELECT FOR UPDATE NOWAIT.\n"
        "Returns 200 OK on success or if already resolved.\n"
        "Returns 409 Conflict if row is currently locked (signals gateway to retry)."
    ),
    responses={
        200: {"description": "Webhook processed successfully or already resolved."},
        401: {"description": "Unauthorized — invalid or missing X-Gateway-Signature."},
        404: {"description": "Payment not found matching reference_id."},
        409: {"description": "Conflict — payment is currently locked by a concurrent process."},
    },
)
async def process_gateway_webhook(
    payload: GatewayWebhookRequest,
    _sig_verified: bytes = Depends(verify_webhook_signature),
    orchestrator: WebhookOrchestrator = Depends(get_webhook_orchestrator),
) -> JSONResponse:
    """
    Handle inbound gateway webhook callback.
    """
    log = logger.bind(reference_id=payload.reference_id, status=payload.status)
    log.info("api.webhook_received")

    try:
        result: WebhookResult = await orchestrator.process_webhook(
            reference_id=payload.reference_id,
            status=payload.status,
        )
        log.info(
            "api.webhook_processed",
            payment_id=result.payment_id,
            already_resolved=result.already_resolved,
            split_brain=result.split_brain,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ok",
                "payment_id": result.payment_id,
                "already_resolved": result.already_resolved,
                "split_brain": result.split_brain,
            },
        )

    except ConcurrentUpdateException as exc:
        # Row is currently locked by another worker (e.g. Reconciler or Orchestrator).
        # Return 409 Conflict so the gateway backsoff and retries the webhook later.
        log.warning("api.webhook_concurrent_conflict", detail=str(exc))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "ConcurrentUpdateConflict",
                "detail": "Payment record is currently locked by another process. Please retry.",
            },
        )

    except EntityNotFoundError as exc:
        log.warning("api.webhook_entity_not_found", detail=exc.detail)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.detail,
        ) from exc
