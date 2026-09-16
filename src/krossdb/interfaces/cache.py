"""Distributed cache contract + a generic cache-aside helper.

Concrete implementations (e.g. Redis) implement :class:`AbstractCache`;
:func:`cache_aside` is backend-agnostic and works with any of them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AbstractCache(ABC):
    """Minimal async key-value cache contract used for cache-aside reads."""

    @abstractmethod
    async def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def clear_prefix(self, prefix: str) -> int:
        """Invalidate every key under ``prefix``; returns the number of keys removed."""


async def cache_aside(
    cache: AbstractCache,
    key: str,
    loader: Callable[[], Awaitable[T]],
    model: type[T],
    *,
    ttl_seconds: int | None = None,
) -> T:
    """Read-through cache-aside: try the cache, fall back to ``loader`` on a miss.

    ``loader`` is only invoked on a cache miss; the loaded value is written
    back to the cache before being returned, so subsequent reads within
    ``ttl_seconds`` are served without hitting the source of truth.
    """

    cached = await cache.get(key)
    if cached is not None:
        return model.model_validate_json(cached)

    value = await loader()
    await cache.set(key, value.model_dump_json().encode("utf-8"), ttl_seconds=ttl_seconds)
    return value


async def invalidate(cache: AbstractCache, key: str) -> None:
    await cache.delete(key)


__all__ = ["AbstractCache", "cache_aside", "invalidate"]
