"""MIME 邮件解析：原始字节 → EmailContent（纯文本正文 + 关键邮件头）。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

_KEY_HEADERS = (
    "auto-submitted",
    "precedence",
    "reply-to",
    "list-unsubscribe",
    "x-auto-response-suppress",
)
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "blockquote", "section", "article", "header", "footer",
}
_SKIP_TAGS = {"script", "style", "head", "title", "meta"}


@dataclass(frozen=True)
class EmailContent:
    message_id: str
    sender: str
    recipient: str
    reply_to: str | None
    subject: str
    received_at: datetime | None
    headers: dict[str, str]
    body_text: str
    body_truncated: bool


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = "".join(extractor.parts)
    lines = (re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        fallback = part.get_payload()
        return fallback if isinstance(fallback, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for candidate in (charset, "utf-8", "gb18030"):
        try:
            return payload.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("latin-1", errors="replace")


def _extract_body(msg: Message) -> str:
    plain = ""
    html = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and not plain:
            plain = _decode_payload(part)
        elif content_type == "text/html" and not html:
            html = _decode_payload(part)
    if plain:
        return plain
    if html:
        return _html_to_text(html)
    return ""


def _fallback_message_id(msg: Message, body: str) -> str:
    seed = "\n".join(
        [
            str(msg.get("From", "")),
            str(msg.get("Subject", "")),
            str(msg.get("Date", "")),
            body,
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256-{digest}"


def _first_address(header_value: str) -> str:
    _, address = parseaddr(header_value)
    return address or header_value.strip()


def parse_email(raw: bytes, *, max_body_chars: int = 8000) -> EmailContent:
    """解析原始邮件；正文超长截断并置位 body_truncated。"""
    if max_body_chars < 1:
        raise ValueError("max_body_chars 必须为正整数")
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = _extract_body(msg)
    body_truncated = len(body) > max_body_chars
    if body_truncated:
        body = body[:max_body_chars]

    message_id = str(msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = _fallback_message_id(msg, body)

    sender = _first_address(str(msg.get("From") or ""))
    recipient = next(
        (addr for _, addr in getaddresses([str(msg.get("To") or "")]) if addr),
        "",
    )
    reply_to_value = str(msg.get("Reply-To") or "").strip()
    reply_to = _first_address(reply_to_value) if reply_to_value else None

    received_at: datetime | None = None
    date_value = msg.get("Date")
    if date_value:
        try:
            parsed_date = parsedate_to_datetime(str(date_value))
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            received_at = parsed_date.astimezone(timezone.utc)
        except (TypeError, ValueError):
            received_at = None

    headers = {
        key: str(msg.get(key)).strip()
        for key in _KEY_HEADERS
        if msg.get(key) is not None and str(msg.get(key)).strip()
    }
    return EmailContent(
        message_id=message_id,
        sender=sender,
        recipient=recipient,
        reply_to=reply_to,
        subject=str(msg.get("Subject") or "").strip(),
        received_at=received_at,
        headers=headers,
        body_text=body,
        body_truncated=body_truncated,
    )
