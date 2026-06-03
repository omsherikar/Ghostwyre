"""SQLAlchemy ORM models.

The declarative `Base` is the metadata target Alembic migrates against. Phase 3
adds the approval-flow tables — DraftBatch, Draft, ApprovalEvent — so the
Slack card can persist drafts before posting and resolve everything by id from
the button payload.

The transcript stored on DraftBatch is CONFIDENTIAL at rest: never log it, and
never put it in a Slack button value.

Status/action columns are stored as VARCHAR (Enum with
`values_callable=lambda e: [m.value for m in e]`) so adding a new enum member
needs no migration.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BatchStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    cancelled = "cancelled"
    expired = "expired"


class DraftStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    regenerating = "regenerating"
    cancelled = "cancelled"


class ApprovalAction(enum.StrEnum):
    approve = "approve"
    regenerate = "regenerate"
    cancel = "cancel"


class DraftPlatform(enum.StrEnum):
    x = "x"
    linkedin = "linkedin"


class DraftBatch(Base):
    __tablename__ = "draft_batch"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slack_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    transcript: Mapped[str] = mapped_column(Text, nullable=False)  # CONFIDENTIAL — never log
    slack_message_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=BatchStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    drafts: Mapped[list[Draft]] = relationship(
        "Draft",
        back_populates="batch",
        order_by="Draft.slot_index",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[ApprovalEvent]] = relationship("ApprovalEvent", back_populates="batch")


class Draft(Base):
    __tablename__ = "draft"
    __table_args__ = (UniqueConstraint("batch_id", "slot_index", name="uq_draft_batch_slot"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-based
    text: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[DraftPlatform] = mapped_column(
        Enum(DraftPlatform, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DraftPlatform.x,
        server_default=DraftPlatform.x.value,
    )
    status: Mapped[DraftStatus] = mapped_column(
        Enum(DraftStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DraftStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    batch: Mapped[DraftBatch] = relationship("DraftBatch", back_populates="drafts")
    events: Mapped[list[ApprovalEvent]] = relationship("ApprovalEvent", back_populates="draft")


class ApprovalEvent(Base):
    __tablename__ = "approval_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft.id", ondelete="SET NULL"),
        nullable=True,  # Cancel has no draft
    )
    action: Mapped[ApprovalAction] = mapped_column(
        Enum(ApprovalAction, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    slack_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    publish_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batch: Mapped[DraftBatch] = relationship("DraftBatch", back_populates="events")
    draft: Mapped[Draft | None] = relationship("Draft", back_populates="events")
