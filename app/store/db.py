"""SQLite 引擎与会话工厂：建库、建表与连接级 PRAGMA。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.store.models import Base


def make_engine(db_path: str | Path) -> Engine:
    """创建 SQLite 引擎；WAL 降低写阻塞，busy_timeout 缓解短时锁冲突。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _add_missing_email_columns(engine)


def _add_missing_email_columns(engine: Engine) -> None:
    """SQLite create_all does not alter existing tables; add nullable columns."""
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(emails)")
        }
    migrations = {
        "imap_uid": "ALTER TABLE emails ADD COLUMN imap_uid VARCHAR(255)",
        "read_at": "ALTER TABLE emails ADD COLUMN read_at DATETIME",
    }
    with engine.begin() as connection:
        for name, statement in migrations.items():
            if name not in columns:
                connection.execute(text(statement))
        # Records created before UID persistence used immediate mark-seen.
        connection.execute(
            text(
                "UPDATE emails SET read_at = updated_at "
                "WHERE imap_uid IS NULL AND read_at IS NULL"
            )
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_emails_imap_uid ON emails (imap_uid)")
        )


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
