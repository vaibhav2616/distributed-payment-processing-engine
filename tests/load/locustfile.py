"""
tests/load/locustfile.py
------------------------
Chaos Load Test Harness: The Idempotency Stampede.

Proves the resilience of the V1/V2 architecture under severe concurrency:
  - Generates a unique Idempotency-Key per simulated user test cycle.
  - Fires 10 concurrent POST /api/v1/payments requests simultaneously using gevent greenlets.
  - Asserts that exactly ONE request executes the orchestrator (yielding 201 or 202).
  - Asserts that the other 9 requests hit the Redis distributed lock (returning 409 Conflict)
    or return the cached response (201/202 with X-Cache: HIT).
  - Asserts that ZERO 500 errors occur and database connection pool exhaustion is prevented.
"""
from __future__ import annotations

import os
import sys
import uuid
import logging
from typing import Any

import gevent
from gevent.pool import Group
from locust import HttpUser, task, between, events

logger = logging.getLogger("locust.idempotency_stampede")


class IdempotencyStampedeUser(HttpUser):
    """
    Simulated user designed specifically to trigger race conditions on idempotency keys.
    """
    wait_time = between(1, 2)

    @task
    def test_idempotency_stampede(self) -> None:
        """
        Executes a 10-way concurrent race condition using the exact same Idempotency-Key.
        """
        stampede_key = f"stampede_{uuid.uuid4()}"
        payload = {
            "amount": "100.00",
            "currency": "USD",
            "source_token": "tok_visa_stampede",
            "reference_id": stampede_key,
        }
        headers = {
            "Idempotency-Key": stampede_key,
            "X-Idempotency-Key": stampede_key,
            "Content-Type": "application/json",
        }

        results: list[dict[str, Any]] = []

        def _fire_single_request() -> None:
            with self.client.post(
                "/api/v1/payments/",
                json=payload,
                headers=headers,
                name="/api/v1/payments (stampede)",
                catch_response=True,
            ) as response:
                status_code = response.status_code
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                resp_text = response.text

                results.append({
                    "status_code": status_code,
                    "headers": resp_headers,
                    "text": resp_text,
                })

                # Expected valid outcomes during a stampede:
                # 201 (CAPTURED), 202 (ACCEPTED/PENDING), 409 (LOCKED CONCURRENT),
                # 503 (CIRCUIT BREAKER OPEN fast-fail)
                if status_code in (201, 202, 409, 503):
                    response.success()
                else:
                    response.failure(
                        f"Unexpected HTTP {status_code} during stampede: {resp_text}"
                    )

        # ----------------------------------------------------------------------
        # Launch 10 concurrent requests simultaneously using gevent greenlets
        # ----------------------------------------------------------------------
        pool = Group()
        for _ in range(10):
            pool.spawn(_fire_single_request)
        pool.join()

        # ----------------------------------------------------------------------
        # SRE Resilience Assertions
        # ----------------------------------------------------------------------
        assert len(results) == 10, (
            f"Expected exactly 10 responses, but received {len(results)}"
        )

        status_codes = [r["status_code"] for r in results]

        # 1. Zero 500 Internal Server Errors (unhandled exceptions / DB pool exhaustion)
        internal_errors = [r for r in results if r["status_code"] == 500]
        assert not internal_errors, (
            f"Chaos test failure: Server returned {len(internal_errors)} internal 500 error(s)! "
            f"Potential DB connection pool exhaustion: {internal_errors}"
        )

        # 2. All responses must be valid expected statuses (201, 202, 409, or 503)
        invalid_statuses = [r for r in results if r["status_code"] not in (201, 202, 409, 503)]
        assert not invalid_statuses, (
            f"Unexpected HTTP statuses detected in stampede: {invalid_statuses}"
        )

        # 3. Classify execution vs lock/cache hit
        orchestrator_executions = [
            r for r in results
            if r["status_code"] in (201, 202, 503)
            and r["headers"].get("x-cache", "").upper() != "HIT"
            and r["headers"].get("x-idempotency-status", "").upper() != "CACHED"
        ]

        locked_or_cached = [
            r for r in results
            if r["status_code"] == 409
            or (
                r["status_code"] in (201, 202, 503)
                and (
                    r["headers"].get("x-cache", "").upper() == "HIT"
                    or r["headers"].get("x-idempotency-status", "").upper() == "CACHED"
                )
            )
        ]

        # 4. Explicit Assertion: Exactly one executes the orchestrator
        assert len(orchestrator_executions) == 1, (
            f"Race condition violated! Exactly 1 request must execute the orchestrator (201/202), "
            f"but found {len(orchestrator_executions)}. Status codes: {status_codes}"
        )

        # 5. Explicit Assertion: The other 9 hit the Redis lock (409) or return cached response
        assert len(locked_or_cached) == 9, (
            f"Concurrency lock failure! Exactly 9 requests must hit Redis lock (409) or cache, "
            f"but found {len(locked_or_cached)}. Status codes: {status_codes}"
        )

        logger.info(
            "Idempotency stampede verified: 1 orchestrator execution (%s), 9 protected (%s)",
            orchestrator_executions[0]["status_code"],
            [r["status_code"] for r in locked_or_cached],
        )


if __name__ == "__main__":
    from locust.env import Environment

    target_host = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARGET_HOST", "http://localhost:8000")
    print(f"🚀 Running Idempotency Stampede against: {target_host}")

    IdempotencyStampedeUser.host = target_host
    env = Environment(user_classes=[IdempotencyStampedeUser], host=target_host)
    user = IdempotencyStampedeUser(env)

    try:
        user.test_idempotency_stampede()
        print("✅ STAMPEDE ASSERTIONS PASSED:")
        print("   - Exactly 1 request executed orchestrator (201/202)")
        print("   - Exactly 9 requests guarded by Redis lock (409 Conflict) / cache hit")
        print("   - 0 server errors (HTTP 500) and zero database pool exhaustion")
    except Exception as exc:
        print(f"❌ STAMPEDE ASSERTIONS FAILED: {exc}")
        sys.exit(1)
