"""SQLAlchemy-backed Unit of Work: one master-routed session per transaction.

Repositories participating in the transaction just call
``adapter.acquire()`` as usual; while this UoW is active it pins the
adapter's task-local session override (see
:meth:`~krossdb.adapters.relational.base.SQLAlchemyAdapter.bind_session`) to
its own session, so every repository call inside the ``async with`` block —
regardless of which ``role`` it requests — participates in the same
transaction. The override is task-local (via ``contextvars``), so concurrent
requests using the same adapter never see each other's session.

Usage::

    async with SQLAlchemyUnitOfWork(adapter) as uow:
        await order_repo.create(order)
        await ledger_repo.update(account_id, {"balance": new_balance})
        uow.register_event(OrderPlaced(order_id=order.id))
        await uow.commit()
"""

from __future__ import annotations

from contextvars import Token

from sqlalchemy.ext.asyncio import AsyncSession

from krossdb.adapters.relational.base import SQLAlchemyAdapter
from krossdb.core.enums import RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.interfaces.unit_of_work import AbstractUnitOfWork


class SQLAlchemyUnitOfWork(AbstractUnitOfWork):
    """Binds one :class:`AsyncSession` (against the master node) per transaction."""

    def __init__(self, adapter: SQLAlchemyAdapter) -> None:
        super().__init__()
        self.adapter = adapter
        self.session: AsyncSession | None = None
        self._bind_token: Token[AsyncSession | None] | None = None

    async def _begin(self) -> None:
        key = self.adapter._select_key(RoutingRole.MASTER)
        self.session = self.adapter._session_factories[key]()
        await self.session.begin()
        self._bind_token = self.adapter.bind_session(self.session)

    async def _commit(self) -> None:
        assert self.session is not None
        try:
            with translate_exceptions(connection=self.adapter.config.name, op="commit"):
                await self.session.commit()
        finally:
            await self._teardown()

    async def _rollback(self) -> None:
        assert self.session is not None
        try:
            with translate_exceptions(connection=self.adapter.config.name, op="rollback"):
                await self.session.rollback()
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._bind_token is not None:
            self.adapter.unbind_session(self._bind_token)
            self._bind_token = None
        if self.session is not None:
            await self.session.close()
            self.session = None


__all__ = ["SQLAlchemyUnitOfWork"]
