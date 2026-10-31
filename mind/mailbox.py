
"""她的邮箱：收邮件、发邮件。

用标准库（smtplib / imaplib / email），不引第三方依赖。

授权码只从 data/mail.json 读，**永远不进聊天、不进日志、不显示**：
命令和状态里只看得到「已配置 / 未配置」。

    {
      "enabled": true,
      "address": "xxx@qq.com",
      "password": "SMTP 授权码（不是登录密码）",
      "owner": "发信默认发给谁",
      "smtp_host": "", "smtp_port": 0, "smtp_ssl": true,
      "imap_host": "", "imap_port": 0, "imap_ssl": true
    }

服务器地址留空就按域名自动猜（qq / 163 / 126 / gmail / outlook / sina）。
"""
from __future__ import annotations

import email
import imaplib
import json
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path
from typing import Any

#: 常见邮箱的服务器（猜错了可以在 mail.json 里手填）
SERVERS: dict[str, tuple[str, int, str, int]] = {
    "qq.com": ("smtp.qq.com", 465, "imap.qq.com", 993),
    "foxmail.com": ("smtp.qq.com", 465, "imap.qq.com", 993),
    "163.com": ("smtp.163.com", 465, "imap.163.com", 993),
    "126.com": ("smtp.126.com", 465, "imap.126.com", 993),
    "gmail.com": ("smtp.gmail.com", 465, "imap.gmail.com", 993),
    "outlook.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "hotmail.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "sina.com": ("smtp.sina.com", 465, "imap.sina.com", 993),
}

MAX_SUBJECT = 120
MAX_BODY = 4000
TIMEOUT = 20


def guess_servers(address: str) -> tuple[str, int, str, int]:
    """按邮箱域名猜服务器；猜不到给空串（配置里手填）。"""
    domain = str(address or "").split("@")[-1].strip().lower()
    return SERVERS.get(domain, ("", 0, "", 0))


@dataclass
class MailConfig:
    enabled: bool = False
    address: str = ""
    password: str = ""
    owner: str = ""
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_ssl: bool = True
    imap_host: str = ""
    imap_port: int = 0
    imap_ssl: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "address": self.address,
            "password": self.password,
            "owner": self.owner,
            "smtp_host": self.smtp_host,
            "smtp_port": int(self.smtp_port or 0),
            "smtp_ssl": bool(self.smtp_ssl),
            "imap_host": self.imap_host,
            "imap_port": int(self.imap_port or 0),
            "imap_ssl": bool(self.imap_ssl),
        }

    def filled(self) -> "MailConfig":
        """服务器没填就按域名补齐（返回新对象，不改自己）。"""
        smtp_host, smtp_port, imap_host, imap_port = guess_servers(self.address)
        return MailConfig(
            enabled=self.enabled,
            address=self.address,
            password=self.password,
            owner=self.owner or self.address,
            smtp_host=self.smtp_host or smtp_host,
            smtp_port=int(self.smtp_port or smtp_port),
            smtp_ssl=bool(self.smtp_ssl),
            imap_host=self.imap_host or imap_host,
            imap_port=int(self.imap_port or imap_port),
            imap_ssl=bool(self.imap_ssl),
        )

    @staticmethod
    def from_dict(data: Any) -> "MailConfig":
        raw = data if isinstance(data, dict) else {}
        try:
            smtp_port = int(raw.get("smtp_port") or 0)
        except (TypeError, ValueError):
            smtp_port = 0
        try:
            imap_port = int(raw.get("imap_port") or 0)
        except (TypeError, ValueError):
            imap_port = 0
        return MailConfig(
            enabled=bool(raw.get("enabled")),
            address=str(raw.get("address") or "").strip(),
            password=str(raw.get("password") or "").strip(),
            owner=str(raw.get("owner") or "").strip(),
            smtp_host=str(raw.get("smtp_host") or "").strip(),
            smtp_port=smtp_port,
            smtp_ssl=bool(raw.get("smtp_ssl", True)),
            imap_host=str(raw.get("imap_host") or "").strip(),
            imap_port=imap_port,
            imap_ssl=bool(raw.get("imap_ssl", True)),
        )

    def masked(self) -> dict[str, Any]:
        """给命令/面板看的样子：密码只有"有没有"，没有本体。"""
        return {
            "enabled": self.enabled,
            "address": self.address,
            "has_password": bool(self.password),
            "owner": self.owner,
            "smtp": self.smtp_host,
            "imap": self.imap_host,
        }

    def ready_for_send(self) -> bool:
        return bool(self.enabled and self.address and self.password and self.smtp_host)

    def ready_for_read(self) -> bool:
        return bool(self.enabled and self.address and self.password and self.imap_host)


def load_config(path: Path | str) -> MailConfig:
    try:
        return MailConfig.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return MailConfig()


def save_config(path: Path | str, config: MailConfig) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError):
        return False


