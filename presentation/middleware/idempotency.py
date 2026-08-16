"""
presentation/middleware/idempotency.py
----------------------------------------
Idempotency middleware: prevents duplicate POST processing.

Flow:
  1. Check Redis for a cached response (cache hit → return immediately)
  2. Acquire a distributed lock (NX=True, 15s TTL)
  3. If lock fails → 409 Conflict (concurrent duplicate in-flight)
  4. Process the request normally
  5. Cache the response body (5 min TTL) on success
  6. Always release the lock (Lua CAS script — atomic)
"""
import json
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from infrastructure.cache.redis import redis_client


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method != "POST":
            return await call_next(request)

        idempotency_key = request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        cache_key = f"idempotency:resp:{idempotency_key}"
        lock_key = f"idempotency:lock:{idempotency_key}"

        # 1. Return cached response immediately if already processed
        cached_response = await redis_client.get(cache_key)
        if cached_response:
            return Response(
                content=cached_response,
                media_type="application/json",
                status_code=200,
            )

        # 2. Acquire atomic distributed lock (prevents race conditions)
        lock_token = await redis_client.acquire_lock(lock_key, expire=15)
        if not lock_token:
            return Response(
                content=json.dumps(
                    {"error": "Concurrent request with same idempotency key is already processing."}
                ),
                media_type="application/json",
                status_code=409,
            )

        try:
            response = await call_next(request)

            # 3. Cache successful responses for idempotent replay
            if response.status_code == 200:
                body = [section async for section in response.body_iterator]
                response.body_iterator = iter(body)
                raw_body = b"".join(body).decode()
                await redis_client.set(cache_key, raw_body, expire=300)

            return response
        finally:
            # 4. Always release — even if the handler raised
            await redis_client.release_lock(lock_key, lock_token)
