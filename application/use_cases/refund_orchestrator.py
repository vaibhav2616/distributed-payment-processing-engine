"""
application/use_cases/refund_orchestrator.py
--------------------------------------------
RefundOrchestrator handles safe, idempotent, partial or full refunds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import structlog

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.entities.payment import PaymentAggregate
from domain.exceptions import EntityNotFoundError, InvalidRefundAmountError
from infrastructure.external.circuit_breaker import CircuitBreakerOpenException
from infrastructure.external.gateway_client import (
    PaymentGatewayClient,
    PaymentGatewayException,
    GatewayDeclineException,
)
from infrastructure.telemetry.context import get_current_trace_id

try:
    import httpx
    _NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = ()  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

_ACCOUNT_RECEIVABLE = "1100.ACCOUNTS_RECEIVABLE"
_ACCOUNT_GW_PAYABLE = "2100.GATEWAY_PAYABLE"


@dataclass(frozen=True)
class RefundResult:
    """Result of processing a refund."""
    payment_id: str
    status: str
    amount_refunded: Decimal
    reconciler_needed: bool = False
    error: str | None = None


class RefundOrchestrator:
    """
    Application-layer use case for processing a payment refund end-to-end.
    """

    def __init__(
        self,
        uow_factory: Callable[[], Any],
        gateway_client: PaymentGatewayClient | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        if gateway_client is None:
            from infrastructure.external.gateway_client import gateway_client as default_gateway
            self._gateway = default_gateway
        else:
            self._gateway = gateway_client

    async def process_refund(
        self,
        payment_id: str,
        amount: Decimal | float | str,
        refund_idempotency_key: str,
    ) -> RefundResult:
        """
        Process a refund across three phases.
        """
        log = logger.bind(payment_id=payment_id, refund_amount=str(amount))
        log.info("refund.process_start")

        amount_decimal = Decimal(str(amount)).quantize(Decimal("0.01"))

        # Phase 1 — Local State Initialisation
        async with self._uow_factory() as uow:
            await uow.begin()
            payment: PaymentAggregate | None = await uow.payments.lock_by_id(payment_id)

            if not payment:
                raise EntityNotFoundError("Payment", payment_id)

            # process_refund throws InvalidRefundAmountError or InvalidStateTransitionError
            payment.process_refund(amount_decimal)

            await uow.payments.update(payment)
            
            await uow.outbox.enqueue(
                aggregate_type="Payment",
                aggregate_id=payment.payment_id,
                event_type="payment.refund_pending",
                payload=json.dumps({
                    "payment_id": payment.payment_id,
                    "amount": str(amount_decimal),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "trace_id": get_current_trace_id(),
                }),
            )
            await uow.commit()

        # Phase 2 — Network Call
        try:
            gateway_response = await self._gateway.refund(
                reference_id=payment.gateway_ref or payment.transaction_id,
                amount=str(amount_decimal),
                idempotency_key=refund_idempotency_key,
            )
        except GatewayDeclineException as exc:
            log.error("refund.gateway_declined", error=str(exc))
            async with self._uow_factory() as uow:
                await uow.begin()
                payment = await uow.payments.lock_by_id(payment_id)
                if not payment:
                    raise EntityNotFoundError("Payment", payment_id)

                payment.fail_refund(amount_decimal)
                await uow.payments.update(payment)
                
                await uow.outbox.enqueue(
                    aggregate_type="Payment",
                    aggregate_id=payment.payment_id,
                    event_type="payment.refund_failed",
                    payload=json.dumps({
                        "payment_id": payment.payment_id,
                        "amount": str(amount_decimal),
                        "currency": payment.currency,
                        "status": payment.status.value,
                        "trace_id": get_current_trace_id(),
                        "error": str(exc),
                    }),
                )
                await uow.commit()
            
            raise  # Re-raise to be handled by the API layer

        except (PaymentGatewayException, CircuitBreakerOpenException) + _NETWORK_ERRORS as exc:
            log.warning(
                "refund.gateway_timeout_or_error",
                error=str(exc),
                reconciler_needed=True,
            )
            return RefundResult(
                payment_id=payment.payment_id,
                status=payment.status.value,
                amount_refunded=payment.amount_refunded,
                reconciler_needed=True,
            )

        # Phase 3 — Local State Finalisation
        async with self._uow_factory() as uow:
            await uow.begin()
            payment = await uow.payments.lock_by_id(payment_id)
            if not payment:
                raise EntityNotFoundError("Payment", payment_id)

            ledger_txn = LedgerTransaction.build(
                reference=f"REFUND-{refund_idempotency_key}",
                description=f"Refund against payment {payment.payment_id}",
                entries=[
                    LedgerEntry.debit(
                        transaction_id=payment.payment_id,
                        account_id=_ACCOUNT_GW_PAYABLE,
                        amount=amount_decimal,
                        currency=payment.currency,
                    ),
                    LedgerEntry.credit(
                        transaction_id=payment.payment_id,
                        account_id=_ACCOUNT_RECEIVABLE,
                        amount=amount_decimal,
                        currency=payment.currency,
                    ),
                ],
            )

            await uow.ledger.add(ledger_txn)
            await uow.outbox.enqueue(
                aggregate_type="Payment",
                aggregate_id=payment.payment_id,
                event_type="payment.refund_completed",
                payload=json.dumps({
                    "payment_id": payment.payment_id,
                    "amount": str(amount_decimal),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "trace_id": get_current_trace_id(),
                }),
            )
            await uow.commit()

        log.info(
            "refund.process_complete",
            payment_id=payment.payment_id,
            total_refunded=str(payment.amount_refunded),
        )

        return RefundResult(
            payment_id=payment.payment_id,
            status=payment.status.value,
            amount_refunded=payment.amount_refunded,
            reconciler_needed=False,
        )
