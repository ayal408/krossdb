"""Generic idempotency-key enforcement, backed by any :class:`AbstractCache`.

Wraps a mutating operation so replaying the same idempotency key returns the
original result instead of re-executing the operation, and raises
:class:`IdempotencyConflictError` if the same key is replayed with a
different request payload (catching client bugs where a key is reused for
unrelated requests).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from krossdb.core.exceptions import IdempotencyConflictError
from krossdb.interfaces.cache import AbstractCache

T = TypeVar("T", bound=BaseModel)


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyManager:
    """Coordinates idempotency-key replay detection for any repository/adapter."""

    def __init__(self, cache: AbstractCache, *, ttl_seconds: int = 86_400, namespace: str = "idem") -> None:
        self._cache = cache
        self._ttl_seconds = ttl_seconds
        self._namespace = namespace

    def _cache_key(self, idempotency_key: str) -> str:
        return f"{self._namespace}:{idempotency_key}"

    async def run(
        self,
        idempotency_key: str,
        request_payload: dict[str, Any],
        operation: Callable[[], Awaitable[T]],
        model: type[T],
    ) -> T:
        """Run ``operation`` at most once per ``idempotency_key``.

        On replay with the same ``request_payload``, returns the cached
        result without calling ``operation`` again. On replay with a
        *different* payload, raises :class:`IdempotencyConflictError`.
        """

        cache_key = self._cache_key(idempotency_key)
        fingerprint = _fingerprint(request_payload)

        cached = await self._cache.get(cache_key)
        if cached is not None:
            record = json.loads(cached)
            if record["fingerprint"] != fingerprint:
                raise IdempotencyConflictError(
                    f"Idempotency key {idempotency_key!r} was replayed with a different payload",
                    context={"idempotency_key": idempotency_key},
                )
            return model.model_validate_json(record["result"])

        result = await operation()
        envelope = json.dumps({"fingerprint": fingerprint, "result": result.model_dump_json()})
        await self._cache.set(cache_key, envelope.encode("utf-8"), ttl_seconds=self._ttl_seconds)
        return result


__all__ = ["IdempotencyManager"]
