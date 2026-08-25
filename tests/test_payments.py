import pytest
from httpx import AsyncClient, ASGITransport
from main import app
from presentation.middleware import idempotency
from presentation.api.v1.payments import get_orchestrator, get_uow_factory
from application.use_cases.payment_orchestrator import PaymentOrchestrator, ProcessPaymentResult
from domain.entities.payment import PaymentStatus, PaymentAggregate
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

@pytest.mark.asyncio
async def test_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

@pytest.mark.asyncio
async def test_create_payment_idempotency_flow(monkeypatch):
    # Mock Redis client
    class MockRedisClient:
        async def get(self, key):
            return None
        async def set(self, key, value, expire=300):
            return True
        async def acquire_lock(self, key, expire=15):
            return "mock-token-123"
        async def release_lock(self, key, token):
            return True


    monkeypatch.setattr(idempotency, "redis_client", MockRedisClient())

    # Mock the orchestrator to bypass real DB/gateway calls
    mock_result = ProcessPaymentResult(
        payment_id="pay_123",
        transaction_id="tx_test_12345",
        status=PaymentStatus.CAPTURED,
        gateway_ref="gw_ref",
        ledger_txn_id="ltx_123",
        reconciler_needed=False,
    )
    mock_orch = MagicMock(spec=PaymentOrchestrator)
    mock_orch.process_payment = AsyncMock(return_value=mock_result)
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch

    # Mock the UoW factory used by the re-fetch after capture
    mock_payment = PaymentAggregate(
        payment_id="pay_123",
        transaction_id="tx_test_12345",
        amount=Decimal("100.50"),
        currency="USD",
        status=PaymentStatus.CAPTURED,
        gateway_ref="gw_ref",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    mock_payments_repo = AsyncMock()
    mock_payments_repo.get_by_payment_id = AsyncMock(return_value=mock_payment)
    mock_ledger_repo = AsyncMock()
    mock_ledger_repo.get_by_reference_id = AsyncMock(return_value=None)
    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.payments = mock_payments_repo
    mock_uow.ledger = mock_ledger_repo
    app.dependency_overrides[get_uow_factory] = lambda: (lambda: mock_uow)

    payload = {
        "amount": "100.50",
        "currency": "USD",
        "source_token": "tok_test_visa",
        "reference_id": "tx_test_12345",
    }
    headers = {"X-Idempotency-Key": "test-key-999"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response_1 = await ac.post("/api/v1/payments/", json=payload, headers=headers)
        response_2 = await ac.post("/api/v1/payments/", json=payload, headers=headers)

    assert response_1.status_code in (200, 201)
    assert response_2.status_code in (200, 201, 409)

    # Clear overrides after test
    app.dependency_overrides.clear()