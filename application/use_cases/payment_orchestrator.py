"""
application/use_cases/payment_orchestrator.py
-----------------------------------------------
PaymentOrchestrator — the authoritative application-layer use case for
processing a payment end-to-end.

Architectural contract
----------------------
This class sits at the **application layer** of the clean architecture:

  ┌─────────────────────────────────────────────────────────┐
  │  Presentation  (FastAPI / gRPC / CLI)                   │
  │    ↓  injects command + dependencies                    │
  │  Application   (PaymentOrchestrator)   ← YOU ARE HERE  │
  │    ↓  calls domain objects only                         │
  │  Domain        (PaymentAggregate, LedgerTransaction)    │
  │    ↓  persisted by                                      │
  │  Infrastructure (UoW, Repos, Gateway, Outbox)           │
  └─────────────────────────────────────────────────────────┘

Rules enforced here:
  - Zero float arithmetic.  All monetary values are ``Decimal``.
  - No ORM imports.  The orchestrator talks to abstractions only.
  - State transitions go through ``PaymentAggregate`` domain methods only.
  - Every database write is wrapped in its own discrete UoW transaction so
    that the two phases (PENDING write, CAPTURED/FAILED write) are each
    individually atomic and independently recoverable.

The three-phase execution flow
-------------------------------
Phase 1 — Local State Initialisation
    Create a PENDING PaymentAggregate, persist it, and enqueue a
    PaymentPending outbox event — all in one transaction.
    If the process crashes after this commit, the Background Reconciler
    sees a PENDING payment with no gateway response and will re-drive it.

Phase 2 — Network Call
    Call gateway.charge().  This is the *only* fallible external call.
    On timeout or 5xx, log and RETURN EARLY.  The DB state stays PENDING.
    The Background Reconciler will detect the stale PENDING record and
    re-query the gateway or retry the charge.

Phase 3 — Local State Finalisation
    On a definitive response (200 success or 4xx hard decline):
      - Transition the aggregate to CAPTURED or FAILED.
      - If CAPTURED, build and persist a balanced LedgerTransaction.
      - Enqueue the final domain event to the outbox.
    All of this happens inside a new, independent transaction.

Account codes
-------------
Two nominal ledger accounts are used for a payment capture:

  ACCOUNTS_RECEIVABLE  (+)  debit  — money owed to the merchant
  GATEWAY_PAYABLE      (-)  credit — liability to the gateway/acquirer

The net is zero, satisfying the double-entry invariant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import structlog

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import PaymentGatewayError
from infrastructure.telemetry.context import get_current_trace_id
from infrastructure.external.gateway_client import (
    PaymentGatewayClient,
    PaymentGatewayException,
)

try:
    import httpx
    _NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = ()  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

# Nominal ledger account codes.
# In production these would come from a chart-of-accounts service.
_ACCOUNT_RECEIVABLE = "1100.ACCOUNTS_RECEIVABLE"   # asset — debit increases it
_ACCOUNT_GW_PAYABLE = "2100.GATEWAY_PAYABLE"        # liability — credit increases it


# ---------------------------------------------------------------------------
# Command / Result value objects (stdlib dataclasses — no Pydantic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessPaymentCommand:
    """
    Input value object for the PaymentOrchestrator.

    Attributes:
        amount          Payment amount as ``Decimal`` — never ``float``.
        currency        ISO 4217 currency code (e.g. ``"INR"``).
        source          Tokenised payment source (e.g. card token from the SDK).
        transaction_id  Merchant-supplied idempotency handle.
        correlation_id  Distributed trace ID propagated from the HTTP layer.
    """

    amount: Decimal
    currency: str
    source: str
    transaction_id: str
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                f"ProcessPaymentCommand.amount must be Decimal, got {type(self.amount).__name__}. "
                "Use Decimal('9.99') rather than 9.99."
            )
        if self.amount <= Decimal("0"):
            raise ValueError("amount must be strictly positive.")


@dataclass(frozen=True)
class ProcessPaymentResult:
    """
    Output value object returned by ``PaymentOrchestrator.process_payment()``.

    Attributes:
        payment_id      Stable aggregate UUID.
        transaction_id  Merchant-supplied idempotency handle (echoed back).
        status          Final ``PaymentStatus`` at the end of this call.
                        Note: may be ``PENDING`` if the gateway timed out —
                        the Background Reconciler will drive the final state.
        gateway_ref     Acquirer reference; ``None`` when status is not CAPTURED.
        ledger_txn_id   ID of the created ``LedgerTransaction``; ``None`` unless
                        the payment was CAPTURED in this call.
        reconciler_needed
                        ``True`` when the gateway was unreachable and the
                        Background Reconciler must follow up.
    """

    payment_id: str
    transaction_id: str
    status: PaymentStatus
    gateway_ref: str | None = None
    ledger_txn_id: str | None = None
    reconciler_needed: bool = False


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class PaymentOrchestrator:
    """
    Application-layer orchestrator for the payment processing workflow.

    Dependencies are injected at construction time so the class is fully
    testable without touching a real database or gateway.

    Args:
        uow:     Unit-of-Work factory.  Must be used as an async context
                 manager; each ``async with uow_factory()`` call opens a new
                 SQLAlchemy session + transaction.
        gateway: Payment gateway client (``PaymentGatewayClient`` or a test double).
    """

    def __init__(
        self,
        uow,                         # AbstractUnitOfWork factory
        gateway: PaymentGatewayClient,
    ) -> None:
        self._uow = uow
        self._gateway = gateway

    async def process_payment(
        self, command: ProcessPaymentCommand
    ) -> ProcessPaymentResult:
        """
        Execute the three-phase payment processing flow.

        Returns a ``ProcessPaymentResult`` describing the outcome.
        Never raises on transient gateway errors — those are handled internally
        and result in ``reconciler_needed=True`` in the returned value.

        Raises:
            DuplicateTransactionError: If ``command.transaction_id`` already
                exists in the database (the caller should surface this as 409).
            DomainException: On any domain invariant violation (e.g. invalid
                currency, zero amount) — these are programming errors and
                should surface as 422/500.
        """
        log = logger.bind(
            transaction_id=command.transaction_id,
            correlation_id=command.correlation_id,
            amount=str(command.amount),
            currency=command.currency,
        )

        # ==================================================================
        # PHASE 1 — Local State Initialisation (one atomic transaction)
        # ==================================================================
        log.info("payment_orchestrator.phase1.start")

        payment = PaymentAggregate.create(
            amount=command.amount,
            currency=command.currency,
            transaction_id=command.transaction_id,
        )

        async with self._uow() as uow:
            await uow.payments.add(payment)
            await uow.outbox.enqueue(
                aggregate_type="Payment",
                aggregate_id=payment.payment_id,
                event_type="payment.pending",
                payload=json.dumps({
                    "payment_id": payment.payment_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "reference_id": payment.transaction_id,
                    "trace_id": get_current_trace_id(),
                }),
            )
            # commit() is called by __aexit__ on clean exit
        # Session is closed here.  payment is now durable in PENDING state.

        log.info("payment_orchestrator.phase1.committed", payment_id=payment.payment_id)

        # ==================================================================
        # PHASE 2 — Network Call (stateless from the DB's point of view)
        # ==================================================================
        log.info("payment_orchestrator.phase2.gateway_call")

        gateway_response: dict | None = None
        is_transient_failure = False

        try:
            gateway_response = await self._gateway.charge_with_fallback(
                payload={
                    "amount": str(payment.amount),       # Decimal serialised as str
                    "currency": payment.currency,
                    "source": command.source,
                    "idempotency_key": payment.payment_id,
                }
            )
            log.info(
                "payment_orchestrator.phase2.gateway_success",
                payment_id=payment.payment_id,
                gateway_ref=gateway_response.get("reference"),
            )

        except (*_NETWORK_ERRORS, PaymentGatewayException) as exc:
            # Transient failure: timeout or 5xx after all retries.
            # CRITICAL: Do NOT write to the DB.  Leave state as PENDING.
            # The Background Reconciler will detect stale PENDING records
            # and re-query the gateway or schedule a retry.
            log.warning(
                "payment_orchestrator.phase2.transient_failure",
                payment_id=payment.payment_id,
                error=str(exc),
                action="leaving_state_pending_for_reconciler",
            )
            is_transient_failure = True

        # Early return on transient gateway failure.
        if is_transient_failure:
            return ProcessPaymentResult(
                payment_id=payment.payment_id,
                transaction_id=payment.transaction_id,
                status=PaymentStatus.PENDING,
                reconciler_needed=True,
            )

        # ==================================================================
        # PHASE 3 — Local State Finalisation (second atomic transaction)
        # ==================================================================
        # At this point gateway_response is guaranteed to be set (200 or 4xx).
        log.info("payment_orchestrator.phase3.start", payment_id=payment.payment_id)

        ledger_txn_id: str | None = None
        gateway_ref: str | None = gateway_response.get("reference")  # type: ignore[union-attr]
        is_success = gateway_response.get("status") == "success"      # type: ignore[union-attr]

        async with self._uow() as uow:
            if is_success:
                # ── CAPTURED path ──────────────────────────────────────────
                # 1. Drive aggregate through its state machine
                payment.authorize(gateway_ref=gateway_ref or "")
                payment.capture()

                # 2. Build a zero-sum LedgerTransaction (double-entry)
                amount: Decimal = payment.amount
                currency: str = payment.currency
                ledger_txn = LedgerTransaction.build(
                    reference=payment.payment_id,
                    description=(
                        f"Payment captured — txn {payment.transaction_id}, "
                        f"gateway ref {gateway_ref}"
                    ),
                    entries=[
                        LedgerEntry.debit(
                            transaction_id="",   # re-stamped by build()
                            account_id=_ACCOUNT_RECEIVABLE,
                            amount=amount,
                            currency=currency,
                        ),
                        LedgerEntry.credit(
                            transaction_id="",   # re-stamped by build()
                            account_id=_ACCOUNT_GW_PAYABLE,
                            amount=amount,
                            currency=currency,
                        ),
                    ],
                )
                ledger_txn_id = ledger_txn.id

                # 3. Persist updated payment + ledger + outbox in one shot
                await uow.payments.add(payment)
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
                log.info(
                    "payment_orchestrator.phase3.captured",
                    payment_id=payment.payment_id,
                    ledger_txn_id=ledger_txn_id,
                )

            else:
                # ── FAILED path (4xx hard decline) ─────────────────────────
                decline_reason: str = gateway_response.get(  # type: ignore[union-attr]
                    "error", "Gateway hard decline."
                )
                payment.fail(reason=decline_reason)

                await uow.payments.add(payment)
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
                log.info(
                    "payment_orchestrator.phase3.failed",
                    payment_id=payment.payment_id,
                    reason=decline_reason,
                )
            # commit() called by __aexit__ on clean exit

        log.info(
            "payment_orchestrator.complete",
            payment_id=payment.payment_id,
            final_status=payment.status.value,
        )

        return ProcessPaymentResult(
            payment_id=payment.payment_id,
            transaction_id=payment.transaction_id,
            status=payment.status,
            gateway_ref=gateway_ref if is_success else None,
            ledger_txn_id=ledger_txn_id,
            reconciler_needed=False,
        )
