"""后台轮询编排：IMAP 未读入库、业务 NEW 恢复与 graph 触发。

安全边界：调度器只调用 graph 生成分类/草稿，不 import 也不调用 SMTP。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Protocol

from app.agent.graph import run_for_email
from app.mailbox.imap_client import FetchedEmail
from app.mailbox.parser import parse_email
from app.store import repo
from app.store.models import EmailStatus


logger = logging.getLogger(__name__)


class MailboxClient(Protocol):
    def fetch_unread(
        self,
        limit: int,
        exclude_uids: set[str] | None = None,
    ) -> list[FetchedEmail]: ...

    def mark_seen(self, uid: str) -> None: ...

    def mark_seen_many(self, uids: list[str] | tuple[str, ...]) -> None: ...


@dataclass(frozen=True)
class PollResult:
    batch_sizes: tuple[int, ...] = ()
    stored_count: int = 0
    duplicate_count: int = 0
    graph_count: int = 0
    recovered_count: int = 0
    skipped: bool = False
    errors: tuple[str, ...] = ()


@dataclass
class _MutableResult:
    batch_sizes: list[int] = field(default_factory=list)
    stored_count: int = 0
    duplicate_count: int = 0
    graph_count: int = 0
    recovered_count: int = 0
    errors: list[str] = field(default_factory=list)

    def freeze(self, *, skipped: bool = False) -> PollResult:
        return PollResult(
            batch_sizes=tuple(self.batch_sizes),
            stored_count=self.stored_count,
            duplicate_count=self.duplicate_count,
            graph_count=self.graph_count,
            recovered_count=self.recovered_count,
            skipped=skipped,
            errors=tuple(self.errors),
        )


class EmailScheduler:
    """单实例防重入的同步调度用例，可独立测试，也可放入后台线程。"""

    def __init__(
        self,
        *,
        mailbox: MailboxClient,
        session_factory,
        graph,
        fetch_limit: int,
        max_body_chars: int,
        alerts: list[str] | None = None,
    ):
        if fetch_limit < 1:
            raise ValueError("fetch_limit 必须为正整数")
        if max_body_chars < 1:
            raise ValueError("max_body_chars 必须为正整数")
        self._mailbox = mailbox
        self._sessions = session_factory
        self._graph = graph
        self._fetch_limit = fetch_limit
        self._max_body_chars = max_body_chars
        self._alerts = alerts
        self._poll_lock = threading.Lock()

    def run_once(
        self, *, stop_event: threading.Event | None = None
    ) -> PollResult:
        """执行一轮：先恢复业务 NEW，再处理当前前 fetch_limit 封未读。"""
        if not self._poll_lock.acquire(blocking=False):
            message = "上一轮轮询尚未结束，本轮跳过"
            self._alert(message)
            return PollResult(skipped=True, errors=(message,))
        try:
            return self._run_once_locked(stop_event=stop_event)
        finally:
            self._poll_lock.release()

    def _run_once_locked(
        self, *, stop_event: threading.Event | None
    ) -> PollResult:
        result = _MutableResult()
        self._recover_new_emails(result, stop_event=stop_event)

        if stop_event is not None and stop_event.is_set():
            return result.freeze()

        try:
            fetched = self._mailbox.fetch_unread(self._fetch_limit)
        except Exception as exc:
            self._record_error(result, "IMAP 拉取失败", exc)
            return result.freeze()

        result.batch_sizes.append(len(fetched))
        for item in fetched:
            if stop_event is not None and stop_event.is_set():
                break
            self._ingest(item, result)

        return result.freeze()

    def _recover_new_emails(
        self, result: _MutableResult, *, stop_event: threading.Event | None
    ) -> None:
        try:
            with self._sessions() as db:
                email_ids = [
                    record.id
                    for record in repo.list_emails(db, status=EmailStatus.NEW)
                ]
        except Exception as exc:
            self._record_error(result, "待恢复 NEW 邮件读取失败", exc)
            return

        for email_id in email_ids:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                run_for_email(self._graph, email_id)
                result.recovered_count += 1
            except Exception as exc:
                self._record_error(
                    result, f"业务 NEW 邮件恢复失败 email_id={email_id}", exc
                )

    def _ingest(self, item: FetchedEmail, result: _MutableResult) -> None:
        try:
            content = parse_email(item.raw, max_body_chars=self._max_body_chars)
        except Exception as exc:
            self._record_error(result, f"邮件解析失败 uid={item.uid}", exc)
            return

        stored = False
        try:
            with self._sessions() as db:
                existing = repo.get_email_by_message_id(db, content.message_id)
                if existing is not None:
                    result.duplicate_count += 1
                else:
                    record = repo.create_email(
                        db,
                        message_id=content.message_id,
                        sender=content.sender,
                        recipient=content.recipient,
                        reply_to=content.reply_to,
                        subject=content.subject,
                        received_at=content.received_at,
                        headers=content.headers,
                        body_text=content.body_text,
                        body_truncated=content.body_truncated,
                        imap_uid=item.uid,
                    )
                    db.commit()
                    stored = True
                    result.stored_count += 1
        except Exception as exc:
            self._record_error(
                result,
                f"邮件入库失败 uid={item.uid} message_id={content.message_id}",
                exc,
            )
            return

        # A different UID carrying a known Message-ID has no separate review
        # record, so release it immediately. New unique records stay unread
        # until the human confirms them in the web UI.
        if existing is not None and existing.imap_uid != item.uid:
            try:
                self._mailbox.mark_seen(item.uid)
            except Exception as exc:
                self._record_error(result, f"重复邮件标记已读失败 uid={item.uid}", exc)

        if stored:
            try:
                run_for_email(self._graph, record.id)
                result.graph_count += 1
            except Exception as exc:
                self._record_error(
                    result, f"工作流执行失败 message_id={content.message_id}", exc
                )

    def _record_error(
        self, result: _MutableResult, context: str, exc: Exception
    ) -> None:
        message = f"M8 轮询错误：{context}：{exc}"
        result.errors.append(message)
        self._alert(message)
        logger.warning("%s", message, exc_info=True)

    def _alert(self, message: str) -> None:
        if self._alerts is None:
            return
        self._alerts.append(message)
        del self._alerts[:-20]


class SchedulerRunner:
    """后台线程包装器：启动立即执行一轮，之后按固定间隔轮询。"""

    def __init__(self, scheduler: EmailScheduler, *, interval_seconds: int):
        if interval_seconds < 1:
            raise ValueError("interval_seconds 必须为正整数")
        self._scheduler = scheduler
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="email-agent-scheduler",
            daemon=False,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self._scheduler.run_once(stop_event=self._stop_event)
                logger.info(
                    "轮询完成：batches=%s stored=%d duplicate=%d graph=%d recovered=%d errors=%d",
                    list(result.batch_sizes),
                    result.stored_count,
                    result.duplicate_count,
                    result.graph_count,
                    result.recovered_count,
                    len(result.errors),
                )
            except Exception:
                # run_once 已覆盖主要异常；这里保护后台线程不被意外杀死。
                logger.exception("调度器发生未预期异常，等待下一轮重试")
            if self._stop_event.wait(self._interval):
                break
