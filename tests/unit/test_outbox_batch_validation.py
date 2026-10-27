"""Batch-size validation is consistent across outbox backends."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, String, Table

from krossdb.events.outbox import InMemoryOutboxStore, SQLAlchemyOutboxStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("limit", [0, -1, -100])
async def test_in_memory_outbox_rejects_invalid_limit(limit: int) -> None:
    store = InMemoryOutboxStore()
    with pytest.raises(ValueError, match="limit must be positive"):
        await store.fetch_unpublished(limit=limit)


@pytest.mark.parametrize("limit", [0, -1, -100])
async def test_sqlalchemy_outbox_rejects_invalid_limit_without_database(limit: int) -> None:
    table = Table("outbox_validation", MetaData(), Column("id", String, primary_key=True))
    store = SQLAlchemyOutboxStore(None, table)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit must be positive"):
        await store.fetch_unpublished(limit=limit)
