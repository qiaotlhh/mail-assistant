"""M1 验收：状态机合法/非法转移、唯一约束、审计留痕与重启持久化。"""

from __future__ import annotations

import pytest

from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import AuditAction, EmailStatus
from app.store.repo import (
    EmailExistsError,
    EmailNotFoundError,
    InvalidTransitionError,
    RewriteLimitExceeded,
    add_draft,
    approve_send,
    create_email,
    edit_send,
    get_email,
    manual_ignore,
    mark_send_failed,
    reclassify_email,
    request_rewrite,
    retry_send,
    route_email,
    upgrade_to_draft,
)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        yield db
    engine.dispose()


def _new_email(db, **overrides):
    params = dict(
        message_id="<test-1@qq.com>",
        sender="alice@qq.com",
        recipient="user@qq.com",
        subject="测试邮件",
        body_text="请问方案什么时候能给到我？",
        headers={"Auto-Submitted": "no"},
    )
    params.update(overrides)
    return create_email(db, **params)


def _actions_of(db, email_id) -> list[str]:
    email = get_email(db, email_id)
    return [log.action for log in email.audit_logs]


def test_create_email_starts_as_new_and_audits(session):
    email = _new_email(session)
    assert email.status == EmailStatus.NEW.value
    assert email.category is None
    assert email.rewrite_rounds == 0
    assert _actions_of(session, email.id) == [AuditAction.CREATE.value]


def test_duplicate_message_id_rejected(session):
    _new_email(session, message_id="<dup@qq.com>")
    with pytest.raises(EmailExistsError, match="拒绝重复入库"):
        _new_email(session, message_id="<dup@qq.com>", sender="bob@qq.com")


@pytest.mark.parametrize(
    ("category", "expected_status"),
    [
        ("ignore", EmailStatus.IGNORED.value),
        ("notify", EmailStatus.NOTIFIED.value),
        ("respond", EmailStatus.DRAFT_PENDING.value),
    ],
)
def test_route_each_category(session, category, expected_status):
    email = _new_email(session, message_id=f"<{category}@qq.com>")
    routed = route_email(session, email.id, category, "测试原因", "rule")
    assert routed.status == expected_status
    assert routed.category == category
    assert routed.route_source == "rule"
    assert AuditAction.ROUTE.value in _actions_of(session, email.id)


def test_route_only_from_new(session):
    email = _new_email(session)
    route_email(session, email.id, "ignore", "系统邮件", "rule")
    with pytest.raises(InvalidTransitionError, match="非法状态转移"):
        route_email(session, email.id, "notify", "再次路由", "rule")


def test_invalid_route_category_and_source(session):
    email = _new_email(session)
    with pytest.raises(ValueError, match="未知分类"):
        route_email(session, email.id, "delete", "未知分类", "rule")
    with pytest.raises(ValueError, match="未知路由来源"):
        route_email(session, email.id, "notify", "未知来源", "human")


def test_upgrade_notified_to_draft(session):
    email = _new_email(session)
    route_email(session, email.id, "notify", "内容模糊", "llm")
    upgraded = upgrade_to_draft(session, email.id)
    assert upgraded.status == EmailStatus.DRAFT_PENDING.value
    assert upgraded.category == "respond"
    assert AuditAction.UPGRADE.value in _actions_of(session, email.id)


def test_upgrade_ignored_to_draft(session):
    email = _new_email(session)
    route_email(session, email.id, "ignore", "系统邮件", "rule")
    upgraded = upgrade_to_draft(session, email.id)
    assert upgraded.status == EmailStatus.DRAFT_PENDING.value
    assert upgraded.category == "respond"
    assert AuditAction.UPGRADE.value in _actions_of(session, email.id)


def test_reclassify_between_ignore_and_notify(session):
    email = _new_email(session)
    route_email(session, email.id, "ignore", "系统邮件", "rule")
    notified = reclassify_email(session, email.id, "notify")
    assert notified.status == EmailStatus.NOTIFIED.value
    assert notified.category == "notify"
    assert notified.route_source == "manual"
    ignored = reclassify_email(session, email.id, "ignore")
    assert ignored.status == EmailStatus.IGNORED.value
    assert ignored.category == "ignore"
    assert AuditAction.RECLASSIFY.value in _actions_of(session, email.id)


def test_illegal_reclassify_from_sent_done(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "明确提问", "llm")
    add_draft(session, email.id, content="草稿", round_number=0)
    approve_send(session, email.id)
    with pytest.raises(InvalidTransitionError, match="reclassify"):
        reclassify_email(session, email.id, "notify")

