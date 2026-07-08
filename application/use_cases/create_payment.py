import json
from domain.entities.payment import PaymentEntity
from application.uow import AbstractUnitOfWork
from infrastructure.database.models import PaymentModel, OutboxEventModel
from infrastructure.external.gateway_client import gateway_client

class CreatePaymentUseCase:
    def __init__(self, uow: AbstractUnitOfWork):
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        gateway_response = await gateway_client.charge_with_fallback({
            "transaction_id": payment_data.transaction_id,
            "amount": payment_data.amount,
            "currency": payment_data.currency
        })
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
                "gateway_reference": gateway_response.get("reference_id", "mock_ref"),
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
