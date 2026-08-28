"""
application/use_cases/process_payment.py
-----------------------------------------
ProcessPaymentUseCase — the core payment processing orchestrator.

Responsibilities (in order of execution):
  1.  Application-level idempotency guard via Redis.
      • If a result already exists for `idempotency_key` → return it immediately
        without touching the database (safe to replay on network retries).
  2.  Duplicate-transaction guard via the payment repository.
      • If `transaction_id` already exists → raise DuplicateTransactionError.
  3.  Build and validate the PaymentAggregate (domain invariants enforced).
  4.  Call the payment gateway (via UoW gateway port).
      • On success → call aggregate.authorize() then aggregate.capture().
      • On failure  → call aggregate.fail().
  5.  Atomically (single DB transaction via UoW):
      • Persist the aggregate to the payments table.
      • Drain domain events via aggregate.collect_events() and write each one
        to the transactional outbox table (guarantee: event ↔ state change are
        in the same transaction → no phantom / lost events).
  6.  Cache the serialised result in Redis (idempotency replay store).
  7.  Return the mutated PaymentAggregate to the caller.

Architecture invariants:
  - This module imports ONLY from `domain.*` and `application.*`.
  - Infrastructure is *injected* via AbstractUnitOfWork — no concrete imports.
  - The Redis client is injected via constructor for testability.
  - structlog is used for structured, correlation-aware logging.
"""
from __future__ import annotations

import json
import structlog

from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import (
    DuplicateTransactionError,
    PaymentGatewayError,
    InvalidStateTransitionError,
)
from domain.events.payment_events import DomainEvent
from application.uow import AbstractUnitOfWork

logger = structlog.get_logger(__name__)

# Redis TTL for idempotency response cache (seconds)
_IDEMPOTENCY_TTL_SECONDS: int = 300


# ---------------------------------------------------------------------------
# Input / Output contracts  (no Pydantic — pure dataclasses for domain purity)
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ProcessPaymentCommand:
    """
    Immutable command object (Data Transfer Object) that carries all the
    information required to initiate a payment.

    Constructed in the *presentation layer* and handed to this use case.
    Using a command DTO (rather than accepting a PaymentAggregate directly)
    means the use case owns the aggregate lifecycle from creation onwards,
    preserving full encapsulation.

    Attributes:
        amount          Gross amount as a string to preserve decimal precision.
        currency        ISO 4217 3-letter currency code.
        transaction_id  External idempotency key / merchant reference.
        idempotency_key HTTP X-Idempotency-Key header value (optional).
        correlation_id  Distributed trace correlation ID from the HTTP layer.
    """

    amount: str
    currency: str
    transaction_id: str
    idempotency_key: str = ""
    correlation_id: str = ""


@dataclass(frozen=True)
class ProcessPaymentResult:
    """
    Immutable result object returned by the use case.
    The presentation layer maps this to the HTTP response DTO.
    """

    payment_id: str
    transaction_id: str
    amount: str
    currency: str
    status: str
    gateway_ref: str | None
    failure_reason: str | None


# ---------------------------------------------------------------------------
# Use Case
# ---------------------------------------------------------------------------


