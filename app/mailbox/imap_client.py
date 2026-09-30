"""QQ 邮箱 IMAP 客户端：按 UID 拉取未读与标记已读。"""

from __future__ import annotations

import imaplib
from dataclasses import dataclass

from app.config import Settings


class MailboxError(RuntimeError):
    """IMAP 连接 / 登录 / 操作失败的统一异常。"""


@dataclass(frozen=True)
class FetchedEmail:
    uid: str
    raw: bytes


class IMAPClient:
    def __init__(self, settings: Settings):
        self._settings = settings

    def _connect(self) -> imaplib.IMAP4_SSL:
        return imaplib.IMAP4_SSL(self._settings.imap_host, self._settings.imap_port)

    def fetch_unread(
        self,
        limit: int,
        exclude_uids: set[str] | None = None,
    ) -> list[FetchedEmail]:
        """拉取最多 limit 封未读；不改标志位，由人工确认后再 mark_seen。"""
        if limit < 1:
            raise ValueError("limit 必须为正整数")
        imap = self._connect()
        try:
            self._login(imap)
            self._check(imap.select("INBOX"), "选择收件箱失败")
            _, data = self._check(imap.uid("SEARCH", "UNSEEN"), "搜索未读失败")
            uids = data[0].split() if data and data[0] else []
            excluded = exclude_uids or set()
            uid_values = [uid.decode("ascii") for uid in uids]
            fetched: list[FetchedEmail] = []
            for uid in [uid for uid in uid_values if uid not in excluded][:limit]:
                _, message_data = self._check(
                    imap.uid("FETCH", uid, "(BODY.PEEK[])"),
                    f"拉取邮件失败 uid={uid}",
                )
                raw = _extract_rfc822(message_data)
                if raw:
                    fetched.append(FetchedEmail(uid=uid, raw=raw))
            return fetched
        except MailboxError:
            raise
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"IMAP 操作失败：{exc}") from exc
        except OSError as exc:
            raise MailboxError(f"IMAP 连接失败：{exc}") from exc
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def mark_seen(self, uid: str) -> None:
        """入库成功后调用；未标记会导致下一批仍返回同一批未读。"""
        imap = self._connect()
        try:
            self._login(imap)
            self._check(imap.select("INBOX"), "选择收件箱失败")
            self._check(
                imap.uid("STORE", uid, "+FLAGS", "(\\Seen)"),
                f"标记已读失败 uid={uid}",
            )
        except MailboxError:
            raise
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"IMAP 操作失败：{exc}") from exc
        except OSError as exc:
            raise MailboxError(f"IMAP 连接失败：{exc}") from exc
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def mark_seen_many(self, uids: list[str] | tuple[str, ...]) -> None:
        """Batch-mark UIDs seen in one IMAP session; empty input is a no-op."""
        unique_uids = list(dict.fromkeys(uids))
        invalid = [uid for uid in unique_uids if not uid.isdigit()]
        if invalid:
            raise ValueError(f"IMAP UID 必须是数字：{invalid[0]}")
        if not unique_uids:
            return
        imap = self._connect()
        try:
            self._login(imap)
            self._check(imap.select("INBOX"), "选择收件箱失败")
            self._check(
                imap.uid("STORE", ",".join(unique_uids), "+FLAGS", "(\\Seen)"),
                f"批量标记已读失败 uids={','.join(unique_uids)}",
            )
        except MailboxError:
            raise
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"IMAP 操作失败：{exc}") from exc
        except OSError as exc:
            raise MailboxError(f"IMAP 连接失败：{exc}") from exc
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    def _login(self, imap: imaplib.IMAP4_SSL) -> None:
        try:
            self._check(
                imap.login(self._settings.mail_address, self._settings.mail_auth_code),
                "IMAP 登录失败：请检查 MAIL_ADDRESS 与 MAIL_AUTH_CODE（授权码，非登录密码）",
            )
        except imaplib.IMAP4.error as exc:
            raise MailboxError(
                "IMAP 登录失败：请检查 MAIL_ADDRESS 与 MAIL_AUTH_CODE（授权码，非登录密码）"
            ) from exc

    @staticmethod
    def _check(result: tuple, context: str) -> tuple:
        status, data = result[0], result[1]
        if status != "OK":
            raise MailboxError(f"{context}：{status} {data!r}")
        return result


def _extract_rfc822(data) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and item[1]:
            return item[1]
    return None
