"""
domain/entities/ledger.py
--------------------------
Immutable Double-Entry Ledger domain model.

Design principles:
  - ZERO framework leakage: stdlib only (dataclasses, decimal, datetime, uuid).
  - All monetary amounts use ``Decimal`` — float is explicitly prohibited.
  - ``LedgerEntry`` and ``LedgerTransaction`` are frozen dataclasses: immutable
    after construction.
  - ``LedgerTransaction.verify_balance()`` enforces the fundamental double-entry
    invariant: debits + credits must net to exactly ``Decimal('0.00')``.
  - Violations raise ``DomainException`` (not ``ValueError``) so the domain
    exception hierarchy remains the single catch boundary for callers.

Double-entry primer
-------------------
Every financial movement is recorded as at least two entries:
  - A **debit** entry on one account  (positive amount by convention here)
  - A **credit** entry on another     (negative amount by convention here)

The sum of all entry amounts in a balanced transaction must be zero:
    debit(+100.00) + credit(-100.00) == 0.00  ✓
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from functools import reduce
from typing import Sequence


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TWO_PLACES = Decimal("0.01")


def _to_decimal(value: Decimal | int | str | float) -> Decimal:
    """
    Coerce *value* to a ``Decimal`` quantised to two decimal places.

    Raises:
        ValueError: If *value* cannot be converted to a valid ``Decimal``.
    """
    try:
        return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(
            f"Invalid monetary amount '{value}': cannot convert to Decimal."
        ) from exc


# ---------------------------------------------------------------------------
# LedgerEntry — a single debit or credit line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """
    A single immutable line in a double-entry ledger transaction.

    By convention:
      - **Positive** ``amount`` represents a **debit** (value flowing into an account).
      - **Negative** ``amount`` represents a **credit** (value flowing out of an account).

    Attributes:
        id              Unique entry identity (UUID string, auto-generated).
        transaction_id  Reference to the containing ``LedgerTransaction``.
        account_id      The account being debited or credited.
        amount          Monetary amount as ``Decimal`` — must not be zero.
        currency        ISO 4217 three-letter currency code (e.g. ``"INR"``).
        created_at      UTC timestamp at which the entry was recorded.
    """

    transaction_id: str
    account_id: str
    amount: Decimal
    currency: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        # --- amount ---
        # Coerce to Decimal (handles int / str inputs passed by callers)
        # object.__setattr__ is required because the dataclass is frozen.
        try:
            coerced_amount = _to_decimal(self.amount)
        except ValueError as exc:
            raise ValueError(
                f"LedgerEntry.amount is invalid: {exc}"
            ) from exc

        object.__setattr__(self, "amount", coerced_amount)

        if self.amount == Decimal("0.00"):
            raise ValueError(
                "LedgerEntry.amount must not be zero. "
                "Use a positive value for debits and a negative value for credits."
            )

        # --- currency ---
        if not self.currency or len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError(
                f"LedgerEntry.currency must be a 3-letter ISO 4217 code; "
                f"got '{self.currency}'."
            )
        object.__setattr__(self, "currency", self.currency.upper())

        # --- ids ---
        if not self.transaction_id:
            raise ValueError("LedgerEntry.transaction_id must not be empty.")
        if not self.account_id:
            raise ValueError("LedgerEntry.account_id must not be empty.")

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def debit(
        cls,
        transaction_id: str,
        account_id: str,
        amount: Decimal | int | str,
        currency: str,
    ) -> "LedgerEntry":
        """
        Construct a debit entry (positive amount).

        Raises:
            ValueError: If *amount* is not strictly positive.
        """
        decimal_amount = _to_decimal(amount)
        if decimal_amount <= Decimal("0.00"):
            raise ValueError(
                f"Debit amount must be strictly positive; got {decimal_amount}."
            )
        return cls(
            transaction_id=transaction_id,
            account_id=account_id,
            amount=decimal_amount,
            currency=currency,
        )

    @classmethod
    def credit(
        cls,
        transaction_id: str,
        account_id: str,
        amount: Decimal | int | str,
        currency: str,
    ) -> "LedgerEntry":
        """
        Construct a credit entry (negative amount).

        Pass *amount* as a positive value — this factory negates it automatically.

        Raises:
            ValueError: If *amount* is not strictly positive.
        """
        decimal_amount = _to_decimal(amount)
        if decimal_amount <= Decimal("0.00"):
            raise ValueError(
                f"Credit amount must be supplied as a strictly positive value; "
                f"got {decimal_amount}. The factory negates it automatically."
            )
        return cls(
            transaction_id=transaction_id,
            account_id=account_id,
            amount=-decimal_amount,
            currency=currency,
        )


# ---------------------------------------------------------------------------
# LedgerTransaction — an atomic, balanced group of entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerTransaction:
    """
    An immutable, atomic double-entry ledger transaction.

    A ``LedgerTransaction`` is the unit of atomicity for the ledger: all of its
    ``LedgerEntry`` objects are persisted together or not at all.  The
    transaction is considered **balanced** when the sum of all entry amounts
    equals exactly ``Decimal('0.00')``.

    Attributes:
        id          Unique transaction identity (UUID string, auto-generated).
        reference   External reference, e.g. ``PaymentAggregate.payment_id``.
        entries     Ordered, immutable sequence of ``LedgerEntry`` objects.
        description Human-readable description of the economic event.
        created_at  UTC timestamp at which the transaction was recorded.
    """

    reference: str
    entries: tuple[LedgerEntry, ...]
    description: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.reference:
            raise ValueError("LedgerTransaction.reference must not be empty.")

        # Coerce any mutable list to an immutable tuple so frozen is honoured.
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))

        if len(self.entries) < 2:
            raise ValueError(
                "LedgerTransaction must contain at least two LedgerEntry objects "
                "(one debit and one credit)."
            )

        # Validate all entries reference this transaction.
        # (Entries created via the factory classmethod always carry the right id,
        # but direct construction could supply a mismatch.)
        for entry in self.entries:
            if entry.transaction_id != self.id:
                raise ValueError(
                    f"LedgerEntry '{entry.id}' references transaction "
                    f"'{entry.transaction_id}' but belongs to transaction '{self.id}'."
                )

        # Validate currency homogeneity — mixed-currency transactions require
        # explicit FX conversion entries and are rejected here.
        currencies = {e.currency for e in self.entries}
        if len(currencies) > 1:
            raise ValueError(
                f"All entries in a LedgerTransaction must share the same currency. "
                f"Found: {sorted(currencies)}. Create separate transactions per currency "
                f"and link them via a foreign-exchange bridge entry."
            )

    # ------------------------------------------------------------------
    # Core invariant
    # ------------------------------------------------------------------

    def verify_balance(self) -> None:
        """
        Assert that this transaction satisfies the double-entry invariant.

        Sums the ``amount`` of every ``LedgerEntry`` using ``Decimal`` arithmetic
        and checks that the result equals exactly ``Decimal('0.00')``.

        Raises:
            LedgerImbalanceError (DomainException): When the net sum is not zero,
                indicating that debits do not equal credits. The exception detail
                includes the actual imbalance so callers can surface it.

        Example::

            txn = LedgerTransaction.build(
                reference="pay-001",
                description="Payment capture",
                entries=[
                    LedgerEntry.debit(..., amount="500.00", ...),
                    LedgerEntry.credit(..., amount="500.00", ...),
                ]
            )
            txn.verify_balance()  # passes silently
        """
        from domain.exceptions import LedgerImbalanceError

        net: Decimal = reduce(
            lambda acc, e: acc + e.amount,
            self.entries,
            Decimal("0.00"),
        ).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)

        if net != Decimal("0.00"):
            raise LedgerImbalanceError(
                transaction_id=self.id,
                reference=self.reference,
                imbalance=net,
            )

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def total_debits(self) -> Decimal:
        """Sum of all positive (debit) entry amounts."""
        return sum(
            (e.amount for e in self.entries if e.amount > Decimal("0")),
            Decimal("0.00"),
        ).quantize(_TWO_PLACES)

    @property
    def total_credits(self) -> Decimal:
        """Sum of all negative (credit) entry amounts (returned as a positive value)."""
        return abs(
            sum(
                (e.amount for e in self.entries if e.amount < Decimal("0")),
                Decimal("0.00"),
            )
        ).quantize(_TWO_PLACES)

    @property
    def currency(self) -> str:
        """Currency shared by all entries (validated in ``__post_init__``)."""
        return self.entries[0].currency

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        reference: str,
        entries: Sequence[LedgerEntry],
        description: str = "",
        transaction_id: str | None = None,
    ) -> "LedgerTransaction":
        """
        Preferred factory: builds a ``LedgerTransaction``, re-stamps every
        entry with the correct ``transaction_id``, and calls ``verify_balance()``
        before returning.

        This guarantees that any ``LedgerTransaction`` obtained via this
        factory is structurally valid and balanced.

        Args:
            reference:      External reference (e.g. a ``PaymentAggregate.payment_id``).
            entries:        Sequence of ``LedgerEntry`` objects to include.
            description:    Human-readable description of the economic event.
            transaction_id: Optional explicit ID; auto-generated when omitted.

        Raises:
            ValueError:           On structural violations (< 2 entries, mixed currency).
            LedgerImbalanceError: When debits ≠ credits.
        """
        txn_id = transaction_id or str(uuid.uuid4())

        # Re-stamp each entry with the transaction's id.
        stamped: list[LedgerEntry] = []
        for entry in entries:
            # Use object.__setattr__ via dataclasses.replace equivalent
            stamped.append(
                LedgerEntry(
                    id=entry.id,
                    transaction_id=txn_id,
                    account_id=entry.account_id,
                    amount=entry.amount,
                    currency=entry.currency,
                    created_at=entry.created_at,
                )
            )

        txn = cls(
            id=txn_id,
            reference=reference,
            entries=tuple(stamped),
            description=description,
        )
        txn.verify_balance()
        return txn
