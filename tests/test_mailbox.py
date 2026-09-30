"""M2 验收：MIME 解析、IMAP 批量拉取、历史未读分批与去重。"""

from __future__ import annotations

import pytest
from email.message import EmailMessage
from email.utils import formatdate
from datetime import datetime, timezone

from app.config import Settings
from app.mailbox.imap_client import IMAPClient, MailboxError
from app.mailbox.parser import parse_email
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailStatus
from app.store.repo import EmailExistsError, create_email, list_emails


def _build_raw_email(
    subject,
    body,
    *,
    message_id=None,
    charset="utf-8",
    html=False,
    extra_headers=None,
    date=None,
):
    msg = EmailMessage()
    msg["From"] = "alice@qq.com"
    msg["To"] = "user@qq.com"
    msg["Subject"] = subject
    msg["Date"] = date or formatdate(localtime=False)
    if message_id:
        msg["Message-ID"] = message_id
    for key, value in (extra_headers or {}).items():
        msg[key] = value
    if html:
        msg.set_content(body, subtype="html", charset=charset)
    else:
        msg.set_content(body, charset=charset)
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, raw_messages):
        self.messages = {
            str(index): raw for index, raw in enumerate(raw_messages, start=1)
        }
        self.seen: set[str] = set()
        self.uid_calls: list[tuple] = []

    def login(self, user, password):
        return ("OK", [b"Logged in"])

    def logout(self):
        return ("BYE", [b""])

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        command = command.upper()
        if command == "SEARCH":
            unseen = [uid for uid in self.messages if uid not in self.seen]
            return ("OK", [" ".join(unseen).encode("ascii")])
        if command == "FETCH":
            uid = args[0].decode("ascii") if isinstance(args[0], bytes) else str(args[0])
            raw = self.messages.get(uid)
            if raw is None:
                return ("OK", [None])
            header = f"UID {uid} RFC822 {{{len(raw)}}}".encode("ascii")
            return ("OK", [(header, raw), b")"])
        if command == "STORE":
            uid = args[0].decode("ascii") if isinstance(args[0], bytes) else str(args[0])
            self.seen.add(uid)
            return ("OK", [f"{uid} (FLAGS (\\Seen))".encode("ascii")])
        raise AssertionError(f"FakeIMAP 未实现命令：{command}")

    def select(self, mailbox):
        return ("OK", [b"[READ-WRITE] INBOX selected"])


