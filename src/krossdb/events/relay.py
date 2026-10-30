"""Outbox relay: publishes the records an :class:`AbstractOutboxStore` holds.

Each batch runs inside :meth:`AbstractOutboxStore.claim`: fetch the oldest
unpublished records, hand each to ``publish``, then mark the successes as
published and count a failed attempt against the rest. A record that keeps
failing stops being fetched once it reaches ``max_attempts``; it stays in the
store, unpublished, for an operator to inspect.

Delivery is at-least-once: if the process dies after ``publish`` succeeded
but before the batch committed, the record is published again by the next
batch, so consumers must tolerate duplicates (``OutboxRecord.id`` is stable
and can serve as a dedup key). Order is by ``created_at`` within a batch, but
a failed record is retried after records that came later.

``publish`` must raise when delivery fails. Don't pass
:meth:`InMemoryEventDispatcher.publish
<krossdb.events.dispatcher.InMemoryEventDispatcher.publish>` directly: it logs
and swallows handler errors, so every record would be marked published.

Usage::

    relay = OutboxRelay(store, publish=send_to_broker)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop=stop))
    ...
    stop.set()
    await task
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable

from krossdb.core.logging import get_logger
from krossdb.interfaces.events import AbstractOutboxStore, OutboxRecord

OutboxPublisher = Callable[[OutboxRecord], Awaitable[None]]

_logger = get_logger("krossdb.events.relay")


class OutboxRelay:
    """Drains an outbox store into ``publish``, one claimed batch at a time."""

    def __init__(
        self,
        store: AbstractOutboxStore,
        publish: OutboxPublisher,
        *,
        batch_size: int = 100,
        max_attempts: int | None = 10,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_attempts is not None and max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._store = store
        self._publish = publish
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        """Publish one batch; returns how many records were published."""

        async with self._store.claim():
            records = await self._store.fetch_unpublished(
                limit=self._batch_size, max_attempts=self._max_attempts
            )
            published: list[uuid.UUID] = []
            failed: list[uuid.UUID] = []
            for record in records:
                try:
                    await self._publish(record)
                except Exception as exc:
                    _logger.warning(
                        "outbox_relay.publish_failed",
                        record_id=str(record.id),
                        event_type=record.event_type,
                        attempt=record.attempts + 1,
                        error=str(exc),
                    )
                    failed.append(record.id)
                else:
                    published.append(record.id)
            await self._store.mark_published(published)
            if failed:
                await self._store.record_failed_attempts(failed)
        return len(published)

    async def run(
        self, *, poll_interval_seconds: float = 1.0, stop: asyncio.Event | None = None
    ) -> None:
        """Publish batches until ``stop`` is set (or forever).

        Drains back-to-back while whole batches publish and sleeps
        ``poll_interval_seconds`` otherwise (caught up, or records failing, so
        a broken publisher isn't retried in a tight loop). A batch that
        raises (e.g. the database is unreachable) is logged and retried after
        the poll interval rather than ending the loop.
        """

        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                published = await self.run_once()
            except Exception as exc:
                _logger.error("outbox_relay.batch_failed", error=str(exc))
                published = 0
            if published >= self._batch_size:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)


__all__ = ["OutboxPublisher", "OutboxRelay"]
