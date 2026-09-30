"""P1 验收：notify 邮件人工升级为 respond 并生成草稿。"""

from __future__ import annotations

import itertools
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.drafter import DraftError
from app.agent.graph import build_graph, run_for_email, upgrade_for_email
from app.agent.router import RouteContext, RouteResult
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailCategory, EmailStatus, RouteSource
from app.store.repo import create_email, get_email
from app.web.routes import ReviewService, create_review_router


_counter = itertools.count(1)


class FakeDrafter:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = []

    def generate(self, context, *, feedback=None, previous_draft=None):
        self.calls.append({"feedback": feedback, "previous_draft": previous_draft})
        if not self.drafts:
            raise DraftError("草稿 provider 全部失败")
        return self.drafts.pop(0)


class FakeSMTP:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


def _notify_result(context: RouteContext) -> RouteResult:
    return RouteResult(
        category=EmailCategory.NOTIFY,
        reason="测试通知类邮件",
        source=RouteSource.LLM,
        confidence=0.91,
    )


def _make_env(tmp_path, *, drafts, category=EmailCategory.NOTIFY):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(tmp_path / "checkpoint.db", check_same_thread=False)
    drafter = FakeDrafter(drafts)
    graph = build_graph(
        factory,
        llm_route=lambda context: RouteResult(
            category=category,
            reason="测试分类邮件",
            source=RouteSource.LLM,
            confidence=0.91,
        ),
        drafter=drafter,
        checkpointer=SqliteSaver(conn),
    )
    with factory() as db:
        email = create_email(
            db,
            message_id=f"<upgrade-{next(_counter)}@example.com>",
            sender="hr@example.com",
            recipient="user@qq.com",
            subject="项目人员安排通知",
            body_text="请知悉下周项目值班安排。",
            headers={},
        )
        db.commit()
    run_for_email(graph, email.id)
    return factory, graph, email, drafter, conn, engine


def test_upgrade_notified_email_generates_draft_and_interrupts(tmp_path):
    factory, graph, email, drafter, conn, engine = _make_env(
        tmp_path, drafts=["您好，请确认下周值班安排。"]
    )

    result = upgrade_for_email(graph, email.id)

    assert result["__interrupt__"]
    assert drafter.calls[0]["feedback"] is None
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.category == "respond"
        assert record.drafts[0].content == "您好，请确认下周值班安排。"
        assert "upgrade" in [log.action for log in record.audit_logs]
    conn.close()
    engine.dispose()


def test_upgrade_ignored_email_generates_draft_and_interrupts(tmp_path):
    factory, graph, email, drafter, conn, engine = _make_env(
        tmp_path, drafts=["您好，这封误判邮件需要回复。"], category=EmailCategory.IGNORE
    )

    result = upgrade_for_email(graph, email.id)

    assert result["__interrupt__"]
    assert drafter.calls[0]["feedback"] is None
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.category == "respond"
        assert record.drafts[0].content == "您好，这封误判邮件需要回复。"
    conn.close()
    engine.dispose()


def test_upgrade_failure_keeps_ignored_state(tmp_path):
    factory, graph, email, _, conn, engine = _make_env(
        tmp_path, drafts=[], category=EmailCategory.IGNORE
    )

    result = upgrade_for_email(graph, email.id)

    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.IGNORED.value
        assert record.category == "ignore"
        assert record.drafts == []
        assert "upgrade" not in [log.action for log in record.audit_logs]
    conn.close()
    engine.dispose()


def test_upgrade_failure_keeps_notified_state(tmp_path):
    factory, graph, email, _, conn, engine = _make_env(tmp_path, drafts=[])

    result = upgrade_for_email(graph, email.id)

    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.NOTIFIED.value
        assert record.category == "notify"
        assert record.drafts == []
        assert "upgrade" not in [log.action for log in record.audit_logs]
    conn.close()
    engine.dispose()


