"""M8 验收：轮询调度、未读队列、崩溃恢复与防重入。"""

from __future__ import annotations

import sqlite3
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.graph import build_graph
from app.agent.router import RouteContext, RouteResult
from app.mailbox.imap_client import FetchedEmail
from app.scheduler import EmailScheduler, SchedulerRunner
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailCategory, EmailStatus, RouteSource
from app.store.repo import create_email, list_emails


class FakeMailbox:
    def __init__(self, raw_messages):
        self.messages = {
            str(index): raw for index, raw in enumerate(raw_messages, start=1)
        }
        self.seen: set[str] = set()
        self.errors: list[Exception] = []
        self.fetch_count = 0

    def fetch_unread(
        self,
        limit: int,
        exclude_uids: set[str] | None = None,
    ) -> list[FetchedEmail]:
        self.fetch_count += 1
        if self.errors:
            raise self.errors.pop(0)
        excluded = self.seen | (exclude_uids or set())
        unseen = [
            FetchedEmail(uid=uid, raw=self.messages[uid])
            for uid in self.messages
            if uid not in excluded
        ]
        return unseen[:limit]

    def mark_seen(self, uid: str) -> None:
        self.seen.add(uid)

    def mark_seen_many(self, uids: list[str] | tuple[str, ...]) -> None:
        self.seen.update(uids)


class FakeRouter:
    def __init__(self):
        self.calls: list[RouteContext] = []
        self.fail_next = False

    def route(self, context: RouteContext) -> RouteResult:
        self.calls.append(context)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("LLM 临时不可用")
        if "通知" in context.subject:
            return RouteResult(
                category=EmailCategory.NOTIFY,
                reason="测试通知",
                source=RouteSource.LLM,
                confidence=0.92,
            )
        return RouteResult(
            category=EmailCategory.RESPOND,
            reason="测试明确提问",
            source=RouteSource.LLM,
            confidence=0.96,
        )


class FakeDrafter:
    def __init__(self):
        self.calls = []

    def generate(self, context, *, feedback=None, previous_draft=None):
        self.calls.append({"subject": context.subject, "feedback": feedback})
        return f"针对《{context.subject}》的安全回复草稿"


