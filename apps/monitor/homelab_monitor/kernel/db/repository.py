"""Thin repository facade over an :class:`AsyncEngine`.

Provides ``execute``, ``fetch_one``, ``fetch_all``, and a ``transaction``
async-context-manager. Each future stage adds its own narrow query helpers as
plain functions in its own module — no per-table classes upfront (per STAGE-001-004
design decisions).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import CursorResult, Executable
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class SqliteRepository:
    """Tiny async wrapper over an :class:`AsyncEngine`.

    All callers go through this — no raw cursor or connection objects leak out
    of the kernel. Methods return SQLAlchemy ``Row`` objects (or ``None``); the
    caller is responsible for mapping rows to its own dataclass / pydantic model.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying engine (used by audit + migration helpers)."""
        return self._engine

    async def execute(
        self,
        stmt: Executable,
        params: Mapping[str, Any] | None = None,
    ) -> CursorResult[Any]:
        """Execute a statement in a one-shot transaction; return the result."""
        async with self._engine.begin() as conn:
            return await conn.execute(stmt, params or {})

    async def fetch_one(
        self,
        stmt: Executable,
        params: Mapping[str, Any] | None = None,
    ) -> Row[Any] | None:
        """Run ``stmt`` and return the first row, or ``None`` if empty."""
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt, params or {})
            return result.first()

    async def fetch_all(
        self,
        stmt: Executable,
        params: Mapping[str, Any] | None = None,
    ) -> list[Row[Any]]:
        """Run ``stmt`` and return all rows."""
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt, params or {})
            return list(result.all())

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncConnection, None]:
        """Open a transaction and yield the connection.

        Use when you need to issue multiple statements atomically::

            async with repo.transaction() as conn:
                await conn.execute(stmt1)
                await conn.execute(stmt2)
        """
        async with self._engine.begin() as conn:
            yield conn
