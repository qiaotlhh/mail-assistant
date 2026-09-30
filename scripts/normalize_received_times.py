"""Backfill legacy received_at values from original IMAP Date headers.

The old parser stored the Date header's wall-clock value without its UTC
offset. This script fetches only Date headers with BODY.PEEK, normalizes them
to UTC, and optionally writes corrections to the local database.
"""

from __future__ import annotations

import argparse
import imaplib
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import EmailRecord


def _utc_wall_clock(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _date_headers(imap: imaplib.IMAP4_SSL) -> dict[str, datetime]:
    status, data = imap.uid("SEARCH", "ALL")
    if status != "OK" or not data or not data[0]:
        return {}
    result: dict[str, datetime] = {}
    for uid in data[0].split():
        status, message_data = imap.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (DATE MESSAGE-ID)])"
        )
        if status != "OK":
            continue
        for item in message_data or []:
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            header = BytesParser(policy=policy.default).parsebytes(item[1])
            message_id = str(header.get("Message-ID") or "").strip()
            date_value = header.get("Date")
            if not message_id or not date_value:
                continue
            try:
                parsed = parsedate_to_datetime(str(date_value))
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result[message_id] = parsed.astimezone(timezone.utc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write corrections. Without this flag the script is a dry run.",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(Path(settings.database_path))
    init_db(engine)
    factory = make_session_factory(engine)

    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.mail_address, settings.mail_auth_code)
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法选择 INBOX")

        date_headers = _date_headers(imap)
        changed = 0
        with factory() as session:
            records = session.scalars(select(EmailRecord)).all()
            for record in records:
                if record.received_at is None:
                    continue
                original = date_headers.get(record.message_id)
                if original is None:
                    print(f"email_id={record.id}: 未找到原始 Date 头，跳过")
                    continue
                target = _utc_wall_clock(original)
                current = _utc_wall_clock(record.received_at)
                if current == target:
                    continue
                print(
                    f"email_id={record.id}: {current} -> {target}"
                    + ("" if args.write else "（dry run）")
                )
                record.received_at = target
                changed += 1
            if args.write and changed:
                session.commit()
        print(f"共 {changed} 条需要修正" + ("，已写入" if args.write else "，未写入"))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
