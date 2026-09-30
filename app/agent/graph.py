"""LangGraph 工作流：分类 → 草稿 → HITL 中断 → 人工动作。

安全边界：本模块只写业务状态机，不 import SMTP；外发动作由 Web 层
在真实发送成功后 resume(approve/edit_send) 触发状态迁移。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.drafter import DraftError, Drafter
from app.agent.router import RouteContext, RouteResult, route
from app.agent.state import AgentState
from app.store import repo
from app.store.models import EmailStatus


LLMRouteFn = Callable[[RouteContext], RouteResult]


class GraphError(RuntimeError):
    """工作流输入或当前状态不合法。"""


def make_sqlite_checkpointer(db_path: str | Path) -> SqliteSaver:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


def build_graph(
    session_factory,
    *,
    llm_route: LLMRouteFn | None = None,
    drafter: Drafter | None = None,
    checkpointer=None,
    max_rewrite_rounds: int = 3,
):
    def classify_node(state: AgentState) -> dict:
        email_id = state["email_id"]
        with session_factory() as db:
            record = repo.get_email(db, email_id)
            if record.status != EmailStatus.NEW.value:
                raise GraphError(
                    f"只有 NEW 状态的邮件可进入工作流："
                    f"email_id={email_id}，当前 {record.status}"
                )
            context = RouteContext.from_record(record)
            result = route(context, llm_router=llm_route)
        return {
            "category": result.category.value,
            "route_reason": result.reason,
            "route_source": result.source.value,
            "confidence": result.confidence,
            "error": None,
        }

    def persist_ignore_node(state: AgentState) -> dict:
        with session_factory() as db:
            repo.route_email(
                db,
                state["email_id"],
                "ignore",
                state["route_reason"],
                state["route_source"],
            )
            db.commit()
        return {}

    def persist_notify_node(state: AgentState) -> dict:
        with session_factory() as db:
            repo.route_email(
                db,
                state["email_id"],
                "notify",
                state["route_reason"],
                state["route_source"],
            )
            db.commit()
        return {}

    def generate_draft_node(state: AgentState) -> dict:
        with session_factory() as db:
            record = repo.get_email(db, state["email_id"])
            context = RouteContext.from_record(record)
        try:
            if drafter is None:
                raise DraftError("未配置草稿生成器")
            draft = drafter.generate(
                context,
                feedback=state.get("feedback"),
                previous_draft=state.get("draft"),
            )
            return {"draft": draft, "error": None}
        except DraftError as exc:
            return {
                "category": "notify",
                "route_reason": (
                    f"{state.get('route_reason') or '通知邮件'}；"
                    f"草稿生成失败，升级取消，保持原状态：{exc}"
                ),
                "error": str(exc),
            }

    def check_upgrade_node(state: AgentState) -> dict:
        email_id = state["email_id"]
        with session_factory() as db:
            record = repo.get_email(db, email_id)
            if record.status not in {
                EmailStatus.NOTIFIED.value,
                EmailStatus.IGNORED.value,
            }:
                raise GraphError(
                    f"只有 NOTIFIED/IGNORED 状态的邮件可人工升级："
                    f"email_id={email_id}，当前 {record.status}"
                )
        return {
            "upgrade": True,
            "category": "respond",
            "route_reason": "人工将邮件升级为需回复",
            "route_source": "llm",
        }

    def cancel_upgrade_node(state: AgentState) -> dict:
        return {}

    def upgrade_to_draft_node(state: AgentState) -> dict:
        with session_factory() as db:
            repo.upgrade_to_draft(db, state["email_id"])
            db.commit()
        return {
            "category": "respond",
            "route_reason": "人工将邮件升级为需回复",
        }

    def persist_draft_node(state: AgentState) -> dict:
        email_id = state["email_id"]
        with session_factory() as db:
            record = repo.get_email(db, email_id)
            if record.status == EmailStatus.NEW.value:
                repo.route_email(
                    db,
                    email_id,
                    "respond",
                    state["route_reason"],
                    state["route_source"],
                )
                round_number = 0
            else:
                round_number = record.rewrite_rounds
            repo.add_draft(
                db,
                email_id,
                content=state["draft"],
                round_number=round_number,
                feedback=state.get("feedback"),
            )
            db.commit()
        return {"rewrite_rounds": round_number}

    def hitl_node(state: AgentState) -> dict:
        payload = interrupt(
            {
                "email_id": state["email_id"],
                "draft": state["draft"],
                "rewrite_rounds": state["rewrite_rounds"],
                "message": "等待人工审核：approve / edit_send / ignore / rewrite",
            }
        )
        return {"pending_action": payload}

    def apply_action_node(state: AgentState) -> dict:
        payload = state["pending_action"]
        action = payload.get("action")
        email_id = state["email_id"]
        if action == "rewrite":
            feedback = (payload.get("feedback") or "").strip()
            if not feedback:
                raise GraphError("rewrite 动作需要提供反馈 feedback")
            with session_factory() as db:
                repo.request_rewrite(
                    db, email_id, feedback=feedback, max_rounds=max_rewrite_rounds
                )
                db.commit()
            return {"feedback": feedback, "upgrade": False}
        if action == "reclassify":
            target_category = (payload.get("target_category") or "").strip()
            with session_factory() as db:
                repo.reclassify_email(db, email_id, target_category)
                db.commit()
            return {}
        if action == "ignore":
            with session_factory() as db:
                repo.manual_ignore(db, email_id)
                db.commit()
            return {}
        if action == "approve":
            with session_factory() as db:
                repo.approve_send(db, email_id)
                db.commit()
            return {}
        if action == "edit_send":
            new_draft = (payload.get("draft") or "").strip()
            if not new_draft:
                raise GraphError("edit_send 动作需要提供修改后的草稿 draft")
            with session_factory() as db:
                record = repo.get_email(db, email_id)
                latest = record.drafts[-1] if record.drafts else None
                already_persisted = (
                    latest is not None
                    and latest.source == "user_edit"
                    and latest.round_number == record.rewrite_rounds
                    and latest.content == new_draft
                )
                if not already_persisted:
                    repo.add_draft(
                        db,
                        email_id,
                        content=new_draft,
                        round_number=record.rewrite_rounds,
                        source="user_edit",
                    )
                repo.edit_send(db, email_id)
                db.commit()
            return {}
        if action == "retry_send":
            sent_kind = payload.get("sent_kind")
            with session_factory() as db:
                repo.retry_send(db, email_id, sent_kind=sent_kind)
                db.commit()
            return {}
        raise GraphError(f"未知人工动作：{action}")

    def branch_after_classify(state: AgentState) -> str:
        return state["category"]

    def branch_after_generate(state: AgentState) -> str:
        if state["category"] != "respond":
            return "cancel_upgrade" if state.get("upgrade") else "persist_notify"
        return "upgrade_to_draft" if state.get("upgrade") else "persist_draft"

    def branch_after_apply(state: AgentState) -> str:
        return "generate_draft" if state["pending_action"]["action"] == "rewrite" else "end"

    builder = StateGraph(AgentState)
    builder.add_node("check_upgrade", check_upgrade_node)
    builder.add_node("cancel_upgrade", cancel_upgrade_node)
    builder.add_node("upgrade_to_draft", upgrade_to_draft_node)
    builder.add_node("classify", classify_node)
    builder.add_node("persist_ignore", persist_ignore_node)
    builder.add_node("persist_notify", persist_notify_node)
    builder.add_node("generate_draft", generate_draft_node)
    builder.add_node("persist_draft", persist_draft_node)
    builder.add_node("hitl", hitl_node)
    builder.add_node("apply_action", apply_action_node)
    builder.add_conditional_edges(
        START,
        lambda state: "check_upgrade" if state.get("upgrade") else "classify",
        {"check_upgrade": "check_upgrade", "classify": "classify"},
    )
    builder.add_edge("check_upgrade", "generate_draft")
    builder.add_edge("cancel_upgrade", END)
    builder.add_edge("upgrade_to_draft", "persist_draft")
    builder.add_conditional_edges(
        "classify",
        branch_after_classify,
        {
            "ignore": "persist_ignore",
            "notify": "persist_notify",
            "respond": "generate_draft",
        },
    )
    builder.add_edge("persist_ignore", END)
    builder.add_edge("persist_notify", END)
    builder.add_conditional_edges(
        "generate_draft",
        branch_after_generate,
        {
            "persist_draft": "persist_draft",
            "persist_notify": "persist_notify",
            "upgrade_to_draft": "upgrade_to_draft",
            "cancel_upgrade": "cancel_upgrade",
        },
    )
    builder.add_edge("persist_draft", "hitl")
    builder.add_edge("hitl", "apply_action")
    builder.add_conditional_edges(
        "apply_action",
        branch_after_apply,
        {"generate_draft": "generate_draft", "end": END},
    )
    return builder.compile(checkpointer=checkpointer)


def thread_config(email_id: int) -> dict:
    return {"configurable": {"thread_id": f"email-{email_id}"}}


def run_for_email(graph, email_id: int) -> dict:
    # A rebuilt business database can reuse email IDs while the checkpointer still
    # holds an older thread. Explicitly reset transient fields so automatic intake
    # can never inherit a stale manual-upgrade or rewrite state.
    return graph.invoke(
        {
            "email_id": email_id,
            "upgrade": False,
            "feedback": None,
            "draft": None,
        },
        config=thread_config(email_id),
    )


def upgrade_for_email(graph, email_id: int) -> dict:
    """NOTIFIED/IGNORED 邮件人工升级：生成草稿成功后才迁移到 DRAFT_PENDING。"""
    return graph.invoke(
        {"email_id": email_id, "upgrade": True},
        config=thread_config(email_id),
    )


def resume_for_email(graph, email_id: int, payload: dict) -> dict:
    return graph.invoke(Command(resume=payload), config=thread_config(email_id))
