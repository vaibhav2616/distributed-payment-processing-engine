import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from application.use_cases.refund_orchestrator import RefundOrchestrator
from domain.entities.payment import PaymentAggregate, PaymentStatus
from infrastructure.external.gateway_client import GatewayDeclineException

class DummyPaymentsRepo:
    def __init__(self, payment):
        self.payment = payment

    async def lock_by_id(self, payment_id):
        return self.payment

    async def update(self, payment):
        pass

class DummyOutboxRepo:
    async def enqueue(self, **kwargs):
        pass

class DummyUoW:
    def __init__(self, payment):
        self.payments = DummyPaymentsRepo(payment)
        self.outbox = DummyOutboxRepo()
        self.ledger = AsyncMock()
        
    async def begin(self):
        pass
        
    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

def uow_factory(payment):
    return lambda: DummyUoW(payment)


@pytest.mark.asyncio
async def test_refund_orchestrator_gateway_decline():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-orchestrator", payment_id="pay-orch")
    payment.status = PaymentStatus.CAPTURED
    
    mock_gateway = AsyncMock()
    mock_gateway.refund.side_effect = GatewayDeclineException("Insufficient funds on card")
    
    orchestrator = RefundOrchestrator(
        uow_factory=uow_factory(payment),
        gateway_client=mock_gateway
    )
    
    with pytest.raises(GatewayDeclineException) as exc:
        await orchestrator.process_refund(
            payment_id="pay-orch",
            amount=Decimal("50.00"),
            refund_idempotency_key="idemp-1"
        )
    
    assert "Insufficient funds on card" in str(exc.value)
    
    # Assert compensation was applied:
    # Phase 1 would add 50.00, then Phase 2 exception triggers Phase 3 compensation which removes 50.00.
    # Therefore, the final state should be reverted back to 0.00 and CAPTURED.
    assert payment.amount_refunded == Decimal("0.00")
    assert payment.status == PaymentStatus.CAPTURED
