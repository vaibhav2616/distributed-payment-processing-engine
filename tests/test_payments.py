import pytest
from httpx import AsyncClient, ASGITransport
from main import app
from presentation.middleware import idempotency
from presentation.api.payments import get_uow
from application.uow import AbstractUnitOfWork

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

    # Mock Gateway Client
    class MockGatewayClient:
        async def charge_with_fallback(self, payload):
            return {"reference_id": "mock_gateway_ref_999"}

    class MockPaymentsRepo:
        async def add(self, entity):
            pass

    class MockOutboxRepo:
        async def enqueue(self, *args, **kwargs):
            pass

    # Mock Unit of Work
    class MockUoW(AbstractUnitOfWork):
        def __init__(self):
            self.gateway = MockGatewayClient()
            self.payments = MockPaymentsRepo()
            self.outbox = MockOutboxRepo()
        
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    monkeypatch.setattr(idempotency, "redis_client", MockRedisClient())
    
    # Override FastAPI dependency
    app.dependency_overrides[get_uow] = lambda: MockUoW()

    payload = {
        "transaction_id": "tx_test_12345",
        "amount": 100.50,
        "currency": "USD"
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