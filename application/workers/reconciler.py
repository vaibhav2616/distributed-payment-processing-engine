"""
application/workers/reconciler.py
-----------------------------------
BackgroundReconciliationEngine — resolves zombie PENDING payments.

What is a "zombie" payment?
---------------------------
A payment enters PENDING state in Phase 1 of the PaymentOrchestrator.
It stays PENDING if:
  (a) the orchestrator process crashed before Phase 3 completed, or
  (b) the gateway timed out / returned 5xx and the orchestrator returned early.

The reconciler sweeps these stale records, re-queries the gateway for their
definitive outcome, and drives them to CAPTURED or FAILED.

Lock-free network I/O pattern (the correct approach)
-----------------------------------------------------
The previous implementation held a single ``FOR UPDATE SKIP LOCKED`` across
ALL network calls in the batch.  This exhausts connection pools because:
  - A PostgreSQL connection is held open for the entire duration of all
    gateway HTTP requests (potentially seconds × batch_size).
  - Under load, every idle connection in the pool is consumed by reconciler
    pods doing nothing but waiting for HTTP responses.

The correct pattern uses three strictly separated phases:

  PHASE 1 — Unlocked batch fetch (no transaction, no locks, no network)
    ``SELECT payment_id FROM payments
      WHERE status = 'PENDING' AND created_at < staleness_cutoff
      LIMIT batch_size``
    Release the connection immediately.

  PHASE 2 — Network I/O (no DB connection held)
    For each ID, call gateway.verify_status(payment_id).
    On timeout / 5xx: skip, retry next cron run.
    On definitive response: carry (id, response) forward.

  PHASE 3 — Just-in-time short transaction per payment
    For each (id, response) pair:
      - Open a new UoW (new connection, new transaction).
      - ``SELECT ... FOR UPDATE NOWAIT`` on the single row.
        → If locked by another pod: rollback, skip.
        → If status is no longer PENDING: rollback, skip (already resolved).
      - Apply state transition (CAPTURED or FAILED).
      - Build LedgerTransaction if CAPTURED.
      - Enqueue outbox event.
      - Commit immediately.  Connection returned to pool.

  Maximum connection hold time = one UPDATE + one INSERT per payment.
  No connection is ever held while waiting for HTTP.

Concurrency safety
------------------
``FOR UPDATE NOWAIT`` in Phase 3 is the safety primitive:
  - Two pods that fetched the same ID in Phase 1 will race to the NOWAIT lock.
  - One wins, acquires the lock, commits.
  - The other gets an ``OperationalError`` immediately, catches it, returns None,
    rolls back, and skips — total extra work: one failed SELECT, zero writes.

The stale-state guard (status != PENDING check after locking) provides a second
layer of safety: even without a lock contention, if the orchestrator resolved
the payment between Phase 1 and Phase 3, we detect it and skip without writing.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import structlog

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.entities.payment import PaymentAggregate, PaymentStatus
from infrastructure.external.gateway_client import (
    PaymentGatewayClient,
    PaymentGatewayException,
)

try:
    import httpx
    _TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
except ImportError:  # pragma: no cover
    _TRANSIENT_ERRORS = ()  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

# Nominal ledger accounts — kept in sync with payment_orchestrator.py
_ACCOUNT_RECEIVABLE = "1100.ACCOUNTS_RECEIVABLE"
_ACCOUNT_GW_PAYABLE = "2100.GATEWAY_PAYABLE"

# Gateway response status strings
_GW_SUCCESS = "success"


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcilerRunResult:
    """
    Summary returned by a single invocation of
    ``BackgroundReconciliationEngine.run()``.

    Attributes:
        swept       Total stale PENDING IDs found in the unlocked sweep.
        captured    Payments transitioned to CAPTURED.
        failed      Payments transitioned to FAILED.
        skipped     Payments skipped: gateway timeout, lock contention,
                    or already resolved by another worker.
        errors      Payments that raised an unexpected exception.
    """

    swept: int = 0
    captured: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

    def __add__(self, other: "ReconcilerRunResult") -> "ReconcilerRunResult":
        return ReconcilerRunResult(
            swept=self.swept + other.swept,
            captured=self.captured + other.captured,
            failed=self.failed + other.failed,
            skipped=self.skipped + other.skipped,
            errors=self.errors + other.errors,
        )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class BackgroundReconciliationEngine:
    """
    Application-layer worker that resolves stale PENDING payments using a
    lock-free network I/O pattern.

    Args:
        uow_factory:       Callable that returns a new ``AbstractUnitOfWork``
                           async context manager.  Each payment in Phase 3
                           gets its own independent, short-lived UoW.
        gateway:           ``PaymentGatewayClient`` (or a test double).
        stale_threshold_s: Seconds before a PENDING payment is considered
                           stale.  Default 300 (5 minutes) prevents races
                           with active orchestrator requests.
        batch_size:        Maximum stale IDs to fetch per run.
    """

    def __init__(
        self,
        uow_factory: Callable,
        gateway: PaymentGatewayClient,
        stale_threshold_s: int = 300,
        batch_size: int = 50,
    ) -> None:
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._stale_threshold_s = stale_threshold_s
        self._batch_size = batch_size

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> ReconcilerRunResult:
        """
        Execute one reconciliation sweep using the three-phase lock-free pattern.

        Phase 1: Unlocked ID fetch  — no connection held after this returns.
        Phase 2: Network I/O        — zero DB resources consumed.
        Phase 3: JIT short txn      — one connection × one payment × ~1 ms.

        Returns:
            ``ReconcilerRunResult`` summarising what happened.
        """
        log = logger.bind(worker="BackgroundReconciliationEngine")
        log.info("reconciler.sweep.start", stale_threshold_s=self._stale_threshold_s)

        # ── PHASE 1: Unlocked batch ID fetch ──────────────────────────────
        # Open a throw-away UoW just to call get_stale_pending_ids(), then
        # immediately exit — the connection goes back to the pool before any
        # network I/O starts.
        async with self._uow_factory() as uow:
            stale_ids: list[str] = await uow.payments.get_stale_pending_ids(
                older_than_seconds=self._stale_threshold_s,
                batch_size=self._batch_size,
            )
        # Connection returned to pool here. No locks held.

        if not stale_ids:
            log.info("reconciler.sweep.empty")
            return ReconcilerRunResult()

        log.info("reconciler.sweep.found", count=len(stale_ids))
        totals = ReconcilerRunResult(swept=len(stale_ids))

        # ── PHASE 2 + 3: Per-ID network call → JIT short transaction ──────
        for payment_id in stale_ids:
            result = await self._process_one_id(payment_id, log)
            totals = totals + result

        log.info(
            "reconciler.sweep.complete",
            swept=totals.swept,
            captured=totals.captured,
            failed=totals.failed,
            skipped=totals.skipped,
            errors=totals.errors,
        )
        return totals

    # ------------------------------------------------------------------
    # Per-ID orchestration
    # ------------------------------------------------------------------

    async def _process_one_id(
        self,
        payment_id: str,
        log,
    ) -> ReconcilerRunResult:
        """
        Phase 2 + Phase 3 for a single payment ID.

        Phase 2: Call gateway.verify_status() with NO DB connection held.
        Phase 3: Open a short UoW, lock the row NOWAIT, apply the transition,
                 commit, release the connection.
        """
        plog = log.bind(payment_id=payment_id)

        # ── PHASE 2: Network I/O (zero DB resources held) ─────────────────
        try:
            gw_response = await self._gateway.verify_status(
                idempotency_key=payment_id
            )
        except (*_TRANSIENT_ERRORS, PaymentGatewayException) as exc:
            # Transient error: gateway timeout or 5xx.
            # Do NOT touch the DB. Leave the payment PENDING.
            # The next cron run will retry this ID.
            plog.warning(
                "reconciler.gateway_transient_error",
                error=str(exc),
                action="skip_retry_next_run",
            )
            return ReconcilerRunResult(skipped=1)
        except Exception as exc:
            plog.error(
                "reconciler.gateway_unexpected_error",
                error=str(exc),
                action="skip_and_continue",
            )
            return ReconcilerRunResult(errors=1)

        # ── PHASE 3: JIT short transaction — lock, write, commit ──────────
        # Each payment opens its own independent UoW.
        # Maximum connection hold time: one SELECT FOR UPDATE NOWAIT +
        # one UPDATE + optional INSERT (ledger) + one INSERT (outbox).
        # Typically < 5 ms of DB time regardless of gateway latency.
        try:
            return await self._finalize_in_transaction(payment_id, gw_response, plog)
        except Exception as exc:
            plog.error(
                "reconciler.finalization_unexpected_error",
                error=str(exc),
            )
            return ReconcilerRunResult(errors=1)

    async def _finalize_in_transaction(
        self,
        payment_id: str,
        gw_response: dict,
        log,
    ) -> ReconcilerRunResult:
        """
        Open a fresh UoW, lock the single payment row with FOR UPDATE NOWAIT,
        verify it is still PENDING, apply the domain transition, persist, commit.

        Returns immediately (with skipped=1) if:
          - The row is already locked by another reconciler pod (NOWAIT).
          - The payment is no longer PENDING (resolved while we were in Phase 2).
        """
        async with self._uow_factory() as uow:
            # lock_pending_by_id uses SELECT FOR UPDATE NOWAIT and checks status.
            # Returns None on lock contention OR status != PENDING.
            payment: PaymentAggregate | None = await uow.payments.lock_pending_by_id(
                payment_id
            )

            if payment is None:
                # Either locked by another pod or already resolved — skip cleanly.
                log.info(
                    "reconciler.payment_already_resolved_or_locked",
                    action="skip",
                )
                # Explicit rollback: nothing was written, but we release the lock
                # attempt cleanly rather than relying on __aexit__ error path.
                await uow.rollback()
                return ReconcilerRunResult(skipped=1)

            gw_status: str = gw_response.get("status", "")

            if gw_status == _GW_SUCCESS:
                return await self._handle_success(uow, payment, gw_response, log)
            else:
                return await self._handle_failure(uow, payment, gw_response, log)
            # __aexit__ commits on clean return

    # ------------------------------------------------------------------
    # Transition handlers (called inside an active UoW)
    # ------------------------------------------------------------------

    async def _handle_success(
        self,
        uow,
        payment: PaymentAggregate,
        gw_response: dict,
        log,
    ) -> ReconcilerRunResult:
        """
        Transition PENDING → AUTHORIZED → CAPTURED.
        Build a zero-sum LedgerTransaction.
        Enqueue payment.captured outbox event.
        """
        gateway_ref: str = gw_response.get("reference", "")

        # Domain state machine — raises InvalidStateTransitionError if wrong state.
        payment.authorize(gateway_ref=gateway_ref)
        payment.capture()

        # Zero-sum double-entry — LedgerTransaction.build() calls verify_balance()
        # internally, so an imbalanced entry set is impossible to persist.
        amount: Decimal = payment.amount   # already Decimal; no float cast ever
        currency: str = payment.currency
        ledger_txn = LedgerTransaction.build(
            reference=payment.payment_id,
            description=(
                f"Reconciled payment capture — txn {payment.transaction_id}, "
                f"gateway ref {gateway_ref}"
            ),
            entries=[
                LedgerEntry.debit(
                    transaction_id="",           # re-stamped by build()
                    account_id=_ACCOUNT_RECEIVABLE,
                    amount=amount,
                    currency=currency,
                ),
                LedgerEntry.credit(
                    transaction_id="",           # re-stamped by build()
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
                "transaction_id": payment.transaction_id,
                "amount": str(payment.amount),   # Decimal → str; never float
                "currency": payment.currency,
                "gateway_ref": gateway_ref,
                "ledger_txn_id": ledger_txn.id,
                "source": "reconciler",
            }),
        )
        log.info(
            "reconciler.payment_captured",
            payment_id=payment.payment_id,
            gateway_ref=gateway_ref,
            ledger_txn_id=ledger_txn.id,
        )
        return ReconcilerRunResult(captured=1)

    async def _handle_failure(
        self,
        uow,
        payment: PaymentAggregate,
        gw_response: dict,
        log,
    ) -> ReconcilerRunResult:
        """
        Transition PENDING → FAILED.
        Enqueue payment.failed outbox event.
        Covers both gateway NOT_FOUND and definitive FAILED responses.
        """
        gw_status: str = gw_response.get("status", "unknown")
        reason: str = gw_response.get(
            "error",
            f"Gateway reconciliation: status={gw_status}.",
        )

        payment.fail(reason=reason)

        await uow.payments.update(payment)
        await uow.outbox.enqueue(
            aggregate_type="Payment",
            aggregate_id=payment.payment_id,
            event_type="payment.failed",
            payload=json.dumps({
                "payment_id": payment.payment_id,
                "transaction_id": payment.transaction_id,
                "reason": reason,
                "gateway_status": gw_status,
                "source": "reconciler",
            }),
        )
        log.info(
            "reconciler.payment_failed",
            payment_id=payment.payment_id,
            reason=reason,
            gw_status=gw_status,
        )
        return ReconcilerRunResult(failed=1)

    async def run_forever(self, interval_seconds: int = 10) -> None:
        """Execute reconciliation sweeps continuously on a scheduled loop."""
        log = logger.bind(worker="BackgroundReconciliationEngine")
        log.info("reconciler.loop.started", interval_seconds=interval_seconds)
        while True:
            try:
                result = await self.run()
                if result.swept > 0:
                    log.info(
                        "reconciler.loop.cycle_summary",
                        swept=result.swept,
                        captured=result.captured,
                        failed=result.failed,
                        skipped=result.skipped,
                        errors=result.errors,
                    )
            except Exception as exc:
                log.error("reconciler.loop.unexpected_error", error=str(exc))
            await asyncio.sleep(interval_seconds)


async def main() -> None:
    from application.uow import SqlAlchemyUnitOfWork
    from infrastructure.external.gateway_client import gateway_client

    interval = int(os.getenv("RECONCILER_INTERVAL_SECONDS", "10"))
    stale_threshold = int(os.getenv("RECONCILER_STALE_THRESHOLD_SECONDS", "60"))
    batch_size = int(os.getenv("RECONCILER_BATCH_SIZE", "50"))

    engine = BackgroundReconciliationEngine(
        uow_factory=SqlAlchemyUnitOfWork,
        gateway=gateway_client,
        stale_threshold_s=stale_threshold,
        batch_size=batch_size,
    )
    await engine.run_forever(interval_seconds=interval)


if __name__ == "__main__":
    asyncio.run(main())

