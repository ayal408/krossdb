"""Unit tests for RedisCache, with the redis.asyncio client mocked via pytest-mock."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexusdb.cache.redis_cache import RedisCache

pytestmark = pytest.mark.unit


class FakeScanIter:
    """Mimics ``redis.asyncio.Redis.scan_iter``, an async-iterable of keys."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for key in self._keys:
            yield key


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock()
    client.delete = AsyncMock()
    client.exists = AsyncMock(return_value=0)
    client.scan_iter = MagicMock(return_value=FakeScanIter([]))
    return client


@pytest.fixture
def cache(mock_client: MagicMock) -> RedisCache:
    return RedisCache(mock_client, key_prefix="nexusdb-test")


async def test_get_prefixes_the_key(cache, mock_client):
    await cache.get("widgets:1")

    mock_client.get.assert_awaited_once_with("nexusdb-test:widgets:1")


async def test_get_returns_none_on_miss(cache, mock_client):
    mock_client.get.return_value = None

    assert await cache.get("missing") is None


async def test_set_prefixes_key_and_passes_ttl(cache, mock_client):
    await cache.set("widgets:1", b"payload", ttl_seconds=60)

    mock_client.set.assert_awaited_once_with("nexusdb-test:widgets:1", b"payload", ex=60)


async def test_delete_prefixes_the_key(cache, mock_client):
    await cache.delete("widgets:1")

    mock_client.delete.assert_awaited_once_with("nexusdb-test:widgets:1")


async def test_exists_returns_bool_not_raw_int(cache, mock_client):
    mock_client.exists.return_value = 1

    assert await cache.exists("widgets:1") is True


async def test_clear_prefix_deletes_every_matching_key_and_returns_count(cache, mock_client):
    mock_client.scan_iter.return_value = FakeScanIter(
        ["nexusdb-test:widgets:1", "nexusdb-test:widgets:2"]
    )

    removed = await cache.clear_prefix("widgets")

    assert removed == 2
    assert mock_client.delete.await_count == 2


async def test_clear_prefix_with_no_matches_returns_zero(cache, mock_client):
    mock_client.scan_iter.return_value = FakeScanIter([])

    assert await cache.clear_prefix("widgets") == 0
