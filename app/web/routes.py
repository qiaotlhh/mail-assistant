"""本地人工审核页：列表、详情与四类审核动作。"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.agent.graph import GraphError, resume_for_email, upgrade_for_email
from app.store import repo
from app.store.models import EmailRecord, EmailStatus


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CATEGORY_LABELS = {
    "ignore": "忽略",
    "notify": "通知",
    "respond": "需回复",
}

STATUS_LABELS = {
    "new": "处理中",
    "ignored": "已忽略",
    "notified": "已通知",
    "sent_done": "已发送",
    "draft_pending": "待审核",
    "send_failed": "发送失败",
}


class SendEmailError(RuntimeError):
    """发送适配器失败；M7 将把该失败写入 SEND_FAILED。"""


class MarkReadError(RuntimeError):
    """IMAP 标记已读失败；本地 read_at 不应提前写入。"""


class AutoMarkReadError(RuntimeError):
    """SMTP 已发送成功，但自动标记邮箱已读失败。"""


class MarkSeenFn(Protocol):
    def __call__(self, uids: list[str] | tuple[str, ...]) -> None: ...


class SendEmailFn(Protocol):
    def __call__(
        self,
        *,
        email_id: int,
        recipient: str,
        reply_to: str | None,
        subject: str,
        body: str,
        message_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class EmailSummary:
    id: int
    sender: str
    recipient: str
    subject: str
    status: str
    category: str | None
    received_at: datetime | None
    updated_at: datetime
    read_at: datetime | None


@dataclass(frozen=True)
class DraftView:
    id: int
    content: str
    round_number: int
    feedback: str | None
    source: str
    created_at: datetime


@dataclass(frozen=True)
class AuditView:
    id: int
    action: str
    detail: dict
    created_at: datetime


@dataclass(frozen=True)
class EmailDetail(EmailSummary):
    message_id: str
    imap_uid: str | None
    body_text: str
    body_truncated: bool
    headers: dict[str, str]
    route_reason: str
    route_source: str | None
    rewrite_rounds: int
    sent_kind: str | None
    drafts: list[DraftView]
    audit_logs: list[AuditView]


def _display_datetime(
    value: datetime | None, display_timezone: ZoneInfo
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(display_timezone)


def _summary(record: EmailRecord, display_timezone: ZoneInfo) -> EmailSummary:
    return EmailSummary(
        id=record.id,
        sender=record.sender,
        recipient=record.recipient,
        subject=record.subject,
        status=record.status,
        category=record.category,
        received_at=_display_datetime(record.received_at, display_timezone),
        updated_at=_display_datetime(record.updated_at, display_timezone),
        read_at=_display_datetime(record.read_at, display_timezone),
    )


def _detail(record: EmailRecord, display_timezone: ZoneInfo) -> EmailDetail:
    try:
        headers = json.loads(record.headers_json or "{}")
    except json.JSONDecodeError:
        headers = {}
    return EmailDetail(
        id=record.id,
        sender=record.sender,
        recipient=record.recipient,
        subject=record.subject,
        status=record.status,
        category=record.category,
        received_at=_display_datetime(record.received_at, display_timezone),
        updated_at=_display_datetime(record.updated_at, display_timezone),
        read_at=_display_datetime(record.read_at, display_timezone),
        message_id=record.message_id,
        imap_uid=record.imap_uid,
        body_text=record.body_text,
        body_truncated=record.body_truncated,
        headers=headers,
        route_reason=record.route_reason,
        route_source=record.route_source,
        rewrite_rounds=record.rewrite_rounds,
        sent_kind=record.sent_kind,
        drafts=[
            DraftView(
                id=draft.id,
                content=draft.content,
                round_number=draft.round_number,
                feedback=draft.feedback,
                source=draft.source,
                created_at=_display_datetime(draft.created_at, display_timezone),
            )
            for draft in record.drafts
        ],
        audit_logs=[
            AuditView(
                id=log.id,
                action=log.action,
                detail=json.loads(log.detail_json or "{}"),
                created_at=_display_datetime(log.created_at, display_timezone),
            )
            for log in record.audit_logs
        ],
    )


HISTORY_PAGE_SIZE = 10


@dataclass(frozen=True)
class HistoryPage:
    emails: list[EmailSummary]
    page: int
    page_size: int
    total: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


class ReviewService:
    """审核页用例层：业务表读取、发送前置动作、graph resume。"""

    def __init__(
        self,
        session_factory,
        graph,
        *,
        sender: SendEmailFn,
        mark_seen: MarkSeenFn | None = None,
        max_rewrite_rounds: int = 3,
        display_timezone: str = "Asia/Shanghai",
    ):
        self._sessions = session_factory
        self._graph = graph
        self._sender = sender
        self._mark_seen = mark_seen
        self.max_rewrite_rounds = max_rewrite_rounds
        self._display_timezone = ZoneInfo(display_timezone)
        self._locks_guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._successfully_sent: set[int] = set()

    def _lock_for(self, email_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(email_id, threading.Lock())

    def list(
        self,
        status: EmailStatus | None = None,
        *,
        unread_only: bool = False,
        read_only: bool = False,
    ) -> list[EmailSummary]:
        with self._sessions() as db:
            return [
                _summary(record, self._display_timezone)
                for record in repo.list_emails(
                    db,
                    status=status,
                    unread_only=unread_only,
                    read_only=read_only,
                )
            ]

    def list_queue(
        self, status: EmailStatus | None = None
    ) -> list[EmailSummary]:
        if status == EmailStatus.SENT_DONE:
            return self.list(status=status)

        records = self.list(status=status, unread_only=True)
        if status is None:
            records = [
                email for email in records if email.status != EmailStatus.NEW.value
            ]
        return records

    def list_history(
        self, status: EmailStatus | None = None, *, page: int = 1
    ) -> HistoryPage:
        if page < 1:
            raise ValueError("page 必须为正整数")
        with self._sessions() as db:
            total = repo.count_emails(
                db, status=status, read_only=True
            )
            total_pages = max(1, math.ceil(total / HISTORY_PAGE_SIZE))
            if page > total_pages:
                raise ValueError("页码超出范围")
            records = repo.list_emails(
                db,
                status=status,
                read_only=True,
                limit=HISTORY_PAGE_SIZE,
                offset=(page - 1) * HISTORY_PAGE_SIZE,
            )
            return HistoryPage(
                emails=[
                    _summary(record, self._display_timezone)
                    for record in records
                ],
                page=page,
                page_size=HISTORY_PAGE_SIZE,
                total=total,
                total_pages=total_pages,
            )

    def get(self, email_id: int) -> EmailDetail:
        with self._sessions() as db:
            return _detail(repo.get_email(db, email_id), self._display_timezone)

    def unread_count(self, status: EmailStatus) -> int:
        with self._sessions() as db:
            return len(repo.list_unread_emails(db, status=status))

    def mark_read(self, email_id: int) -> str:
        with self._lock_for(email_id):
            return self._mark_read_locked(email_id, auto=False)

    def _mark_read_locked(self, email_id: int, *, auto: bool) -> str:
        with self._sessions() as db:
            record = repo.get_email(db, email_id)
            if record.read_at is not None:
                return "该邮件已是已读"
            uids = [record.imap_uid] if record.imap_uid else []
        try:
            if uids:
                self._require_mark_seen()(uids)
        except Exception as exc:
            error_type = AutoMarkReadError if auto else MarkReadError
            raise error_type(f"标记邮箱已读失败：{exc}") from exc
        with self._sessions() as db:
            repo.mark_email_read(db, email_id)
            db.commit()
        return "已标记为已读"

    def mark_ignored_read(self) -> int:
        with self._sessions() as db:
            records = repo.list_unread_emails(db, status=EmailStatus.IGNORED)
            ids = [record.id for record in records]
            uids = [record.imap_uid for record in records if record.imap_uid]
        if uids:
            try:
                self._require_mark_seen()(uids)
            except Exception as exc:
                raise MarkReadError(f"批量标记邮箱已读失败：{exc}") from exc
        if not ids:
            return 0
        with self._sessions() as db:
            for email_id in ids:
                repo.mark_email_read(db, email_id)
            db.commit()
        return len(ids)

    def _require_mark_seen(self) -> MarkSeenFn:
        if self._mark_seen is None:
            raise MarkReadError("当前应用未配置邮箱已读适配器")
        return self._mark_seen

    def next_actionable(self) -> EmailSummary | None:
        with self._sessions() as db:
            record = repo.next_actionable_email(db)
            if record is None:
                return None
            return _summary(record, self._display_timezone)

    def act(
        self,
        email_id: int,
        *,
        action: str,
        draft: str | None = None,
        feedback: str | None = None,
        target_category: str | None = None,
    ) -> str:
        supported = {
            "upgrade",
            "reclassify",
            "approve",
            "edit_send",
            "ignore",
            "rewrite",
            "retry_send",
        }
        if action not in supported:
            raise HTTPException(status_code=400, detail=f"未知审核动作：{action}")

        with self._lock_for(email_id):
            with self._sessions() as db:
                record = repo.get_email(db, email_id)
                allowed_statuses = {
                    "upgrade": {
                        EmailStatus.NOTIFIED.value,
                        EmailStatus.IGNORED.value,
                    },
                    "reclassify": {
                        EmailStatus.NOTIFIED.value,
                        EmailStatus.IGNORED.value,
                        EmailStatus.DRAFT_PENDING.value,
                        EmailStatus.SEND_FAILED.value,
                    },
                    "retry_send": {EmailStatus.SEND_FAILED.value},
                }.get(action, {EmailStatus.DRAFT_PENDING.value})
                if record.status not in allowed_statuses:
                    expected = "、".join(sorted(allowed_statuses))
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"动作 {action} 要求状态为 {expected}，"
                            f"当前状态：{record.status}"
                        ),
                    )
                current_draft = record.drafts[-1].content if record.drafts else ""
                try:
                    headers = json.loads(record.headers_json or "{}")
                except json.JSONDecodeError:
                    headers = {}

            if action == "rewrite" and not (feedback or "").strip():
                raise HTTPException(status_code=400, detail="重写需要填写反馈")

            target = (target_category or "").strip().lower()
            if action == "reclassify":
                if target not in {"ignore", "notify", "respond"}:
                    raise HTTPException(
                        status_code=400,
                        detail="重新分类需要有效的 target_category",
                    )
                if target == record.category:
                    raise HTTPException(
                        status_code=400,
                        detail="目标分类与当前分类相同",
                    )
                if target == "respond":
                    if record.status not in {
                        EmailStatus.NOTIFIED.value,
                        EmailStatus.IGNORED.value,
                    }:
                        raise HTTPException(
                            status_code=409,
                            detail="只有已通知/已忽略邮件可升级为需回复",
                        )
                    action = "upgrade"
                elif record.status in {
                    EmailStatus.NOTIFIED.value,
                    EmailStatus.IGNORED.value,
                }:
                    with self._sessions() as db:
                        repo.reclassify_email(db, email_id, target)
                        db.commit()
                    return f"已重新分类为{'通知' if target == 'notify' else '忽略'}"

            payload: dict = {"action": action}
            if action == "reclassify":
                payload["target_category"] = target
            if action == "upgrade":
                result = upgrade_for_email(self._graph, email_id)
                if not result.get("__interrupt__"):
                    raise HTTPException(
                        status_code=400,
                        detail="草稿生成失败，邮件保持原状态",
                    )
                return "已升级并生成回复草稿"
            if action in {"approve", "edit_send", "retry_send"}:
                body_to_send = current_draft
                if action == "edit_send":
                    body_to_send = (draft or "").strip()
                    if not body_to_send:
                        raise HTTPException(
                            status_code=400, detail="修改发送需要填写草稿内容"
                        )
                    payload["draft"] = body_to_send
                    with self._sessions() as db:
                        current = repo.get_email(db, email_id)
                        latest = current.drafts[-1] if current.drafts else None
                        already_saved = (
                            latest is not None
                            and latest.source == "user_edit"
                            and latest.round_number == current.rewrite_rounds
                            and latest.content == body_to_send
                        )
                        if not already_saved:
                            repo.add_draft(
                                db,
                                email_id,
                                content=body_to_send,
                                round_number=current.rewrite_rounds,
                                source="user_edit",
                            )
                            db.commit()
                elif action == "approve":
                    payload["sent_kind"] = "approved"
                else:
                    payload["sent_kind"] = record.sent_kind or "approved"
                if action == "edit_send":
                    payload["sent_kind"] = "edited"
                if not body_to_send:
                    raise HTTPException(
                        status_code=400, detail="当前邮件没有可发送草稿"
                    )
                self._send_locked(
                    email_id=email_id,
                    recipient=record.reply_to or record.sender,
                    reply_to=record.reply_to,
                    subject=record.subject,
                    body=body_to_send,
                    message_id=record.message_id,
                    headers=headers,
                    intended_sent_kind=payload["sent_kind"],
                )
            elif action == "rewrite":
                payload["feedback"] = (feedback or "").strip()

            resume_for_email(self._graph, email_id, payload)
            if action in {"approve", "edit_send", "retry_send"}:
                # 发送与状态迁移已成功；IMAP 失败只提示补标记，不回滚发送。
                self._mark_read_locked(email_id, auto=True)
            self._successfully_sent.discard(email_id)
            message = {
                "approve": "已批准并发送",
                "edit_send": "已按修改稿发送",
                "ignore": "已忽略该邮件",
                "rewrite": "已根据反馈生成新草稿",
                "retry_send": "人工重试发送成功",
                "reclassify": "",
            }[action]
            if action == "reclassify":
                return f"已重新分类为{'通知' if target == 'notify' else '忽略'}"
            return message

    def _send_locked(
        self,
        *,
        email_id: int,
        recipient: str,
        reply_to: str | None,
        subject: str,
        body: str,
        message_id: str | None,
        headers: dict[str, str],
        intended_sent_kind: str | None,
    ) -> None:
        if email_id in self._successfully_sent:
            return
        try:
            self._sender(
                email_id=email_id,
                recipient=recipient,
                reply_to=reply_to,
                subject=subject,
                body=body,
                message_id=message_id,
                headers=headers,
            )
            self._successfully_sent.add(email_id)
        except Exception as exc:
            error = f"发送适配器调用失败：{exc}"
            with self._sessions() as db:
                repo.mark_send_failed(
                    db, email_id, error, sent_kind=intended_sent_kind
                )
                db.commit()
            raise SendEmailError(error) from exc


def create_review_router(
    service: ReviewService,
    *,
    alerts: list[str] | None = None,
) -> APIRouter:
    router = APIRouter()
    alert_messages = alerts if alerts is not None else []

    @router.get("/")
    async def index(
        request: Request,
        status: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ):
        selected = None
        if status:
            try:
                selected = EmailStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="未知状态筛选") from exc
        view = request.query_params.get("view", "queue")
        if view not in {"queue", "history"}:
            raise HTTPException(status_code=400, detail="未知列表视图")
        history = None
        if view == "history":
            try:
                page = int(request.query_params.get("page", "1"))
                history = service.list_history(selected, page=page)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            emails = history.emails
        else:
            emails = service.list_queue(selected)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "emails": emails,
                "history_page": history,
                "selected_status": status or "",
                "selected_view": view,
                "ignored_unread_count": service.unread_count(EmailStatus.IGNORED),
                "category_labels": CATEGORY_LABELS,
                "status_labels": STATUS_LABELS,
                "message": message,
                "error": error,
                "alerts": alert_messages[-5:],
            },
        )

    @router.post("/alerts/clear")
    async def clear_alerts():
        alert_messages.clear()
        return RedirectResponse(url="/?message=已清除告警", status_code=303)


    @router.post("/reviews/mark-read/batch")
    async def mark_ignored_read():
        try:
            count = service.mark_ignored_read()
        except MarkReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = f"已批量标记 {count} 封忽略邮件为已读"
        return RedirectResponse(
            url=f"/?status=ignored&message={quote(message)}", status_code=303
        )

    @router.get("/reviews/{email_id}")
    async def detail(
        request: Request,
        email_id: int,
        message: str | None = None,
        error: str | None = None,
    ):
        try:
            email = service.get(email_id)
        except repo.EmailNotFoundError as exc:
            raise HTTPException(status_code=404, detail="邮件不存在") from exc
        from_target = request.query_params.get("from")
        history_mode = from_target == "history"
        history_page = 1
        if history_mode:
            try:
                history_page = int(request.query_params.get("page", "1"))
                if history_page < 1:
                    raise ValueError("page 必须为正整数")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            list_url = f"/?view=history&page={history_page}"
        else:
            if from_target:
                try:
                    EmailStatus(from_target)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="未知来源筛选") from exc
            list_url = f"/?status={from_target}" if from_target else "/"
        return TEMPLATES.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                "email": email,
                "list_url": list_url,
                "history_mode": history_mode,
                "history_page": history_page,
                "current_draft": email.drafts[-1].content if email.drafts else "",
                "max_rewrite_rounds": service.max_rewrite_rounds,
                "category_labels": CATEGORY_LABELS,
                "status_labels": STATUS_LABELS,
                "message": message,
                "error": error,
                "alerts": alert_messages[-5:],
            },
        )

    @router.post("/reviews/{email_id}/read")
    async def mark_email_read(email_id: int):
        try:
            email = service.get(email_id)
            message = service.mark_read(email_id)
        except repo.EmailNotFoundError as exc:
            raise HTTPException(status_code=404, detail="邮件不存在") from exc
        except MarkReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            url=f"/?status={email.status}&message={quote(message)}",
            status_code=303,
        )

    @router.post("/reviews/{email_id}/actions")
    async def submit_action(
        email_id: int,
        action: str = Form(...),
        draft: str | None = Form(default=None),
        feedback: str | None = Form(default=None),
        target_category: str | None = Form(default=None),
        continue_review: str | None = Form(default=None),
    ):
        try:
            message = service.act(
                email_id,
                action=action,
                draft=draft,
                feedback=feedback,
                target_category=target_category,
            )
        except repo.EmailNotFoundError as exc:
            raise HTTPException(status_code=404, detail="邮件不存在") from exc
        except AutoMarkReadError as exc:
            warning = f"邮件已发送，但自动{exc}"
            if alert_messages is not None:
                alert_messages.append(warning)
            return RedirectResponse(
                url=f"/?status=sent_done&error={quote(warning)}",
                status_code=303,
            )
        except (
            GraphError,
            repo.InvalidTransitionError,
            repo.RewriteLimitExceeded,
            ValueError,
            SendEmailError,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if continue_review:
            next_email = service.next_actionable()
            if next_email is not None:
                return RedirectResponse(
                    url=f"/reviews/{next_email.id}?message={quote(message)}",
                    status_code=303,
                )
            message = f"{message}，暂无其他待审核邮件"
        redirect_status = (
            "sent_done" if action in {"approve", "edit_send", "retry_send"} else None
        )
        url = f"/?status={redirect_status}&message={quote(message)}" if redirect_status else f"/?message={quote(message)}"
        return RedirectResponse(url=url, status_code=303)

    return router