class ProcessPaymentUseCase:
    """
    Core payment processing use case.

    Injection points:
        uow         AbstractUnitOfWork — provides .payments, .outbox, .gateway
        redis       RedisClient-compatible object (duck-typed) — for idempotency
    """

    def __init__(self, uow: AbstractUnitOfWork, redis) -> None:  # type: ignore[type-arg]
        self._uow = uow
        self._redis = redis

    async def execute(self, command: ProcessPaymentCommand) -> ProcessPaymentResult:
        """
        Execute the full payment processing workflow.

        Returns:
            ProcessPaymentResult describing the outcome.

        Raises:
            DuplicateTransactionError   — transaction_id already processed.
            PaymentGatewayError         — all acquirers are unavailable.
            InvalidStateTransitionError — domain logic bug (should never reach prod).
        """
        log = logger.bind(
            transaction_id=command.transaction_id,
            idempotency_key=command.idempotency_key or "none",
            correlation_id=command.correlation_id or "none",
        )

        # ------------------------------------------------------------------
        # Step 1 — Application-layer idempotency guard (Redis)
        # ------------------------------------------------------------------
        if command.idempotency_key:
            cached = await self._check_idempotency_cache(command.idempotency_key)
            if cached is not None:
                log.info("process_payment_idempotent_replay")
                return cached

        # ------------------------------------------------------------------
        # Step 2 — Duplicate transaction guard (DB-level, inside UoW)
        # ------------------------------------------------------------------
        async with self._uow as uow:
            already_exists = await uow.payments.exists(command.transaction_id)
            if already_exists:
                log.warning("process_payment_duplicate_transaction")
                raise DuplicateTransactionError(
                    f"Transaction '{command.transaction_id}' has already been processed.",
                    detail=(
                        f"A payment with transaction_id='{command.transaction_id}' "
                        "already exists in the system.  Supply a new transaction_id "
                        "or use an idempotency key to replay the original response."
                    ),
                )

        # ------------------------------------------------------------------
        # Step 3 — Build aggregate (domain invariants enforced by __post_init__)
        # ------------------------------------------------------------------
        log.info("process_payment_initiated")
        payment = PaymentAggregate.create(
            amount=Decimal(command.amount),
            currency=command.currency,
            transaction_id=command.transaction_id,
        )

        # ------------------------------------------------------------------
        # Step 4 — Call payment gateway  (infrastructure injected via UoW)
        # ------------------------------------------------------------------
        gateway_response: dict = {}
        gateway_error: str | None = None

        try:
            log.info("process_payment_gateway_charge", acquirer="primary")
            gateway_response = await self._uow.gateway.charge_with_fallback(
                {
                    "transaction_id": payment.transaction_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                }
            )
            gateway_ref: str = gateway_response.get("reference_id", "")

            # Authorize then immediately capture (one-step acquirer flow)
            payment.authorize(gateway_ref)
            payment.capture()
            log.info(
                "process_payment_gateway_success",
                gateway_ref=gateway_ref,
                status=payment.status.value,
            )

        except (PaymentGatewayError, Exception) as exc:
            # Distinguish domain gateway errors from unexpected infra failures
            reason = str(exc)
            gateway_error = reason
            log.error("process_payment_gateway_failed", error=reason)

            try:
                payment.fail(reason)
            except InvalidStateTransitionError:
                # Should never happen on a freshly-created aggregate,
                # but we guard defensively.
                log.critical(
                    "process_payment_unexpected_state_on_fail",
                    current_state=payment.status.value,
                )

            # Re-raise only genuine gateway errors — let the presentation
            # layer translate them to HTTP 502/503.
            if isinstance(exc, PaymentGatewayError):
                # Still persist the FAILED aggregate + events before raising
                await self._persist_aggregate(uow=None, payment=payment, command=command, log=log)
                raise

        # ------------------------------------------------------------------
        # Step 5 — Atomically persist aggregate + outbox events
        # ------------------------------------------------------------------
        await self._persist_aggregate(uow=None, payment=payment, command=command, log=log)

        # ------------------------------------------------------------------
        # Step 6 — Build result and populate idempotency cache
        # ------------------------------------------------------------------
        result = ProcessPaymentResult(
            payment_id=payment.payment_id,
            transaction_id=payment.transaction_id,
            amount=str(payment.amount),
            currency=payment.currency,
            status=payment.status.value,
            gateway_ref=payment.gateway_ref,
            failure_reason=payment.failure_reason,
        )

        if command.idempotency_key:
            await self._cache_idempotency_result(command.idempotency_key, result)

        log.info(
            "process_payment_complete",
            status=payment.status.value,
            payment_id=payment.payment_id,
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _persist_aggregate(
        self,
        uow,  # typed as None at call-site so we always open a fresh context
        payment: PaymentAggregate,
        command: ProcessPaymentCommand,
        log,
    ) -> None:
        """
        Opens a fresh UoW context and atomically:
          1. Saves the aggregate to the payments table.
          2. Drains the aggregate's domain events and writes each to the outbox.
        """
        async with self._uow as uow:
            await uow.payments.add(payment)

            # Drain events from the aggregate.  collect_events() clears the
            # internal list atomically so events are never double-published.
            events: list[DomainEvent] = payment.collect_events()
            for event in events:
                # Enrich every event with the HTTP-layer correlation_id before
                # writing to the outbox so downstream consumers can correlate.
                enriched = event.with_correlation_id(command.correlation_id)
                await uow.outbox.enqueue(
                    aggregate_type="Payment",
                    aggregate_id=payment.payment_id,
                    event_type=enriched.event_type,
                    payload=json.dumps(enriched.to_dict()),
                )
                log.debug(
                    "process_payment_event_enqueued",
                    event_type=enriched.event_type,
                    event_id=enriched.event_id,
                )

    async def _check_idempotency_cache(
        self, idempotency_key: str
    ) -> ProcessPaymentResult | None:
        """
        Attempts to retrieve a previously-cached ProcessPaymentResult from Redis.
        Returns None on a cache miss or deserialisation failure.
        """
        cache_key = f"payment:idempotency:{idempotency_key}"
        try:
            raw = await self._redis.get(cache_key)
            if raw:
                data = json.loads(raw)
                return ProcessPaymentResult(**data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "process_payment_idempotency_cache_read_error",
                idempotency_key=idempotency_key,
                error=str(exc),
            )
        return None

    async def _cache_idempotency_result(
        self, idempotency_key: str, result: ProcessPaymentResult
    ) -> None:
        """
        Serialises and caches the result in Redis with a 5-minute TTL.
        Cache write failures are logged and swallowed — they are non-critical.
        """
        cache_key = f"payment:idempotency:{idempotency_key}"
        try:
            payload = json.dumps(
                {
                    "payment_id": result.payment_id,
                    "transaction_id": result.transaction_id,
                    "amount": result.amount,
                    "currency": result.currency,
                    "status": result.status,
                    "gateway_ref": result.gateway_ref,
                    "failure_reason": result.failure_reason,
                }
            )
            await self._redis.set(cache_key, payload, expire=_IDEMPOTENCY_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "process_payment_idempotency_cache_write_error",
                idempotency_key=idempotency_key,
                error=str(exc),
            )
