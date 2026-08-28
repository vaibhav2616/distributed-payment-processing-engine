"""
tests/domain/test_payment_aggregate.py
---------------------------------------
Unit tests for the PaymentAggregate domain entity and state machine.

These tests are intentionally infrastructure-free:
  - No database, no Redis, no FastAPI test client.
  - Pure domain logic verification.
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from domain.entities.payment import (
    PaymentAggregate,
    PaymentStatus,
    Currency,
    Money,
    _ALLOWED_TRANSITIONS,
)
from domain.events.payment_events import (
    PaymentInitiated,
    PaymentAuthorized,
    PaymentCaptured,
    PaymentFailed,
    PaymentRefunded,
)
from domain.exceptions import InvalidStateTransitionError


# ---------------------------------------------------------------------------
# Value Object tests
# ---------------------------------------------------------------------------


class TestCurrencyValueObject:
    def test_valid_currency_uppercases(self):
        assert Currency("inr") == "INR"
        assert Currency("usd") == "USD"

    def test_invalid_currency_non_alpha(self):
        with pytest.raises(ValueError, match="ISO 4217"):
            Currency("12$")

    def test_invalid_currency_wrong_length(self):
        with pytest.raises(ValueError, match="ISO 4217"):
            Currency("IN")

    def test_empty_currency_raises(self):
        with pytest.raises(ValueError):
            Currency("")


class TestMoneyValueObject:
    def test_positive_amount_stores_two_decimal_places(self):
        m = Money("100.123", "INR")
        assert m.amount == Decimal("100.12")

    def test_zero_amount_raises(self):
        with pytest.raises(ValueError, match="positive"):
            Money("0.00", "INR")

    def test_negative_amount_raises(self):
        with pytest.raises(ValueError, match="positive"):
            Money("-50", "USD")

    def test_invalid_amount_raises(self):
        with pytest.raises(ValueError, match="Invalid monetary"):
            Money("abc", "INR")

    def test_currency_is_uppercased(self):
        m = Money("10.00", "eur")
        assert m.currency == "EUR"


# ---------------------------------------------------------------------------
# Aggregate factory tests
# ---------------------------------------------------------------------------


class TestPaymentAggregateCreate:
    def test_factory_creates_pending_aggregate(self):
        p = PaymentAggregate.create(amount="500.00", currency="INR")
        assert p.status == PaymentStatus.PENDING
        assert p.amount == Decimal("500.00")
        assert p.currency == "INR"
        assert p.payment_id
        assert p.transaction_id

    def test_factory_accepts_custom_transaction_id(self):
        p = PaymentAggregate.create(amount="10.00", currency="USD", transaction_id="TXN-001")
        assert p.transaction_id == "TXN-001"

    def test_factory_raises_on_zero_amount(self):
        with pytest.raises(ValueError, match="positive"):
            PaymentAggregate.create(amount="0", currency="INR")

    def test_factory_raises_on_invalid_currency(self):
        with pytest.raises(ValueError, match="ISO 4217"):
            PaymentAggregate.create(amount="100", currency="EURO")

    def test_factory_records_payment_initiated_event(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR", transaction_id="TXN-X")
        events = p.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], PaymentInitiated)
        assert events[0].aggregate_id == p.payment_id
        assert events[0].transaction_id == "TXN-X"
        assert events[0].amount == "100.00"
        assert events[0].currency == "INR"

    def test_collect_events_clears_internal_list(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        p.collect_events()  # first drain
        assert p.collect_events() == []


# ---------------------------------------------------------------------------
# State machine — happy path
# ---------------------------------------------------------------------------


class TestPaymentStateMachineHappyPath:
    def test_pending_to_authorized(self):
        p = PaymentAggregate.create(amount="200.00", currency="USD")
        p.collect_events()  # drain initiation event
        p.authorize("GW-REF-001")
        assert p.status == PaymentStatus.AUTHORIZED
        assert p.gateway_ref == "GW-REF-001"
        events = p.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], PaymentAuthorized)
        assert events[0].gateway_ref == "GW-REF-001"

    def test_authorized_to_captured(self):
        p = PaymentAggregate.create(amount="200.00", currency="USD")
        p.authorize("GW-REF-002")
        p.collect_events()  # drain
        p.capture()
        assert p.status == PaymentStatus.CAPTURED
        events = p.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], PaymentCaptured)
        assert events[0].gateway_ref == "GW-REF-002"

    def test_captured_to_refunded(self):
        p = PaymentAggregate.create(amount="200.00", currency="USD")
        p.authorize("GW-REF-003")
        p.capture()
        p.collect_events()
        p.refund()
        assert p.status == PaymentStatus.REFUNDED
        events = p.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], PaymentRefunded)

    def test_pending_to_failed(self):
        p = PaymentAggregate.create(amount="200.00", currency="INR")
        p.collect_events()
        p.fail("Gateway timeout")
        assert p.status == PaymentStatus.FAILED
        assert p.failure_reason == "Gateway timeout"
        events = p.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], PaymentFailed)
        assert events[0].reason == "Gateway timeout"

    def test_authorized_to_failed(self):
        p = PaymentAggregate.create(amount="200.00", currency="EUR")
        p.authorize("GW-REF-004")
        p.collect_events()
        p.fail("Capture declined")
        assert p.status == PaymentStatus.FAILED


# ---------------------------------------------------------------------------
# State machine — guardrail / illegal transition tests
# ---------------------------------------------------------------------------


class TestPaymentStateMachineGuardrails:
    def test_cannot_authorize_from_captured(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        p.authorize("REF")
        p.capture()
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            p.authorize("REF-2")
        err = exc_info.value
        assert err.from_state == PaymentStatus.CAPTURED
        assert err.to_state == PaymentStatus.AUTHORIZED

    def test_cannot_capture_from_pending(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        with pytest.raises(InvalidStateTransitionError):
            p.capture()

    def test_cannot_refund_from_pending(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        with pytest.raises(InvalidStateTransitionError):
            p.refund()

    def test_cannot_fail_from_captured(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        p.authorize("REF")
        p.capture()
        with pytest.raises(InvalidStateTransitionError):
            p.fail("Late failure")

    def test_cannot_fail_from_refunded(self):
        p = PaymentAggregate.create(amount="100.00", currency="INR")
        p.authorize("REF")
        p.capture()
        p.refund()
        with pytest.raises(InvalidStateTransitionError):
            p.fail("Too late")

    def test_terminal_states_have_no_outbound_transitions(self):
        assert _ALLOWED_TRANSITIONS[PaymentStatus.FAILED] == frozenset()
        assert _ALLOWED_TRANSITIONS[PaymentStatus.REFUNDED] == frozenset()


# ---------------------------------------------------------------------------
# Domain event contract tests
# ---------------------------------------------------------------------------


class TestDomainEventContracts:
    def test_domain_event_is_immutable(self):
        event = PaymentInitiated(
            aggregate_id="AGG-1",
            transaction_id="TXN-1",
            amount="100.00",
            currency="INR",
        )
        with pytest.raises((AttributeError, TypeError)):
            event.transaction_id = "MUTATED"  # type: ignore[misc]

    def test_to_dict_contains_required_envelope_fields(self):
        event = PaymentCaptured(
            aggregate_id="AGG-2",
            transaction_id="TXN-2",
            amount="500.00",
            currency="USD",
            gateway_ref="GW-001",
        )
        d = event.to_dict()
        assert "event_id" in d
        assert "event_type" in d
        assert "aggregate_id" in d
        assert "correlation_id" in d
        assert "occurred_at" in d
        assert d["event_type"] == "payment.captured"
        assert d["transaction_id"] == "TXN-2"
        assert d["gateway_ref"] == "GW-001"

    def test_with_correlation_id_returns_new_instance(self):
        event = PaymentFailed(
            aggregate_id="AGG-3",
            transaction_id="TXN-3",
            reason="Timeout",
        )
        enriched = event.with_correlation_id("CORR-999")
        assert enriched.correlation_id == "CORR-999"
        assert event.correlation_id == ""  # original unchanged

    def test_each_event_has_unique_event_id(self):
        e1 = PaymentInitiated(aggregate_id="A", transaction_id="T", amount="10", currency="INR")
        e2 = PaymentInitiated(aggregate_id="A", transaction_id="T", amount="10", currency="INR")
        assert e1.event_id != e2.event_id

    def test_payment_initiated_event_type(self):
        e = PaymentInitiated(aggregate_id="A", transaction_id="T", amount="1.00", currency="INR")
        assert e.event_type == "payment.initiated"

    def test_payment_authorized_event_type(self):
        e = PaymentAuthorized(aggregate_id="A", transaction_id="T", gateway_ref="G")
        assert e.event_type == "payment.authorized"

    def test_payment_captured_event_type(self):
        e = PaymentCaptured(aggregate_id="A", transaction_id="T", amount="1", currency="INR")
        assert e.event_type == "payment.captured"

    def test_payment_failed_event_type(self):
        e = PaymentFailed(aggregate_id="A", transaction_id="T", reason="err")
        assert e.event_type == "payment.failed"

    def test_payment_refunded_event_type(self):
        e = PaymentRefunded(aggregate_id="A", transaction_id="T", amount="1", currency="INR")
        assert e.event_type == "payment.refunded"
