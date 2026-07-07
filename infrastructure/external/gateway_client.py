import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

class PaymentGatewayException(Exception):
    pass

class PaymentGatewayClient:
    def __init__(self, primary_url: str = "https://api.primary-acquirer.com", fallback_url: str = "https://api.fallback-acquirer.com"):
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.timeout = httpx.Timeout(5.0, connect=2.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException, PaymentGatewayException)),
        reraise=True
    )
    async def _call_gateway(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{url}/charge", json=payload)
            if response.status_code >= 500:
                raise PaymentGatewayException(f"Acquirer server error: {response.status_code}")
            response.raise_for_status()
            return response.json()

    async def charge_with_fallback(self, payload: dict) -> dict:
        """Attempts to process payment via primary acquirer, falling back to secondary if primary fails."""
        try:
            logger.info("Attempting charge with primary payment gateway...")
            return await self._call_gateway(self.primary_url, payload)
        except Exception as primary_error:
            logger.warning(f"Primary gateway failed: {primary_error}. Failing over to fallback...")
            try:
                return await self._call_gateway(self.fallback_url, payload)
            except Exception as fallback_error:
                logger.error(f"Both gateways failed. Fallback error: {fallback_error}")
                raise PaymentGatewayException("All payment acquirers are currently unavailable.")

gateway_client = PaymentGatewayClient()