def _decode(raw: Any) -> str:
    """邮件头解码（=?utf-8?b?...?= 那种）。"""
    if raw is None:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(str(raw)):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", "replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode("utf-8", "replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def _snippet(message: email.message.Message, limit: int = 60) -> str:
    try:
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    return " ".join(text.split())[:limit]
            return ""
        payload = message.get_payload(decode=True) or b""
        text = payload.decode(message.get_content_charset() or "utf-8", "replace")
        return " ".join(text.split())[:limit]
    except Exception:  # noqa: BLE001 - 解析不出来的邮件就只显示标题
        return ""


@dataclass
class MailItem:
    subject: str
    sender: str
    date: str
    unread: bool
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date,
            "unread": self.unread,
            "snippet": self.snippet,
        }


class Mailbox:
    """收发实现。所有异常都变成"人话错误"，绝不往聊天里抛。"""

    def __init__(self, config: MailConfig | None = None, logger: Any = None) -> None:
        self.config = (config or MailConfig()).filled()
        self.logger = logger

    @property
    def address(self) -> str:
        return self.config.address

    def status(self) -> dict[str, Any]:
        return {
            **self.config.masked(),
            "send_ready": self.config.ready_for_send(),
            "read_ready": self.config.ready_for_read(),
        }

    # -- 发 ---------------------------------------------------------------
    def send(self, subject: str, body: str, to: str = "") -> tuple[bool, str]:
        conf = self.config
        if not conf.ready_for_send():
            return False, "邮箱还没配好（要 address / password / smtp_host）"
        target = (to or conf.owner or conf.address).strip()
        if not target or "@" not in target:
            return False, "收件地址不对：" + target
        title = (subject or "（无主题）").strip()[:MAX_SUBJECT]
        content = (body or "").strip()[:MAX_BODY]
        if not content:
            return False, "正文是空的"

        message = EmailMessage()
        message["From"] = conf.address
        message["To"] = target
        message["Subject"] = title
        message["Date"] = formatdate(localtime=True)
        message.set_content(content)

        try:
            if conf.smtp_ssl:
                server = smtplib.SMTP_SSL(
                    conf.smtp_host, int(conf.smtp_port), timeout=TIMEOUT,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(conf.smtp_host, int(conf.smtp_port), timeout=TIMEOUT)
                server.starttls(context=ssl.create_default_context())
            with server:
                server.login(conf.address, conf.password)
                server.send_message(message)
            return True, "发出去了 → " + target
        except smtplib.SMTPAuthenticationError:
            return False, "登录被拒：password 要填「授权码」，不是邮箱密码"
        except (smtplib.SMTPException, socket.error, ssl.SSLError, OSError) as exc:
            return False, "发信失败：%s: %s" % (type(exc).__name__, exc)

    # -- 收 ---------------------------------------------------------------
    def recent(self, limit: int = 5, unread_only: bool = False) -> tuple[list[MailItem], str]:
        conf = self.config
        if not conf.ready_for_read():
            return [], "邮箱还没配好（要 address / password / imap_host）"
        count = max(1, min(20, int(limit or 5)))
        client = None
        try:
            if conf.imap_ssl:
                client = imaplib.IMAP4_SSL(conf.imap_host, int(conf.imap_port), timeout=TIMEOUT)
            else:
                client = imaplib.IMAP4(conf.imap_host, int(conf.imap_port))
            client.login(conf.address, conf.password)
            client.select("INBOX", readonly=True)
            criteria = "UNSEEN" if unread_only else "ALL"
            status, data = client.search(None, criteria)
            if status != "OK":
                return [], "收件箱读不出来"
            ids = (data[0] or b"").split()
            if not ids:
                return [], ""
            items: list[MailItem] = []
            for uid in reversed(ids[-count:]):
                ok, payload = client.fetch(uid, "(RFC822)")
                if ok != "OK" or not payload:
                    continue
                raw = b""
                for chunk in payload:
                    if isinstance(chunk, tuple) and len(chunk) > 1:
                        raw = chunk[1]
                        break
                if not raw:
                    continue
                message = email.message_from_bytes(raw)
                sender = parseaddr(_decode(message.get("From")))[0] or _decode(message.get("From"))
                items.append(
                    MailItem(
                        subject=_decode(message.get("Subject")) or "（无主题）",
                        sender=sender,
                        date=_decode(message.get("Date")),
                        unread=False,
                        snippet=_snippet(message),
                    )
                )
            return items, ""
        except imaplib.IMAP4.error as exc:
            return [], "收信登录失败：%s（多半也是授权码的问题）" % exc
        except (socket.error, ssl.SSLError, OSError) as exc:
            return [], "收信失败：%s: %s" % (type(exc).__name__, exc)
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    pass

    def unread_count(self) -> int:
        conf = self.config
        if not conf.ready_for_read():
            return 0
        client = None
        try:
            if conf.imap_ssl:
                client = imaplib.IMAP4_SSL(conf.imap_host, int(conf.imap_port), timeout=TIMEOUT)
            else:
                client = imaplib.IMAP4(conf.imap_host, int(conf.imap_port))
            client.login(conf.address, conf.password)
            client.select("INBOX", readonly=True)
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                return 0
            return len((data[0] or b"").split())
        except Exception:  # noqa: BLE001 - 未读数只是个装饰，失败了就当 0
            return 0
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    pass


__all__ = [
    "SERVERS", "MailConfig", "MailItem", "Mailbox",
    "guess_servers", "load_config", "save_config",
]

