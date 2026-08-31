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
from infrastructure.external.circuit_breaker import CircuitBreakerOpenException
from main import app
from presentation.api.v1.payments import get_orchestrator, get_uow_factory


client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_redis_for_router(monkeypatch):
    class MockRedisClient:
        async def get(self, key):
            return None
        async def set(self, key, value, expire=300):
            return True
        async def acquire_lock(self, key, expire=15):
            return "mock-token-123"
        async def release_lock(self, key, token):
            return True
    from presentation.middleware import idempotency
    monkeypatch.setattr(idempotency, "redis_client", MockRedisClient())


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


def test_process_payment_503_circuit_breaker_open():
    """
    Test that an open circuit breaker returns 503 Service Unavailable with Retry-After: 60 header.
    """
    mock_orch = MockOrchestrator()
    mock_orch.process_payment.side_effect = CircuitBreakerOpenException(
        "Payment gateway circuit breaker is OPEN. Upstream acquirer is unavailable.",
        retry_after=60,
    )

    app.dependency_overrides[get_orchestrator] = lambda: mock_orch

    payload = {
        "amount": "150.00",
        "currency": "USD",
        "source_token": "tok_visa",
        "reference_id": "order_777",
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    response = client.post("/api/v1/payments/", json=payload, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "60"
    data = response.json()
    assert data["status"] == 503
    assert "circuit breaker is OPEN" in data["detail"]


@pytest.mark.asyncio
async def test_circuit_breaker_open_critical_invariant_no_pending_in_db():
    """
    Critical invariant test:
    When the gateway circuit breaker is OPEN, PaymentOrchestrator must fast-fail
    without saving the payment as PENDING in the database.
    """
    from unittest.mock import MagicMock
    from application.use_cases.payment_orchestrator import PaymentOrchestrator, ProcessPaymentCommand
    from infrastructure.external.circuit_breaker import RedisCircuitBreaker

    class MockOpenRedis:
        async def get(self, key: str) -> str | None:
            if "open" in key:
                return "1"
            return None

    mock_breaker = RedisCircuitBreaker(redis_client=MockOpenRedis(), name="test_gw")
    mock_gateway = AsyncMock()
    mock_gateway.circuit_breaker = mock_breaker

    mock_uow_instance = AsyncMock()
    mock_uow_instance.payments = AsyncMock()
    mock_uow_instance.outbox = AsyncMock()
    mock_uow_factory = MagicMock(return_value=mock_uow_instance)
    mock_uow_instance.__aenter__.return_value = mock_uow_instance

    orchestrator = PaymentOrchestrator(uow=mock_uow_factory, gateway=mock_gateway)

    command = ProcessPaymentCommand(
        amount=Decimal("150.00"),
        currency="USD",
        source="tok_visa",
        transaction_id="order_cb_test",
    )

    with pytest.raises(CircuitBreakerOpenException):
        await orchestrator.process_payment(command)

    # CRITICAL INVARIANT ASSERTIONS:
    # 1. No UoW transaction was committed to persist PENDING payment
    mock_uow_instance.payments.add.assert_not_called()
    mock_uow_instance.outbox.enqueue.assert_not_called()
    # 2. No gateway charge call was made
    mock_gateway.charge.assert_not_called()

