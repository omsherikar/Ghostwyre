"""SQLAlchemy ORM models.

Phase 0 defines only the declarative Base so Alembic has a metadata target.
The real tables — DraftBatch, Draft, ApprovalEvent — arrive in Phase 3 when the
approval flow needs to persist and look up drafts by id.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
