"""M5 验收：respond 中断等待人工、ignore/notify 落库、重写循环与重启恢复。"""

from __future__ import annotations

import itertools
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.drafter import DraftError
from sqlalchemy import text

from app.agent.graph import (
    GraphError,
    build_graph,
    resume_for_email,
    run_for_email,
    upgrade_for_email,
)
from app.agent.router import RouteResult
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailCategory, EmailStatus, RouteSource
from app.store.repo import (
    RewriteLimitExceeded,
    create_email,
    get_email,
    route_email,
)


_counter = itertools.count(1)


def _create(db, **overrides):
    params = dict(
        message_id=f"<graph-{next(_counter)}@qq.com>",
        sender="alice@qq.com",
        recipient="user@qq.com",
        subject="咨询",
        body_text="请问方案什么时候能给到我？",
        headers={},
    )
    params.update(overrides)
    email = create_email(db, **params)
    db.commit()
    return email


def _llm(category="respond", confidence=0.95):
    def fn(context):
        return RouteResult(
            EmailCategory(category), "测试分类", RouteSource.LLM, confidence
        )

    return fn


def _llm_must_not_be_called(context):
    raise AssertionError("规则命中时不应调用 LLM")


class FakeDrafter:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = []

    def generate(self, context, *, feedback=None, previous_draft=None):
        self.calls.append({"feedback": feedback, "previous_draft": previous_draft})
        return self.drafts.pop(0)


class RaisingDrafter:
    def generate(self, context, *, feedback=None, previous_draft=None):
        raise DraftError("全部 provider 失败")


@pytest.fixture
def env(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(tmp_path / "checkpoint.db", check_same_thread=False)
    saver = SqliteSaver(conn)
    yield factory, saver
    conn.close()
    engine.dispose()


def _graph(factory, saver, *, llm=None, drafter=None):
    return build_graph(
        factory,
        llm_route=llm or _llm(),
        drafter=drafter or FakeDrafter(["您好，这是回复草稿。"]),
        checkpointer=saver,
    )


def _latest_draft(record):
    return record.drafts[-1]


def test_ignore_email_skips_llm_and_draft(env):
    factory, saver = env
    with factory() as db:
        email = _create(db, headers={"auto-submitted": "auto"})
    graph = _graph(factory, saver, llm=_llm_must_not_be_called)
    result = run_for_email(graph, email.id)
    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.IGNORED.value
        assert record.category == "ignore"
        assert record.route_source == "rule"
        assert record.drafts == []


def test_notify_email_persisted(env):
    factory, saver = env
    with factory() as db:
        email = _create(db, subject="项目周报", body_text="本周进展顺利。")
    graph = _graph(factory, saver, llm=_llm("notify", 0.9))
    result = run_for_email(graph, email.id)
    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.NOTIFIED.value
        assert record.category == "notify"
        assert record.route_source == "llm"
        assert record.drafts == []


def test_respond_pauses_at_interrupt_then_approve(env):
    factory, saver = env
    drafter = FakeDrafter(["您好，方案预计周五提供。"])
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver, drafter=drafter)
    result = run_for_email(graph, email.id)
    interrupts = result["__interrupt__"]
    assert interrupts
    assert interrupts[0].value["draft"] == "您好，方案预计周五提供。"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.category == "respond"
        assert len(record.drafts) == 1
        assert record.drafts[0].round_number == 0

    resumed = resume_for_email(graph, email.id, {"action": "approve"})
    assert not resumed.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "approved"


def test_ignore_action_at_interrupt(env):
    factory, saver = env
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver)
    run_for_email(graph, email.id)
    resumed = resume_for_email(graph, email.id, {"action": "ignore"})
    assert not resumed.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.IGNORED.value


def test_edit_send_action_stores_user_draft(env):
    factory, saver = env
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver)
    run_for_email(graph, email.id)
    resume_for_email(graph, email.id, {"action": "edit_send", "draft": "人工修改后的草稿"})
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert record.sent_kind == "edited"
        assert len(record.drafts) == 2
        assert _latest_draft(record).content == "人工修改后的草稿"
        assert _latest_draft(record).source == "user_edit"


