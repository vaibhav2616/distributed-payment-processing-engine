"""
infrastructure/external/gateway_client.py
------------------------------------------
Resilient HTTP payment gateway client with retry + fallback logic.
Uses structlog so all log lines automatically carry the correlation_id.
"""
from __future__ import annotations

import structlog
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = structlog.get_logger(__name__)


class PaymentGatewayException(Exception):
    """Low-level infrastructure exception for gateway failures."""


class PaymentGatewayClient:
    def __init__(self, primary_url: str = "https://api.primary-acquirer.com", fallback_url: str = "https://api.fallback-acquirer.com") -> None:
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout = httpx.Timeout(5.0, connect=2.0)

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
            response.raise_for_status()
            return response.json()

    async def charge_with_fallback(self, payload: dict) -> dict:
        try:
            logger.info("gateway_charge_attempt", acquirer="primary")
            return await self._call_gateway(self.primary_url, payload)
        except Exception as primary_error:
            logger.warning("gateway_primary_failed", acquirer="primary", error=str(primary_error))
            try:
                logger.info("gateway_charge_attempt", acquirer="fallback")
                return await self._call_gateway(self.fallback_url, payload)
            except Exception as fallback_error:
                logger.error("gateway_all_acquirers_failed", primary_error=str(primary_error), fallback_error=str(fallback_error))
                raise PaymentGatewayException("All payment acquirers are currently unavailable.") from fallback_error


gateway_client = PaymentGatewayClient()
