"""Unit tests for the AbstractUnitOfWork transaction/event-flush semantics."""

from __future__ import annotations

import pytest

from krossdb.core.exceptions import NoActiveTransactionError, TransactionAlreadyActiveError
from krossdb.interfaces.events import DomainEvent
from krossdb.interfaces.unit_of_work import AbstractUnitOfWork

pytestmark = pytest.mark.unit


class SomethingHappened(DomainEvent):
    detail: str = "x"


class FakeUnitOfWork(AbstractUnitOfWork):
    """Records lifecycle calls in-memory instead of touching a real backend."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.flushed_events: list[DomainEvent] = []

    async def _begin(self) -> None:
        self.calls.append("begin")

    async def _commit(self) -> None:
        self.calls.append("commit")

    async def _rollback(self) -> None:
        self.calls.append("rollback")

    async def _persist_events(self, events: list[DomainEvent]) -> None:
        self.flushed_events.extend(events)


async def test_commit_flushes_events_exactly_once():
    uow = FakeUnitOfWork()

    async with uow as active:
        active.register_event(SomethingHappened())
        await active.commit()

    assert uow.calls == ["begin", "commit"]
    assert len(uow.flushed_events) == 1


async def test_exception_inside_block_triggers_rollback_and_drops_events():
    uow = FakeUnitOfWork()

    with pytest.raises(ValueError):
        async with uow as active:
            active.register_event(SomethingHappened())
            raise ValueError("business logic failed")

    assert uow.calls == ["begin", "rollback"]
    assert uow.flushed_events == []


async def test_missing_explicit_commit_is_treated_as_rollback():
    uow = FakeUnitOfWork()

    async with uow as active:
        active.register_event(SomethingHappened())
        # deliberately do not call commit()

    assert uow.calls == ["begin", "rollback"]
    assert uow.flushed_events == []


async def test_double_entry_raises_transaction_already_active():
    uow = FakeUnitOfWork()

    async with uow:
        with pytest.raises(TransactionAlreadyActiveError):
            await uow.__aenter__()


async def test_commit_without_active_transaction_raises():
    uow = FakeUnitOfWork()

    with pytest.raises(NoActiveTransactionError):
        await uow.commit()


async def test_rollback_without_active_transaction_raises():
    uow = FakeUnitOfWork()

    with pytest.raises(NoActiveTransactionError):
        await uow.rollback()