def _raw_email(
    subject: str,
    body: str,
    *,
    message_id: str,
    auto_submitted: bool = False,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "user@qq.com"
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    if auto_submitted:
        msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(body, charset="utf-8")
    return msg.as_bytes()


@pytest.fixture
def runtime(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(tmp_path / "checkpoint.db", check_same_thread=False)
    router = FakeRouter()
    drafter = FakeDrafter()
    graph = build_graph(
        factory,
        llm_route=router.route,
        drafter=drafter,
        checkpointer=SqliteSaver(conn),
    )
    alerts: list[str] = []
    yield SimpleNamespace(
        factory=factory,
        graph=graph,
        router=router,
        drafter=drafter,
        alerts=alerts,
        conn=conn,
        engine=engine,
    )
    conn.close()
    engine.dispose()


def _make_scheduler(runtime, mailbox, *, limit=10):
    return EmailScheduler(
        mailbox=mailbox,
        session_factory=runtime.factory,
        graph=runtime.graph,
        fetch_limit=limit,
        max_body_chars=8000,
        alerts=runtime.alerts,
    )


def test_unread_queue_keeps_current_batch_until_marked_read(runtime):
    messages = [
        _raw_email(
            f"历史邮件 {index}",
            f"这是第 {index} 封历史未读邮件。",
            message_id=f"<history-{index}@example.com>",
        )
        for index in range(23)
    ]
    mailbox = FakeMailbox(messages)
    scheduler = _make_scheduler(runtime, mailbox)

    first_result = scheduler.run_once()

    assert first_result.batch_sizes == (10,)
    assert first_result.stored_count == 10
    assert first_result.graph_count == 10
    assert first_result.duplicate_count == 0
    assert first_result.errors == ()
    assert mailbox.seen == set()
    with runtime.factory() as db:
        records = list_emails(db)
        assert len(records) == 10
        assert all(record.status != EmailStatus.NEW.value for record in records)
        assert all(record.read_at is None for record in records)
        assert {record.imap_uid for record in records} == {
            str(index) for index in range(1, 11)
        }

    repeated_result = scheduler.run_once()

    assert repeated_result.batch_sizes == (10,)
    assert repeated_result.stored_count == 0
    assert repeated_result.graph_count == 0
    assert repeated_result.duplicate_count == 10
    assert repeated_result.errors == ()

    mailbox.mark_seen_many([str(index) for index in range(1, 11)])
    after_first_batch = scheduler.run_once()

    assert after_first_batch.batch_sizes == (10,)
    assert after_first_batch.stored_count == 10
    assert after_first_batch.duplicate_count == 0

    mailbox.mark_seen_many([str(index) for index in range(11, 21)])
    final_batch = scheduler.run_once()

    assert final_batch.batch_sizes == (3,)
    assert final_batch.stored_count == 3
    assert final_batch.duplicate_count == 0
    with runtime.factory() as db:
        assert len(list_emails(db)) == 23

def test_rule_ignored_email_skips_llm(runtime):
    mailbox = FakeMailbox(
        [
            _raw_email(
                "系统通知",
                "系统自动邮件，请勿回复。",
                message_id="<scheduler-rule@example.com>",
                auto_submitted=True,
            )
        ]
    )
    scheduler = _make_scheduler(runtime, mailbox)

    result = scheduler.run_once()

    assert result.errors == ()
    assert runtime.router.calls == []
    with runtime.factory() as db:
        record = list_emails(db)[0]
        assert record.status == EmailStatus.IGNORED.value
        assert record.route_source == "rule"


def test_duplicate_message_marks_both_uids_seen_without_second_graph(runtime):
    raw = _raw_email(
        "重复邮件",
        "同一封邮件在邮箱里出现两次。",
        message_id="<scheduler-duplicate@example.com>",
    )
    mailbox = FakeMailbox([raw, raw])
    scheduler = _make_scheduler(runtime, mailbox)

    result = scheduler.run_once()

    assert result.batch_sizes == (2,)
    assert result.stored_count == 1
    assert result.duplicate_count == 1
    assert result.graph_count == 1
    assert mailbox.seen == {"2"}
    with runtime.factory() as db:
        records = list_emails(db)
        assert len(records) == 1
        assert records[0].imap_uid == "1"
        assert records[0].read_at is None


def test_existing_new_email_is_recovered_without_imap_fetch(runtime):
    with runtime.factory() as db:
        email = create_email(
            db,
            message_id="<scheduler-recovery@example.com>",
            sender="alice@example.com",
            recipient="user@qq.com",
            subject="恢复通知",
            body_text="服务重启前已入库，但 graph 未执行。",
            headers={},
        )
        db.commit()
    mailbox = FakeMailbox([])
    scheduler = _make_scheduler(runtime, mailbox)

    result = scheduler.run_once()

    assert result.recovered_count == 1
    assert result.stored_count == 0
    assert mailbox.fetch_count == 1
    with runtime.factory() as db:
        assert list_emails(db)[0].status == EmailStatus.NOTIFIED.value


def test_failed_new_email_is_retried_next_round_and_alerts(runtime):
    with runtime.factory() as db:
        email = create_email(
            db,
            message_id="<scheduler-retry@example.com>",
            sender="alice@example.com",
            recipient="user@qq.com",
            subject="重试通知",
            body_text="第一轮 LLM 暂时失败。",
            headers={},
        )
        db.commit()
    runtime.router.fail_next = True
    scheduler = _make_scheduler(runtime, FakeMailbox([]))

    failed = scheduler.run_once()
    recovered = scheduler.run_once()

    assert failed.recovered_count == 0
    assert failed.errors
    assert runtime.alerts
    assert recovered.recovered_count == 1
    with runtime.factory() as db:
        assert list_emails(db)[0].status == EmailStatus.NOTIFIED.value


def test_imap_error_does_not_kill_runner(runtime):
    mailbox = FakeMailbox([])
    mailbox.errors.append(RuntimeError("QQ IMAP 连接中断"))
    scheduler = _make_scheduler(runtime, mailbox)
    runner = SchedulerRunner(scheduler, interval_seconds=1)

    runner.start()
    deadline = time.monotonic() + 4
    while mailbox.fetch_count < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    alive_after_error = runner.running
    runner.stop()

    assert mailbox.fetch_count >= 2
    assert alive_after_error
    assert not runner.running
    assert runtime.alerts


def test_same_scheduler_instance_is_not_reentrant(runtime):
    entered = threading.Event()
    release = threading.Event()

    class BlockingMailbox(FakeMailbox):
        def fetch_unread(self, limit: int, exclude_uids: set[str] | None = None):
            self.fetch_count += 1
            entered.set()
            release.wait(timeout=3)
            return []

    mailbox = BlockingMailbox([])
    scheduler = _make_scheduler(runtime, mailbox)
    worker = threading.Thread(target=scheduler.run_once)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        skipped = scheduler.run_once()
    finally:
        release.set()
        worker.join(timeout=3)

    assert skipped.skipped is True
    assert mailbox.fetch_count == 1