def test_rewrite_loop_regenerates_with_feedback(env):
    factory, saver = env
    drafter = FakeDrafter(["初稿", "重写稿"])
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver, drafter=drafter)
    run_for_email(graph, email.id)
    result = resume_for_email(
        graph, email.id, {"action": "rewrite", "feedback": "更正式一些"}
    )
    interrupts = result["__interrupt__"]
    assert interrupts[0].value["draft"] == "重写稿"
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.rewrite_rounds == 1
        assert [draft.round_number for draft in record.drafts] == [0, 1]
    assert drafter.calls[1]["feedback"] == "更正式一些"
    assert drafter.calls[1]["previous_draft"] == "初稿"

    resume_for_email(graph, email.id, {"action": "approve"})
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value


def test_rewrite_cap_enforced(env):
    factory, saver = env
    drafter = FakeDrafter(["d0", "d1", "d2", "d3"])
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver, drafter=drafter)
    run_for_email(graph, email.id)
    for round_number in range(1, 4):
        result = resume_for_email(
            graph,
            email.id,
            {"action": "rewrite", "feedback": f"第 {round_number} 轮反馈"},
        )
        assert result["__interrupt__"]
    with factory() as db:
        record = get_email(db, email.id)
        assert record.rewrite_rounds == 3
    with pytest.raises(RewriteLimitExceeded):
        resume_for_email(graph, email.id, {"action": "rewrite", "feedback": "第 4 轮"})
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.DRAFT_PENDING.value
        assert record.rewrite_rounds == 3


def test_invalid_action_rejected(env):
    factory, saver = env
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver)
    run_for_email(graph, email.id)
    with pytest.raises(GraphError, match="未知人工动作"):
        resume_for_email(graph, email.id, {"action": "delete"})


def test_draft_failure_falls_to_notify(env):
    factory, saver = env
    with factory() as db:
        email = _create(db)
    graph = _graph(factory, saver, drafter=RaisingDrafter())
    result = run_for_email(graph, email.id)
    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.NOTIFIED.value
        assert "草稿生成失败" in record.route_reason
        assert record.drafts == []


def test_only_new_emails_can_enter(env):
    factory, saver = env
    with factory() as db:
        email = _create(db)
        route_email(db, email.id, "ignore", "系统邮件", "rule")
        db.commit()
    graph = _graph(factory, saver)
    with pytest.raises(GraphError, match="只有 NEW 状态"):
        run_for_email(graph, email.id)


def test_restart_recovery_from_checkpoint(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    checkpoint_path = tmp_path / "checkpoint.db"

    conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
    first_graph = build_graph(
        factory,
        llm_route=_llm(),
        drafter=FakeDrafter(["重启前的草稿"]),
        checkpointer=SqliteSaver(conn),
    )
    with factory() as db:
        email = _create(db)
    result = run_for_email(first_graph, email.id)
    assert result["__interrupt__"]
    conn.close()

    recovered_conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
    recovered_graph = build_graph(
        factory,
        llm_route=_llm(),
        drafter=FakeDrafter(["不应再次生成"]),
        checkpointer=SqliteSaver(recovered_conn),
    )
    resumed = resume_for_email(recovered_graph, email.id, {"action": "approve"})
    assert not resumed.get("__interrupt__")
    with factory() as db:
        record = get_email(db, email.id)
        assert record.status == EmailStatus.SENT_DONE.value
        assert len(record.drafts) == 1
    recovered_conn.close()
    engine.dispose()


def test_automatic_intake_ignores_stale_upgrade_checkpoint(env):
    factory, saver = env
    with factory() as db:
        first = _create(db)
        route_email(db, first.id, "notify", "测试通知", "llm")
        db.commit()

    graph = _graph(factory, saver)
    assert upgrade_for_email(graph, first.id)["__interrupt__"]

    with factory() as db:
        engine = db.get_bind().engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM drafts"))
        conn.execute(text("DELETE FROM audit_logs"))
        conn.execute(text("DELETE FROM emails"))

    with factory() as db:
        reused = _create(db, headers={"auto-submitted": "auto"})
        assert reused.id == first.id

    result = run_for_email(graph, reused.id)
    assert not result.get("__interrupt__")
    with factory() as db:
        record = get_email(db, reused.id)
        assert record.status == EmailStatus.IGNORED.value
        assert record.category == "ignore"