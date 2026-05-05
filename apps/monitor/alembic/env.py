"""Alembic env.py — async runner using ``connection.run_sync``.

Reads ``sqlalchemy.url`` from the Alembic config (set programmatically by the
kernel, or from the ini file when invoked via the CLI). Runs migrations against
an :class:`AsyncEngine` so the same machinery works for tests against an
in-memory / temp-file DB.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from logging.config import fileConfig
from typing import Any

from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # raw SQL migrations; no MetaData object


def run_migrations_offline() -> None:
    """Generate SQL without a live DB (``alembic upgrade --sql head``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Any) -> None:  # noqa: ANN401
    """Configure Alembic against a live (sync) connection and run."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online async path: build an AsyncEngine and dispatch via ``run_sync``."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entrypoint: run async migrations under :func:`asyncio.run`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, safe to use asyncio.run()
        asyncio.run(run_async_migrations())
    else:
        # Already inside an event loop (e.g., pytest-asyncio); run in a separate thread
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(asyncio.run, run_async_migrations()).result()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
