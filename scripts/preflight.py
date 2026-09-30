"""真机链路预检：配置、IMAP、SMTP、LLM；全程不发送邮件。"""

from __future__ import annotations

import argparse

from app.agent.router import RouteContext
from app.config import get_settings
from app.llm.provider import build_llm_router
from app.mailbox.imap_client import IMAPClient
from app.mailbox.parser import parse_email
from app.mailbox.smtp_client import SMTPClient


def _ok(name: str, detail: str) -> None:
    print(f"[OK] {name}: {detail}")


def _fail(name: str, exc: Exception) -> None:
    print(f"[FAIL] {name}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 邮箱与 LLM 真机预检")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="跳过 LLM 调用，只检查邮箱网络与授权",
    )
    args = parser.parse_args()

    settings = get_settings()
    try:
        settings.ensure_ready()
        _ok(
            "配置",
            (
                f"邮箱={settings.mail_address}，fetch_limit="
                f"{settings.mail_fetch_limit}，poll={settings.poll_interval_seconds}s，"
                f"providers={','.join(settings.provider_order)}"
            ),
        )
    except Exception as exc:
        _fail("配置", exc)
        return 1

    try:
        fetched = IMAPClient(settings).fetch_unread(limit=1)
        if fetched:
            content = parse_email(fetched[0].raw, max_body_chars=120)
            _ok("IMAP", f"登录成功，当前有未读；样例主题：{content.subject or '(无主题)'}")
        else:
            _ok("IMAP", "登录成功，当前没有未读邮件")
    except Exception as exc:
        _fail("IMAP", exc)
        return 1

    try:
        SMTPClient(
            mail_address=settings.mail_address,
            auth_code=settings.mail_auth_code,
            host=settings.smtp_host,
            port=settings.smtp_port,
        ).verify_connection()
        _ok("SMTP", "登录与 NOOP 成功，未发送任何邮件")
    except Exception as exc:
        _fail("SMTP", exc)
        return 1

    if args.skip_llm:
        _ok("LLM", "已按参数跳过")
        return 0

    try:
        router = build_llm_router(settings)
        result = router.route(
            RouteContext(
                sender="preflight@example.com",
                subject="预检：请回复确认收到",
                body="这是一封内部预检文本，请确认是否需要回复。",
                headers={},
            )
        )
        detail = f"分类={result.category.value}，来源={result.source.value}，置信度={result.confidence:.2f}"
        if router.last_alerts:
            detail += f"；告警={'；'.join(router.last_alerts)}"
        _ok("LLM", detail)
        return 0
    except Exception as exc:
        _fail("LLM", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
