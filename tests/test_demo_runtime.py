"""离线演示数据与配置验证。"""

from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.graph import build_graph
from app.config import get_settings
from app.demo_runtime import DemoDrafter, DemoLLMRouter, seed_demo_data
from app.store.db import init_db, make_engine, make_session_factory


def test_demo_mode_does_not_require_real_credentials():
    settings = get_settings()
    assert settings.demo_mode is False
    object.__setattr__(settings, "demo_mode", True)
    try:
        settings.ensure_ready()
    finally:
        object.__setattr__(settings, "demo_mode", False)


def test_demo_seed_covers_realistic_statuses(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(tmp_path / "checkpoint.db", check_same_thread=False)
    router = DemoLLMRouter()
    graph = build_graph(
        factory,
        llm_route=router.route,
        drafter=DemoDrafter(providers=[]),
        checkpointer=SqliteSaver(conn),
    )

    statuses = seed_demo_data(factory, graph)
    repeated = seed_demo_data(factory, graph)

    assert statuses == repeated
    assert set(statuses.values()) == {
        "ignored",
        "notified",
        "draft_pending",
        "sent_done",
        "send_failed",
    }
    conn.close()
    engine.dispose()
