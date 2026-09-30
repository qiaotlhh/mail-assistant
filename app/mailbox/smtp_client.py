"""SMTP 回复发送器：只允许 Web 人工审核动作调用。"""

from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage
from typing import Callable, ContextManager


class SMTPSendError(RuntimeError):
    pass


SMTPClientFactory = Callable[[str, int, float], ContextManager[smtplib.SMTP]]


class SMTPClient:
    def __init__(
        self,
        *,
        mail_address: str,
        auth_code: str,
        host: str = "smtp.qq.com",
        port: int = 465,
        timeout: float = 30.0,
        client_factory: SMTPClientFactory | None = None,
    ):
        if not mail_address or not auth_code:
            raise SMTPSendError("SMTP 发送需要配置邮箱地址和授权码")
        self._mail_address = mail_address
        self._auth_code = auth_code
        self._host = host
        self._port = port
        self._timeout = timeout
        self._client_factory = client_factory or self._create_ssl_client

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
    ) -> None:
        self.send_reply(
            email_id=email_id,
            recipient=recipient,
            subject=subject,
            body=body,
            message_id=message_id,
            headers=headers,
        )

    def send_reply(
        self,
        *,
        email_id: int,
        recipient: str,
        subject: str,
        body: str,
        message_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not recipient.strip():
            raise SMTPSendError("回复收件人为空，拒绝发送")
        if not body.strip():
            raise SMTPSendError("回复正文为空，拒绝发送")

        message = EmailMessage()
        message["From"] = self._mail_address
        message["To"] = recipient
        message["Subject"] = self._reply_subject(subject)
        if message_id:
            message["In-Reply-To"] = message_id
            references = self._merged_references(headers or {}, message_id)
            if references:
                message["References"] = references
        message.set_content(body)

        try:
            with self._client_factory(
                self._host, self._port, self._timeout
            ) as client:
                client.login(self._mail_address, self._auth_code)
                client.send_message(message)
        except SMTPSendError:
            raise
        except Exception as exc:
            raise SMTPSendError(f"SMTP 发送失败：{exc}") from exc

    def verify_connection(self) -> None:
        """只登录并执行 NOOP，用于真机预检；不会发送邮件。"""
        try:
            with self._client_factory(
                self._host, self._port, self._timeout
            ) as client:
                client.login(self._mail_address, self._auth_code)
                status, _message = client.noop()
                if not str(status).startswith("2"):
                    raise SMTPSendError(f"SMTP NOOP 失败：{status} {_message}")
        except SMTPSendError:
            raise
        except Exception as exc:
            raise SMTPSendError(f"SMTP 连接/登录失败：{exc}") from exc

    @staticmethod
    def _create_ssl_client(
        host: str, port: int, timeout: float
    ) -> smtplib.SMTP:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)

    @staticmethod
    def _reply_subject(subject: str) -> str:
        text = subject.strip() or "回复"
        if re.match(r"^re[:：]\s*", text, flags=re.IGNORECASE):
            return text
        return f"Re: {text}"

    @staticmethod
    def _merged_references(headers: dict[str, str], message_id: str) -> str:
        existing = ""
        for name, value in headers.items():
            if name.lower() == "references":
                existing = value.strip()
                break
        parts = [item.strip() for item in existing.split() if item.strip()]
        if message_id not in parts:
            parts.append(message_id)
        return " ".join(parts)
