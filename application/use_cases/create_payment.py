"""
application/use_cases/create_payment.py
----------------------------------------
CreatePayment orchestration use case.

Application-layer rules:
  - May import from domain (entities, exceptions, interfaces).
  - Must NOT import ORM models or infrastructure clients directly.
  - Infrastructure is injected via the UnitOfWork abstraction.
"""
from __future__ import annotations

import json

from domain.entities.payment import PaymentEntity, PaymentStatus
from domain.exceptions import PaymentGatewayError
from application.uow import AbstractUnitOfWork


class CreatePaymentUseCase:
    """
    Orchestrates the end-to-end payment creation flow:
      1. Delegate charge to the payment gateway (via UoW gateway port).
      2. Persist the payment record and outbox event atomically.
      3. Return the updated domain entity.
    """

    def __init__(self, uow: AbstractUnitOfWork) -> None:
        self.uow = uow

    async def execute(self, payment_data: PaymentEntity) -> PaymentEntity:
        try:
            gateway_response = await self.uow.gateway.charge_with_fallback(
                {
                    "transaction_id": payment_data.transaction_id,
                    "amount": payment_data.amount,
                    "currency": payment_data.currency,
                }
            )
        except Exception as exc:
            raise PaymentGatewayError(
                "Payment gateway unavailable.",
                detail=str(exc),
            ) from exc

        async with self.uow as uow:
            payment_data.mark_success()
            event_payload = json.dumps(
                {
                    "transaction_id": payment_data.transaction_id,
                    "amount": payment_data.amount,
                    "currency": payment_data.currency,
                    "gateway_reference": gateway_response.get("reference_id", ""),
                    "status": payment_data.status.value,
                }
            )
            await uow.payments.add(payment_data)
            await uow.outbox.enqueue(
                aggregate_type="Payment",
                aggregate_id=payment_data.transaction_id,
                event_type="payment.completed",
                payload=event_payload,
            )

        return payment_data
