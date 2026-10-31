
"""她的邮箱：配置、发信、收信、命令。全程不打真实网络（全部打桩）。"""
from __future__ import annotations

import email.message
import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import mailbox as mb

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


def _config(**over) -> mb.MailConfig:
    base = dict(
        enabled=True,
        address="her@qq.com",
        password="授权码",
        owner="boss@qq.com",
    )
    base.update(over)
    return mb.MailConfig(**base).filled()


class ConfigTest(unittest.TestCase):
    def test_guess_servers(self) -> None:
        self.assertEqual(mb.guess_servers("a@qq.com")[0], "smtp.qq.com")
        self.assertEqual(mb.guess_servers("a@163.com")[2], "imap.163.com")
        self.assertEqual(mb.guess_servers("a@gmail.com")[1], 465)
        self.assertEqual(mb.guess_servers("a@company.cn"), ("", 0, "", 0))

    def test_filled_autocompletes_servers(self) -> None:
        conf = mb.MailConfig(enabled=True, address="her@qq.com", password="x").filled()
        self.assertEqual(conf.smtp_host, "smtp.qq.com")
        self.assertEqual(conf.smtp_port, 465)
        self.assertEqual(conf.imap_host, "imap.qq.com")
        self.assertEqual(conf.owner, "her@qq.com", "没填收件人就发给自己")

    def test_explicit_servers_win(self) -> None:
        conf = mb.MailConfig(address="her@qq.com", smtp_host="mail.example.com").filled()
        self.assertEqual(conf.smtp_host, "mail.example.com")
        self.assertEqual(conf.imap_host, "imap.qq.com")

    def test_masked_never_leaks_password(self) -> None:
        shown = mb.MailConfig(enabled=True, address="a@qq.com", password="secret").masked()
        self.assertNotIn("secret", json.dumps(shown, ensure_ascii=False))
        self.assertTrue(shown["has_password"])

    def test_ready_flags(self) -> None:
        self.assertTrue(_config().ready_for_send())
        self.assertTrue(_config().ready_for_read())
        self.assertFalse(_config(enabled=False).ready_for_send())
        self.assertFalse(_config(password="").ready_for_send())

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.json"
            conf = _config()
            self.assertTrue(mb.save_config(path, conf))
            self.assertEqual(mb.load_config(path).to_dict(), conf.to_dict())

    def test_broken_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.json"
            path.write_text("{oops", encoding="utf-8")
            self.assertFalse(mb.load_config(path).enabled)
            self.assertEqual(mb.load_config(Path(tmp) / "nope.json").to_dict(),
                             mb.MailConfig().to_dict())

    def test_bad_port_values_do_not_crash(self) -> None:
        conf = mb.MailConfig.from_dict({"smtp_port": "abc", "imap_port": None})
        self.assertEqual((conf.smtp_port, conf.imap_port), (0, 0))


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_auth = False

    def __init__(self, host: str, port: int, timeout: float = 0, context=None) -> None:
        self.host, self.port = host, port
        self.sent: list[email.message.EmailMessage] = []
        self.logged: list[tuple[str, str]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def starttls(self, **_kwargs) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")
        self.logged.append((user, password))

    def send_message(self, message) -> None:
        self.sent.append(message)


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeSMTP.instances = []
        FakeSMTP.fail_auth = False

    def test_send_builds_the_message(self) -> None:
        with mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
            ok, message = mb.Mailbox(_config()).send("标题", "正文内容")
        self.assertTrue(ok, message)
        server = FakeSMTP.instances[-1]
        self.assertEqual(server.host, "smtp.qq.com")
        self.assertEqual(server.logged, [("her@qq.com", "授权码")])
        mail = server.sent[-1]
        self.assertEqual(mail["To"], "boss@qq.com")
        self.assertEqual(mail["Subject"], "标题")
        self.assertIn("正文内容", mail.get_content())

    def test_send_without_config(self) -> None:
        ok, message = mb.Mailbox(mb.MailConfig()).send("x", "y")
        self.assertFalse(ok)
        self.assertIn("还没配好", message)

    def test_empty_body_rejected(self) -> None:
        with mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
            ok, message = mb.Mailbox(_config()).send("标题", "   ")
        self.assertFalse(ok)
        self.assertIn("正文", message)
        self.assertEqual(FakeSMTP.instances, [])

    def test_bad_recipient_rejected(self) -> None:
        ok, message = mb.Mailbox(_config(owner="not-an-address")).send("t", "body")
        self.assertFalse(ok)
        self.assertIn("收件地址", message)

    def test_auth_failure_says_auth_code(self) -> None:
        FakeSMTP.fail_auth = True
        with mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
            ok, message = mb.Mailbox(_config()).send("标题", "正文")
        self.assertFalse(ok)
        self.assertIn("授权码", message)


class FakeIMAP:
    messages: list[bytes] = []

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.host, self.port = host, port
        self.logged: list[tuple[str, str]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.logged.append((user, password))
        return "OK", [b""]

    def select(self, box: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def search(self, charset, criteria: str) -> tuple[str, list[bytes]]:
        ids = [str(i + 1).encode() for i in range(len(FakeIMAP.messages))]
        return "OK", [b" ".join(ids)]

    def fetch(self, uid, spec: str):
        index = int(uid) - 1
        return "OK", [(b"1 (RFC822 {n}", FakeIMAP.messages[index]), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]


def _raw_mail(subject: str, sender: str, body: str) -> bytes:
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Tue, 03 Nov 2026 10:00:00 +0800"
    message.set_content(body)
    return message.as_bytes()


class ReadTest(unittest.TestCase):
    def test_read_recent(self) -> None:
        FakeIMAP.messages = [
            _raw_mail("第一封", "a@qq.com", "早"),
            _raw_mail("第二封", "b@qq.com", "今天的作业好多"),
        ]
        with mock.patch.object(mb.imaplib, "IMAP4_SSL", FakeIMAP):
            items, err = mb.Mailbox(_config()).recent(2)
        self.assertEqual(err, "")
        self.assertEqual([item.subject for item in items], ["第二封", "第一封"], "最新的在前")
        self.assertEqual(items[0].sender, "b@qq.com")
        self.assertIn("作业", items[0].snippet)

    def test_empty_inbox(self) -> None:
        FakeIMAP.messages = []
        with mock.patch.object(mb.imaplib, "IMAP4_SSL", FakeIMAP):
            items, err = mb.Mailbox(_config()).recent()
        self.assertEqual((items, err), ([], ""))

    def test_unread_count(self) -> None:
        FakeIMAP.messages = [_raw_mail("x", "a@qq.com", "y")] * 3
        with mock.patch.object(mb.imaplib, "IMAP4_SSL", FakeIMAP):
            self.assertEqual(mb.Mailbox(_config()).unread_count(), 3)

    def test_read_without_config(self) -> None:
        items, err = mb.Mailbox(mb.MailConfig()).recent()
        self.assertEqual(items, [])
        self.assertIn("还没配好", err)

    def test_network_error_is_a_sentence(self) -> None:
        class Boom(FakeIMAP):
            def __init__(self, *a, **k):
                raise OSError("network down")

        with mock.patch.object(mb.imaplib, "IMAP4_SSL", Boom):
            items, err = mb.Mailbox(_config()).recent()
        self.assertEqual(items, [])
        self.assertIn("收信失败", err)

    def test_unread_count_swallows_errors(self) -> None:
        class Boom(FakeIMAP):
            def __init__(self, *a, **k):
                raise OSError("no network")

        with mock.patch.object(mb.imaplib, "IMAP4_SSL", Boom):
            self.assertEqual(mb.Mailbox(_config()).unread_count(), 0)


class PluginMailTest(PluginHarness, unittest.TestCase):
    def test_status_when_unconfigured(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_mail("#邮件")
            self.assertIn("还没配", text)
            self.assertIn("mail.json", text)
            status = await plugin._debug_status(FakeEvent("x", umo=PRIVATE), PRIVATE)
            self.assertIn("邮件：", status)

        self.run_async(scenario())

    def test_read_command_when_unconfigured(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_mail("#邮件 看")
            self.assertIn("还没配好", text)

        self.run_async(scenario())

    def test_configured_status_hides_password(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.mail = mb.Mailbox(_config())
            text = await plugin._debug_mail("#邮件")
            self.assertIn("her@qq.com", text)
            self.assertNotIn("授权码", text)
            self.assertNotIn("password", text)

        self.run_async(scenario())

    def test_send_from_chat(self) -> None:
        async def scenario() -> None:
            FakeSMTP.instances = []
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.mail = mb.Mailbox(_config())
            with mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
                text = await plugin._debug_mail("#邮件 发 今天好累，想你了")
            self.assertTrue(text.startswith("✅"), text)
            self.assertIn("今天好累，想你了", FakeSMTP.instances[-1].sent[-1].get_content())

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

