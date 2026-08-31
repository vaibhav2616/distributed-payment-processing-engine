"""
tests/infrastructure/external/test_circuit_breaker.py
-----------------------------------------------------
Unit tests for RedisCircuitBreaker mocking Redis.
Proves state transitions:
  CLOSED -> OPEN -> HALF-OPEN -> CLOSED
  CLOSED -> OPEN -> HALF-OPEN -> OPEN
Validates:
  - Failure threshold of 5 consecutive failures
  - 60s cool-off period on OPEN state
  - Fast-failure when OPEN without calling downstream service
  - Only exactly ONE probe request allowed in HALF-OPEN
  - 4xx domain declines do NOT count as failures
  - Gateway client wrapping charge and verify_status
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from infrastructure.external.circuit_breaker import (
    CircuitBreakerOpenException,
    CircuitBreakerState,
    RedisCircuitBreaker,
)
from infrastructure.external.gateway_client import (
    PaymentGatewayClient,
    PaymentGatewayException,
)


class MockRedisStore:
    """In-memory mock for async Redis commands used by RedisCircuitBreaker."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def incr(self, key: str) -> int:
        val = int(self.data.get(key, 0)) + 1
        self.data[key] = str(val)
        return val

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                self.ttls.pop(k, None)
                deleted += 1
        return deleted

    def expire_key(self, key: str) -> None:
        """Simulate TTL expiration."""
        self.data.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def mock_redis():
    return MockRedisStore()


@pytest.fixture
def breaker(mock_redis):
    return RedisCircuitBreaker(
        redis_client=mock_redis,
        name="test_gateway",
        failure_threshold=5,
        cool_off_period=60,
    )


# ---------------------------------------------------------------------------
# 1. State Transitions: CLOSED -> OPEN -> HALF-OPEN -> CLOSED / OPEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_is_closed(breaker):
    """Breaker starts in CLOSED state."""
    state = await breaker.get_state()
    assert state == CircuitBreakerState.CLOSED
    # Permission is granted without exception
    await breaker.acquire_permission()


@pytest.mark.asyncio
async def test_closed_to_open_after_5_consecutive_failures(breaker, mock_redis):
    """5 consecutive failures transition the state from CLOSED to OPEN with 60s TTL."""
    # Failures 1 through 4: still CLOSED
    for i in range(1, 5):
        await breaker.record_failure()
        state = await breaker.get_state()
        assert state == CircuitBreakerState.CLOSED, f"Should remain CLOSED at {i} failures"

    # 5th consecutive failure: transitions to OPEN
    await breaker.record_failure()
    state = await breaker.get_state()
    assert state == CircuitBreakerState.OPEN

    # Verify Redis TTL on the OPEN state is strictly 60 seconds
    assert mock_redis.ttls.get(breaker.open_key) == 60


@pytest.mark.asyncio
async def test_fast_failure_when_open(breaker):
    """When breaker is OPEN, acquire_permission immediately raises CircuitBreakerOpenException."""
    # Trip breaker
    for _ in range(5):
        await breaker.record_failure()

    assert await breaker.get_state() == CircuitBreakerState.OPEN

    # Calling acquire_permission raises without calling downstream
    with pytest.raises(CircuitBreakerOpenException) as exc_info:
        await breaker.acquire_permission()

    assert exc_info.value.retry_after == 60
    assert "OPEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_open_to_half_open_after_ttl_expires(breaker, mock_redis):
    """After 60 seconds (open_key expires), breaker transitions to HALF-OPEN."""
    for _ in range(5):
        await breaker.record_failure()
    assert await breaker.get_state() == CircuitBreakerState.OPEN

    # Simulate expiration of the 60s cool-off period
    mock_redis.expire_key(breaker.open_key)

    state = await breaker.get_state()
    assert state == CircuitBreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_allows_exactly_one_probe_request(breaker, mock_redis):
    """HALF-OPEN allows exactly 1 probe request through; concurrent requests fast-fail."""
    for _ in range(5):
        await breaker.record_failure()
    mock_redis.expire_key(breaker.open_key)

    assert await breaker.get_state() == CircuitBreakerState.HALF_OPEN

    # First request: successfully acquires the single probe permit
    await breaker.acquire_permission()

    # Second request while probe is in flight: fast-fails with CircuitBreakerOpenException
    with pytest.raises(CircuitBreakerOpenException) as exc_info:
        await breaker.acquire_permission()

    assert "HALF-OPEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_half_open_recovery_to_closed_on_success(breaker, mock_redis):
    """When the probe request succeeds in HALF-OPEN, breaker resets to CLOSED."""
    for _ in range(5):
        await breaker.record_failure()
    mock_redis.expire_key(breaker.open_key)

    # Probe request acquires permit and succeeds
    await breaker.acquire_permission()
    await breaker.record_success()

    # Breaker should now be fully reset to CLOSED
    state = await breaker.get_state()
    assert state == CircuitBreakerState.CLOSED

    # Subsequent requests should pass cleanly
    await breaker.acquire_permission()


