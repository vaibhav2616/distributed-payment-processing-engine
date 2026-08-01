"""
infrastructure/cache/redis.py
------------------------------
Async Redis client with distributed lock primitives.
"""
import uuid
import redis.asyncio as redis
from core.config import settings


class RedisClient:
    """Thin async Redis wrapper exposing get/set and atomic distributed lock operations."""

    def __init__(self) -> None:
        self.client = redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self.client.get(key)

    async def set(self, key: str, value: str, expire: int = 300) -> None:
        await self.client.set(key, value, ex=expire)

    async def acquire_lock(self, lock_key: str, expire: int = 10) -> str | None:
        """Acquires an atomic distributed lock. Returns a unique token if successful, else None."""
        token = str(uuid.uuid4())
        acquired = await self.client.set(lock_key, token, nx=True, ex=expire)
        return token if acquired else None

    async def release_lock(self, lock_key: str, token: str) -> None:
        """Safely releases the lock only if the token matches (Lua CAS script)."""
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await self.client.eval(lua_script, 1, lock_key, token)


redis_client = RedisClient()
