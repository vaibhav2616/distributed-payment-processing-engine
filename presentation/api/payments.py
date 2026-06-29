from fastapi import APIRouter
from pydantic import BaseModel, Field
from application.uow import SqlAlchemyUnitOfWork
from application.use_cases.create_payment import CreatePaymentUseCase
from domain.entities.payment import PaymentEntity

router = APIRouter(prefix="/payments", tags=["Payments"])

class PaymentCreateRequest(BaseModel):
    transaction_id: str
    amount: float = Field(gt=0)
    currency: str = "INR"

@router.post("/")
async def create_payment(payload: PaymentCreateRequest):
    uow = SqlAlchemyUnitOfWork()
    use_case = CreatePaymentUseCase(uow)
    entity = PaymentEntity(
        transaction_id=payload.transaction_id,
        amount=payload.amount,
        currency=payload.currency
    )
    result = await use_case.execute(entity)
    return {"status": "success", "data": result}
