"""向本地 SQLite 写入网页演示数据。"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.graph import build_graph
from app.demo_runtime import DemoDrafter, DemoLLMRouter, seed_demo_data
from app.store.db import init_db, make_engine, make_session_factory


def main() -> None:
    parser = argparse.ArgumentParser(description="生成邮件审核页离线演示数据")
    parser.add_argument(
        "--database",
        default="data/mail.db",
        help="业务数据库路径，默认 data/mail.db",
    )
    args = parser.parse_args()

    database_path = Path(args.database)
    checkpoint_path = database_path.with_name("checkpoint.db")
    engine = make_engine(database_path)
    init_db(engine)
    factory = make_session_factory(engine)
    conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
    try:
        router = DemoLLMRouter()
        graph = build_graph(
            factory,
            llm_route=router.route,
            drafter=DemoDrafter(providers=[]),
            checkpointer=SqliteSaver(conn),
        )
        statuses = seed_demo_data(factory, graph)
    finally:
        conn.close()
        engine.dispose()

    print("演示数据状态：")
    for message_id, status in statuses.items():
        print(f"- {message_id}: {status}")


if __name__ == "__main__":
    main()
