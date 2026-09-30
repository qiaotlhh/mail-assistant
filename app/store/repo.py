"""仓储层与状态机：所有状态转移必须经过这里统一校验并写审计日志。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import (
    AuditAction,
    AuditLog,
    DraftRecord,
    DraftSource,
    EmailCategory,
    EmailRecord,
    EmailStatus,
    RouteSource,
    SentKind,
)


class EmailNotFoundError(RuntimeError):
    pass


class EmailExistsError(RuntimeError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


class RewriteLimitExceeded(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump_json(value: Any) -> str:
    payload = {} if value is None else value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _status_of(email: EmailRecord) -> EmailStatus:
    return EmailStatus(email.status)


@dataclass(frozen=True)
class _Transition:
    action: AuditAction
    allowed_from: frozenset[EmailStatus]
    target: EmailStatus


_ROUTE_TARGETS = {
    EmailCategory.IGNORE: EmailStatus.IGNORED,
    EmailCategory.NOTIFY: EmailStatus.NOTIFIED,
    EmailCategory.RESPOND: EmailStatus.DRAFT_PENDING,
}


def _add_audit(
    session: Session,
    email: EmailRecord,
    action: AuditAction,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            email_id=email.id,
            action=action.value,
            detail_json=_dump_json(detail),
            created_at=_now(),
        )
    )


def _require_email(session: Session, email_id: int) -> EmailRecord:
    email = session.get(EmailRecord, email_id)
    if email is None:
        raise EmailNotFoundError(f"邮件不存在：email_id={email_id}")
    return email


def _apply_transition(
    session: Session,
    email_id: int,
    transition: _Transition,
    *,
    field_updates: dict[str, Any] | None = None,
    audit_detail: dict[str, Any] | None = None,
) -> EmailRecord:
    email = _require_email(session, email_id)
    current = _status_of(email)
    if current not in transition.allowed_from:
        expected = "、".join(
            status.value
            for status in sorted(transition.allowed_from, key=lambda item: item.value)
        )
        raise InvalidTransitionError(
            f"非法状态转移：{transition.action.value} 要求当前状态为 [{expected}]，"
            f"实际为 {current.value}（email_id={email_id}）"
        )
    email.status = transition.target.value
    if field_updates:
        for field, value in field_updates.items():
            setattr(email, field, value)
    email.updated_at = _now()
    _add_audit(session, email, transition.action, audit_detail)
    session.flush()
    return email


def create_email(
    session: Session,
    *,
    message_id: str,
    sender: str,
    recipient: str,
    reply_to: str | None = None,
    subject: str = "",
    received_at: datetime | None = None,
    headers: dict[str, str] | None = None,
    body_text: str = "",
    body_truncated: bool = False,
    imap_uid: str | None = None,
) -> EmailRecord:
    """新邮件入库，初始状态 NEW；Message-ID 重复直接拒绝。"""
    existing = session.scalar(
        select(EmailRecord).where(EmailRecord.message_id == message_id)
    )
    if existing is not None:
        raise EmailExistsError(f"邮件已存在，拒绝重复入库：message_id={message_id}")
    now = _now()
    email = EmailRecord(
        message_id=message_id,
        imap_uid=imap_uid,
        sender=sender,
        recipient=recipient,
        reply_to=reply_to,
        subject=subject,
        received_at=received_at,
        headers_json=_dump_json(headers),
        body_text=body_text,
        body_truncated=body_truncated,
        created_at=now,
        updated_at=now,
    )
    session.add(email)
    session.flush()
    _add_audit(
        session,
        email,
        AuditAction.CREATE,
        {"message_id": message_id, "imap_uid": imap_uid},
    )
    session.flush()
    return email


def get_email(session: Session, email_id: int) -> EmailRecord:
    return _require_email(session, email_id)


def get_email_by_message_id(session: Session, message_id: str) -> EmailRecord | None:
    return session.scalar(
        select(EmailRecord).where(EmailRecord.message_id == message_id)
    )


def _list_conditions(
    *,
    status: EmailStatus | None,
    category: EmailCategory | None,
    unread_only: bool,
    read_only: bool,
):
    conditions = []
    if status is not None:
        conditions.append(EmailRecord.status == status.value)
    if category is not None:
        conditions.append(EmailRecord.category == category.value)
    if unread_only:
        conditions.append(EmailRecord.read_at.is_(None))
    if read_only:
        conditions.append(EmailRecord.read_at.is_not(None))
    return conditions


def list_emails(
    session: Session,
    *,
    status: EmailStatus | None = None,
    category: EmailCategory | None = None,
    unread_only: bool = False,
    read_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[EmailRecord]:
    stmt = select(EmailRecord).order_by(
        EmailRecord.received_at.desc().nulls_last(), EmailRecord.id.desc()
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    conditions = _list_conditions(
        status=status,
        category=category,
        unread_only=unread_only,
        read_only=read_only,
    )
    if conditions:
        stmt = stmt.where(*conditions)
    return list(session.scalars(stmt))


def count_emails(
    session: Session,
    *,
    status: EmailStatus | None = None,
    category: EmailCategory | None = None,
    unread_only: bool = False,
    read_only: bool = False,
) -> int:
    conditions = _list_conditions(
        status=status,
        category=category,
        unread_only=unread_only,
        read_only=read_only,
    )
    stmt = select(func.count()).select_from(EmailRecord)
    if conditions:
        stmt = stmt.where(*conditions)
    return int(session.scalar(stmt) or 0)


def list_unread_emails(
    session: Session,
    *,
    status: EmailStatus | None = None,
) -> list[EmailRecord]:
    stmt = select(EmailRecord).where(EmailRecord.read_at.is_(None))
    if status is not None:
        stmt = stmt.where(EmailRecord.status == status.value)
    return list(session.scalars(stmt.order_by(EmailRecord.id.asc())))


def pending_imap_uids(session: Session) -> set[str]:
    rows = session.execute(
        select(EmailRecord.imap_uid).where(
            EmailRecord.imap_uid.is_not(None),
            EmailRecord.read_at.is_(None),
        )
    )
    return {uid for (uid,) in rows if uid}


def mark_email_read(session: Session, email_id: int) -> EmailRecord:
    email = _require_email(session, email_id)
    if email.read_at is None:
        now = _now()
        email.read_at = now
        email.updated_at = now
        _add_audit(
            session,
            email,
            AuditAction.MARK_READ,
            {"imap_uid": email.imap_uid},
        )
        session.flush()
    return email

def next_actionable_email(session: Session) -> EmailRecord | None:
    """Return the oldest email still waiting for a human decision."""
    actionable_statuses = [
        EmailStatus.DRAFT_PENDING.value,
        EmailStatus.SEND_FAILED.value,
    ]
    stmt = (
        select(EmailRecord)
        .where(EmailRecord.status.in_(actionable_statuses))
        .order_by(EmailRecord.received_at.asc().nulls_first(), EmailRecord.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def route_email(
    session: Session,
    email_id: int,
    category: EmailCategory | str,
    reason: str,
    source: RouteSource | str,
) -> EmailRecord:
    """NEW → IGNORED / NOTIFIED / DRAFT_PENDING，并记录分类信息。"""
    try:
        cat = EmailCategory(category)
    except ValueError as exc:
        raise ValueError(f"未知分类：{category}，仅支持 ignore/notify/respond") from exc
    try:
        src = RouteSource(source)
    except ValueError as exc:
        raise ValueError(f"未知路由来源：{source}，仅支持 rule/llm/fallback") from exc
    transition = _Transition(
        action=AuditAction.ROUTE,
        allowed_from=frozenset({EmailStatus.NEW}),
        target=_ROUTE_TARGETS[cat],
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={
            "category": cat.value,
            "route_reason": reason,
            "route_source": src.value,
        },
        audit_detail={"category": cat.value, "reason": reason, "source": src.value},
    )


def upgrade_to_draft(session: Session, email_id: int) -> EmailRecord:
    """NOTIFIED/IGNORED → DRAFT_PENDING：人工把误判邮件升级为需回复。"""
    transition = _Transition(
        action=AuditAction.UPGRADE,
        allowed_from=frozenset({EmailStatus.NOTIFIED, EmailStatus.IGNORED}),
        target=EmailStatus.DRAFT_PENDING,
    )
    email = _apply_transition(session, email_id, transition)
    email.category = EmailCategory.RESPOND.value
    session.flush()
    return email


def reclassify_email(
    session: Session,
    email_id: int,
    target_category: EmailCategory | str,
) -> EmailRecord:
    """人工修正分类：在 IGNORE 与 NOTIFY 之间互转，不触发外发。"""
    try:
        cat = EmailCategory(target_category)
    except ValueError as exc:
        raise ValueError(
            f"未知目标分类：{target_category}，仅支持 ignore/notify"
        ) from exc
    if cat == EmailCategory.RESPOND:
        raise ValueError("改为需回复请使用 upgrade 动作先生成草稿")
    current = _require_email(session, email_id)
    from_category = current.category
    transition = _Transition(
        action=AuditAction.RECLASSIFY,
        allowed_from=frozenset(
            {
                EmailStatus.IGNORED,
                EmailStatus.NOTIFIED,
                EmailStatus.DRAFT_PENDING,
                EmailStatus.SEND_FAILED,
            }
        ),
        target=_ROUTE_TARGETS[cat],
    )
    reason = "人工重新分类为通知" if cat == EmailCategory.NOTIFY else "人工重新分类为忽略"
    email = _apply_transition(
        session,
        email_id,
        transition,
        field_updates={
            "category": cat.value,
            "route_reason": reason,
            "route_source": RouteSource.MANUAL.value,
        },
        audit_detail={
            "from_category": from_category,
            "target_category": cat.value,
            "reason": reason,
            "source": RouteSource.MANUAL.value,
        },
    )
    return email

def add_draft(
    session: Session,
    email_id: int,
    *,
    content: str,
    round_number: int,
    feedback: str | None = None,
    source: DraftSource | str = DraftSource.LLM,
) -> DraftRecord:
    _require_email(session, email_id)
    if not content.strip():
        raise ValueError("草稿内容不能为空")
    if round_number < 0:
        raise ValueError("草稿轮次不能为负数")
    src = DraftSource(source)
    draft = DraftRecord(
        email_id=email_id,
        content=content,
        round_number=round_number,
        feedback=feedback,
        source=src.value,
        created_at=_now(),
    )
    session.add(draft)
    session.flush()
    return draft


def request_rewrite(
    session: Session,
    email_id: int,
    *,
    feedback: str,
    max_rounds: int = 3,
) -> EmailRecord:
    """DRAFT_PENDING 自迁移：重写轮次 +1，超过上限拒绝。"""
    if max_rounds < 0:
        raise ValueError("重写轮数上限不能为负数")
    email = _require_email(session, email_id)
    if _status_of(email) != EmailStatus.DRAFT_PENDING:
        raise InvalidTransitionError(
            f"非法状态转移：rewrite 要求当前状态为 [draft_pending]，"
            f"实际为 {email.status}（email_id={email_id}）"
        )
    if email.rewrite_rounds >= max_rounds:
        raise RewriteLimitExceeded(
            f"重写轮数已达上限 {max_rounds} 轮（email_id={email_id}），"
            "请直接编辑草稿或忽略该邮件"
        )
    transition = _Transition(
        action=AuditAction.REWRITE,
        allowed_from=frozenset({EmailStatus.DRAFT_PENDING}),
        target=EmailStatus.DRAFT_PENDING,
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={"rewrite_rounds": email.rewrite_rounds + 1},
        audit_detail={
            "feedback": feedback,
            "round": email.rewrite_rounds + 1,
            "max_rounds": max_rounds,
        },
    )


def approve_send(session: Session, email_id: int) -> EmailRecord:
    transition = _Transition(
        action=AuditAction.APPROVE_SEND,
        allowed_from=frozenset({EmailStatus.DRAFT_PENDING}),
        target=EmailStatus.SENT_DONE,
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={"sent_kind": SentKind.APPROVED.value},
        audit_detail={"sent_kind": SentKind.APPROVED.value},
    )


def edit_send(session: Session, email_id: int) -> EmailRecord:
    transition = _Transition(
        action=AuditAction.EDIT_SEND,
        allowed_from=frozenset({EmailStatus.DRAFT_PENDING}),
        target=EmailStatus.SENT_DONE,
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={"sent_kind": SentKind.EDITED.value},
        audit_detail={"sent_kind": SentKind.EDITED.value},
    )


def manual_ignore(session: Session, email_id: int) -> EmailRecord:
    transition = _Transition(
        action=AuditAction.MANUAL_IGNORE,
        allowed_from=frozenset({EmailStatus.DRAFT_PENDING}),
        target=EmailStatus.IGNORED,
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={
            "category": EmailCategory.IGNORE.value,
            "route_reason": "人工忽略该邮件",
            "route_source": RouteSource.MANUAL.value,
        },
        audit_detail={"target_category": EmailCategory.IGNORE.value},
    )


def mark_send_failed(
    session: Session,
    email_id: int,
    error: str,
    *,
    sent_kind: SentKind | str | None = None,
) -> EmailRecord:
    transition = _Transition(
        action=AuditAction.SEND_FAILED,
        allowed_from=frozenset({EmailStatus.DRAFT_PENDING, EmailStatus.SEND_FAILED}),
        target=EmailStatus.SEND_FAILED,
    )
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates=(
            {"sent_kind": SentKind(sent_kind).value}
            if sent_kind is not None
            else None
        ),
        audit_detail={"error": error[:500]},
    )


def retry_send(
    session: Session,
    email_id: int,
    *,
    sent_kind: SentKind | str | None = None,
) -> EmailRecord:
    transition = _Transition(
        action=AuditAction.RETRY_SEND,
        allowed_from=frozenset({EmailStatus.SEND_FAILED}),
        target=EmailStatus.SENT_DONE,
    )
    kind = SentKind(sent_kind).value if sent_kind is not None else None
    return _apply_transition(
        session,
        email_id,
        transition,
        field_updates={"sent_kind": kind} if kind is not None else None,
        audit_detail={"sent_kind": kind} if kind is not None else None,
    )
