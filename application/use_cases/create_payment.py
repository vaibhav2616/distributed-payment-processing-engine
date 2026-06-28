import json
from domain.entities.payment import PaymentEntity
from application.uow import AbstractUnitOfWork
from infrastructure.database.models import PaymentModel, OutboxEventModel

class CreatePaymentUseCase:
    def __init__(self, uow: AbstractUnitOfWork):
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        async with self.uow as uow:
            db_payment = PaymentModel(
                transaction_id=payment_data.transaction_id,
                amount=payment_data.amount,
                currency=payment_data.currency,
                status="SUCCESS"
            )
            uow.session.add(db_payment)
            event_payload = {
                "transaction_id": payment_data.transaction_id,
                "amount": payment_data.amount,
                "currency": payment_data.currency,
                "status": "SUCCESS"
            }
            outbox_event = OutboxEventModel(
                aggregate_type="Payment",
                aggregate_id=payment_data.transaction_id,
                event_type="payment.completed",
                payload=json.dumps(event_payload)
            )
            uow.session.add(outbox_event)
        return payment_data
