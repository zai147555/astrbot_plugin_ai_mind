
"""面板后端：模仿 / 邮箱 / 今天（本轮新加的三个接口）。"""
from __future__ import annotations

import unittest

from mind import mailbox as mb

from tests.test_mailbox import FakeIMAP, FakeSMTP, _config
from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class ImitateApiTest(PluginHarness, unittest.TestCase):
    def test_get_returns_settings_and_profiles(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "imitate"))["data"]
            self.assertIn("settings", data)
            self.assertFalse(data["settings"]["enabled"], "默认是关的")
            self.assertEqual(data["profiles"], [])

        self.run_async(scenario())

    def test_save_then_capture_shows_up(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            saved = (await self.api(plugin, "imitate/save", body={
                "enabled": True, "inject": True, "min_samples": 3,
                "targets": [OWNER],
            }))["data"]
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["settings"]["targets"], [OWNER])
            for text in ("行吧", "就是就是啊", "真的假的啊？"):
                await plugin.on_imitate_capture(FakeEvent(text, umo=PRIVATE, uid=OWNER))
            data = (await self.api(plugin, "imitate"))["data"]
            row = [item for item in data["profiles"] if item["uid"] == OWNER][0]
            self.assertEqual(row["samples"], 3)
            self.assertTrue(row["target"])
            self.assertTrue(row["lines"], "面板要能直接显示话风")

        self.run_async(scenario())

    def test_targets_accept_a_string(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "imitate/save", body={
                "targets": "2020689845，42 999",
            }))["data"]
            self.assertEqual(data["settings"]["targets"], ["2020689845", "42", "999"])

        self.run_async(scenario())

    def test_clear_all_and_one(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.api(plugin, "imitate/save", body={"enabled": True, "targets": [OWNER]})
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            one = (await self.api(plugin, "imitate/clear", body={"uid": OWNER}))["data"]
            self.assertEqual(one["removed"], 1)
            self.assertEqual(one["stats"]["samples"], 0)

        self.run_async(scenario())

    def test_settings_persist_to_disk(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.api(plugin, "imitate/save", body={"enabled": True, "targets": [OWNER]})
            self.assertTrue((plugin.data_dir / "imitate.json").is_file())

        self.run_async(scenario())


class MailApiTest(PluginHarness, unittest.TestCase):
    def test_status_hides_password(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "mail"))["data"]
            self.assertFalse(data["enabled"])
            self.assertNotIn("password", data, "面板接口里绝不能出现授权码")

        self.run_async(scenario())

    def test_save_keeps_old_password_when_blank(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.api(plugin, "mail/save", body={
                "enabled": True, "address": "her@qq.com", "password": "授权码", "owner": "boss@qq.com",
            })
            self.assertEqual(plugin.mail.config.password, "授权码")
            data = (await self.api(plugin, "mail/save", body={
                "enabled": True, "address": "her@qq.com", "password": "",
            }))["data"]
            self.assertEqual(plugin.mail.config.password, "授权码", "留空不该把授权码抹掉")
            self.assertNotIn("password", data)

        self.run_async(scenario())

    def test_save_autofills_servers_and_writes_file(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "mail/save", body={
                "enabled": True, "address": "her@163.com", "password": "x",
            }))["data"]
            self.assertEqual(data["smtp"], "smtp.163.com")
            self.assertEqual(data["imap"], "imap.163.com")
            self.assertTrue((plugin.data_dir / "mail.json").is_file())

        self.run_async(scenario())

    def test_inbox_and_send_use_the_stubbed_network(self) -> None:
        async def scenario() -> None:
            from tests.test_mailbox import _raw_mail

            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.mail = mb.Mailbox(_config())
            FakeIMAP.messages = [_raw_mail("来信", "a@qq.com", "今天怎么样")]
            with unittest.mock.patch.object(mb.imaplib, "IMAP4_SSL", FakeIMAP):
                data = (await self.api(plugin, "mail/inbox", body={"limit": 1}))["data"]
            self.assertEqual(data["error"], "")
            self.assertEqual(data["items"][0]["subject"], "来信")

            FakeSMTP.instances = []
            with unittest.mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
                sent = (await self.api(plugin, "mail/send", body={
                    "subject": "在吗", "body": "今天的作业写完了",
                }))["data"]
            self.assertTrue(sent["ok"], sent)
            self.assertIn("作业", FakeSMTP.instances[-1].sent[-1].get_content())

        self.run_async(scenario())

    def test_send_requires_body(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.mail = mb.Mailbox(_config())
            data = (await self.api(plugin, "mail/send", body={"body": "  "}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())

    def test_test_button(self) -> None:
        async def scenario() -> None:
            FakeSMTP.instances = []
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.mail = mb.Mailbox(_config())
            with unittest.mock.patch.object(mb.smtplib, "SMTP_SSL", FakeSMTP):
                data = (await self.api(plugin, "mail/test"))["data"]
            self.assertTrue(data["ok"], data)

        self.run_async(scenario())


class DailyApiTest(PluginHarness, unittest.TestCase):
    def test_daily_shape(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "daily"))["data"]
            self.assertIn("day", data)
            self.assertIn("slot", data)
            self.assertTrue(data["day"]["date"])
            self.assertIn(data["day"]["kind"],
                          ("school", "weekend", "holiday", "vacation", "makeup"))
            self.assertTrue(data["spans"], "今年的假期表要能画出来")
            for span in data["spans"]:
                self.assertGreaterEqual(span["days"], 1)
                self.assertIn("name", span)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

