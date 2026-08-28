"""
presentation/api/v1/schemas.py
--------------------------------
Pydantic request/response schemas for the v1 Payments API.

Design rules:
  - No domain objects cross this boundary.  All domain types are mapped to
    plain Python primitives before being handed to Pydantic.
  - ``amount`` is modelled as ``Decimal`` on the wire (serialised as a JSON
    string so the client receives "100.00", never 100.0).
  - The ``field_validator`` on CreatePaymentRequest rejects raw float values
    before the orchestrator ever sees the request.
  - RFC 7807 Problem Details (https://www.rfc-editor.org/rfc/rfc7807) is used
    for all 4xx error responses that carry domain-level semantics.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Annotated amount type — reused by both request and response schemas
# ---------------------------------------------------------------------------

_PositiveDecimal = Annotated[
    Decimal,
    Field(
        gt=0,
        description=(
            "Monetary amount as an exact decimal (e.g. \"100.00\"). "
            "Must be strictly greater than zero. "
            "Transmitted as a JSON string to prevent floating-point precision loss."
        ),
        examples=["100.00", "1999.50", "0.01"],
    ),
]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreatePaymentRequest(BaseModel):
    """
    POST /api/v1/payments — request body.

    ``reference_id`` is the upstream order / invoice identifier supplied by
    the merchant.  It is used as ``transaction_id`` on the aggregate and as
    the idempotency handle at the domain level.
    """

    model_config = ConfigDict(json_encoders={Decimal: str})

    amount: _PositiveDecimal
    currency: str = Field(
        ...,
        min_length=3,
        max_length=3,
        description="ISO 4217 three-letter currency code (e.g. \"INR\", \"USD\").",
        examples=["INR", "USD", "EUR"],
    )
    source_token: str = Field(
        ...,
        min_length=1,
        description=(
            "Tokenised payment source returned by the client-side SDK "
            "(card token, UPI VPA, etc.)."
        ),
        examples=["tok_visa_4242", "upi:user@bank"],
    )
    reference_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Merchant-supplied idempotency handle — maps back to the upstream "
            "order or invoice.  Duplicate requests with the same reference_id "
            "are deduplicated by the idempotency layer."
        ),
        examples=["order_8a3f91", "inv-2024-10050001"],
    )

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount_to_decimal(cls, v: object) -> Decimal:
        """
        Accept numeric strings from JSON bodies and coerce to Decimal.
        Reject float literals to enforce exact precision at the API boundary.
        """
        if isinstance(v, float):
            raise ValueError(
                "amount must be supplied as a decimal string (e.g. \"100.00\"), "
                "not a JSON number, to prevent floating-point precision loss."
            )
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Invalid amount value: {v!r}") from exc


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PaymentResponse(BaseModel):
    """
    Standard payment response — returned on 201 (CAPTURED) and 200 (GET poll).

    ``amount`` is serialised as a string to guarantee the client receives
    the exact decimal that was stored (e.g. "100.00", never 100.0).
    """

    model_config = ConfigDict(json_encoders={Decimal: str})

    id: str = Field(..., description="Stable aggregate UUID assigned by the system.")
    reference_id: str = Field(..., description="Merchant-supplied reference_id echoed back.")
    status: str = Field(
        ...,
        description="Payment lifecycle state: PENDING | AUTHORIZED | CAPTURED | FAILED | REFUNDED.",
    )
    amount: Decimal = Field(..., description="Settled amount as an exact decimal string.")
    currency: str = Field(..., description="ISO 4217 three-letter currency code.")
    gateway_ref: str | None = Field(
        None, description="Acquirer reference number, populated on CAPTURED."
    )
    ledger_txn_id: str | None = Field(
        None, description="ID of the double-entry LedgerTransaction, populated on CAPTURED."
    )
    created_at: datetime = Field(..., description="UTC timestamp at which the payment was created.")


class LedgerEntryResponse(BaseModel):
    """
    A single immutable debit or credit line within a LedgerTransaction.

    Exposed by GET /api/v1/payments/{payment_id}/ledger for audit purposes.
    The sum of all ``amount`` values across entries for one transaction
    must equal zero (double-entry invariant).

    Sign convention:
      positive amount → debit  (e.g. 100.00 on ACCOUNTS_RECEIVABLE)
      negative amount → credit (e.g. -100.00 on GATEWAY_PAYABLE)
    """

    model_config = ConfigDict(json_encoders={Decimal: str})

    account_id: str = Field(
        ...,
        description="Nominal ledger account code (e.g. '1100.ACCOUNTS_RECEIVABLE').",
    )
    amount: Decimal = Field(
        ..., description="Entry amount: positive=debit, negative=credit."
    )
    currency: str = Field(..., description="ISO 4217 three-letter currency code.")
    created_at: datetime = Field(
        ..., description="UTC timestamp at which this entry was recorded."
    )


class LedgerResponse(BaseModel):
    """
    Full ledger view for a payment — returned by
    GET /api/v1/payments/{payment_id}/ledger.

    ``entries`` contains all debit and credit lines.
    """

    model_config = ConfigDict(json_encoders={Decimal: str})

    payment_id: str
    entries: list[LedgerEntryResponse]


# ---------------------------------------------------------------------------
# RFC 7807 Problem Details — used for 422 / 4xx domain errors
# ---------------------------------------------------------------------------


class ProblemDetail(BaseModel):
    """
    RFC 7807 Problem Details object.

    https://www.rfc-editor.org/rfc/rfc7807

    Used for domain-level error responses (card declined, duplicate, etc.)
    so clients can distinguish between framework validation failures
    (FastAPI's native 422) and domain-semantic rejections.
    """

    type: str = Field(
        ...,
        description="A URI identifying the problem type (e.g. 'payment-declined').",
        examples=["payment-declined", "duplicate-transaction"],
    )
    title: str = Field(
        ...,
        description="Short, human-readable summary of the problem type.",
        examples=["Gateway Rejected", "Duplicate Transaction"],
    )
    status: int = Field(..., description="HTTP status code.", examples=[422, 409])
    detail: str = Field(
        ..., description="Human-readable explanation specific to this occurrence."
    )
    instance: str | None = Field(
        None,
        description="URI identifying the specific resource that caused the error.",
    )
