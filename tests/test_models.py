"""Pure metadata tests for the Phase 3 ORM models.

No DB connection: assertions run against SQLAlchemy `.__table__` metadata and
the enum members, mirroring the existing pure-test style. Importing `app.db`
constructs the engine lazily (no connection), so these stay Postgres-free.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from app.db.models import (
    ApprovalAction,
    ApprovalEvent,
    BatchStatus,
    Draft,
    DraftBatch,
    DraftStatus,
)


def test_models_import() -> None:
    assert DraftBatch is not None
    assert Draft is not None
    assert ApprovalEvent is not None


def test_draft_batch_id_is_uuid_primary_key() -> None:
    id_col = DraftBatch.__table__.c.id
    assert id_col.primary_key is True
    assert isinstance(id_col.type, UUID)


def test_draft_batch_transcript_not_null() -> None:
    assert "transcript" in DraftBatch.__table__.c
    assert DraftBatch.__table__.c.transcript.nullable is False


def test_draft_batch_id_foreign_key_cascade() -> None:
    fks = list(Draft.__table__.c.batch_id.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "draft_batch"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_draft_unique_constraint_name() -> None:
    names = {c.name for c in Draft.__table__.constraints if c.name is not None}
    assert "uq_draft_batch_slot" in names


def test_approval_event_draft_id_nullable() -> None:
    assert ApprovalEvent.__table__.c.draft_id.nullable is True


def test_approval_event_draft_id_foreign_key_set_null() -> None:
    fks = list(ApprovalEvent.__table__.c.draft_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"


def test_status_columns_compile_to_varchar_not_native_enum() -> None:
    # Locked design decision: status/action stored as VARCHAR (native_enum=False),
    # so adding an enum member needs no ALTER TYPE migration. Native PG enums
    # would compile to a bare type name (e.g. "batchstatus") instead.
    dialect = postgresql.dialect()
    for col in (
        DraftBatch.__table__.c.status,
        Draft.__table__.c.status,
        ApprovalEvent.__table__.c.action,
    ):
        compiled = col.type.compile(dialect=dialect)
        assert compiled.startswith("VARCHAR"), f"{col} compiled to {compiled!r}, expected VARCHAR"


def test_enum_values() -> None:
    assert BatchStatus.pending.value == "pending"
    assert BatchStatus.approved.value == "approved"
    assert {m.value for m in ApprovalAction} == {"approve", "regenerate", "cancel"}
    assert "regenerating" in {m.value for m in DraftStatus}


def test_db_package_reexports_session_local() -> None:
    from app.db import SessionLocal

    assert SessionLocal is not None
