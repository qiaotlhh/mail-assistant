"""SMTP 发送器单元测试：全部使用 Fake SMTP，不触网。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.mailbox.smtp_client import SMTPClient, SMTPSendError


class FakeSMTPServer:
    def __init__(self):
        self.login_args = []
        self.messages = []
        self.closed = False

    def login(self, username, password):
        self.login_args.append((username, password))

    def send_message(self, message):
        self.messages.append(message)

    def noop(self):
        return (250, b"OK")

    def quit(self):
        self.closed = True


def make_client(server):
    @contextmanager
    def factory(host, port, timeout):
        server.host = host
        server.port = port
        server.timeout = timeout
        yield server
        server.quit()

    return SMTPClient(
        mail_address="user@qq.com",
        auth_code="auth-code",
        host="smtp.qq.com",
        port=465,
        timeout=3,
        client_factory=factory,
    )


def test_send_reply_builds_headers_and_logs_in():
    server = FakeSMTPServer()
    client = make_client(server)
    client(
        email_id=1,
        recipient="alice@example.com",
        reply_to=None,
        subject="请问方案时间",
        body="您好，方案周五提供。",
        message_id="<original@example.com>",
        headers={"References": "<old@example.com>"},
    )
    assert server.host == "smtp.qq.com"
    assert server.port == 465
    assert server.login_args == [("user@qq.com", "auth-code")]
    assert len(server.messages) == 1
    message = server.messages[0]
    assert message["From"] == "user@qq.com"
    assert message["To"] == "alice@example.com"
    assert message["Subject"] == "Re: 请问方案时间"
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<old@example.com> <original@example.com>"
    assert "方案周五提供" in message.get_content()


def test_send_reply_rejects_empty_body():
    client = make_client(FakeSMTPServer())
    with pytest.raises(SMTPSendError, match="回复正文为空"):
        client(
            email_id=1,
            recipient="alice@example.com",
            reply_to=None,
            subject="主题",
            body=" ",
        )


def test_verify_connection_logs_in_and_noop_without_send():
    server = FakeSMTPServer()
    client = make_client(server)
    client.verify_connection()
    assert server.login_args == [("user@qq.com", "auth-code")]
    assert server.messages == []
