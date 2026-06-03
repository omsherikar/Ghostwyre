"""Tests for the Phase 3 Alembic migration.

`test_migration_file_exists` is PURE (no DB): it globs the versions dir and loads
the Alembic ScriptDirectory to assert exactly one head and that the migration
imports cleanly. The upgrade/downgrade tests are `@pytest.mark.slow` because they
talk to the live Postgres; each is self-restoring so the suite leaves the DB at
head and usable.

Alembic's `command` API runs `env.py` (which is async via `asyncio.run`), so the
slow tests drive it through a subprocess to avoid nesting event loops.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"

EXPECTED_TABLES = {"draft_batch", "draft", "approval_event"}


async def _table_names() -> set[str]:
    """Inspect the live DB (no sync driver is installed, so use an async engine).

    A throwaway engine per call keeps each async test self-contained — the shared
    `app.db.session.engine` pools connections bound to the first test's event loop,
    which pytest-asyncio replaces between tests.
    """
    from app.config import get_settings

    def _names(conn: Connection) -> set[str]:
        return set(inspect(conn).get_table_names())

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_names)
    finally:
        await engine.dispose()


async def _draft_unique_constraints() -> set[str]:
    from app.config import get_settings

    def _names(conn: Connection) -> set[str]:
        return {uc["name"] for uc in inspect(conn).get_unique_constraints("draft") if uc["name"]}

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_names)
    finally:
        await engine.dispose()


async def _draft_columns() -> set[str]:
    from app.config import get_settings

    def _names(conn: Connection) -> set[str]:
        return {c["name"] for c in inspect(conn).get_columns("draft")}

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_names)
    finally:
        await engine.dispose()


def _run_alembic(*args: str) -> None:
    """Run alembic in a subprocess (env.py runs its own asyncio loop)."""
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_migration_file_exists() -> None:
    matches = list(VERSIONS_DIR.glob("*phase3*.py"))
    assert matches, "expected at least one *phase3*.py migration in migrations/versions/"

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one head revision, got {heads}"

    # Loading the revision imports the module — fails loudly if it has a syntax/import error.
    revision = script.get_revision(heads[0])
    assert revision is not None
    assert revision.module is not None


@pytest.mark.slow
async def test_migration_upgrade_creates_tables() -> None:
    # Force a real rebuild even from a dirty starting state (e.g. alembic_version
    # claims head but the physical tables were dropped): `upgrade head` is a no-op
    # when already at head, so go to base first.
    _run_alembic("downgrade", "base")
    _run_alembic("upgrade", "head")

    tables = await _table_names()
    assert EXPECTED_TABLES <= tables, f"missing tables: {EXPECTED_TABLES - tables}"

    uc_names = await _draft_unique_constraints()
    assert "uq_draft_batch_slot" in uc_names

    # The draft_platform migration adds the per-draft platform column.
    assert "platform" in await _draft_columns()


@pytest.mark.slow
async def test_migration_downgrade_drops_tables() -> None:
    # Downgrade the whole chain to base (robust to the number of migrations) and
    # assert the schema is torn down.
    _run_alembic("upgrade", "head")
    _run_alembic("downgrade", "base")

    tables = await _table_names()
    assert not (EXPECTED_TABLES & tables), (
        f"tables not dropped by downgrade: {EXPECTED_TABLES & tables}"
    )

    # Restore so the suite leaves the DB usable at head.
    _run_alembic("upgrade", "head")