@pytest.fixture
def db(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def make_client(monkeypatch):
    def _make(fake):
        settings = Settings(
            _env_file=None,
            mail_address="user@qq.com",
            mail_auth_code="auth-code",
        )
        client = IMAPClient(settings)
        monkeypatch.setattr(IMAPClient, "_connect", lambda self: fake)
        return client

    return _make


def _store_content(session, content):
    try:
        return create_email(
            session,
            message_id=content.message_id,
            sender=content.sender,
            recipient=content.recipient,
            reply_to=content.reply_to,
            subject=content.subject,
            received_at=content.received_at,
            headers=content.headers,
            body_text=content.body_text,
            body_truncated=content.body_truncated,
        )
    except EmailExistsError:
        return None


def _ingest_all(client, session, limit=10):
    batch_sizes = []
    while True:
        fetched = client.fetch_unread(limit)
        if not fetched:
            break
        for item in fetched:
            content = parse_email(item.raw)
            _store_content(session, content)
            client.mark_seen(item.uid)
        batch_sizes.append(len(fetched))
    return batch_sizes


def test_parse_utf8_chinese_with_key_headers():
    raw = _build_raw_email(
        "关于方案时间",
        "请问方案什么时候能给到我？",
        message_id="<utf8@qq.com>",
        extra_headers={
            "Reply-To": "alice-reply@qq.com",
            "Auto-Submitted": "auto-replied",
            "Precedence": "bulk",
        },
    )
    content = parse_email(raw)
    assert content.message_id == "<utf8@qq.com>"
    assert content.subject == "关于方案时间"
    assert content.body_text.strip() == "请问方案什么时候能给到我？"
    assert content.sender == "alice@qq.com"
    assert content.recipient == "user@qq.com"
    assert content.reply_to == "alice-reply@qq.com"
    assert content.headers["auto-submitted"] == "auto-replied"
    assert content.headers["precedence"] == "bulk"
    assert content.received_at is not None
    assert content.body_truncated is False


def test_parse_date_is_normalized_to_utc():
    raw = _build_raw_email(
        "时区测试",
        "正文",
        message_id="<timezone@qq.com>",
        date="Tue, 22 Sep 2026 16:19:41 +0800",
    )

    content = parse_email(raw)

    assert content.received_at == datetime(
        2026, 9, 22, 8, 19, 41, tzinfo=timezone.utc
    )


def test_parse_gbk_charset():
    raw = _build_raw_email(
        "GBK 编码",
        "中文编码内容测试",
        message_id="<gbk@qq.com>",
        charset="gbk",
    )
    content = parse_email(raw)
    assert content.subject == "GBK 编码"
    assert content.body_text.strip() == "中文编码内容测试"


def test_parse_html_only_body_skips_script():
    raw = _build_raw_email(
        "HTML 邮件",
        "<div><p>你好，</p><p>请查收说明。</p><script>alert(1)</script></div>",
        message_id="<html@qq.com>",
        html=True,
    )
    content = parse_email(raw)
    assert "你好，" in content.body_text
    assert "请查收说明。" in content.body_text
    assert "alert" not in content.body_text


def test_parse_prefers_plain_over_html():
    msg = EmailMessage()
    msg["From"] = "alice@qq.com"
    msg["To"] = "user@qq.com"
    msg["Subject"] = "多部分邮件"
    msg["Message-ID"] = "<multi@qq.com>"
    msg.set_content("纯文本版本")
    msg.add_alternative("<p>HTML 版本</p>", subtype="html")
    content = parse_email(msg.as_bytes())
    assert content.body_text.strip() == "纯文本版本"


def test_missing_message_id_fallback_is_stable():
    raw = _build_raw_email("缺 Message-ID", "同一封邮件")
    first = parse_email(raw)
    second = parse_email(raw)
    assert first.message_id.startswith("sha256-")
    assert first.message_id == second.message_id
    different = parse_email(_build_raw_email("另一封邮件", "内容不同"))
    assert different.message_id != first.message_id


def test_body_truncated_flag():
    raw = _build_raw_email("超长邮件", "a" * 100, message_id="<long@qq.com>")
    content = parse_email(raw, max_body_chars=10)
    assert content.body_text == "a" * 10
    assert content.body_truncated is True


def test_missing_headers_tolerated():
    msg = EmailMessage()
    msg.set_content("只有正文")
    content = parse_email(msg.as_bytes())
    assert content.message_id.startswith("sha256-")
    assert content.sender == ""
    assert content.recipient == ""
    assert content.reply_to is None
    assert content.subject == ""
    assert content.received_at is None
    assert content.body_text.strip() == "只有正文"


def test_fetch_respects_limit(make_client):
    messages = [
        _build_raw_email(f"第 {i} 封", f"正文 {i}", message_id=f"<{i}@qq.com>")
        for i in range(15)
    ]
    fake = FakeIMAP(messages)
    client = make_client(fake)
    fetched = client.fetch_unread(10)
    assert len(fetched) == 10
    assert fake.seen == set()
    assert [item.uid for item in fetched] == [str(i) for i in range(1, 11)]
    fetch_calls = [call for call in fake.uid_calls if call[0] == "FETCH"]
    assert len(fetch_calls) == 10
    assert all(call[2] == "(BODY.PEEK[])" for call in fetch_calls)


def test_invalid_fetch_limit(make_client):
    client = make_client(FakeIMAP([]))
    with pytest.raises(ValueError, match="limit 必须为正整数"):
        client.fetch_unread(0)


def test_same_batch_returned_without_mark_seen(make_client):
    messages = [
        _build_raw_email(f"第 {i} 封", f"正文 {i}", message_id=f"<{i}@qq.com>")
        for i in range(15)
    ]
    fake = FakeIMAP(messages)
    client = make_client(fake)
    first = client.fetch_unread(10)
    second = client.fetch_unread(10)
    assert [item.uid for item in first] == [item.uid for item in second]


def test_historical_unread_processed_in_batches(make_client, db):
    messages = [
        _build_raw_email(
            f"历史邮件 {i}",
            f"历史正文 {i}",
            message_id=f"<history-{i}@qq.com>",
        )
        for i in range(23)
    ]
    fake = FakeIMAP(messages)
    client = make_client(fake)
    batches = _ingest_all(client, db, limit=10)
    assert batches == [10, 10, 3]
    assert len(list_emails(db)) == 23
    assert len(fake.seen) == 23
    assert client.fetch_unread(10) == []


def test_duplicate_message_not_stored_twice(make_client, db):
    raw = _build_raw_email("重复邮件", "同一 Message-ID", message_id="<dup@qq.com>")
    fake = FakeIMAP([raw, raw])
    client = make_client(fake)
    batches = _ingest_all(client, db, limit=10)
    assert batches == [2]
    assert len(list_emails(db)) == 1
    assert len(fake.seen) == 2
    assert list_emails(db)[0].status == EmailStatus.NEW.value


def test_login_failure_clear_message(make_client):
    class BadLoginIMAP(FakeIMAP):
        def login(self, user, password):
            return ("NO", [b"AUTHENTICATIONFAILED"])

    client = make_client(BadLoginIMAP([]))
    with pytest.raises(MailboxError, match="授权码"):
        client.fetch_unread(10)
