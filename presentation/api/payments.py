"""
presentation/api/payments.py
------------------------------
Payments REST API router (presentation layer).
Pydantic models live here only — domain entities are constructed and returned.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from application.uow import AbstractUnitOfWork, SqlAlchemyUnitOfWork
from application.use_cases.create_payment import CreatePaymentUseCase
from domain.entities.payment import PaymentEntity

router = APIRouter(prefix="/payments", tags=["Payments"])


def get_uow() -> AbstractUnitOfWork:
    """Dependency provider for the Unit of Work."""
    return SqlAlchemyUnitOfWork()


class PaymentCreateRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, description="Unique transaction identifier")
    amount: float = Field(..., gt=0, description="Payment amount — must be positive")
    currency: str = Field("INR", min_length=3, max_length=3, description="ISO 4217 currency code")


class PaymentResponse(BaseModel):
    transaction_id: str
    amount: float
    currency: str
    status: str
    created_at: datetime


@router.post(
    "/",
    response_model=PaymentResponse,
    status_code=201,
    summary="Create a payment",
    description="Initiates a payment transaction through the configured gateway. Idempotent when X-Idempotency-Key is provided.",
)
async def create_payment(
    payload: PaymentCreateRequest,
    uow: AbstractUnitOfWork = Depends(get_uow),
) -> PaymentResponse:
    entity = PaymentEntity(
        transaction_id=payload.transaction_id,
        amount=payload.amount,
        currency=payload.currency,
    )
    use_case = CreatePaymentUseCase(uow)
    result: PaymentEntity = await use_case.execute(entity)

    return PaymentResponse(
        transaction_id=result.transaction_id,
        amount=result.amount,
        currency=result.currency,
        status=result.status.value,
        created_at=result.created_at,
    )
