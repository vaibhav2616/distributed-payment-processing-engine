"""
infrastructure/external/circuit_breaker.py
-------------------------------------------
Distributed Circuit Breaker implemented with Redis.
Prevents cascading network failures and protects PaymentGatewayInterface.

States:
  - CLOSED: Normal operation. Consecutively counts failures.
  - OPEN: Outage mode. Fast-fails immediately without calling downstream service.
          Has a 60s Redis TTL cool-off period.
  - HALF-OPEN: Recovery testing. After 60s TTL, allows exactly one probe request.
               If probe succeeds -> transitions to CLOSED.
               If probe fails -> reverts to OPEN with fresh 60s TTL.
"""
from __future__ import annotations

import functools
from enum import Enum
from typing import Any, Callable

import httpx
import structlog

logger = structlog.get_logger(__name__)


class CircuitBreakerState(str, Enum):
    """Circuit breaker operational states."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF-OPEN"


class CircuitBreakerOpenException(Exception):
    """
    Raised when an operation is short-circuited because the circuit breaker is OPEN
    or HALF-OPEN with a probe request already in flight.
    """

    def __init__(
        self,
        message: str = "Circuit breaker is OPEN. Upstream service is unavailable.",
        *,
        retry_after: int = 60,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class RedisCircuitBreaker:
    """
    Distributed Redis-backed Circuit Breaker.

    Manages state across distributed pods via atomic Redis operations.

    Args:
        redis_client: Optional async Redis client or wrapper. Defaults to singleton.
        name: Name identifier for the protected service (e.g., 'payment_gateway').
        failure_threshold: Number of consecutive failures before opening breaker (default 5).
        cool_off_period: Cooldown window in seconds before attempting recovery (default 60).
    """

    def __init__(
        self,
        redis_client: Any = None,
        name: str = "payment_gateway",
        failure_threshold: int = 5,
        cool_off_period: int = 60,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cool_off_period = cool_off_period

        # Redis key names
        self.open_key = f"circuit_breaker:{name}:open"
        self.tripped_key = f"circuit_breaker:{name}:tripped"
        self.failure_key = f"circuit_breaker:{name}:failures"
        self.half_open_probe_key = f"circuit_breaker:{name}:probe"

        self._redis_override = redis_client

    @property
    def redis(self) -> Any:
        """Dynamically resolve the active Redis client."""
        if self._redis_override is not None:
            if hasattr(self._redis_override, "client"):
                return self._redis_override.client
            return self._redis_override

        from infrastructure.cache.redis import redis_client
        if hasattr(redis_client, "client"):
            return redis_client.client
        return redis_client

    async def get_state(self) -> CircuitBreakerState:
        """
        Determine the current state of the circuit breaker.

        Transitions:
          - If open_key exists in Redis -> OPEN
          - Else if tripped_key exists in Redis -> HALF-OPEN (cool-off expired)
          - Else -> CLOSED
        """
        is_open = await self.redis.get(self.open_key)
        if is_open:
            return CircuitBreakerState.OPEN

        is_tripped = await self.redis.get(self.tripped_key)
        if is_tripped:
            return CircuitBreakerState.HALF_OPEN

        return CircuitBreakerState.CLOSED

    async def acquire_permission(self) -> None:
        """
        Check if a request is permitted through the circuit breaker.

        Raises:
            CircuitBreakerOpenException: If breaker is OPEN or HALF-OPEN probe is in-flight.
        """
        state = await self.get_state()

        if state == CircuitBreakerState.OPEN:
            logger.warning(
                "circuit_breaker_fast_failure",
                name=self.name,
                state=state.value,
                retry_after=self.cool_off_period,
            )
            raise CircuitBreakerOpenException(
                f"Circuit breaker '{self.name}' is OPEN. Fast failing request.",
                retry_after=self.cool_off_period,
            )

        if state == CircuitBreakerState.HALF_OPEN:
            # Atomic test-and-set: allow exactly ONE probe request through
            acquired = await self.redis.set(
                self.half_open_probe_key,
                "1",
                nx=True,
                ex=self.cool_off_period,
            )
            if not acquired:
                logger.warning(
                    "circuit_breaker_half_open_probe_in_flight",
                    name=self.name,
                    state=state.value,
                    retry_after=self.cool_off_period,
                )
                raise CircuitBreakerOpenException(
                    f"Circuit breaker '{self.name}' is HALF-OPEN. Recovery probe already in flight.",
                    retry_after=self.cool_off_period,
                )
            logger.info("circuit_breaker_half_open_probe_allowed", name=self.name)

    async def ensure_available(self) -> None:
        """
        Pre-flight check without making an HTTP call.
        Used by callers (like the payment orchestrator) to reject requests immediately
        before opening database transactions or persisting pending state.
        """
        state = await self.get_state()
        if state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpenException(
                f"Circuit breaker '{self.name}' is OPEN.",
                retry_after=self.cool_off_period,
            )
        if state == CircuitBreakerState.HALF_OPEN:
            # If a probe is already in flight, other requests should fail fast
            in_flight = await self.redis.get(self.half_open_probe_key)
            if in_flight:
                raise CircuitBreakerOpenException(
                    f"Circuit breaker '{self.name}' is HALF-OPEN and probe is already in flight.",
                    retry_after=self.cool_off_period,
                )

    async def record_success(self) -> None:
        """
        Record a successful response (or healthy domain 4xx).

        If in HALF-OPEN state: resets breaker to CLOSED.
        If in CLOSED state: resets consecutive failure count to 0.
        """
        current_state = await self.get_state()
        await self.redis.delete(
            self.failure_key,
            self.open_key,
            self.tripped_key,
            self.half_open_probe_key,
        )
        if current_state == CircuitBreakerState.HALF_OPEN:
            logger.info("circuit_breaker_recovered_to_closed", name=self.name)

    async def record_failure(self) -> None:
        """
        Record a network timeout or 5xx server failure.

        If in HALF-OPEN state: reverts back to OPEN with fresh 60s TTL.
        If in CLOSED state: increments failure count. If count >= 5, trips to OPEN with 60s TTL.
        """
        current_state = await self.get_state()

        if current_state == CircuitBreakerState.HALF_OPEN:
            # Probe failed: revert to OPEN for another cool_off_period
            await self.redis.set(self.open_key, "1", ex=self.cool_off_period)
            await self.redis.delete(self.half_open_probe_key)
            logger.warning(
                "circuit_breaker_probe_failed_reverting_to_open",
                name=self.name,
                cool_off=self.cool_off_period,
            )
            return

        # CLOSED state: increment consecutive failures
        failures = await self.redis.incr(self.failure_key)
        if int(failures) >= self.failure_threshold:
            # 5 consecutive failures reached: transition to OPEN
            await self.redis.set(self.open_key, "1", ex=self.cool_off_period)
            await self.redis.set(self.tripped_key, "1")
            await self.redis.delete(self.failure_key)
            logger.error(
                "circuit_breaker_tripped_to_open",
                name=self.name,
                consecutive_failures=failures,
                threshold=self.failure_threshold,
                cool_off=self.cool_off_period,
            )

    def is_failure(self, exc: BaseException) -> bool:
        """
        Determine if an exception counts as a gateway network failure.

        Strictly counts:
          - Network timeouts (httpx.TimeoutException)
          - Network request errors (httpx.RequestError)
          - HTTP 5xx errors (PaymentGatewayException or httpx.HTTPStatusError >= 500)

        Does NOT count:
          - HTTP 4xx errors (Card Declined, Insufficient Funds, 400, 422)
          - CircuitBreakerOpenException (fast failure, not a downstream call)
        """
        if isinstance(exc, CircuitBreakerOpenException):
            return False

        from infrastructure.external.gateway_client import PaymentGatewayException

        if isinstance(exc, (httpx.TimeoutException, httpx.RequestError, PaymentGatewayException)):
            return True

        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500

        return False

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Wrap an async callable with the circuit breaker protection."""
        await self.acquire_permission()
        try:
            result = await func(*args, **kwargs)
            await self.record_success()
            return result
        except Exception as exc:
            if isinstance(exc, CircuitBreakerOpenException):
                raise
            if self.is_failure(exc):
                await self.record_failure()
            else:
                # 4xx or domain response: gateway is healthy
                await self.record_success()
            raise

    async def __aenter__(self) -> "RedisCircuitBreaker":
        await self.acquire_permission()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is None:
            await self.record_success()
            return False

        if issubclass(exc_type, CircuitBreakerOpenException):
            return False

        if exc_val is not None and self.is_failure(exc_val):
            await self.record_failure()
            return False

        # Non-failure exception (domain error, 4xx decline, etc.): gateway is healthy
        await self.record_success()
        return False

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator for async functions."""
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.call(func, *args, **kwargs)

        return wrapper
