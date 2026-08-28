"""
tests/domain/test_ledger.py
-----------------------------
Unit tests for the double-entry ledger domain model.
Zero infrastructure dependencies — pure domain logic only.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from domain.entities.ledger import LedgerEntry, LedgerTransaction
from domain.exceptions import LedgerImbalanceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TXN_ID = str(uuid.uuid4())


def _debit(amount: str, account: str = "ACC-DR", currency: str = "INR") -> LedgerEntry:
    return LedgerEntry.debit(
        transaction_id=TXN_ID, account_id=account, amount=amount, currency=currency
    )


def _credit(amount: str, account: str = "ACC-CR", currency: str = "INR") -> LedgerEntry:
    return LedgerEntry.credit(
        transaction_id=TXN_ID, account_id=account, amount=amount, currency=currency
    )


# ---------------------------------------------------------------------------
# LedgerEntry tests
# ---------------------------------------------------------------------------


class TestLedgerEntryConstruction:
    def test_amount_stored_as_decimal(self):
        e = _debit("100.00")
        assert isinstance(e.amount, Decimal)
        assert e.amount == Decimal("100.00")

    def test_amount_quantised_to_two_dp(self):
        e = _debit("99.999")  # rounds to 100.00
        assert e.amount == Decimal("100.00")

    def test_zero_amount_raises(self):
        with pytest.raises(ValueError, match="must not be zero"):
            LedgerEntry(
                transaction_id=TXN_ID,
                account_id="ACC",
                amount=Decimal("0.00"),
                currency="INR",
            )

    def test_invalid_amount_string_raises(self):
        with pytest.raises(ValueError, match="Invalid monetary amount"):
            LedgerEntry(
                transaction_id=TXN_ID,
                account_id="ACC",
                amount="abc",  # type: ignore[arg-type]  — exercised by _to_decimal()
                currency="INR",
            )

    def test_currency_uppercased(self):
        e = _debit("50.00", currency="usd")
        assert e.currency == "USD"

    def test_invalid_currency_raises(self):
        with pytest.raises(ValueError, match="ISO 4217"):
            LedgerEntry(
                transaction_id=TXN_ID,
                account_id="ACC",
                amount=Decimal("10.00"),
                currency="US",  # too short
            )

    def test_empty_transaction_id_raises(self):
        with pytest.raises(ValueError, match="transaction_id"):
            LedgerEntry(
                transaction_id="",
                account_id="ACC",
                amount=Decimal("10.00"),
                currency="INR",
            )

    def test_empty_account_id_raises(self):
        with pytest.raises(ValueError, match="account_id"):
            LedgerEntry(
                transaction_id=TXN_ID,
                account_id="",
                amount=Decimal("10.00"),
                currency="INR",
            )

    def test_entry_is_immutable(self):
        e = _debit("100.00")
        with pytest.raises((AttributeError, TypeError)):
            e.amount = Decimal("999.00")  # type: ignore[misc]

    def test_auto_generated_id_is_uuid(self):
        e = _debit("10.00")
        uuid.UUID(e.id)  # raises if not a valid UUID

    def test_debit_factory_produces_positive_amount(self):
        e = _debit("200.00")
        assert e.amount > Decimal("0")

    def test_credit_factory_produces_negative_amount(self):
        e = _credit("200.00")
        assert e.amount < Decimal("0")
        assert e.amount == Decimal("-200.00")

    def test_debit_factory_rejects_zero(self):
        with pytest.raises(ValueError, match="strictly positive"):
            LedgerEntry.debit(TXN_ID, "ACC", "0.00", "INR")

    def test_debit_factory_rejects_negative(self):
        with pytest.raises(ValueError, match="strictly positive"):
            LedgerEntry.debit(TXN_ID, "ACC", "-50.00", "INR")

    def test_credit_factory_rejects_zero(self):
        with pytest.raises(ValueError, match="strictly positive"):
            LedgerEntry.credit(TXN_ID, "ACC", "0.00", "INR")


# ---------------------------------------------------------------------------
# LedgerTransaction structural tests
# ---------------------------------------------------------------------------


class TestLedgerTransactionConstruction:
    def test_build_balanced_two_entry_transaction(self):
        entries = [_debit("500.00"), _credit("500.00")]
        txn = LedgerTransaction.build(reference="REF-001", entries=entries)
        assert txn.reference == "REF-001"
        assert len(txn.entries) == 2

    def test_build_balanced_multi_entry_transaction(self):
        # Three entries: one debit of 300, two credits of 150 each
        entries = [
            _debit("300.00", account="ACC-DR"),
            _credit("150.00", account="ACC-CR-1"),
            _credit("150.00", account="ACC-CR-2"),
        ]
        txn = LedgerTransaction.build(reference="REF-002", entries=entries)
        txn.verify_balance()  # must not raise

    def test_build_sets_transaction_id_on_entries(self):
        entries = [_debit("100.00"), _credit("100.00")]
        txn = LedgerTransaction.build(reference="REF-003", entries=entries)
        for entry in txn.entries:
            assert entry.transaction_id == txn.id

    def test_transaction_is_immutable(self):
        entries = [_debit("100.00"), _credit("100.00")]
        txn = LedgerTransaction.build(reference="REF-004", entries=entries)
        with pytest.raises((AttributeError, TypeError)):
            txn.reference = "MUTATED"  # type: ignore[misc]

    def test_fewer_than_two_entries_raises(self):
        with pytest.raises(ValueError, match="at least two"):
            LedgerTransaction.build(reference="REF-X", entries=[_debit("100.00")])

    def test_auto_generated_id_is_uuid(self):
        entries = [_debit("10.00"), _credit("10.00")]
        txn = LedgerTransaction.build(reference="REF-005", entries=entries)
        uuid.UUID(txn.id)

    def test_explicit_transaction_id_is_respected(self):
        entries = [_debit("10.00"), _credit("10.00")]
        explicit_id = str(uuid.uuid4())
        txn = LedgerTransaction.build(
            reference="REF-006", entries=entries, transaction_id=explicit_id
        )
        assert txn.id == explicit_id

    def test_mixed_currency_raises(self):
        entries = [
            _debit("100.00", currency="INR"),
            _credit("100.00", currency="USD"),
        ]
        with pytest.raises(ValueError, match="same currency"):
            LedgerTransaction.build(reference="REF-007", entries=entries)


# ---------------------------------------------------------------------------
# verify_balance() — the core double-entry invariant
# ---------------------------------------------------------------------------


class TestVerifyBalance:
    def test_balanced_transaction_does_not_raise(self):
        entries = [_debit("1000.00"), _credit("1000.00")]
        txn = LedgerTransaction.build(reference="VB-001", entries=entries)
        txn.verify_balance()  # must be silent

    def test_imbalanced_transaction_raises_ledger_imbalance_error(self):
        # Build bypassing the factory to inject an imbalanced transaction directly
        txn_id = str(uuid.uuid4())
        dr = LedgerEntry(
            transaction_id=txn_id, account_id="ACC-DR",
            amount=Decimal("500.00"), currency="INR",
        )
        cr = LedgerEntry(
            transaction_id=txn_id, account_id="ACC-CR",
            amount=Decimal("-400.00"), currency="INR",  # intentional mismatch
        )
        txn = LedgerTransaction(
            id=txn_id, reference="VB-BAD", entries=(dr, cr)
        )
        with pytest.raises(LedgerImbalanceError) as exc_info:
            txn.verify_balance()
        err = exc_info.value
        assert err.transaction_id == txn_id
        assert err.imbalance == Decimal("100.00")

    def test_ledger_imbalance_error_is_domain_exception(self):
        from domain.exceptions import DomainException
        assert issubclass(LedgerImbalanceError, DomainException)

    def test_build_factory_calls_verify_balance_automatically(self):
        # Construct a deliberately unbalanced set and expect build() to reject it
        txn_id = str(uuid.uuid4())
        dr = LedgerEntry(
            transaction_id=txn_id, account_id="ACC-DR",
            amount=Decimal("100.00"), currency="INR",
        )
        cr = LedgerEntry(
            transaction_id=txn_id, account_id="ACC-CR",
            amount=Decimal("-90.00"), currency="INR",
        )
        with pytest.raises(LedgerImbalanceError):
            LedgerTransaction.build(
                reference="VB-FACTORY-BAD",
                entries=[dr, cr],
                transaction_id=txn_id,
            )

    def test_precise_decimal_balance_not_float(self):
        # 0.1 + 0.2 == 0.3 is False in float; must be True in Decimal
        entries = [
            LedgerEntry(
                transaction_id=TXN_ID, account_id="A",
                amount=Decimal("0.10"), currency="INR"
            ),
            LedgerEntry(
                transaction_id=TXN_ID, account_id="B",
                amount=Decimal("0.20"), currency="INR"
            ),
            LedgerEntry(
                transaction_id=TXN_ID, account_id="C",
                amount=Decimal("-0.30"), currency="INR"
            ),
        ]
        txn = LedgerTransaction.build(reference="VB-DECIMAL", entries=entries)
        txn.verify_balance()  # would fail with float due to 0.1+0.2 != 0.3


# ---------------------------------------------------------------------------
# Computed properties
# ---------------------------------------------------------------------------


class TestComputedProperties:
    def test_total_debits(self):
        entries = [_debit("300.00"), _debit("200.00"), _credit("500.00")]
        txn = LedgerTransaction.build(reference="CP-001", entries=entries)
        assert txn.total_debits == Decimal("500.00")

    def test_total_credits(self):
        entries = [_debit("500.00"), _credit("300.00"), _credit("200.00")]
        txn = LedgerTransaction.build(reference="CP-002", entries=entries)
        assert txn.total_credits == Decimal("500.00")

    def test_currency_property(self):
        entries = [_debit("100.00", currency="USD"), _credit("100.00", currency="USD")]
        txn = LedgerTransaction.build(reference="CP-003", entries=entries)
        assert txn.currency == "USD"