def test_illegal_upgrade_from_draft_pending(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "已有草稿", "llm")
    with pytest.raises(InvalidTransitionError, match="upgrade"):
        upgrade_to_draft(session, email.id)


def test_illegal_send_from_notified(session):
    email = _new_email(session)
    route_email(session, email.id, "notify", "未生成草稿", "llm")
    with pytest.raises(InvalidTransitionError, match="approve_send"):
        approve_send(session, email.id)


def test_approve_and_edit_send_flows(session):
    for kind, sender_fn in (("approved", approve_send), ("edited", edit_send)):
        email = _new_email(session, message_id=f"<{kind}@qq.com>")
        route_email(session, email.id, "respond", "明确提问", "llm")
        add_draft(
            session,
            email.id,
            content=f"{kind} 草稿",
            round_number=0,
        )
        sent = sender_fn(session, email.id)
        assert sent.status == EmailStatus.SENT_DONE.value
        assert sent.sent_kind == kind


def test_manual_ignore_from_draft_pending(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "明确提问", "llm")
    ignored = manual_ignore(session, email.id)
    assert ignored.status == EmailStatus.IGNORED.value
    assert AuditAction.MANUAL_IGNORE.value in _actions_of(session, email.id)


def test_rewrite_rounds_increment_and_cap(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "明确提问", "llm")
    add_draft(session, email.id, content="初稿", round_number=0)
    for round_number in range(1, 4):
        result = request_rewrite(
            session, email.id, feedback=f"第 {round_number} 轮反馈"
        )
        assert result.rewrite_rounds == round_number
        add_draft(
            session,
            email.id,
            content=f"重写稿 {round_number}",
            round_number=round_number,
            feedback=f"第 {round_number} 轮反馈",
        )
    assert len(get_email(session, email.id).drafts) == 4
    with pytest.raises(RewriteLimitExceeded, match="上限 3 轮"):
        request_rewrite(session, email.id, feedback="第 4 轮反馈")


def test_rewrite_only_from_draft_pending(session):
    email = _new_email(session)
    with pytest.raises(InvalidTransitionError, match="rewrite"):
        request_rewrite(session, email.id, feedback="尚未路由")


def test_send_failed_then_retry(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "明确提问", "llm")
    failed = mark_send_failed(session, email.id, "SMTPConnectionError: 连接超时")
    assert failed.status == EmailStatus.SEND_FAILED.value
    with pytest.raises(InvalidTransitionError, match="rewrite"):
        request_rewrite(session, email.id, feedback="发送失败后不能重写")
    sent = retry_send(session, email.id)
    assert sent.status == EmailStatus.SENT_DONE.value
    assert AuditAction.RETRY_SEND.value in _actions_of(session, email.id)


def test_retry_only_from_send_failed(session):
    email = _new_email(session)
    route_email(session, email.id, "respond", "明确提问", "llm")
    with pytest.raises(InvalidTransitionError, match="retry_send"):
        retry_send(session, email.id)


def test_draft_validation(session):
    email = _new_email(session)
    with pytest.raises(ValueError, match="草稿内容不能为空"):
        add_draft(session, email.id, content="   ", round_number=0)
    with pytest.raises(ValueError, match="草稿轮次不能为负数"):
        add_draft(session, email.id, content="草稿", round_number=-1)


def test_get_email_not_found(session):
    with pytest.raises(EmailNotFoundError, match="邮件不存在"):
        get_email(session, 9999)


def test_persistence_across_restart(tmp_path):
    db_path = tmp_path / "mail.db"
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory.begin() as db:
        email = create_email(
            db,
            message_id="<restart@qq.com>",
            sender="alice@qq.com",
            recipient="user@qq.com",
            subject="重启测试",
            body_text="重启后数据还在吗？",
        )
        route_email(db, email.id, "respond", "明确提问", "llm")
        add_draft(db, email.id, content="重启前草稿", round_number=0)
        request_rewrite(db, email.id, feedback="更正式一些")
        add_draft(
            db,
            email.id,
            content="重启前重写稿",
            round_number=1,
            feedback="更正式一些",
        )
    engine.dispose()

    restarted_engine = make_engine(db_path)
    init_db(restarted_engine)  # 幂等建表
    restarted_factory = make_session_factory(restarted_engine)
    with restarted_factory() as db:
        email = get_email(db, 1)
        assert email.message_id == "<restart@qq.com>"
        assert email.status == EmailStatus.DRAFT_PENDING.value
        assert email.category == "respond"
        assert email.rewrite_rounds == 1
        assert [draft.round_number for draft in email.drafts] == [0, 1]
        assert len(email.audit_logs) == 3  # create + route + rewrite（add_draft 不写审计）
    restarted_engine.dispose()
