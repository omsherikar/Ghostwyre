"""Shared pytest fixtures for the DB-backed (`@pytest.mark.slow`) tests.

A dedicated test database (`ghostwyre_test`) is derived from
`Settings.database_url` by swapping the db name, so the slow suite never touches
the development database. The database is created if missing (via the `postgres`
maintenance DB), and the schema is built once per session from `Base.metadata`.

The live engine is **function-scoped** and uses `NullPool`: with
`asyncio_mode=auto`, pytest-asyncio runs each test on its own event loop, and a
pooled asyncpg connection bound to a different loop raises "another operation is
in progress". A fresh, unpooled engine per test keeps every connection on the
running test's loop. Per-test isolation is by TRUNCATE between tests.

`test_sessionmaker` is exported so later handler tests can bind the app's
`SessionLocal` to the test engine (e.g. monkeypatch `app.slack.actions.SessionLocal`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db.models import Base

TEST_DB_NAME = "ghostwyre_test"
# Derived from the model metadata (children-before-parents) so adding a table
# later self-maintains the TRUNCATE list rather than silently leaving rows.
_TABLES_NEWEST_FIRST = tuple(t.name for t in reversed(Base.metadata.sorted_tables))


def _swap_db_name(url: str, db_name: str) -> str:
    """Return `url` with its path (database name) replaced by `db_name`."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{db_name}"))


def _test_url() -> str:
    return _swap_db_name(get_settings().database_url, TEST_DB_NAME)


async def _ensure_test_database(test_url: str) -> None:
    """Create the test database if it does not already exist.

    Connects to the `postgres` maintenance DB with plain asyncpg (no driver
    prefix) and issues CREATE DATABASE; a duplicate-database error is ignored.
    """
    admin_url = _swap_db_name(test_url, "postgres")
    # asyncpg wants a bare postgres:// DSN, not SQLAlchemy's +asyncpg suffix.
    dsn = admin_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    except asyncpg.DuplicateDatabaseError:
        pass
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _schema() -> AsyncIterator[None]:
    """Create the test DB + schema once per session; drop the schema at teardown.

    Uses its own short-lived engine so no pooled connection outlives this
    setup/teardown loop.
    """
    test_url = _test_url()
    await _ensure_test_database(test_url)

    engine = create_async_engine(test_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    try:
        yield
    finally:
        engine = create_async_engine(test_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def test_engine(_schema: None) -> AsyncIterator[AsyncEngine]:
    """Function-scoped, unpooled engine bound to the dedicated test database."""
    engine = create_async_engine(_test_url(), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test session with truncate-based isolation.

    Truncation (rather than a wrapping rollback) is deliberate: Postgres'
    `now()` — backing the models' `server_default`/`onupdate` — is fixed for the
    lifetime of a transaction, so a single-transaction fixture could never show
    `updated_at` advancing. Truncating between tests lets a test commit between
    writes and observe a real timestamp change. repo functions still only flush;
    the test owns any commit, and reads through this same session see flushed-but-
    uncommitted writes.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as sess:
        try:
            yield sess
        finally:
            await sess.rollback()
            await sess.execute(
                text(f"TRUNCATE {', '.join(_TABLES_NEWEST_FIRST)} RESTART IDENTITY CASCADE")
            )
            await sess.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def test_sessionmaker(
    test_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the (function-scoped) test engine.

    Lets later handler tests swap the app's `SessionLocal` for one that talks to
    the test database.
    """
    return async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
