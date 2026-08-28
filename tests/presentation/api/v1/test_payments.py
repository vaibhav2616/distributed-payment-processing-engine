"""
tests/presentation/api/v1/test_payments.py
-------------------------------------------
Integration suite for the v1 payments router.
Covers 201 Created, 202 Accepted, and 422 Unprocessable Entity responses.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from application.use_cases.payment_orchestrator import ProcessPaymentResult
from domain.entities.payment import PaymentAggregate, PaymentStatus
from main import app
from presentation.api.v1.payments import get_orchestrator, get_uow_factory


client = TestClient(app)


# ---------------------------------------------------------------------------
# Mock Dependencies
# ---------------------------------------------------------------------------


class MockUoW:
    """A minimal mock UoW that just returns a pre-configured PaymentAggregate."""
    def __init__(self, aggregate: PaymentAggregate):
        self.aggregate = aggregate
        self.payments = AsyncMock()
        self.payments.get_by_payment_id.return_value = self.aggregate

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockOrchestrator:
    """Mocks the PaymentOrchestrator to return specific results."""
    def __init__(self):
        self.process_payment = AsyncMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_process_payment_201_captured():
    """
    Test that a fully processed payment returns 201 CAPTURED.
    """
    payment_id = str(uuid.uuid4())
    transaction_id = str(uuid.uuid4())
    
    mock_result = ProcessPaymentResult(
        payment_id=payment_id,
        transaction_id=transaction_id,
        status=PaymentStatus.CAPTURED,
        gateway_ref="gw_123",
        ledger_txn_id="ltx_456",
        reconciler_needed=False,
    )
    
    mock_aggregate = PaymentAggregate(
        payment_id=payment_id,
        transaction_id=transaction_id,
        amount=Decimal("150.00"),
        currency="USD",
        status=PaymentStatus.CAPTURED,
        gateway_ref="gw_123",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_orch = MockOrchestrator()
    mock_orch.process_payment.return_value = mock_result
    
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch
    app.dependency_overrides[get_uow_factory] = lambda: lambda: MockUoW(mock_aggregate)

    payload = {
        "amount": "150.00",
        "currency": "USD",
        "source_token": "tok_visa",
        "reference_id": "order_777"
    }
    
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    response = client.post("/api/v1/payments/", json=payload, headers=headers)
    
    app.dependency_overrides.clear()

    assert response.status_code == 201
    data = response.json()
    assert data["id"] == payment_id
    assert data["status"] == "CAPTURED"
    assert data["amount"] == "150.00"
    assert data["currency"] == "USD"
    assert data["reference_id"] == transaction_id


def test_process_payment_202_accepted_pending():
    """
    Test that a gateway timeout returns 202 ACCEPTED with Retry-After header.
    """
    payment_id = str(uuid.uuid4())
    transaction_id = str(uuid.uuid4())
    
    mock_result = ProcessPaymentResult(
        payment_id=payment_id,
        transaction_id=transaction_id,
        status=PaymentStatus.PENDING,
        gateway_ref=None,
        ledger_txn_id=None,
        reconciler_needed=True,
    )

    mock_orch = MockOrchestrator()
    mock_orch.process_payment.return_value = mock_result
    
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch
    
    payload = {
        "amount": "150.00",
        "currency": "USD",
        "source_token": "tok_visa",
        "reference_id": "order_777"
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    response = client.post("/api/v1/payments/", json=payload, headers=headers)
    
    app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.headers.get("Retry-After") == "15"
    data = response.json()
    assert data["id"] == payment_id
    assert data["status"] == "PENDING"
    assert "Poll GET" in data["message"]


def test_process_payment_422_declined():
    """
    Test that a declined payment returns 422 with RFC 7807 Problem Details.
    """
    payment_id = str(uuid.uuid4())
    transaction_id = str(uuid.uuid4())
    
    mock_result = ProcessPaymentResult(
        payment_id=payment_id,
        transaction_id=transaction_id,
        status=PaymentStatus.FAILED,
        gateway_ref=None,
        ledger_txn_id=None,
        reconciler_needed=False,
    )

    mock_orch = MockOrchestrator()
    mock_orch.process_payment.return_value = mock_result
    
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch
    
    payload = {
        "amount": "150.00",
        "currency": "USD",
        "source_token": "tok_visa",
        "reference_id": "order_777"
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    response = client.post("/api/v1/payments/", json=payload, headers=headers)
    
    app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    data = response.json()
    assert data["type"] == "payment-declined"
    assert data["title"] == "Gateway Rejected"
    assert data["status"] == 422
    assert data["instance"] == f"/api/v1/payments/{payment_id}"
