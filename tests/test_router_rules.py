"""M3 验收：确定性规则命中 ignore，且命中时绝不调用 LLM。"""

from __future__ import annotations

import pytest
from email.message import EmailMessage
from email.utils import formatdate

from app.agent.router import RouteContext, RouteResult, route, rule_route
from app.mailbox.parser import parse_email
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailCategory, RouteSource
from app.store.repo import create_email


def _context(sender="alice@qq.com", subject="", body="", headers=None):
    return RouteContext(
        sender=sender,
        subject=subject,
        body=body,
        headers=headers or {},
    )


def _llm_must_not_be_called(context):
    raise AssertionError("规则命中时不应调用 LLM")


def _assert_ignore(result, rule_name):
    assert result.category == EmailCategory.IGNORE
    assert result.source == RouteSource.RULE
    assert result.confidence == 1.0
    assert result.rule_name == rule_name


def _assert_rule_notify(result, rule_name):
    assert result.category == EmailCategory.NOTIFY
    assert result.source == RouteSource.RULE
    assert result.confidence == 1.0
    assert result.rule_name == rule_name


@pytest.mark.parametrize("value", ["auto", "auto-generated", "auto-replied", "Auto-Generated"])
def test_auto_submitted_header_ignores(value):
    result = rule_route(_context(headers={"auto-submitted": value}, body="任何内容"))
    _assert_ignore(result, "auto_submitted")
    assert "Auto-Submitted" in result.reason


def test_auto_submitted_no_is_not_ignored():
    assert rule_route(_context(headers={"auto-submitted": "no"}, body="人工邮件")) is None


@pytest.mark.parametrize("value", ["bulk", "junk", "list", "BULK"])
def test_precedence_bulk_ignores(value):
    result = rule_route(_context(headers={"precedence": value}, body="批量邮件"))
    _assert_ignore(result, "precedence")
    assert "Precedence" in result.reason


def test_precedence_first_class_not_ignored():
    assert rule_route(_context(headers={"precedence": "first-class"}, body="普通邮件")) is None


@pytest.mark.parametrize("value", ["All", "ALL", "AutoReply"])
def test_x_auto_response_suppress_ignores(value):
    result = rule_route(_context(headers={"x-auto-response-suppress": value}, body="通知"))
    _assert_ignore(result, "x_auto_response_suppress")


@pytest.mark.parametrize(
    "sender",
    [
        "noreply@qq.com",
        "no-reply@github.com",
        "no_reply@example.com",
        "donotreply@example.com",
        "do-not-reply@example.com",
        "mailer-daemon@qq.com",
        "postmaster@example.com",
        "noreply-bot@example.com",
    ],
)
def test_system_sender_ignores(sender):
    result = rule_route(_context(sender=sender, body="普通通知"))
    _assert_ignore(result, "system_sender")
    assert sender in result.reason


def test_normal_sender_not_ignored_by_sender_rule():
    assert rule_route(_context(sender="alice@qq.com", body="正常邮件")) is None


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("系统通知", "此邮件由系统自动发送，请勿回复"),
        ("自动通知", "系统自动邮件，请勿答复"),
        ("Notification", "This is an automated email. Do not reply."),
        ("Auto message", "THIS IS AN AUTO-GENERATED MESSAGE."),
    ],
)
def test_no_reply_or_automated_wording_alone_delegates_to_llm(subject, body):
    assert rule_route(_context(subject=subject, body=body)) is None


def test_no_reply_with_attention_required_notifies():
    result = rule_route(
        _context(
            sender="campus@example.com",
            subject="xx集团2027校园招聘测评邀请",
            body=(
                "此邮件由系统发出，请勿直接回复或转发他人。"
                "亲爱的同学，恭喜你通过简历初筛环节，进入人才测评阶段。"
                "本次测评邀请将于 2026年09月29日 周二 12:15 生效，于7日后失效。"
            ),
            headers={"auto-submitted": "auto-generated"},
        )
    )
    _assert_rule_notify(result, "attention_required")
    assert "需本人关注" in result.reason


def test_normal_question_not_ignored_by_keyword():
    assert rule_route(_context(subject="咨询", body="请问方案什么时候能给到我？")) is None


def test_no_reply_instruction_downgrades_llm_respond_to_notify():
    def fake_llm(context):
        return RouteResult(
            EmailCategory.RESPOND,
            "模型认为需要回复",
            RouteSource.LLM,
            0.9,
        )

    result = route(
        _context(subject="通知", body="请勿回复。请完成系统操作。"),
        llm_router=fake_llm,
    )
    assert result.category == EmailCategory.NOTIFY
    assert result.source == RouteSource.LLM
    assert result.confidence == 0.9
    assert result.rule_name == "reply_suppressed"


def test_list_unsubscribe_ignores():
    result = rule_route(
        _context(
            headers={"list-unsubscribe": "<https://example.com/unsubscribe>"},
            body="周刊内容",
        )
    )
    _assert_ignore(result, "list_unsubscribe")


def test_rule_hit_never_invokes_llm():
    context = _context(
        sender="noreply@example.com",
        subject="系统通知",
        body="系统自动邮件，请勿回复",
    )
    result = route(context, llm_router=_llm_must_not_be_called)
    _assert_ignore(result, "system_sender")


def test_llm_invoked_when_no_rule():
    calls = []

    def fake_llm(context):
        calls.append(context)
        return RouteResult(
            category=EmailCategory.RESPOND,
            reason="明确提问，需要回应",
            source=RouteSource.LLM,
            confidence=0.92,
        )

    context = _context(subject="咨询", body="请问方案什么时候能给到我？")
    result = route(context, llm_router=fake_llm)
    assert result.category == EmailCategory.RESPOND
    assert result.source == RouteSource.LLM
    assert calls == [context]


def test_fallback_notify_without_llm():
    context = _context(subject="通知", body="内容模糊的邮件")
    result = route(context)
    assert result.category == EmailCategory.NOTIFY
    assert result.source == RouteSource.FALLBACK
    assert result.confidence == 0.0


def _build_raw_email(headers):
    msg = EmailMessage()
    msg["From"] = "noreply@example.com"
    msg["To"] = "user@qq.com"
    msg["Subject"] = "系统通知"
    msg["Message-ID"] = "<router-integration@example.com>"
    msg["Date"] = formatdate(localtime=False)
    for key, value in headers.items():
        msg[key] = value
    msg.set_content("此邮件由系统自动发送，请勿回复")
    return msg.as_bytes()


def test_context_from_parsed_email():
    content = parse_email(_build_raw_email({"Auto-Submitted": "auto-generated"}))
    result = rule_route(RouteContext.from_email_content(content))
    _assert_ignore(result, "auto_submitted")


def test_context_from_record(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        record = create_email(
            db,
            message_id="<router-record@example.com>",
            sender="noreply@example.com",
            recipient="user@qq.com",
            subject="系统通知",
            headers={"auto-submitted": "auto"},
            body_text="系统自动邮件，请勿回复",
        )
        result = rule_route(RouteContext.from_record(record))
        _assert_ignore(result, "auto_submitted")
    engine.dispose()
