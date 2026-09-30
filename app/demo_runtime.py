"""离线演示用的确定性 LLM 替身与种子数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.drafter import Drafter
from app.agent.graph import run_for_email
from app.agent.router import RouteContext, RouteResult
from app.store import repo
from app.store.models import EmailCategory, EmailStatus, RouteSource


class DemoLLMRouter:
    """无网络分类替身：规则未命中后按演示文案给出稳定结果。"""

    def route(self, context: RouteContext) -> RouteResult:
        if "通知" in context.subject or "安排" in context.subject:
            return RouteResult(
                category=EmailCategory.NOTIFY,
                reason="演示分类：内容偏信息通知，暂不需要直接回复",
                source=RouteSource.LLM,
                confidence=0.91,
            )
        return RouteResult(
            category=EmailCategory.RESPOND,
            reason="演示分类：对方提出明确问题，需要人工审核回复",
            source=RouteSource.LLM,
            confidence=0.96,
        )


class DemoDrafter(Drafter):
    """无网络草稿替身，保留 feedback / previous_draft 的重写语义。"""

    def generate(
        self,
        context: RouteContext,
        *,
        feedback: str | None = None,
        previous_draft: str | None = None,
    ) -> str:
        if feedback:
            return (
                f"{context.sender}，您好：\n\n"
                f"感谢您的来信。我们已根据审核反馈调整本封回复：{feedback}。\n\n"
                "如仍有需要补充的信息，我会尽快同步。祝工作顺利。\n\n"
                "此致\nLangGraph 邮件智能助手演示"
            )
        return (
            f"{context.sender}，您好：\n\n"
            "感谢您的来信。针对您提到的内容，当前处理进展正常，"
            "我会继续跟进并在有明确结论后第一时间回复。\n\n"
            "如有其他问题，欢迎随时联系。祝工作顺利。\n\n"
            "此致\nLangGraph 邮件智能助手演示"
        )


class DemoMailbox:
    """无网络邮箱替身：演示数据由 seed 脚本预置，调度器不访问 IMAP。"""

    def fetch_unread(self, limit: int, exclude_uids: set[str] | None = None) -> list:
        if limit < 1:
            raise ValueError("limit 必须为正整数")
        return []

    def mark_seen(self, uid: str) -> None:
        return None

    def mark_seen_many(self, uids: list[str] | tuple[str, ...]) -> None:
        return None


def seed_demo_data(session_factory, graph) -> dict[str, str]:
    """写入一组固定演示邮件；重复执行会跳过，保护用户操作结果。"""

    now = datetime.now(timezone.utc)
    specs = [
        {
            "message_id": "<demo-ignore-newsletter@example.com>",
            "sender": "newsletter@example.com",
            "recipient": "user@qq.com",
            "subject": "开发者技术周刊：本周文章精选",
            "body_text": "本期汇总了多篇公开技术文章，如不需要可随时退订。",
            "headers": {"precedence": "bulk", "list-unsubscribe": "<https://example.com/unsubscribe>"},
            "received_at": now - timedelta(hours=5),
        },
        {
            "message_id": "<demo-notify-hr@example.com>",
            "sender": "hr-notice@example.com",
            "recipient": "user@qq.com",
            "subject": "关于下周值班安排的通知",
            "body_text": "各部门请知悉下周值班安排，如有冲突请在系统内调整。",
            "headers": {},
            "received_at": now - timedelta(hours=4),
        },
        {
            "message_id": "<demo-respond-alice@example.com>",
            "sender": "alice.customer@example.com",
            "recipient": "user@qq.com",
            "subject": "请问方案初稿什么时候能给到我？",
            "body_text": "你好，我们内部评审安排在周五，请问方案初稿什么时候能给到我？最好能附上价格区间。",
            "headers": {},
            "received_at": now - timedelta(hours=3),
        },
        {
            "message_id": "<demo-sent-wang@example.com>",
            "sender": "wang.vendor@example.com",
            "recipient": "user@qq.com",
            "subject": "需要确认部署验收时间",
            "body_text": "请确认本周四下午能否进行部署验收？如时间不合适，请回复两个可选时间段。",
            "headers": {},
            "received_at": now - timedelta(hours=2),
        },
        {
            "message_id": "<demo-failed-invoice@example.com>",
            "sender": "finance.partner@example.com",
            "recipient": "user@qq.com",
            "subject": "发票信息需要回复确认",
            "body_text": "请确认发票抬头和税号是否按照上次合同填写，我们需要在本月完成开票。",
            "headers": {},
            "received_at": now - timedelta(hours=1),
        },
    ]

    with session_factory() as db:
        existing = {
            spec["message_id"]: repo.get_email_by_message_id(db, spec["message_id"])
            for spec in specs
        }
        db.commit()
    if any(existing.values()):
        return {
            message_id: record.status
            for message_id, record in existing.items()
            if record is not None
        }

    email_ids: dict[str, int] = {}
    for spec in specs:
        with session_factory() as db:
            record = repo.create_email(db, **spec)
            db.commit()
            email_ids[spec["message_id"]] = record.id
        run_for_email(graph, record.id)

    with session_factory() as db:
        failed = repo.get_email_by_message_id(
            db, "<demo-failed-invoice@example.com>"
        )
        repo.mark_send_failed(
            db, failed.id, "演示场景：Fake SMTP 连接超时，等待 M7 人工重试"
        )
        db.commit()

    sent_id = email_ids["<demo-sent-wang@example.com>"]
    from app.agent.graph import resume_for_email

    resume_for_email(
        graph,
        sent_id,
        {"action": "rewrite", "feedback": "明确写出周四 14:00，并补充需要提前准备的事项"},
    )
    resume_for_email(
        graph,
        sent_id,
        {
            "action": "edit_send",
            "draft": (
                "wang.vendor@example.com，您好：\n\n"
                "周四 14:00 可以进行部署验收。请提前准备生产环境只读账号、"
                "变更回滚说明和验收签字人信息。\n\n"
                "如时间需要调整，请回复两个备选时间段。祝工作顺利。\n\n"
                "此致\n演示邮箱用户"
            ),
        },
    )

    with session_factory() as db:
        return {
            spec["message_id"]: repo.get_email_by_message_id(
                db, spec["message_id"]
            ).status
            for spec in specs
        }
