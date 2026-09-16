"""Unit of Work: coordinates one transactional boundary across repositories.

A concrete UoW (e.g. SQLAlchemy-backed) exposes the repositories that
participate in a given transaction as attributes, and collects domain events
raised during the transaction so they can be dispatched atomically via the
Outbox pattern after commit (see :mod:`krossdb.events.outbox`).

Usage::

    async with uow_factory() as uow:
        user = await uow.users.get_by_id_or_raise(user_id)
        await uow.orders.create(new_order)
        uow.register_event(OrderPlaced(order_id=new_order.id))
        await uow.commit()
    # events are flushed to the outbox exactly once, only if commit succeeded
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

from krossdb.core.exceptions import NoActiveTransactionError, TransactionAlreadyActiveError
from krossdb.core.logging import get_logger
from krossdb.interfaces.events import DomainEvent

_logger = get_logger("krossdb.uow")


class AbstractUnitOfWork(ABC):
    """Base class for Unit of Work implementations.

    Subclasses must implement :meth:`_begin`, :meth:`_commit`, and
    :meth:`_rollback`; the public ``__aenter__``/``commit``/``rollback`` methods
    layer state tracking and event collection on top so every backend behaves
    identically to callers.
    """

    def __init__(self) -> None:
        self._active = False
        self._committed = False
        self._finished = False
        self._pending_events: list[DomainEvent] = []

    # -- transaction boundary -------------------------------------------------

    async def __aenter__(self) -> Self:
        if self._active:
            raise TransactionAlreadyActiveError("Unit of Work is already active")
        await self._begin()
        self._active = True
        self._committed = False
        self._finished = False
        self._pending_events.clear()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if not self._finished:
                # Neither commit() nor rollback() was called explicitly inside
                # the block (whether because an exception propagated, or the
                # caller simply forgot): treat it as a rollback. Silent partial
                # commits are a correctness hazard, so we never guess intent.
                await self.rollback()
        finally:
            self._active = False

    async def commit(self) -> None:
        if not self._active:
            raise NoActiveTransactionError("commit() called with no active transaction")
        await self._commit()
        self._committed = True
        self._finished = True
        await self._flush_events()

    async def rollback(self) -> None:
        if not self._active:
            raise NoActiveTransactionError("rollback() called with no active transaction")
        await self._rollback()
        self._finished = True
        self._pending_events.clear()

    # -- domain events / outbox -------------------------------------------------

    def register_event(self, event: DomainEvent) -> None:
        """Queue a domain event to be persisted to the outbox atomically on commit."""

        self._pending_events.append(event)

    async def _flush_events(self) -> None:
        if not self._pending_events:
            return
        await self._persist_events(list(self._pending_events))
        _logger.info("uow.events_flushed", count=len(self._pending_events))
        self._pending_events.clear()

    async def _persist_events(self, events: list[DomainEvent]) -> None:
        """Default no-op; override to write into the transactional outbox table/collection."""

    # -- backend hooks (implemented by concrete subclasses) ----------------------

    @abstractmethod
    async def _begin(self) -> None: ...

    @abstractmethod
    async def _commit(self) -> None: ...

    @abstractmethod
    async def _rollback(self) -> None: ...


__all__ = ["AbstractUnitOfWork"]
