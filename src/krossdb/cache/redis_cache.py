"""Redis-backed implementation of :class:`AbstractCache`, used for cache-aside reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from krossdb.core.exception_mapper import translate_exceptions
from krossdb.interfaces.cache import AbstractCache

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisCache(AbstractCache):
    """Thin wrapper around ``redis.asyncio.Redis`` satisfying :class:`AbstractCache`.

    Assumes the client was constructed without ``decode_responses=True``
    (the default), so replies come back as ``bytes``; passing a
    string-decoding client will break :meth:`get`'s contract.
    """

    def __init__(self, client: Redis, *, key_prefix: str = "krossdb") -> None:
        self._client = client
        self._key_prefix = key_prefix

    def _prefixed(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"

    async def get(self, key: str) -> bytes | None:
        with translate_exceptions(cache="redis", op="get"):
            value = await self._client.get(self._prefixed(key))
        return cast("bytes | None", value)

    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        with translate_exceptions(cache="redis", op="set"):
            await self._client.set(self._prefixed(key), value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        with translate_exceptions(cache="redis", op="delete"):
            await self._client.delete(self._prefixed(key))

    async def exists(self, key: str) -> bool:
        with translate_exceptions(cache="redis", op="exists"):
            return bool(await self._client.exists(self._prefixed(key)))

    async def clear_prefix(self, prefix: str) -> int:
        pattern = self._prefixed(f"{prefix}*")
        removed = 0
        with translate_exceptions(cache="redis", op="clear_prefix"):
            async for key in self._client.scan_iter(match=pattern):
                await self._client.delete(key)
                removed += 1
        return removed


def _register_redis_mapper() -> None:
    try:
        import redis.exceptions as redis_errors
    except ImportError:
        return

    from krossdb.core import exceptions as exc
    from krossdb.core.exception_mapper import register_mapper

    def mapper(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, redis_errors.AuthenticationError):
            return exc.AuthenticationError(str(error))
        if isinstance(error, redis_errors.TimeoutError):
            return exc.ConnectionTimeoutError(str(error))
        if isinstance(error, redis_errors.ConnectionError):
            return exc.ConnectionError_(str(error))
        if isinstance(error, redis_errors.RedisError):
            return exc.CacheError(str(error))
        return None

    register_mapper("redis", mapper)


_register_redis_mapper()

__all__ = ["RedisCache"]