@pytest.mark.asyncio
async def test_half_open_reverts_to_open_on_failure(breaker, mock_redis):
    """When the probe request fails in HALF-OPEN, breaker reverts to OPEN for 60s."""
    for _ in range(5):
        await breaker.record_failure()
    mock_redis.expire_key(breaker.open_key)

    # Probe request acquires permit but fails
    await breaker.acquire_permission()
    await breaker.record_failure()

    # Breaker reverts to OPEN with fresh 60s TTL
    state = await breaker.get_state()
    assert state == CircuitBreakerState.OPEN
    assert mock_redis.ttls.get(breaker.open_key) == 60

    # Fast-fails immediately
    with pytest.raises(CircuitBreakerOpenException):
        await breaker.acquire_permission()


# ---------------------------------------------------------------------------
# 2. Consecutiveness & 4xx vs 5xx Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_resets_failure_count(breaker):
    """4 failures followed by 1 success resets counter; subsequent 4 failures do not trip."""
    for _ in range(4):
        await breaker.record_failure()
    assert await breaker.get_state() == CircuitBreakerState.CLOSED

    # Successful call resets failure count
    await breaker.record_success()

    # 4 more failures (total 8 failures, but NOT consecutive): stays CLOSED
    for _ in range(4):
        await breaker.record_failure()
    assert await breaker.get_state() == CircuitBreakerState.CLOSED


@pytest.mark.asyncio
async def test_4xx_domain_responses_do_not_count_as_failures(breaker):
    """HTTP 4xx responses (card declined, invalid card) must NOT trip breaker."""
    mock_response_422 = MagicMock(spec=httpx.Response)
    mock_response_422.status_code = 422
    err_422 = httpx.HTTPStatusError("Unprocessable", request=MagicMock(), response=mock_response_422)

    assert breaker.is_failure(err_422) is False

    mock_response_400 = MagicMock(spec=httpx.Response)
    mock_response_400.status_code = 400
    err_400 = httpx.HTTPStatusError("Bad Request", request=MagicMock(), response=mock_response_400)
    assert breaker.is_failure(err_400) is False

    # 5xx error DOES count as failure
    mock_response_503 = MagicMock(spec=httpx.Response)
    mock_response_503.status_code = 503
    err_503 = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=mock_response_503)
    assert breaker.is_failure(err_503) is True

    # Timeouts DO count as failure
    timeout_err = httpx.TimeoutException("Connection timed out")
    assert breaker.is_failure(timeout_err) is True


@pytest.mark.asyncio
async def test_breaker_call_wrapper(breaker):
    """Test breaker.call wrapper executes callable, handles success and failure."""
    dummy_fn = AsyncMock(return_value={"status": "ok"})
    res = await breaker.call(dummy_fn, "arg1")
    assert res == {"status": "ok"}
    dummy_fn.assert_called_once_with("arg1")

    # Trip breaker
    for _ in range(5):
        await breaker.record_failure()

    # Should raise without calling dummy_fn
    dummy_fn.reset_mock()
    with pytest.raises(CircuitBreakerOpenException):
        await breaker.call(dummy_fn)
    dummy_fn.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Gateway Client Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_client_charge_and_verify_status_protected_by_breaker(mock_redis):
    """PaymentGatewayClient wraps charge and verify_status with circuit breaker."""
    breaker = RedisCircuitBreaker(redis_client=mock_redis, name="gw_integration", failure_threshold=5, cool_off_period=60)
    client = PaymentGatewayClient(circuit_breaker=breaker)

    # 1. 4xx card decline: handled as valid response, breaker stays CLOSED
    with patch.object(client, "_call_gateway", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"status": "failed", "error": "insufficient_funds"}
        res = await client.charge({"amount": "100.00"})
        assert res["status"] == "failed"
        assert await breaker.get_state() == CircuitBreakerState.CLOSED

    # 2. 5 consecutive 5xx errors trip breaker to OPEN
    with patch.object(client, "_call_gateway", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = PaymentGatewayException("503 Gateway Down")
        for _ in range(5):
            with pytest.raises(PaymentGatewayException):
                await client.charge({"amount": "100.00"})

    assert await breaker.get_state() == CircuitBreakerState.OPEN

    # 3. Fast failure: subsequent charge and verify_status fail fast without calling gateway
    with patch.object(client, "_call_gateway", new_callable=AsyncMock) as mock_call:
        with pytest.raises(CircuitBreakerOpenException):
            await client.charge({"amount": "100.00"})
        mock_call.assert_not_called()

        with pytest.raises(CircuitBreakerOpenException):
            await client.verify_status("tx_123")
        mock_call.assert_not_called()