def test_web_upgrade_action_does_not_send_email(tmp_path):
    factory, graph, email, _, conn, engine = _make_env(
        tmp_path, drafts=["这是升级后的回复草稿"]
    )
    smtp = FakeSMTP()
    service = ReviewService(factory, graph, sender=smtp)
    app = FastAPI()
    app.include_router(create_review_router(service))

    with TestClient(app) as client:
        detail = client.get(f"/reviews/{email.id}")
        review_list = client.get("/")
        response = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "upgrade"},
            follow_redirects=False,
        )
        duplicate = client.post(f"/reviews/{email.id}/actions", data={"action": "upgrade"})

    assert detail.status_code == 200
    assert "升级为待回复" in detail.text
    assert '<table class="review-table">' in review_list.text
    assert ">处理</a>" in review_list.text
    assert response.status_code == 303
    assert duplicate.status_code == 409
    assert smtp.calls == []
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.drafts[-1].content == "这是升级后的回复草稿"
    conn.close()
    engine.dispose()


def test_manual_reclassify_covers_all_category_pairs(tmp_path):
    factory, graph, email, _, conn, engine = _make_env(
        tmp_path,
        drafts=["初始草稿", "升级后的回复草稿"],
        category=EmailCategory.RESPOND,
    )
    smtp = FakeSMTP()
    service = ReviewService(factory, graph, sender=smtp)
    app = FastAPI()
    app.include_router(create_review_router(service))

    with TestClient(app) as client:
        respond_detail = client.get(f"/reviews/{email.id}")
        assert "改为忽略" in respond_detail.text
        assert "改为通知" in respond_detail.text
        assert "升级为待回复" not in respond_detail.text

        to_notify = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "reclassify", "target_category": "notify"},
            follow_redirects=False,
        )
        notified_detail = client.get(f"/reviews/{email.id}")
        assert "改为忽略" in notified_detail.text
        assert "升级为待回复" in notified_detail.text
        assert "改为通知" not in notified_detail.text

        to_ignore = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "reclassify", "target_category": "ignore"},
            follow_redirects=False,
        )
        ignored_detail = client.get(f"/reviews/{email.id}")
        assert "改为通知" in ignored_detail.text
        assert "升级为待回复" in ignored_detail.text
        assert "改为忽略" not in ignored_detail.text

        to_respond = client.post(
            f"/reviews/{email.id}/actions",
            data={"action": "upgrade"},
            follow_redirects=False,
        )

    assert to_notify.status_code == 303
    assert to_ignore.status_code == 303
    assert to_respond.status_code == 303
    assert smtp.calls == []
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.category == "respond"
        assert [draft.content for draft in record.drafts] == [
            "初始草稿",
            "升级后的回复草稿",
        ]
        actions = [log.action for log in record.audit_logs]
        assert actions.count("reclassify") == 2
        assert "upgrade" in actions
    conn.close()
    engine.dispose()

def test_upgraded_email_recovers_after_restart(tmp_path):
    factory, graph, email, _, conn, engine = _make_env(
        tmp_path, drafts=["重启前生成的升级草稿"]
    )
    first_result = upgrade_for_email(graph, email.id)
    assert first_result["__interrupt__"]
    conn.close()

    recovered_conn = sqlite3.connect(
        tmp_path / "checkpoint.db", check_same_thread=False
    )
    recovered_graph = build_graph(
        factory,
        llm_route=lambda context: RouteResult(
            category=category,
            reason="测试分类邮件",
            source=RouteSource.LLM,
            confidence=0.91,
        ),
        drafter=FakeDrafter(["重启后不应重新生成"]),
        checkpointer=SqliteSaver(recovered_conn),
    )
    from app.agent.graph import resume_for_email

    resumed = resume_for_email(recovered_graph, email.id, {"action": "ignore"})
    assert not resumed.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.IGNORED.value
        assert [draft.content for draft in record.drafts] == ["重启前生成的升级草稿"]
    recovered_conn.close()
    engine.dispose()
