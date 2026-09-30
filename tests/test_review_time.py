"""审核页时间显示验收：UTC 存储值转换为配置时区。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import AuditLog, DraftRecord
from app.store.repo import create_email
from app.web.routes import ReviewService


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_review_service_displays_all_times_in_configured_timezone(tmp_path):
    engine = make_engine(tmp_path / "mail.db")
    init_db(engine)
    factory = make_session_factory(engine)
    received_at = datetime(2026, 9, 22, 16, 30, tzinfo=timezone.utc)

    with factory() as db:
        email = create_email(
            db,
            message_id="<time@example.com>",
            sender="alice@example.com",
            recipient="user@qq.com",
            subject="时间显示",
            body_text="请确认时间。",
            received_at=received_at,
        )
        db.add(
            DraftRecord(
                email_id=email.id,
                content="草稿",
                round_number=0,
                source="llm",
                created_at=datetime(2026, 9, 22, 16, 31, tzinfo=timezone.utc),
            )
        )
        db.add(
            AuditLog(
                email_id=email.id,
                action="create",
                detail_json="{}",
                created_at=datetime(2026, 9, 22, 16, 32, tzinfo=timezone.utc),
            )
        )
        db.commit()
        email_id = email.id
        raw_updated = email.updated_at
        with factory() as snapshot:
            record = snapshot.get(type(email), email_id)
            raw_draft = record.drafts[0].created_at
            raw_audit = record.audit_logs[-1].created_at

    service = ReviewService(
        factory,
        graph=None,
        sender=lambda **kwargs: None,
        display_timezone="Asia/Shanghai",
    )
    summary = service.list()[0]
    detail = service.get(email_id)
    tz = ZoneInfo("Asia/Shanghai")

    assert detail.received_at == received_at.astimezone(tz)
    assert detail.received_at.hour == 0
    assert summary.updated_at == _as_utc(raw_updated).astimezone(tz)
    assert detail.drafts[0].created_at == _as_utc(raw_draft).astimezone(tz)
    assert detail.audit_logs[-1].created_at == _as_utc(raw_audit).astimezone(tz)
    assert detail.drafts[0].created_at.hour == 0
    assert detail.audit_logs[-1].created_at.hour == 0
    engine.dispose()
