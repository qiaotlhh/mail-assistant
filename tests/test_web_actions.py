"""M6 验收：审核页渲染与四类人工动作。"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import get_settings
from app.agent.graph import build_graph, run_for_email
from app.agent.router import RouteContext, RouteResult
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailCategory, EmailStatus, RouteSource
from app.store.repo import create_email, get_email, mark_email_read, route_email
from app.web.routes import ReviewService, create_review_router


_counter = itertools.count(1)


class FakeDrafter:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = []

    def generate(self, context, *, feedback=None, previous_draft=None):
        self.calls.append({"feedback": feedback, "previous_draft": previous_draft})
        return self.drafts.pop(0)


class FakeSMTP:
    def __init__(self, error: Exception | None = None):
        self.calls = []
        self.error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class FakeMarkSeen:
    def __init__(self, error: Exception | None = None):
        self.calls = []
        self.error = error

    def __call__(self, uids):
        self.calls.append(tuple(uids))
        if self.error is not None:
            raise self.error


def _make_review_app(tmp_path, *, drafts, smtp, mark_seen=None):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(tmp_path / "checkpoint.db", check_same_thread=False)
    drafter = FakeDrafter(drafts)

    def llm(context):
        return RouteResult(
            category=EmailCategory.RESPOND,
            reason="测试明确提问",
            source=RouteSource.LLM,
            confidence=0.97,
        )

    graph = build_graph(
        factory,
        llm_route=llm,
        drafter=drafter,
        checkpointer=SqliteSaver(conn),
    )
    with factory() as db:
        email = create_email(
            db,
            message_id=f"<web-{next(_counter)}@qq.com>",
            sender="alice@example.com",
            recipient="user@qq.com",
            subject="请问方案时间",
            body_text="<script>alert(1)</script>请问方案什么时候能给到我？",
            headers={},
            received_at=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc),
        )
        db.commit()
    run_for_email(graph, email.id)

    service = ReviewService(factory, graph, sender=smtp, mark_seen=mark_seen)
    app = FastAPI()
    alerts = ["测试告警：IMAP 连接失败"]
    app.include_router(create_review_router(service, alerts=alerts))
    return app, factory, graph, email, conn, engine, drafter


def test_review_pages_render_original_and_draft(tmp_path):
    app, _, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["您好，方案预计周五提供。"], smtp=FakeSMTP()
    )
    with TestClient(app) as client:
        list_response = client.get("/")
        detail_response = client.get(f"/reviews/{email.id}")
    assert list_response.status_code == 200
    assert "请问方案时间" in list_response.text
    assert "<th>操作</th>" in list_response.text
    assert ">需回复</td>" in list_response.text
    assert ">待审核</span></td>" in list_response.text
    assert ">待审核</a>" in list_response.text
    assert ">审核</a>" in list_response.text
    assert detail_response.status_code == 200
    assert "&lt;script&gt;" in detail_response.text
    assert "您好，方案预计周五提供。" in detail_response.text
    assert "<script>alert(1)</script>" not in detail_response.text
    conn.close()
    engine.dispose()


def test_alerts_can_be_manually_cleared(tmp_path):
    app, _, _, _, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["您好，方案预计周五提供。"], smtp=FakeSMTP()
    )
    with TestClient(app) as client:
        before = client.get("/")
        clear = client.post("/alerts/clear", follow_redirects=False)
        after = client.get("/")
    assert before.status_code == 200
    assert "测试告警：IMAP 连接失败" in before.text
    assert "清除告警" in before.text
    assert clear.status_code == 303
    assert clear.headers["location"] == f"/?message={quote_plus("已清除告警")}"
    assert after.status_code == 200
    assert "测试告警：IMAP 连接失败" not in after.text
    assert "清除告警" not in after.text
    conn.close()
    engine.dispose()

def test_approve_uses_fake_sender_and_persists_sent(tmp_path):
    smtp = FakeSMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["您好，方案预计周五提供。"], smtp=smtp
    )
    with TestClient(app) as client:
        response = client.post(
            f"/reviews/{email.id}/actions", data={"action": "approve"}, follow_redirects=False
        )
        duplicate = client.post(f"/reviews/{email.id}/actions", data={"action": "approve"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?status=sent_done&message=")
    assert duplicate.status_code == 409
    assert len(smtp.calls) == 1
    assert smtp.calls[0]["recipient"] == "alice@example.com"
    assert smtp.calls[0]["body"] == "您好，方案预计周五提供。"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "approved"
        assert record.read_at is not None
    conn.close()
    engine.dispose()


def test_sent_email_stays_in_sent_and_history_after_auto_read(tmp_path):
    mark_seen = FakeMarkSeen()
    smtp = FakeSMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["已回复内容"], smtp=smtp, mark_seen=mark_seen
    )
    with factory() as db:
        get_email(db, email.id).imap_uid = "201"
        db.commit()

    with TestClient(app) as client:
        queue_before = client.get("/")
        approved = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "approve"},
            follow_redirects=False,
        )
        queue_after = client.get("/")
        sent = client.get("/?status=sent_done")
        history = client.get("/?view=history")

    assert ">待处理</a>" in queue_before.text
    assert 'href="/?status=new"' not in queue_before.text
    assert approved.status_code == 303
    assert approved.headers["location"].startswith("/?status=sent_done&message=")
    assert "请问方案时间" not in queue_after.text
    assert "请问方案时间" in sent.text
    assert "请问方案时间" in history.text
    assert f'href="/reviews/{email.id}?from=history&page=1"' in history.text
    assert mark_seen.calls == [("201",)]
    with factory() as db:
        assert get_email(db, email.id).read_at is not None
    conn.close()
    engine.dispose()


def test_auto_read_failure_keeps_sent_email_visible_and_alerts(tmp_path):
    mark_seen = FakeMarkSeen(error=RuntimeError("IMAP unavailable"))
    smtp = FakeSMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["已经发出的回复"], smtp=smtp, mark_seen=mark_seen
    )
    with factory() as db:
        get_email(db, email.id).imap_uid = "202"
        db.commit()

    with TestClient(app) as client:
        approved = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "approve"},
            follow_redirects=False,
        )
        sent = client.get("/?status=sent_done")

    assert approved.status_code == 303
    assert approved.headers["location"].startswith("/?status=sent_done&error=")
    assert "邮件已发送，但自动标记邮箱已读失败" in sent.text
    assert "请问方案时间" in sent.text
    assert len(smtp.calls) == 1
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.read_at is None
    conn.close()
    engine.dispose()


def test_read_actions_are_scoped_by_category(tmp_path):
    mark_seen = FakeMarkSeen()
    smtp = FakeSMTP()
    app, factory, graph, pending, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["待审核草稿"], smtp=smtp, mark_seen=mark_seen
    )
    with factory() as db:
        notified = create_email(
            db,
            message_id=f"<web-notify-read-{next(_counter)}@qq.com>",
            sender="notice@example.com",
            recipient="user@qq.com",
            subject="系统通知",
            body_text="请知悉本周安排。",
            headers={},
            imap_uid="101",
        )
        route_email(db, notified.id, "notify", "测试通知", "llm")
        ignored = create_email(
            db,
            message_id=f"<web-ignore-read-{next(_counter)}@qq.com>",
            sender="news@example.com",
            recipient="user@qq.com",
            subject="技术周刊",
            body_text="本期无待办。",
            headers={},
            imap_uid="102",
        )
        route_email(db, ignored.id, "ignore", "测试忽略", "rule")
        db.commit()

    with TestClient(app) as client:
        notify_detail = client.get(f"/reviews/{notified.id}")
        ignored_list = client.get("/?status=ignored")
        ignored_detail = client.get(f"/reviews/{ignored.id}?from=ignored")
        invalid_detail = client.get(f"/reviews/{ignored.id}?from=unknown")
        mark_one = client.post(f"/reviews/{notified.id}/read", follow_redirects=False)
        mark_batch = client.post(
            "/reviews/mark-read/batch", follow_redirects=False
        )
        notified_queue_after = client.get("/?status=notified")
        ignored_queue_after = client.get("/?status=ignored")
        all_history = client.get("/")
        history_page = client.get("/?view=history")
        notified_history_detail = client.get(
            f"/reviews/{notified.id}?from=history"
        )

    assert notify_detail.status_code == 200
    assert "下一封待审核" not in notify_detail.text
    assert "收取并处理下一封" not in notify_detail.text
    assert "标记已读" in notify_detail.text
    assert ignored_list.status_code == 200
    assert f'href="/reviews/{ignored.id}?from=ignored"' in ignored_list.text
    assert ignored_detail.status_code == 200
    assert 'href="/?status=ignored"' in ignored_detail.text
    assert "返回列表" in ignored_detail.text
    assert '<a href="/">审核列表</a>' in ignored_detail.text
    assert invalid_detail.status_code == 400
    assert "批量已读（1 封）" in ignored_list.text
    assert mark_one.status_code == 303
    assert mark_one.headers["location"].startswith("/?status=notified&message=")
    assert mark_batch.status_code == 303
    assert mark_batch.headers["location"].startswith("/?status=ignored&message=")
    assert notified_queue_after.status_code == 200
    assert "系统通知" not in notified_queue_after.text
    assert ignored_queue_after.status_code == 200
    assert "技术周刊" not in ignored_queue_after.text
    assert all_history.status_code == 200
    assert "系统通知" not in all_history.text
    assert "技术周刊" not in all_history.text
    assert history_page.status_code == 200
    assert "系统通知" in history_page.text
    assert "技术周刊" in history_page.text
    assert f'>查看</a>' in history_page.text
    assert notified_history_detail.status_code == 200
    assert "历史记录仅支持查看，不再修改分类。" in notified_history_detail.text
    assert "改为通知" not in notified_history_detail.text
    assert mark_seen.calls == [("101",), ("102",)]
    with factory() as db:
        assert get_email(db, notified.id).read_at is not None
        assert get_email(db, ignored.id).read_at is not None
        assert get_email(db, pending.id).read_at is None
    conn.close()
    engine.dispose()


def test_history_is_paginated(tmp_path):
    smtp = FakeSMTP()
    app, factory, _, pending, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["待审核草稿"], smtp=smtp
    )
    page_two_detail_id = 0
    with factory() as db:
        for index in range(24):
            record = create_email(
                db,
                message_id=f"<web-history-{next(_counter)}@qq.com>",
                sender="history@example.com",
                recipient="user@qq.com",
                subject=f"历史{index:02d}",
                body_text="这是已处理历史邮件。",
                headers={},
                received_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
                + timedelta(days=index),
            )
            mark_email_read(db, record.id)
            if index == 13:
                page_two_detail_id = record.id
        db.commit()

    with TestClient(app) as client:
        first = client.get("/?view=history")
        second = client.get("/?view=history&page=2")
        invalid_zero = client.get("/?view=history&page=0")
        invalid_range = client.get("/?view=history&page=4")
        detail = client.get(f"/reviews/{page_two_detail_id}?from=history&page=2")

    assert first.status_code == 200
    assert "历史23" in first.text
    assert "历史14" in first.text
    assert "历史13" not in first.text
    assert "第 1 / 3 页 · 共 24 条" in first.text
    assert 'href="/?view=history&page=2"' in first.text
    assert second.status_code == 200
    assert "历史13" in second.text
    assert "历史04" in second.text
    assert "历史14" not in second.text
    assert "第 2 / 3 页 · 共 24 条" in second.text
    assert 'href="/?view=history&page=1"' in second.text
    assert invalid_zero.status_code == 400
    assert invalid_range.status_code == 400
    assert detail.status_code == 200
    assert 'href="/?view=history&amp;page=2"' in detail.text
    conn.close()
    engine.dispose()


def test_review_queue_continues_to_next_pending_email(tmp_path):
    smtp = FakeSMTP()
    app, factory, graph, first, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["第一封草稿", "第二封草稿"], smtp=smtp
    )
    with factory() as db:
        second = create_email(
            db,
            message_id=f"<web-next-{next(_counter)}@qq.com>",
            sender="bob@example.com",
            recipient="user@qq.com",
            subject="请确认会议地点",
            body_text="请问明天在北京哪里见面？",
            headers={},
            received_at=datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc),
        )
        db.commit()
    run_for_email(graph, second.id)

    with TestClient(app) as client:
        detail = client.get(f"/reviews/{first.id}")
        approved = client.post(
            f"/reviews/{first.id}/actions",
            data={"action": "approve", "continue_review": "1"},
            follow_redirects=False,
        )
        next_detail = client.get(approved.headers["location"])

    assert detail.status_code == 200
    assert "下一封待审核" not in detail.text
    assert "收取并处理下一封" not in detail.text
    assert "detail-header" in detail.text
    assert approved.status_code == 303
    assert approved.headers["location"] == (
        f"/reviews/{second.id}?message={quote_plus('已批准并发送')}"
    )
    assert next_detail.status_code == 200
    assert "请确认会议地点" in next_detail.text
    assert len(smtp.calls) == 1
    conn.close()
    engine.dispose()


def test_edit_send_stores_user_draft_before_resume(tmp_path):
    smtp = FakeSMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["初稿"], smtp=smtp
    )
    with TestClient(app) as client:
        response = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "edit_send", "draft": "人工修改后的正式回复"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert smtp.calls[0]["body"] == "人工修改后的正式回复"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "edited"
        assert [draft.source for draft in record.drafts] == ["llm", "user_edit"]
    conn.close()
    engine.dispose()


def test_rewrite_and_ignore_actions_round_trip(tmp_path):
    smtp = FakeSMTP()
    app, factory, _, email, conn, engine, drafter = _make_review_app(
        tmp_path, drafts=["初稿", "更正式稿"], smtp=smtp
    )
    with TestClient(app) as client:
        rewrite = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "rewrite", "feedback": "请更正式"},
            follow_redirects=False,
        )
        ignore = client.post(
            f"/reviews/{email.id}/actions", data={"action": "ignore"}, follow_redirects=False
        )
        ignored_list = client.get("/")
        ignored_detail = client.get(f"/reviews/{email.id}")
    assert rewrite.status_code == 303
    assert ">已忽略</span></td>" in ignored_list.text
    assert ">查看</a>" in ignored_list.text
    assert "升级为待回复" in ignored_detail.text
    assert ">已忽略</span>" in ignored_detail.text
    assert ignore.status_code == 303
    assert not smtp.calls
    assert drafter.calls[1]["feedback"] == "请更正式"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.IGNORED.value
        assert record.rewrite_rounds == 1
        assert [draft.content for draft in record.drafts] == ["初稿", "更正式稿"]
    conn.close()
    engine.dispose()


def test_invalid_action_and_sender_failure_do_not_transition(tmp_path):
    smtp = FakeSMTP(error=RuntimeError("SMTP 未就绪"))
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["初稿"], smtp=smtp
    )
    with TestClient(app) as client:
        invalid = client.post(f"/reviews/{email.id}/actions", data={"action": "delete"})
        failed = client.post(f"/reviews/{email.id}/actions", data={"action": "approve"})
        detail = client.get(f"/reviews/{email.id}")
    assert invalid.status_code == 400
    assert failed.status_code == 400
    assert len(smtp.calls) == 1
    assert "人工重试发送" in detail.text
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SEND_FAILED.value
        assert record.sent_kind == "approved"
    conn.close()
    engine.dispose()


def test_retry_send_after_failure_succeeds_once(tmp_path):
    class FlakySMTP:
        def __init__(self):
            self.calls = []
            self.fail_first = True

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("QQ SMTP 暂时不可用")

    smtp = FlakySMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["这是待重试的回复"], smtp=smtp
    )
    with TestClient(app) as client:
        failed = client.post(f"/reviews/{email.id}/actions", data={"action": "approve"})
        retry = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "retry_send"},
            follow_redirects=False,
        )
        duplicate = client.post(f"/reviews/{email.id}/actions", data={"action": "retry_send"})
    assert failed.status_code == 400
    assert retry.status_code == 303
    assert duplicate.status_code == 409
    assert len(smtp.calls) == 2
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "approved"
        actions = [log.action for log in record.audit_logs]
        assert "send_failed" in actions
        assert "retry_send" in actions
    conn.close()
    engine.dispose()


def test_duplicate_click_sends_only_once(tmp_path):
    class SlowSMTP:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            time.sleep(0.15)
            self.calls.append(kwargs)

    smtp = SlowSMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["只发送一次"], smtp=smtp
    )
    results = []
    with TestClient(app) as client:
        def submit():
            response = client.post(
                f"/reviews/{email.id}/actions",
                data={"action": "approve"},
                follow_redirects=False,
            )
            results.append(response.status_code)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert sorted(results) == [303, 409]
    assert len(smtp.calls) == 1
    with factory() as db:
        assert get_email(db, email.id).status == EmailStatus.SENT_DONE.value
    conn.close()
    engine.dispose()


def test_edit_send_failure_keeps_user_draft_for_retry(tmp_path):
    class FlakySMTP:
        def __init__(self):
            self.calls = []
            self.failed = False

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if not self.failed:
                self.failed = True
                raise RuntimeError("连接中断")

    smtp = FlakySMTP()
    app, factory, _, email, conn, engine, _ = _make_review_app(
        tmp_path, drafts=["原始草稿"], smtp=smtp
    )
    with TestClient(app) as client:
        failed = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "edit_send", "draft": "失败后仍要保留的修改稿"},
        )
        retry = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "retry_send"},
            follow_redirects=False,
        )
    assert failed.status_code == 400
    assert retry.status_code == 303
    assert smtp.calls[-1]["body"] == "失败后仍要保留的修改稿"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "edited"
        assert record.drafts[-1].content == "失败后仍要保留的修改稿"
        assert record.drafts[-1].source == "user_edit"
        assert len(record.drafts) == 2
    conn.close()
    engine.dispose()


def test_main_app_serves_local_review_page(monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_ADDRESS", "user@qq.com")
    monkeypatch.setenv("MAIL_AUTH_CODE", "test-auth-code")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "deepseek")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "main.db"))
    get_settings.cache_clear()

    from app.main import app as main_app

    class NoNetworkMailbox:
        def fetch_unread(self, limit: int, exclude_uids: set[str] | None = None):
            return []

        def mark_seen(self, uid: str):
            return None

        def mark_seen_many(self, uids):
            return None

    monkeypatch.setattr(
        "app.main.IMAPClient", lambda settings: NoNetworkMailbox()
    )

    with TestClient(main_app) as client:
        health = client.get("/api/health")
        review_home = client.get("/")
        poll = client.post("/actions/poll", follow_redirects=False)
    settings = get_settings()
    assert settings.host == "127.0.0.1"
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert review_home.status_code == 200
    assert poll.status_code == 303
    assert "message=" in poll.headers["location"]
    assert poll.headers["location"].startswith("/")
    assert "邮件智能助手" in review_home.text
    get_settings.cache_clear()
