"""
tests/application/test_webhook_orchestrator.py
----------------------------------------------
Unit tests for WebhookOrchestrator.
Validates:
  - SELECT FOR UPDATE NOWAIT locking
  - Raising ConcurrentUpdateException on lock collision
  - Stale-state guard (silent success when status != PENDING)
  - Zero-sum double-entry ledger persistence on CAPTURED
  - Outbox event enqueuing on CAPTURED and FAILED
"""
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.use_cases.webhook_orchestrator import (
    WebhookOrchestrator,
    WebhookResult,
)
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import ConcurrentUpdateException, EntityNotFoundError


@pytest.fixture
def pending_payment():
    return PaymentAggregate.create(
        amount=Decimal("250.00"),
        currency="USD",
        transaction_id="tx_order_100",
    )


@pytest.mark.asyncio
async def test_process_webhook_captured_transitions_and_creates_ledger(pending_payment):
    """
    When webhook arrives with CAPTURED status:
      - Payment transitions to CAPTURED.
      - A zero-sum LedgerTransaction is persisted.
      - An Outbox event is enqueued.
      - UoW commits.
    """
    mock_uow = MagicMock()
    mock_uow.begin = AsyncMock()
    mock_uow.commit = AsyncMock()
    mock_uow.rollback = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    mock_payments_repo = AsyncMock()
    mock_payments_repo.lock_by_reference_id.return_value = pending_payment
    mock_uow.payments = mock_payments_repo

    mock_ledger_repo = AsyncMock()
    mock_uow.ledger = mock_ledger_repo

    mock_outbox_repo = AsyncMock()
    mock_uow.outbox = mock_outbox_repo

    orchestrator = WebhookOrchestrator(uow_factory=lambda: mock_uow)

    result = await orchestrator.process_webhook(
        reference_id="gw_ch_999",
        status="CAPTURED",
    )

    assert result.payment_id == pending_payment.payment_id
    assert result.status == PaymentStatus.CAPTURED
    assert result.already_resolved is False

    # Invariant assertions
    assert pending_payment.status == PaymentStatus.CAPTURED
    assert pending_payment.gateway_ref == "gw_ch_999"

    mock_payments_repo.update.assert_called_once_with(pending_payment)
    mock_ledger_repo.add.assert_called_once()
    ledger_txn = mock_ledger_repo.add.call_args[0][0]
    assert ledger_txn.total_debits == ledger_txn.total_credits
    ledger_txn.verify_balance()

    mock_outbox_repo.enqueue.assert_called_once()
    outbox_call = mock_outbox_repo.enqueue.call_args[1]
    assert outbox_call["event_type"] == "payment.captured"
    payload_data = json.loads(outbox_call["payload"])
    assert payload_data["status"] == "CAPTURED"
    assert "source_token" not in payload_data  # PII exclusion check

    mock_uow.commit.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_failed_transitions_and_enqueues_event(pending_payment):
    """
    When webhook arrives with FAILED status:
      - Payment transitions to FAILED.
      - No ledger transaction is created.
      - Outbox event payment.failed is enqueued.
      - UoW commits.
    """
    mock_uow = MagicMock()
    mock_uow.begin = AsyncMock()
    mock_uow.commit = AsyncMock()
    mock_uow.rollback = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    mock_payments_repo = AsyncMock()
    mock_payments_repo.lock_by_reference_id.return_value = pending_payment
    mock_uow.payments = mock_payments_repo

    mock_ledger_repo = AsyncMock()
    mock_uow.ledger = mock_ledger_repo

    mock_outbox_repo = AsyncMock()
    mock_uow.outbox = mock_outbox_repo

    orchestrator = WebhookOrchestrator(uow_factory=lambda: mock_uow)

    result = await orchestrator.process_webhook(
        reference_id="gw_ch_999",
        status="FAILED",
    )

    assert result.status == PaymentStatus.FAILED
    assert pending_payment.status == PaymentStatus.FAILED
    mock_ledger_repo.add.assert_not_called()
    mock_outbox_repo.enqueue.assert_called_once()
    assert mock_outbox_repo.enqueue.call_args[1]["event_type"] == "payment.failed"
    mock_uow.commit.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_stale_already_resolved_silently_succeeds(pending_payment):
    """
    If payment was already resolved by Reconciler and incoming status matches (status == target_status):
      - Silently returns success with already_resolved=True, split_brain=False (true idempotency).
      - No ledger or outbox write occurs.
      - Transaction is rolled back without modifying state.
    """
    pending_payment.authorize("gw_ref_earlier")
    pending_payment.capture()

    mock_uow = MagicMock()
    mock_uow.begin = AsyncMock()
    mock_uow.commit = AsyncMock()
    mock_uow.rollback = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    mock_payments_repo = AsyncMock()
    mock_payments_repo.lock_by_reference_id.return_value = pending_payment
    mock_uow.payments = mock_payments_repo
    mock_uow.ledger = AsyncMock()
    mock_uow.outbox = AsyncMock()

    orchestrator = WebhookOrchestrator(uow_factory=lambda: mock_uow)

    result = await orchestrator.process_webhook(
        reference_id="gw_ch_999",
        status="CAPTURED",
    )

    assert result.already_resolved is True
    assert result.split_brain is False
    assert result.status == PaymentStatus.CAPTURED
    mock_uow.ledger.add.assert_not_called()
    mock_uow.outbox.enqueue.assert_not_called()
    mock_uow.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_split_brain_collision_logs_critical_alert(pending_payment):
    """
    If payment was already resolved to a different status (e.g. CAPTURED in DB, but webhook reports FAILED):
      - Distributed Split-Brain detected.
      - Transition is aborted and transaction rolled back.
      - CRITICAL security alert is emitted with payment_id, db_status, and webhook_status.
      - Returns success (already_resolved=True, split_brain=True) to avoid gateway retry loops.
    """
    from structlog.testing import capture_logs

    pending_payment.authorize("gw_ref_earlier")
    pending_payment.capture()  # DB status is CAPTURED

    mock_uow = MagicMock()
    mock_uow.begin = AsyncMock()
    mock_uow.commit = AsyncMock()
    mock_uow.rollback = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    mock_payments_repo = AsyncMock()
    mock_payments_repo.lock_by_reference_id.return_value = pending_payment
    mock_uow.payments = mock_payments_repo
    mock_uow.ledger = AsyncMock()
    mock_uow.outbox = AsyncMock()

    orchestrator = WebhookOrchestrator(uow_factory=lambda: mock_uow)

    with capture_logs() as cap_logs:
        result = await orchestrator.process_webhook(
            reference_id="gw_ch_999",
            status="FAILED",  # Incoming webhook status is FAILED != CAPTURED
        )

    assert result.already_resolved is True
    assert result.split_brain is True
    assert result.status == PaymentStatus.CAPTURED
    mock_uow.commit.assert_not_called()
    mock_uow.ledger.add.assert_not_called()
    mock_uow.outbox.enqueue.assert_not_called()
    mock_uow.rollback.assert_called_once()

    # Verify CRITICAL security alert was logged detailing payment_id, db_status, and webhook_status
    critical_logs = [
        entry for entry in cap_logs
        if entry.get("event") == "security_alert.distributed_split_brain"
        or entry.get("log_level") == "critical"
    ]
    assert len(critical_logs) >= 1
    alert = critical_logs[0]
    assert alert["payment_id"] == pending_payment.payment_id
    assert alert["db_status"] == "CAPTURED"
    assert alert["webhook_status"] == "FAILED"


@pytest.mark.asyncio
async def test_process_webhook_concurrent_lock_raises_concurrent_update_exception():
    """
    If row is locked by another transaction, lock_by_reference_id raises
    ConcurrentUpdateException and orchestrator propagates it.
    """
    mock_uow = MagicMock()
    mock_uow.begin = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    mock_payments_repo = AsyncMock()
    mock_payments_repo.lock_by_reference_id.side_effect = ConcurrentUpdateException(
        "Row locked by concurrent worker."
    )
    mock_uow.payments = mock_payments_repo

    orchestrator = WebhookOrchestrator(uow_factory=lambda: mock_uow)

    with pytest.raises(ConcurrentUpdateException):
        await orchestrator.process_webhook(
            reference_id="gw_ch_999",
            status="CAPTURED",
        )
