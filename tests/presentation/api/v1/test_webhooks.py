"""
tests/presentation/api/v1/test_webhooks.py
-------------------------------------------
Integration tests for POST /api/v1/webhooks/gateway.
Covers:
  - 401 Unauthorized on invalid HMAC SHA-256 signature
  - 401 Unauthorized on missing signature header
  - 401 Unauthorized on missing timestamp header
  - 401 Unauthorized on timestamp older than 5 minutes (Replay Attack detected)
  - 401 Unauthorized on timestamp far in future (> 5 minutes)
  - 200 OK on timestamp within 5-minute tolerance window
  - 200 OK on valid signature and successful state transition
  - 200 OK when signature embeds timestamp (t=...,v1=...)
  - 200 OK when payment is already resolved (stale-state guard / true idempotency)
  - 200 OK on distributed split-brain state collision (critical alert logged, no retry)
  - 409 Conflict when payment row is locked (ConcurrentUpdateException)
"""
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from application.use_cases.webhook_orchestrator import (
    WebhookOrchestrator,
    WebhookResult,
)
from core.config import settings
from domain.entities.payment import PaymentAggregate, PaymentStatus
from domain.exceptions import ConcurrentUpdateException
from main import app
from presentation.api.v1.webhooks import get_uow_factory, get_webhook_orchestrator

client = TestClient(app)


def _compute_hmac(body: bytes, timestamp: str, secret: str = settings.WEBHOOK_SECRET) -> str:
    """Helper to compute valid HMAC SHA-256 signature over f'{timestamp}.{raw_body.decode()}'."""
    payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# HMAC & Replay Attack Security Tests
# ---------------------------------------------------------------------------


def test_webhook_invalid_signature_returns_401():
    """Prove that an invalid HMAC signature immediately returns 401 Unauthorized."""
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
        "X-Gateway-Signature": "invalid_hex_signature_here_00000000000000000000",
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    assert response.status_code == 401
    assert "Invalid webhook signature" in response.json()["detail"]


def test_webhook_missing_signature_returns_401():
    """Prove that a missing X-Gateway-Signature header returns 401 Unauthorized."""
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    assert response.status_code == 401
    assert "Missing X-Gateway-Signature" in response.json()["detail"]


def test_webhook_missing_timestamp_returns_401():
    """Prove that a missing timestamp header returns 401 Unauthorized."""
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Signature": "some_signature_value",
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    assert response.status_code == 401
    assert "timestamp" in response.json()["detail"].lower()


def test_webhook_replay_attack_older_than_5_minutes_rejected_401():
    """
    Prove that a webhook with a valid signature but older than 5 minutes (300 seconds)
    is rejected with 401 Unauthorized (Replay Attack detected).
    """
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    # 305 seconds ago (> 300 seconds / 5 minutes tolerance)
    old_timestamp = str(int(time.time() - 305))
    sig = _compute_hmac(raw_body, old_timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": old_timestamp,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "Replay Attack detected" in detail
    assert "5 minutes" in detail


def test_webhook_replay_attack_future_timestamp_rejected_401():
    """
    Prove that a webhook with timestamp in the far future (> 300 seconds)
    is rejected with 401 Unauthorized (Replay Attack detected).
    """
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    future_timestamp = str(int(time.time() + 350))
    sig = _compute_hmac(raw_body, future_timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": future_timestamp,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "Replay Attack detected" in detail


def test_webhook_timestamp_within_5_minute_tolerance_accepted_200():
    """
    Prove that a webhook timestamp within the 5-minute tolerance window (e.g. 2 minutes old)
    is accepted.
    """
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    valid_ts = str(int(time.time() - 120))
    sig = _compute_hmac(raw_body, valid_ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        return_value=WebhookResult(
            payment_id="pay_999",
            status=PaymentStatus.CAPTURED,
            already_resolved=False,
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": valid_ts,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_signature_with_embedded_timestamp_success():
    """
    Prove support for gateways passing timestamp embedded in the signature header
    (e.g., 't=1234567890,v1=<hex>').
    """
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _compute_hmac(raw_body, ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        return_value=WebhookResult(
            payment_id="pay_999",
            status=PaymentStatus.CAPTURED,
            already_resolved=False,
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    # No X-Gateway-Timestamp header; timestamp is embedded in X-Gateway-Signature
    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Signature": f"t={ts},v1={sig}",
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Business Logic & HTTP Semantics Tests
# ---------------------------------------------------------------------------


def test_webhook_valid_signature_success_200():
    """Valid HMAC signature and timestamp with successful processing returns 200 OK."""
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _compute_hmac(raw_body, ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        return_value=WebhookResult(
            payment_id="pay_999",
            status=PaymentStatus.CAPTURED,
            already_resolved=False,
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["payment_id"] == "pay_999"
    assert data["already_resolved"] is False


def test_webhook_already_resolved_returns_200():
    """Payment already resolved to same status silently returns 200 OK (true idempotency)."""
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _compute_hmac(raw_body, ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        return_value=WebhookResult(
            payment_id="pay_999",
            status=PaymentStatus.CAPTURED,
            already_resolved=True,
            split_brain=False,
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["already_resolved"] is True
    assert data["split_brain"] is False


def test_webhook_split_brain_returns_200_with_flag():
    """
    On distributed split-brain state collision, orchestrator flags split_brain=True.
    Endpoint returns 200 OK so the gateway does not loop retries,
    while response and logs alert engineering.
    """
    payload = {
        "reference_id": "gw_ref_12345",
        "status": "FAILED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _compute_hmac(raw_body, ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        return_value=WebhookResult(
            payment_id="pay_999",
            status=PaymentStatus.CAPTURED,
            already_resolved=True,
            split_brain=True,
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["already_resolved"] is True
    assert data["split_brain"] is True


def test_webhook_concurrent_update_conflict_returns_409():
    """
    If the row is locked (ConcurrentUpdateException), API returns 409 Conflict
    so the gateway knows to retry the webhook later.
    """
    payload = {
        "reference_id": "gw_ref_locked",
        "status": "CAPTURED",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _compute_hmac(raw_body, ts)

    mock_orch = MagicMock(spec=WebhookOrchestrator)
    mock_orch.process_webhook = AsyncMock(
        side_effect=ConcurrentUpdateException(
            "Payment is locked by another process.",
            detail="Row is locked.",
        )
    )

    app.dependency_overrides[get_webhook_orchestrator] = lambda: mock_orch

    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Timestamp": ts,
        "X-Gateway-Signature": sig,
    }

    response = client.post("/api/v1/webhooks/gateway", content=raw_body, headers=headers)

    app.dependency_overrides.clear()

    assert response.status_code == 409
    data = response.json()
    assert "Concurrent" in data.get("error", "") or "Concurrent" in data.get("title", "")
