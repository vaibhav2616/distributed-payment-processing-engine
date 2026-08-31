"""
presentation/middleware/idempotency.py
----------------------------------------
Idempotency middleware: prevents duplicate POST processing.

Flow:
  1. Check Redis for a cached response (cache hit → return immediately)
  2. Acquire a distributed lock (NX=True, 15s TTL)
  3. If lock fails → 409 Conflict (concurrent duplicate in-flight)
  4. Process the request normally
  5. Cache the response body (48 hr TTL) on success
  6. Always release the lock (Lua CAS script — atomic)
"""
import json
import hashlib
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from infrastructure.cache.redis import redis_client


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method != "POST":
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        cache_key = f"idempotency:resp:{idempotency_key}"
        hash_key = f"idempotency:hash:{idempotency_key}"
        lock_key = f"idempotency:lock:{idempotency_key}"

        # 0. Hash the canonicalized request payload
        #    Parsing as JSON and re-serializing with sorted keys ensures that
        #    cosmetic differences (whitespace, key order) from upstream clients
        #    do not produce false-positive 409 Conflict responses.
        req_body = await request.body()
        try:
            parsed = json.loads(req_body)
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            req_hash = hashlib.sha256(canonical.encode()).hexdigest()
        except (json.JSONDecodeError, ValueError):
            # Non-JSON body (e.g., empty body): fall back to raw bytes
            req_hash = hashlib.sha256(req_body).hexdigest()

        # 1. Return cached response immediately if already processed
        cached_hash = await redis_client.get(hash_key)
        if cached_hash:
            cached_hash_str = cached_hash.decode() if isinstance(cached_hash, bytes) else cached_hash
            if cached_hash_str != req_hash:
                return Response(
                    content=json.dumps({"error": "Idempotency-Key already used with a different payload."}),
                    media_type="application/json",
                    status_code=409,
                )

        cached_response = await redis_client.get(cache_key)
        if cached_response:
            try:
                cached_data = json.loads(cached_response)
                content = cached_data["body"]
                status_code = cached_data["status"]
            except Exception:
                content = cached_response
                status_code = 200

            return Response(
                content=content,
                media_type="application/json",
                status_code=status_code,
                headers={"X-Cache": "HIT", "X-Idempotency-Status": "CACHED"},
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
                headers={"X-Cache": "BYPASS", "X-Idempotency-Status": "LOCKED"},
            )

        try:
            response = await call_next(request)
            response.headers["X-Cache"] = "MISS"
            response.headers["X-Idempotency-Status"] = "EXECUTED"

            # 3. Cache successful responses for idempotent replay
            if response.status_code in (200, 201, 202, 422):
                body = [section async for section in response.body_iterator]
                raw_body = b"".join(body).decode()

                async def _body_iter(chunks: list) -> ...:
                    for chunk in chunks:
                        yield chunk

                response.body_iterator = _body_iter(body)

                
                # Do not cache Pydantic/FastAPI schema validation errors —
                # those are set by the exception handler via request.state.
                # Clients must be allowed to fix the payload and retry.
                if getattr(request.state, "skip_idempotency_cache", False):
                    pass
                else:
                    cache_payload = json.dumps({
                        "status": response.status_code,
                        "body": raw_body
                    })
                    await redis_client.set(cache_key, cache_payload, expire=172800)
                    await redis_client.set(hash_key, req_hash, expire=172800)

            return response
        finally:
            # 4. Always release — even if the handler raised
            await redis_client.release_lock(lock_key, lock_token)
