"""
infrastructure/external/gateway_client.py
------------------------------------------
Resilient HTTP payment gateway client with retry + fallback logic,
protected by a distributed Redis circuit breaker.
Uses structlog so all log lines automatically carry the correlation_id.
"""
from __future__ import annotations

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from domain.interfaces.gateway import PaymentGatewayInterface
from infrastructure.external.circuit_breaker import (
    CircuitBreakerOpenException,
    RedisCircuitBreaker,
)

logger = structlog.get_logger(__name__)


class PaymentGatewayException(Exception):
    """Low-level infrastructure exception for gateway failures."""


class GatewayDeclineException(Exception):
    """Raised when the payment gateway definitively rejects a transaction (e.g. 4xx error)."""
    pass


class PaymentGatewayClient(PaymentGatewayInterface):
    """
    Concrete HTTP payment gateway adapter protected by RedisCircuitBreaker.

    Manages failover between primary and secondary acquirers, retries on
    transient errors, and fast-fails via circuit breaker when downstream
    services suffer persistent outages.
    """

    def __init__(
        self,
        primary_url: str = "https://api.primary-acquirer.com",
        fallback_url: str = "https://api.fallback-acquirer.com",
        circuit_breaker: RedisCircuitBreaker | None = None,
    ) -> None:
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout = httpx.Timeout(5.0, connect=2.0)
        self.circuit_breaker = circuit_breaker or RedisCircuitBreaker(name="payment_gateway")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException, PaymentGatewayException)),
        reraise=True,
    )
    async def _call_gateway(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{url}/charge", json=payload)
            if response.status_code >= 500:
                raise PaymentGatewayException(f"Acquirer server error: {response.status_code}")
            if 400 <= response.status_code < 500:
                # 4xx is a valid domain response (Card Declined, Insufficient Funds, etc.)
                try:
                    return response.json()
                except Exception:
                    return {"status": "failed", "error": f"Declined ({response.status_code})"}
            response.raise_for_status()
            return response.json()

    async def charge(self, payload: dict) -> dict:
        """
        Public charge method implementing PaymentGatewayInterface.
        Protected by the circuit breaker.
        """
        return await self.charge_with_fallback(payload)

    async def charge_with_fallback(self, payload: dict) -> dict:
        """
        Execute charge with fallback acquirer, wrapped by circuit breaker.

        Strictly counts network timeouts and 5xx responses as failures.
        Does NOT count 4xx domain declines as failures.
        """
        async with self.circuit_breaker:
            try:
                logger.info("gateway_charge_attempt", acquirer="primary")
                return await self._call_gateway(self.primary_url, payload)
            except (PaymentGatewayException, httpx.RequestError, httpx.TimeoutException) as primary_error:
                logger.warning("gateway_primary_failed", acquirer="primary", error=str(primary_error))
                try:
                    logger.info("gateway_charge_attempt", acquirer="fallback")
                    return await self._call_gateway(self.fallback_url, payload)
                except Exception as fallback_error:
                    logger.error(
                        "gateway_all_acquirers_failed",
                        primary_error=str(primary_error),
                        fallback_error=str(fallback_error),
                    )
                    raise PaymentGatewayException(
                        "All payment acquirers are currently unavailable."
                    ) from fallback_error

    async def verify_status(self, idempotency_key: str) -> dict:
        """
        Query the gateway for the current status of a previously initiated charge.
        Protected by the circuit breaker.

        Raises:
            CircuitBreakerOpenException: If the circuit breaker is OPEN.
            PaymentGatewayException: On 5xx from the gateway.
            httpx.TimeoutException: On network timeout.
        """
        async with self.circuit_breaker:
            verify_timeout = httpx.Timeout(10.0, connect=3.0)
            async with httpx.AsyncClient(timeout=verify_timeout) as client:
                logger.info("gateway_verify_status", idempotency_key=idempotency_key)
                response = await client.get(
                    f"{self.primary_url}/status/{idempotency_key}"
                )
                if response.status_code >= 500:
                    raise PaymentGatewayException(
                        f"Gateway status check server error: {response.status_code}"
                    )
                if response.status_code == 404:
                    return {"status": "not_found"}
                if 400 <= response.status_code < 500:
                    return response.json()
                response.raise_for_status()
                return response.json()

    async def refund(self, reference_id: str, amount: str, idempotency_key: str) -> dict:
        """
        Initiate a refund with the acquirer. Protected by the circuit breaker.
        """
        async with self.circuit_breaker:
            refund_timeout = httpx.Timeout(8.0, connect=2.0)
            payload = {
                "reference_id": reference_id,
                "amount": amount,
                "idempotency_key": idempotency_key,
            }
            async with httpx.AsyncClient(timeout=refund_timeout) as client:
                logger.info("gateway_refund_attempt", reference_id=reference_id, amount=amount)
                response = await client.post(
                    f"{self.primary_url}/refund", json=payload
                )
                if response.status_code >= 500:
                    raise PaymentGatewayException(
                        f"Gateway refund server error: {response.status_code}"
                    )
                if 400 <= response.status_code < 500:
                    error_msg = f"Refund failed ({response.status_code})"
                    try:
                        error_msg = response.json().get("error", error_msg)
                    except Exception:
                        pass
                    raise GatewayDeclineException(error_msg)
                response.raise_for_status()
                return response.json()


gateway_client = PaymentGatewayClient()
