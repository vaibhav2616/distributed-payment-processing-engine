import pytest
from decimal import Decimal
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import InvalidRefundAmountError, InvalidStateTransitionError

def test_refund_invariant_valid_partial():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-1", payment_id="pay-1")
    payment.status = PaymentStatus.CAPTURED
    
    payment.process_refund(Decimal("40.00"))
    assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
    assert payment.amount_refunded == Decimal("40.00")
    
    payment.process_refund(Decimal("60.00"))
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.amount_refunded == Decimal("100.00")

def test_refund_invariant_excessive_amount():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-2", payment_id="pay-2")
    payment.status = PaymentStatus.CAPTURED
    
    payment.process_refund(Decimal("50.00"))
    assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
    assert payment.amount_refunded == Decimal("50.00")
    
    with pytest.raises(InvalidRefundAmountError) as exc_info:
        payment.process_refund(Decimal("60.00"))
    
    assert "exceeds available amount 50.00" in str(exc_info.value)

def test_refund_invariant_invalid_state():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-3", payment_id="pay-3")
    payment.status = PaymentStatus.AUTHORIZED
    
    with pytest.raises(InvalidStateTransitionError):
        payment.process_refund(Decimal("50.00"))

def test_refund_legacy_method():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-4", payment_id="pay-4")
    payment.status = PaymentStatus.CAPTURED
    
    payment.refund()
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.amount_refunded == Decimal("100.00")

def test_fail_refund_reverts_state():
    payment = PaymentAggregate.create(amount="100.00", currency="USD", transaction_id="tx-5", payment_id="pay-5")
    payment.status = PaymentStatus.CAPTURED
    
    # 1. Partial refund succeeds
    payment.process_refund(Decimal("40.00"))
    assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
    assert payment.amount_refunded == Decimal("40.00")
    
    # 2. Second refund attempts to refund the rest, but gateway will decline it
    payment.process_refund(Decimal("60.00"))
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.amount_refunded == Decimal("100.00")
    
    # 3. Simulate Gateway decline compensation
    payment.fail_refund(Decimal("60.00"))
    assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
    assert payment.amount_refunded == Decimal("40.00")
    
    # 4. Simulate a full revert to 0
    payment.fail_refund(Decimal("40.00"))
    assert payment.status == PaymentStatus.CAPTURED
    assert payment.amount_refunded == Decimal("0.00")
