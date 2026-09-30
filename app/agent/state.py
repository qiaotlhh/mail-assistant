"""LangGraph 工作流状态。业务数据以 SQLite 业务表为准，状态只承载流程推进所需字段。"""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    email_id: int
    upgrade: bool
    category: str
    route_reason: str
    route_source: str
    confidence: float
    draft: str
    feedback: str
    rewrite_rounds: int
    pending_action: dict
    error: str | None
