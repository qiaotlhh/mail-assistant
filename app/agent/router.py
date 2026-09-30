"""分类路由：确定性规则优先，LLM 兜底（M4 接入），最终兜底 notify。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from app.mailbox.parser import EmailContent
from app.store.models import (
    EmailCategory,
    EmailRecord,
    RouteSource,
)


@dataclass(frozen=True)
class RouteResult:
    category: EmailCategory
    reason: str
    source: RouteSource
    confidence: float = 1.0
    rule_name: str | None = None


@dataclass(frozen=True)
class RouteContext:
    sender: str
    subject: str
    body: str
    headers: dict[str, str]

    @classmethod
    def from_email_content(cls, content: EmailContent) -> "RouteContext":
        return cls(
            sender=content.sender,
            subject=content.subject,
            body=content.body_text,
            headers=dict(content.headers),
        )

    @classmethod
    def from_record(cls, record: EmailRecord) -> "RouteContext":
        try:
            headers = json.loads(record.headers_json or "{}")
        except json.JSONDecodeError:
            headers = {}
        return cls(
            sender=record.sender,
            subject=record.subject,
            body=record.body_text,
            headers=headers,
        )


_PRECEDENCE_IGNORE = {"bulk", "junk", "list", "auto_reply"}
_X_AUTO_SUPPRESS_IGNORE = {"all", "autoreply"}
_SYSTEM_LOCALPARTS = {"noreply", "donotreply", "mailerdaemon", "postmaster"}
_SYSTEM_PREFIXES = ("noreply", "donotreply")
# No-reply wording suppresses outbound replies; it does not imply low value.
_NO_REPLY_KEYWORDS = (
    "请勿回复",
    "请勿答复",
    "请勿直接回复",
    "do not reply",
    "don't reply",
    "dont reply",
    "please do not reply",
)
# Transactional and time-sensitive content should be visible even when it comes
# from an automated sender or asks the recipient not to reply.
_ATTENTION_KEYWORDS = (
    "验证码",
    "校验码",
    "安全提醒",
    "异常登录",
    "初筛",
    "录取",
    "录用",
    "面试",
    "测评",
    "考试",
    "准考证",
    "账单",
    "发票",
    "订单",
    "支付",
    "退款",
    "预约",
    "报名",
    "日程",
    "邀请",
    "生效",
    "截止",
    "到期",
    "失效",
    "verification code",
    "security alert",
    "invoice",
    "appointment",
    "deadline",
    "expired",
)


def _header(context: RouteContext, name: str) -> str:
    return context.headers.get(name, "").strip().lower()


def _is_auto_submitted(context: RouteContext) -> bool:
    value = _header(context, "auto-submitted")
    return bool(value) and value != "no" and value.startswith("auto")


def _is_bulk_precedence(context: RouteContext) -> bool:
    return _header(context, "precedence") in _PRECEDENCE_IGNORE


def _suppresses_auto_response(context: RouteContext) -> bool:
    return _header(context, "x-auto-response-suppress") in _X_AUTO_SUPPRESS_IGNORE


def _is_system_sender(context: RouteContext) -> bool:
    localpart = context.sender.split("@", 1)[0] if "@" in context.sender else ""
    normalized = re.sub(r"[^a-z0-9]", "", localpart.lower())
    return normalized in _SYSTEM_LOCALPARTS or normalized.startswith(_SYSTEM_PREFIXES)


def _matched_keyword(
    context: RouteContext, keywords: tuple[str, ...]
) -> str | None:
    text = f"{context.subject}\n{context.body}".lower()
    for keyword in keywords:
        if keyword in text:
            return keyword
    return None


def _matched_no_reply_keyword(context: RouteContext) -> str | None:
    return _matched_keyword(context, _NO_REPLY_KEYWORDS)


def _matched_attention_keyword(context: RouteContext) -> str | None:
    return _matched_keyword(context, _ATTENTION_KEYWORDS)


def _has_list_unsubscribe(context: RouteContext) -> bool:
    return bool(_header(context, "list-unsubscribe"))


@dataclass(frozen=True)
class _Rule:
    name: str
    reason: str
    matches: Callable[[RouteContext], bool]


_RULES: list[_Rule] = [
    _Rule(
        name="auto_submitted",
        reason="邮件头 Auto-Submitted 表明为自动发送",
        matches=_is_auto_submitted,
    ),
    _Rule(
        name="precedence",
        reason="邮件头 Precedence 表明为批量/群发邮件",
        matches=_is_bulk_precedence,
    ),
    _Rule(
        name="x_auto_response_suppress",
        reason="邮件头 X-Auto-Response-Suppress 要求抑制自动回复",
        matches=_suppresses_auto_response,
    ),
    _Rule(
        name="system_sender",
        reason="发件人为系统/无人值守地址",
        matches=_is_system_sender,
    ),

    _Rule(
        name="list_unsubscribe",
        reason="邮件含 List-Unsubscribe 头，判定为邮件列表",
        matches=_has_list_unsubscribe,
    ),
]


def rule_route(context: RouteContext) -> RouteResult | None:
    """先保护需关注邮件，再执行高置信 ignore 规则；其余交给 LLM。"""
    attention = _matched_attention_keyword(context)
    reply_suppressed = (
        _matched_no_reply_keyword(context) is not None
        or _is_auto_submitted(context)
        or _is_system_sender(context)
    )
    if attention and reply_suppressed:
        return RouteResult(
            category=EmailCategory.NOTIFY,
            reason=(
                f"邮件包含需本人关注的信号：{attention}；"
            ),
            source=RouteSource.RULE,
            confidence=1.0,
            rule_name="attention_required",
        )

    for rule in _RULES:
        if not rule.matches(context):
            continue
        reason = rule.reason
        if rule.name == "system_sender":
            reason = f"{reason}：{context.sender}"
        elif rule.name == "precedence":
            reason = f"{reason}：Precedence={_header(context, 'precedence')}"
        return RouteResult(
            category=EmailCategory.IGNORE,
            reason=reason,
            source=RouteSource.RULE,
            confidence=1.0,
            rule_name=rule.name,
        )
    return None


def route(
    context: RouteContext,
    llm_router: Callable[[RouteContext], RouteResult] | None = None,
) -> RouteResult:
    """统一入口：规则优先且短路；LLM 兜底；无 LLM 结果时默认 notify。"""
    result = rule_route(context)
    if result is not None:
        return result
    if llm_router is not None:
        result = llm_router(context)
        no_reply = _matched_no_reply_keyword(context)
        if no_reply and result.category == EmailCategory.RESPOND:
            return RouteResult(
                category=EmailCategory.NOTIFY,
                reason=(
                    f"邮件包含“{no_reply}”，不应直接回复；"
                    f"LLM 判定为需回复，已保守降级为待通知"
                    f"（原判定理由：{result.reason}）"
                ),
                source=result.source,
                confidence=result.confidence,
                rule_name="reply_suppressed",
            )
        return result
    return RouteResult(
        category=EmailCategory.NOTIFY,
        reason="确定性规则未命中且暂无 LLM 结果，默认进入待通知",
        source=RouteSource.FALLBACK,
        confidence=0.0,
    )
