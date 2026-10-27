"""Unit tests for IdempotencyManager, backed by a trivial in-memory fake cache."""

from __future__ import annotations

import pytest

from krossdb.core.exceptions import IdempotencyConflictError
from krossdb.core.idempotency import IdempotencyManager

pytestmark = pytest.mark.unit


class FakeCache:
    """Minimal AbstractCache-shaped fake; not a mock, just a plain dict-backed store."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def clear_prefix(self, prefix: str) -> int:
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            del self._store[k]
        return len(keys)


async def test_first_call_executes_the_operation(make_widget):
    cache = FakeCache()
    manager = IdempotencyManager(cache)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return make_widget(name="created-once")

    result = await manager.run("key-1", {"name": "created-once"}, operation, type(make_widget()))

    assert calls == 1
    assert result.name == "created-once"


async def test_replay_with_same_payload_returns_cached_result_without_rerunning(make_widget):
    cache = FakeCache()
    manager = IdempotencyManager(cache)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return make_widget(name="widget")

    model_cls = type(make_widget())
    first = await manager.run("key-1", {"name": "widget"}, operation, model_cls)
    second = await manager.run("key-1", {"name": "widget"}, operation, model_cls)

    assert calls == 1  # operation only ran once
    assert second.id == first.id


async def test_replay_with_different_payload_raises_conflict(make_widget):
    cache = FakeCache()
    manager = IdempotencyManager(cache)

    async def operation():
        return make_widget(name="widget")

    model_cls = type(make_widget())
    await manager.run("key-1", {"name": "widget"}, operation, model_cls)

    with pytest.raises(IdempotencyConflictError):
        await manager.run("key-1", {"name": "different-widget"}, operation, model_cls)


async def test_different_keys_do_not_collide(make_widget):
    cache = FakeCache()
    manager = IdempotencyManager(cache)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return make_widget(name=f"widget-{calls}")

    model_cls = type(make_widget())
    await manager.run("key-1", {"n": 1}, operation, model_cls)
    await manager.run("key-2", {"n": 2}, operation, model_cls)

    assert calls == 2


@pytest.mark.parametrize("ttl,namespace", [(0, "idem"), (-1, "idem"), (10, "")])
def test_invalid_manager_configuration_is_rejected(ttl, namespace):
    with pytest.raises(ValueError):
        IdempotencyManager(FakeCache(), ttl_seconds=ttl, namespace=namespace)


async def test_empty_idempotency_key_is_rejected(make_widget):
    manager = IdempotencyManager(FakeCache())
    called = False

    async def operation():
        nonlocal called
        called = True
        return make_widget(name="invalid")

    with pytest.raises(ValueError, match="idempotency_key"):
        await manager.run("", {"name": "invalid"}, operation, type(make_widget()))
    assert not called


async def test_equivalent_payload_key_order_replays(make_widget):
    manager = IdempotencyManager(FakeCache())
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return make_widget(name="ordered")

    model_cls = type(make_widget())
    await manager.run("ordered-key", {"a": 1, "b": 2}, operation, model_cls)
    await manager.run("ordered-key", {"b": 2, "a": 1}, operation, model_cls)
    assert calls == 1
