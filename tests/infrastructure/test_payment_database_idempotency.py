"""Unit and schema tests for database-level idempotency anchor."""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import Request

from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import IdempotencyConflictException
from infrastructure.database.models import PaymentModel
from infrastructure.database.repositories.payment import SqlAlchemyPaymentRepository
from presentation.middleware.exception_handler import _handle_idempotency_conflict


def test_payment_model_idempotency_key_schema():
    """Verify PaymentModel defines idempotency_key with unique, index, and not-null constraints."""
    col = PaymentModel.__table__.c.idempotency_key
    assert col is not None
    assert col.nullable is False
    assert col.unique is True
    # Column or index exists
    indexes = [ix.name for ix in PaymentModel.__table__.indexes]
    # Check if indexed or unique
    assert col.unique or any("idempotency_key" in [c.name for c in ix.columns] for ix in PaymentModel.__table__.indexes)


def test_payment_repository_to_model_and_to_entity_mapping():
    """Verify SqlAlchemyPaymentRepository maps idempotency_key bidirectional."""
    aggregate = PaymentAggregate(
        payment_id="pay_abc",
        transaction_id="tx_123",
        idempotency_key="idem_key_456",
        amount=Decimal("250.00"),
        currency="USD",
        status=PaymentStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    model = SqlAlchemyPaymentRepository._to_model(aggregate)
    assert model.payment_id == "pay_abc"
    assert model.transaction_id == "tx_123"
    assert model.idempotency_key == "idem_key_456"

    roundtrip = SqlAlchemyPaymentRepository._to_entity(model)
    assert roundtrip.payment_id == "pay_abc"
    assert roundtrip.transaction_id == "tx_123"
    assert roundtrip.idempotency_key == "idem_key_456"


@pytest.mark.asyncio
async def test_payment_repository_get_by_idempotency_key():
    """Verify get_by_idempotency_key calls select with idempotency_key filter."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_model = PaymentModel(
        payment_id="pay_abc",
        transaction_id="tx_123",
        idempotency_key="idem_key_456",
        amount=Decimal("250.00"),
        currency="USD",
        status=PaymentStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    mock_result.scalar_one_or_none.return_value = mock_model
    session.execute.return_value = mock_result

    repo = SqlAlchemyPaymentRepository(session)
    result = await repo.get_by_idempotency_key("idem_key_456")

    assert result is not None
    assert result.payment_id == "pay_abc"
    assert result.idempotency_key == "idem_key_456"
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_exception_handler_maps_idempotency_conflict_to_409():
    """Verify _handle_idempotency_conflict returns RFC 7807 409 Conflict response."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/payments/",
        "headers": [],
    }
    request = Request(scope)
    request.state.correlation_id = "corr-test-123"

    exc = IdempotencyConflictException(
        "Payment with idempotency key 'key-123' already exists.",
        detail="Duplicate idempotency key 'key-123' detected at database level.",
    )

    response = await _handle_idempotency_conflict(request, exc)
    assert response.status_code == 409
    assert response.media_type == "application/problem+json"
    import json
    data = json.loads(response.body.decode())
    assert data["status"] == 409
    assert data["title"] == "Idempotency Conflict"
    assert data["type"] == "https://api.example.com/errors/idempotency-conflict"
    assert "Duplicate idempotency key 'key-123'" in data["detail"]
    assert data["correlation_id"] == "corr-test-123"
