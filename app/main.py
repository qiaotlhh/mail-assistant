"""FastAPI 启动入口：初始化存储、工作流、审核页与后台轮询。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.agent.drafter import Drafter
from app.agent.graph import build_graph, make_sqlite_checkpointer
from app.agent.router import RouteContext, RouteResult
from app.config import get_settings
from app.demo_runtime import DemoDrafter, DemoLLMRouter, DemoMailbox
from app.llm.provider import build_llm_router, build_providers
from app.mailbox.imap_client import IMAPClient
from app.mailbox.smtp_client import SMTPClient
from app.scheduler import EmailScheduler, SchedulerRunner
from app.store.db import init_db, make_engine, make_session_factory
from app.web.routes import ReviewService, create_review_router

logger = logging.getLogger(__name__)


def checkpoint_path_for_database(database_path: str | Path) -> Path:
    """Keep LangGraph threads scoped to one business database.

    Email IDs are only unique inside a business database, so reusing one
    checkpoint file across mail.db / m9.db would mix unrelated threads.
    """
    database = Path(database_path)
    return database.with_name(f"{database.stem}.checkpoint.db")


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    settings.ensure_ready()
    engine = make_engine(settings.database_path)
    init_db(engine)
    application.state.engine = engine
    application.state.session_factory = make_session_factory(engine)

    checkpoint_path = checkpoint_path_for_database(settings.database_path)
    checkpointer = make_sqlite_checkpointer(checkpoint_path)
    if settings.demo_mode:
        llm_router = DemoLLMRouter()
        drafter = DemoDrafter(providers=[])
    else:
        llm_router = build_llm_router(settings)
        drafter = Drafter(
            build_providers(settings),
            timeout=settings.llm_timeout_seconds,
        )
    alerts: list[str] = []

    def route_with_alerts(context: RouteContext) -> RouteResult:
        result = llm_router.route(context)
        if llm_router.last_alerts:
            alerts.extend(llm_router.last_alerts)
            del alerts[:-20]
        return result

    graph = build_graph(
        application.state.session_factory,
        llm_route=route_with_alerts,
        drafter=drafter,
        checkpointer=checkpointer,
        max_rewrite_rounds=settings.max_rewrite_rounds,
    )

    def fake_send(**kwargs) -> None:
        email_id = kwargs["email_id"]
        logger.warning(
            "M6 Fake 发送记录：email_id=%s to=%s subject=%s body_chars=%d",
            email_id,
            kwargs["recipient"],
            kwargs["subject"],
            len(kwargs["body"]),
        )

    sender = (
        fake_send
        if settings.demo_mode
        else SMTPClient(
            mail_address=settings.mail_address,
            auth_code=settings.mail_auth_code,
            host=settings.smtp_host,
            port=settings.smtp_port,
        )
    )

    application.state.llm_router = llm_router
    application.state.graph = graph
    mailbox = DemoMailbox() if settings.demo_mode else IMAPClient(settings)
    application.state.review_service = ReviewService(
        application.state.session_factory,
        graph,
        sender=sender,
        mark_seen=mailbox.mark_seen_many,
        max_rewrite_rounds=settings.max_rewrite_rounds,
        display_timezone=settings.display_timezone,
    )
    if not getattr(application.state, "review_router_installed", False):
        application.include_router(create_review_router(
            application.state.review_service,
            alerts=alerts,
        ))
        application.state.review_router_installed = True

    scheduler = EmailScheduler(
        mailbox=mailbox,
        session_factory=application.state.session_factory,
        graph=graph,
        fetch_limit=settings.mail_fetch_limit,
        max_body_chars=settings.max_email_body_chars,
        alerts=alerts,
    )
    runner = SchedulerRunner(scheduler, interval_seconds=settings.poll_interval_seconds)
    application.state.scheduler = scheduler
    application.state.scheduler_runner = runner

    logger.info("数据库初始化完成：%s", settings.database_path)
    logger.info(
        "配置校验通过：mail=%s，fetch_limit=%d，poll=%ds，providers=%s",
        settings.mail_address,
        settings.mail_fetch_limit,
        settings.poll_interval_seconds,
        settings.provider_order,
    )
    runner.start()
    try:
        yield
    finally:
        runner.stop()
        getattr(checkpointer, "conn", None).close()
        engine.dispose()


app = FastAPI(
    title="LangGraph 人机协同邮件智能助手",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": app.version,
        "mode": "demo" if settings.demo_mode else "normal",
        "mail_fetch_limit": settings.mail_fetch_limit,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "providers": [] if settings.demo_mode else settings.provider_order,
    }


@app.post("/actions/poll")
async def poll_now(request: Request):
    """人工触发一次收信；与后台轮询共用调度器防重入锁。"""
    scheduler = request.app.state.scheduler
    result = await asyncio.to_thread(scheduler.run_once)
    if result.skipped:
        message = "上一轮邮件处理尚未结束，本次未重复执行"
    elif result.errors:
        message = f"本轮收取完成，但有 {len(result.errors)} 个错误，请查看顶部告警"
    else:
        message = (
            f"本轮收取完成：新增 {result.stored_count} 封，"
            f"重复 {result.duplicate_count} 封，恢复 {result.recovered_count} 封"
        )
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


def main() -> None:
    """`python -m app.main` 启动本地服务，仅监听 127.0.0.1。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
