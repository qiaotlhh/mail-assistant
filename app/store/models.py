"""SQLAlchemy ORM 模型：emails / drafts / audit_logs。"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EmailStatus(str, enum.Enum):
    NEW = "new"
    IGNORED = "ignored"
    NOTIFIED = "notified"
    DRAFT_PENDING = "draft_pending"
    SEND_FAILED = "send_failed"
    SENT_DONE = "sent_done"


class EmailCategory(str, enum.Enum):
    IGNORE = "ignore"
    NOTIFY = "notify"
    RESPOND = "respond"


class RouteSource(str, enum.Enum):
    RULE = "rule"
    LLM = "llm"
    FALLBACK = "fallback"
    MANUAL = "manual"


class SentKind(str, enum.Enum):
    APPROVED = "approved"
    EDITED = "edited"


class DraftSource(str, enum.Enum):
    LLM = "llm"
    USER_EDIT = "user_edit"


class AuditAction(str, enum.Enum):
    CREATE = "create"
    ROUTE = "route"
    UPGRADE = "upgrade"
    RECLASSIFY = "reclassify"
    APPROVE_SEND = "approve_send"
    EDIT_SEND = "edit_send"
    MANUAL_IGNORE = "manual_ignore"
    REWRITE = "rewrite"
    SEND_FAILED = "send_failed"
    RETRY_SEND = "retry_send"
    MARK_READ = "mark_read"


class EmailRecord(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    imap_uid: Mapped[str | None] = mapped_column(String(255), index=True)
    sender: Mapped[str] = mapped_column(String(320))
    recipient: Mapped[str] = mapped_column(String(320))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    headers_json: Mapped[str] = mapped_column(Text, default="{}")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(16))
    route_reason: Mapped[str] = mapped_column(Text, default="")
    route_source: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32), default=EmailStatus.NEW.value, index=True
    )
    sent_kind: Mapped[str | None] = mapped_column(String(16))
    rewrite_rounds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    drafts: Mapped[list[DraftRecord]] = relationship(
        back_populates="email",
        order_by="DraftRecord.round_number, DraftRecord.id",
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="email", order_by="AuditLog.id"
    )


class DraftRecord(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    round_number: Mapped[int] = mapped_column(Integer, default=0)
    feedback: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default=DraftSource.LLM.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    email: Mapped[EmailRecord] = relationship(back_populates="drafts")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    email: Mapped[EmailRecord] = relationship(back_populates="audit_logs")
