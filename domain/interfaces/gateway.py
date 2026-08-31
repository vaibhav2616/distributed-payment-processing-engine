"""
domain/interfaces/gateway.py
----------------------------
Abstract payment gateway interface (port) for clean architecture.
Defines the contract for external acquirer communication.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentGatewayInterface(ABC):
    """Abstract contract for payment gateway implementations."""

    @abstractmethod
    async def charge(self, payload: dict) -> dict:
        """
        Initiate a charge request with the acquirer.

        Args:
            payload: Gateway payload containing amount, currency, source token, etc.

        Returns:
            Dictionary containing acquirer response.
        """
        ...

    @abstractmethod
    async def verify_status(self, idempotency_key: str) -> dict:
        """
        Query the acquirer for status of a previously initiated charge.

        Args:
            idempotency_key: Unique identifier used during initial charge.

        Returns:
            Dictionary containing status report.
        """
        ...

    @abstractmethod
    async def refund(self, reference_id: str, amount: str, idempotency_key: str) -> dict:
        """
        Initiate a refund with the acquirer.

        Args:
            reference_id: Gateway reference ID of the captured charge.
            amount: Amount to refund as a string (e.g. '10.00').
            idempotency_key: Unique identifier for this refund request.

        Returns:
            Dictionary containing acquirer response.
        """
        ...
