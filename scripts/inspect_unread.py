"""Inspect unread QQ mailbox emails without entering the agent workflow."""

from __future__ import annotations

import argparse
import imaplib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from app.mailbox.imap_client import IMAPClient, MailboxError
from app.mailbox.parser import parse_email


def _search_all_unread(settings: Settings) -> list[str]:
    """Return every UID currently reported by IMAP SEARCH UNSEEN in INBOX, and print diagnostics for all folders."""
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.mail_address, settings.mail_auth_code)
        status, select_data = imap.select("INBOX")
        if status != "OK":
            raise RuntimeError(f"select INBOX failed: {status} {select_data!r}")

        print("IMAP SELECT 返回:", status, select_data)

        # 4. 检查 INBOX 总邮件数
        status, all_data = imap.uid("SEARCH", "ALL")
        if status == "OK":
            all_uids = all_data[0].split() if all_data and all_data[0] else []
            print(f"INBOX 总邮件数: {len(all_uids)}")
        else:
            print(f"INBOX SEARCH ALL 失败: {status} {all_data!r}")

        status, search_data = imap.uid("SEARCH", "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"SEARCH UNSEEN failed: {status} {search_data!r}")

        print("IMAP SEARCH UNSEEN 原始返回:", search_data)
        raw_uids = search_data[0] if search_data and search_data[0] else b""
        unseen_uids = [uid.decode("ascii") for uid in raw_uids.split()]
        print(f"INBOX 未读 UID 总数: {len(unseen_uids)}")
        print("INBOX 未读 UID:", ",".join(unseen_uids) if unseen_uids else "<无>")

        # 3. 遍历所有文件夹，统计未读
        print("\n--- 所有文件夹未读统计 ---")
        status, folders = imap.list()
        if status == "OK":
            for folder_info in folders:
                if not folder_info:
                    continue
                # 解析文件夹名，例如 b'(\\HasNoChildren) "/" "INBOX"'
                m = re.search(rb'"([^"]*)"\s*$', folder_info)
                if not m:
                    continue
                folder_name = m.group(1).decode("ascii")  # modified UTF-7 编码的 ASCII 字符串

                # 跳过不可选择的文件夹
                if b"\\Noselect" in folder_info:
                    print(f"文件夹: {folder_name} (不可选择，跳过)")
                    continue

                try:
                    st, _ = imap.select(folder_name, readonly=True)
                    if st != "OK":
                        print(f"文件夹: {folder_name} select 失败: {st}")
                        continue

                    st, data = imap.uid("SEARCH", "UNSEEN")
                    if st == "OK":
                        uids = data[0].split() if data and data[0] else []
                        print(f"文件夹: {folder_name} 未读数: {len(uids)}")
                    else:
                        print(f"文件夹: {folder_name} SEARCH UNSEEN 失败: {st} {data!r}")
                except Exception as e:
                    print(f"文件夹: {folder_name} 处理异常: {e}")
        else:
            print(f"LIST 文件夹失败: {status} {folders!r}")

        # 重新 select INBOX 以恢复状态（可选，因为即将 logout）
        imap.select("INBOX")

        return unseen_uids
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _print_email(uid: str, raw: bytes, body_chars: int) -> None:
    email = parse_email(raw, max_body_chars=max(body_chars, 8000))
    body = email.body_text.replace("\r", "").strip()
    if body_chars > 0:
        shown_body = body[:body_chars]
        omitted = len(body) - len(shown_body)
        if omitted > 0:
            shown_body += f"\n...（省略 {omitted} 字符）"
    else:
        shown_body = "<已省略正文>"

    print()
    print(f"UID: {uid}")
    print(f"Message-ID: {email.message_id}")
    print(f"Date(UTC): {email.received_at.isoformat() if email.received_at else '<空>'}")
    print(f"From: {email.sender}")
    print(f"To: {email.recipient}")
    print(f"Reply-To: {email.reply_to or '<空>'}")
    print(f"Subject: {email.subject}")
    print(f"关键头: {email.headers or '{}'}")
    print(f"原始邮件字节数: {len(raw)}")
    print(f"正文（已转纯文本）:\n{shown_body}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读诊断 QQ 邮箱未读邮件；不标已读、不入库、不调用 LLM、不发送。"
    )
    parser.add_argument("--limit", type=int, default=10, help="最多拉取多少封，默认 10")
    parser.add_argument(
        "--body-chars",
        type=int,
        default=240,
        help="每封邮件显示多少个正文字符；0 表示不显示正文",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须为正整数")
    if args.body_chars < 0:
        parser.error("--body-chars 不能为负数")

    settings = Settings(_env_file=PROJECT_ROOT / ".env")
    if not settings.mail_address or not settings.mail_auth_code:
        print("配置缺失：请在项目根目录的 .env 中设置 MAIL_ADDRESS 和 MAIL_AUTH_CODE")
        return 2

    print(f"账号: {settings.mail_address}")
    print(f"IMAP: {settings.imap_host}:{settings.imap_port}")
    print(f"拉取上限: {args.limit}")
    try:
        uids = _search_all_unread(settings)
        print(f"\n服务器报告的 INBOX 未读 UID 总数: {len(uids)}")
        print("全部 INBOX 未读 UID:", ",".join(uids) if uids else "<无>")

        fetched = IMAPClient(settings).fetch_unread(args.limit)
        print(f"\nBODY.PEEK[] 实际取回并解析成功的邮件数: {len(fetched)}")
        for index, item in enumerate(fetched, start=1):
            print(f"\n========== 第 {index}/{len(fetched)} 封 ==========")
            _print_email(item.uid, item.raw, args.body_chars)
    except (MailboxError, imaplib.IMAP4.error, OSError, RuntimeError) as exc:
        print(f"\n诊断失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())