"""
presentation/api/v1/dependencies.py
-----------------------------------
FastAPI dependencies for the v1 API, including webhook security and HMAC verification.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

from core.config import settings


async def verify_webhook_signature(request: Request) -> bytes:
    """
    FastAPI dependency validating the HMAC SHA-256 signature of incoming webhooks
    with replay-attack protection.

    Gateways sign their webhook payloads using HMAC-SHA256 over:
        f"{timestamp}.{raw_body.decode()}"

    Headers checked:
        X-Gateway-Signature: HMAC SHA-256 signature (hex format, optional prefix 'sha256=' or 'v1=').
        X-Gateway-Timestamp: Unix timestamp in seconds (or extracted from signature header).

    Tolerance check:
        If timestamp is older than 5 minutes (300 seconds) from server time,
        immediately raises 401 Unauthorized (Replay Attack detected).

    Returns:
        The raw request body bytes if verification succeeds.

    Raises:
        HTTPException: 401 Unauthorized if header is missing, timestamp is expired/invalid,
                       or signature does not match.
    """
    signature: str | None = request.headers.get("X-Gateway-Signature")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Gateway-Signature header.",
        )

    clean_sig = signature.strip()
    timestamp: str | None = request.headers.get("X-Gateway-Timestamp")

    # If timestamp not provided via dedicated header, attempt to extract from signature string
    if "t=" in signature or "timestamp=" in signature:
        parts: dict[str, str] = {}
        for token in signature.replace(";", ",").split(","):
            token = token.strip()
            if "=" in token:
                k, v = token.split("=", 1)
                parts[k.strip().lower()] = v.strip()
        if not timestamp:
            timestamp = parts.get("t") or parts.get("timestamp")
        if "v1" in parts:
            clean_sig = parts["v1"]
        elif "sig" in parts:
            clean_sig = parts["sig"]
        elif "sha256" in parts:
            clean_sig = parts["sha256"]

    if not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook timestamp header (X-Gateway-Timestamp required).",
        )

    # Validate timestamp format
    try:
        ts_val = float(timestamp)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook timestamp format.",
        )

    # Tolerance Check: older than 5 minutes (300 seconds) from server time
    current_server_time = time.time()
    if (current_server_time - ts_val) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook timestamp older than 5 minutes (Replay Attack detected).",
        )
    if (ts_val - current_server_time) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook timestamp in future exceeds tolerance (Replay Attack detected).",
        )

    # Strip optional 'sha256=' or 'v1=' prefixes
    if clean_sig.startswith("sha256="):
        clean_sig = clean_sig[7:]
    elif clean_sig.startswith("v1="):
        clean_sig = clean_sig[3:]

    body: bytes = await request.body()
    secret: str = getattr(settings, "WEBHOOK_SECRET", "test_webhook_secret_key")

    raw_body_str = body.decode("utf-8")
    hmac_payload = f"{timestamp}.{raw_body_str}"

    expected_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=hmac_payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(clean_sig.lower(), expected_sig.lower()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    return body
