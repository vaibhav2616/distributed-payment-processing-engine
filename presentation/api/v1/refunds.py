"""
presentation/api/v1/refunds.py
------------------------------
Refunds API router for partial and full refunds.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict

from application.uow import SqlAlchemyUnitOfWork
from application.use_cases.refund_orchestrator import RefundOrchestrator
from domain.exceptions import EntityNotFoundError, InvalidRefundAmountError
from infrastructure.external.gateway_client import gateway_client, GatewayDeclineException
from presentation.api.v1.schemas import _PositiveDecimal

router = APIRouter(prefix="/payments/{payment_id}/refunds", tags=["Refunds v1"])


class RefundRequest(BaseModel):
    """
    POST /api/v1/payments/{payment_id}/refunds — request body.
    """
    model_config = ConfigDict(json_encoders={type(_PositiveDecimal): str})
    amount: _PositiveDecimal


def get_uow_factory():
    """Unit-of-work dependency factory."""
    return SqlAlchemyUnitOfWork


def get_refund_orchestrator(
    uow_factory=Depends(get_uow_factory),
) -> RefundOrchestrator:
    """Inject RefundOrchestrator use-case."""
    return RefundOrchestrator(uow_factory=uow_factory, gateway_client=gateway_client)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Refund a payment",
)
async def process_refund(
    payment_id: str,
    payload: RefundRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    orchestrator: RefundOrchestrator = Depends(get_refund_orchestrator),
) -> dict:
    """
    Process a refund against a CAPTURED or PARTIALLY_REFUNDED payment.
    """
    try:
        result = await orchestrator.process_refund(
            payment_id=payment_id,
            amount=payload.amount,
            refund_idempotency_key=idempotency_key,
        )
    except EntityNotFoundError as exc:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"error": exc.message}
    except InvalidRefundAmountError as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return {"error": exc.message, "detail": exc.detail}
    except GatewayDeclineException as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return {"error": "Gateway Decline", "detail": str(exc)}

    if result.reconciler_needed:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "15"
        return {
            "status": "processing",
            "payment_id": result.payment_id,
            "amount_refunded": str(result.amount_refunded),
            "error": result.error,
        }

    return {
        "status": result.status,
        "payment_id": result.payment_id,
        "amount_refunded": str(result.amount_refunded),
    }
