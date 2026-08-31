"""
application/use_cases/webhook_orchestrator.py
---------------------------------------------
WebhookOrchestrator — processes asynchronous callback events from payment gateways.

Guarantees & Invariants:
  - Strict Concurrency Control: Uses SELECT FOR UPDATE NOWAIT to serialize state mutations.
    If row is locked, immediately raises ConcurrentUpdateException.
  - Stale-State Guard: If payment was already resolved by the Reconciler (no longer PENDING),
    silently returns success (idempotent no-op).
  - Double-Entry Accounting: Generates a balanced zero-sum LedgerTransaction when CAPTURED.
  - Outbox Pattern: Enqueues domain events with correlation trace_id and zero PII.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import structlog

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import ConcurrentUpdateException, EntityNotFoundError
from infrastructure.telemetry.context import get_current_trace_id

logger = structlog.get_logger(__name__)

# Nominal ledger accounts — consistent with payment_orchestrator.py and reconciler.py
_ACCOUNT_RECEIVABLE = "1100.ACCOUNTS_RECEIVABLE"
_ACCOUNT_GW_PAYABLE = "2100.GATEWAY_PAYABLE"


@dataclass(frozen=True)
class WebhookResult:
    """Result of processing a gateway webhook."""
    payment_id: str
    status: PaymentStatus
    already_resolved: bool = False
    split_brain: bool = False


class WebhookOrchestrator:
    """
    Application-layer use case for asynchronously resolving payments via webhooks.

    Args:
        uow_factory: AbstractUnitOfWork factory callable.
    """

    def __init__(self, uow_factory: Callable[[], Any]) -> None:
        self._uow_factory = uow_factory

    async def process_webhook(
        self,
        reference_id: str,
        status: str | PaymentStatus,
    ) -> WebhookResult:
        """
        Process an asynchronous gateway callback with SELECT FOR UPDATE NOWAIT.

        Args:
            reference_id: External gateway identifier (or payment reference).
            status: Final status reported by the acquirer ('CAPTURED' or 'FAILED').

        Returns:
            WebhookResult indicating outcome and whether payment was already resolved.

        Raises:
            ConcurrentUpdateException: If the row is currently locked by another process.
            EntityNotFoundError: If no payment matches reference_id.
        """
        log = logger.bind(reference_id=reference_id, incoming_status=str(status))
        log.info("webhook.process_start")

        # Normalize incoming status
        status_str = status.value if isinstance(status, PaymentStatus) else str(status).upper()
        if status_str in ("CAPTURED", "SUCCESS", "SUCCEEDED", "COMPLETED"):
            target_status = PaymentStatus.CAPTURED
        else:
            target_status = PaymentStatus.FAILED

        async with self._uow_factory() as uow:
            await uow.begin()

            # Concurrency Control (CRITICAL): SELECT FOR UPDATE NOWAIT
            # Raises ConcurrentUpdateException if the row is currently locked.
            payment: PaymentAggregate | None = await uow.payments.lock_by_reference_id(
                reference_id
            )

            if payment is None:
                log.warning("webhook.payment_not_found")
                raise EntityNotFoundError("Payment", reference_id)

            # State Verification & Distributed Split-Brain Guard:
            # If row is found but no longer PENDING, compare DB status with incoming webhook status.
            if payment.status != PaymentStatus.PENDING:
                if payment.status == target_status:
                    # True idempotency: already resolved to the same status
                    log.info(
                        "webhook.idempotent_duplicate",
                        payment_id=payment.payment_id,
                        db_status=payment.status.value,
                        webhook_status=target_status.value,
                    )
                    await uow.rollback()
                    return WebhookResult(
                        payment_id=payment.payment_id,
                        status=payment.status,
                        already_resolved=True,
                        split_brain=False,
                    )
                else:
                    # Distributed Split-Brain: payment.status != webhook_status
                    # Do not apply the transition, but do not fail silently.
                    # Explicitly log a CRITICAL security alert with payment_id, db_status, and webhook_status.
                    log.critical(
                        "security_alert.distributed_split_brain",
                        payment_id=payment.payment_id,
                        db_status=payment.status.value,
                        webhook_status=target_status.value,
                        detail=(
                            f"Distributed Split-Brain detected: payment {payment.payment_id} has "
                            f"db_status='{payment.status.value}' but incoming webhook reported "
                            f"webhook_status='{target_status.value}'. Transition aborted. "
                            "Immediate manual engineering review required."
                        ),
                    )
                    await uow.rollback()
                    return WebhookResult(
                        payment_id=payment.payment_id,
                        status=payment.status,
                        already_resolved=True,
                        split_brain=True,
                    )

            # State Transition & Domain Updates
            if target_status == PaymentStatus.CAPTURED:
                payment.authorize(gateway_ref=reference_id)
                payment.capture()

                # Build balanced zero-sum LedgerTransaction
                amount: Decimal = payment.amount
                currency: str = payment.currency
                ledger_txn = LedgerTransaction.build(
                    reference=payment.payment_id,
                    description=f"Webhook capture — txn {payment.transaction_id}, ref {reference_id}",
                    entries=[
                        LedgerEntry.debit(
                            transaction_id=payment.payment_id,
                            account_id=_ACCOUNT_RECEIVABLE,
                            amount=amount,
                            currency=currency,
                        ),
                        LedgerEntry.credit(
                            transaction_id=payment.payment_id,
                            account_id=_ACCOUNT_GW_PAYABLE,
                            amount=amount,
                            currency=currency,
                        ),
                    ],
                )

                await uow.payments.update(payment)
                await uow.ledger.add(ledger_txn)
                await uow.outbox.enqueue(
                    aggregate_type="Payment",
                    aggregate_id=payment.payment_id,
                    event_type="payment.captured",
                    payload=json.dumps({
                        "payment_id": payment.payment_id,
                        "amount": str(payment.amount),
                        "currency": payment.currency,
                        "status": payment.status.value,
                        "reference_id": payment.transaction_id,
                        "trace_id": get_current_trace_id(),
                    }),
                )
                log.info("webhook.payment_captured", payment_id=payment.payment_id)

            else:
                # FAILED transition
                payment.fail(reason=f"Gateway webhook reported failure (ref: {reference_id})")
                await uow.payments.update(payment)
                await uow.outbox.enqueue(
                    aggregate_type="Payment",
                    aggregate_id=payment.payment_id,
                    event_type="payment.failed",
                    payload=json.dumps({
                        "payment_id": payment.payment_id,
                        "amount": str(payment.amount),
                        "currency": payment.currency,
                        "status": payment.status.value,
                        "reference_id": payment.transaction_id,
                        "trace_id": get_current_trace_id(),
                    }),
                )
                log.info("webhook.payment_failed", payment_id=payment.payment_id)

            await uow.commit()

        log.info(
            "webhook.process_complete",
            payment_id=payment.payment_id,
            final_status=payment.status.value,
        )

        return WebhookResult(
            payment_id=payment.payment_id,
            status=payment.status,
            already_resolved=False,
        )
