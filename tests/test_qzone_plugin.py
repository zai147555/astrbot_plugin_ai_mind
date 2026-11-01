"""QQ 空间接进插件：写日记、配图、发说说、面板接口、定时任务。"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"q" * 48
DIARY = '{"title": "放学", "body": "下 午 很 热 ， 我 没 出 门 。", "mood": "闷闷的"}'


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.tid = "tid-1"
        self.feeds: dict = {"status": "ok", "data": {"msglist": []}}
        self.profile: dict = {"status": "ok", "data": {"uin": "123", "nickname": "杜可怡"}}

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.fail:
            raise RuntimeError("空间接口不可用")
        if action == "send_qzone_msg":
            return {"status": "ok", "retcode": 0, "data": {"tid": self.tid, "time": 1000}}
        if action == "get_qzone_msg_list":
            return self.feeds
        if action == "get_qzone_profile":
            return self.profile
        return {"status": "ok"}


class FakeBot:
    def __init__(self) -> None:
        self.api = FakeApi()


class BotEvent(FakeEvent):
    def __init__(self, text="#日记", umo=PRIVATE, uid=OWNER) -> None:
        super().__init__(text, umo=umo, uid=uid)
        self.bot = FakeBot()


class QzoneBase(PluginHarness):
    def seed(self, plugin, **kwargs) -> int:
        params = {"title": "今天", "body": "下 午 很 热", "mood": "闷闷的"}
        params.update(kwargs)
        return plugin.diaries.add(**params)


class PanelTest(QzoneBase, unittest.TestCase):
    def test_payload_empty(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "qzone"))["data"]
            self.assertIn("settings", data)
            self.assertEqual(data["stats"]["total"], 0)
            self.assertIn("moods", data)
            self.assertTrue(data["settings"]["ugc_rights"])

        self.run_async(scenario())

    def test_save_settings(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "qzone/save", body={
                "auto_post": True, "ugc_right": 64, "auto_hour": 8,
                "auto_minute": 30, "signature": "——可怡", "with_image": False,
            }))["data"]
            self.assertTrue(data["saved"])
            settings = data["settings"]
            self.assertTrue(settings["auto_post"])
            self.assertEqual(settings["ugc_right"], 64)
            self.assertEqual(settings["auto_hour"], 8)
            self.assertEqual(settings["auto_minute"], 30)
            self.assertFalse(settings["with_image"])
            saved = json.loads((Path(plugin.data_dir) / "qzone.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["auto_post"])

        self.run_async(scenario())

    def test_save_clamps_junk(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "qzone/save", body={
                "ugc_right": 777, "auto_hour": 99, "max_chars": 5,
            }))["data"]
            self.assertEqual(data["settings"]["ugc_right"], 1)
            self.assertEqual(data["settings"]["auto_hour"], 23)
            self.assertEqual(data["settings"]["max_chars"], 80)

        self.run_async(scenario())

    def test_write_creates_draft(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = DIARY
            data = (await self.api(plugin, "qzone/write", body={}))["data"]
            self.assertTrue(data["ok"])
            entry = data["entry"]
            self.assertEqual(entry["title"], "放学")
            self.assertEqual(entry["body"], "下午很热，我没出门。")
            self.assertEqual(entry["status"], "draft")
            self.assertEqual(data["stats"]["drafts"], 1)
            self.assertIn("14 岁", self.provider.prompts[-1] + self._system_prompt(plugin))

        self.run_async(scenario())

    @staticmethod
    def _system_prompt(plugin) -> str:
        from mind import qzone

        return qzone.DIARY_SYSTEM

    def test_write_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.qzone_settings.enabled = False
            data = (await self.api(plugin, "qzone/write", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("关", data["error"])

        self.run_async(scenario())

    def test_write_without_provider(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = ""
            data = (await self.api(plugin, "qzone/write", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("正文", data["error"])

        self.run_async(scenario())

    def test_delete(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            data = (await self.api(plugin, "qzone/delete", body={"id": ident}))["data"]
            self.assertTrue(data["done"])
            self.assertEqual(data["stats"]["total"], 0)

        self.run_async(scenario())


class PublishTest(QzoneBase, unittest.TestCase):
    def test_publish_success(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            event = BotEvent()
            result = await plugin._qzone_publish(ident, event)
            self.assertTrue(result["ok"])
            self.assertEqual(result["tid"], "tid-1")
            entry = plugin.diaries.get(ident)
            self.assertEqual(entry.status, "published")
            action, params = event.bot.api.calls[0]
            self.assertEqual(action, "send_qzone_msg")
            self.assertIn("下午很热", params["content"])
            self.assertEqual(params["ugc_right"], 1)
            self.assertNotIn("images", params)

        self.run_async(scenario())

    def test_publish_with_image_base64(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "a.png"
                image.write_bytes(PNG)
                ident = self.seed(plugin, image=str(image))
                event = BotEvent()
                result = await plugin._qzone_publish(ident, event)
            self.assertTrue(result["ok"])
            _, params = event.bot.api.calls[0]
            self.assertTrue(params["images"][0].startswith("base64://"))

        self.run_async(scenario())

    def test_publish_failure_marks_failed(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            event = BotEvent()
            event.bot.api.fail = True
            result = await plugin._qzone_publish(ident, event)
            self.assertFalse(result["ok"])
            self.assertIn("发送失败", result["error"])
            self.assertEqual(plugin.diaries.get(ident).status, "failed")

        self.run_async(scenario())

    def test_publish_rejects_duplicate(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            plugin.diaries.mark_published(ident, "tid-x")
            result = await plugin._qzone_publish(ident, BotEvent())
            self.assertFalse(result["ok"])
            self.assertIn("发过", result["error"])

        self.run_async(scenario())

    def test_publish_without_bot(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            result = await plugin._qzone_publish(ident)
            self.assertFalse(result["ok"])
            self.assertIn("QQ", result["error"])

        self.run_async(scenario())

    def test_publish_without_draft(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            result = await plugin._qzone_publish(0, BotEvent())
            self.assertFalse(result["ok"])
            self.assertIn("没有", result["error"])

        self.run_async(scenario())

    def test_publish_uses_signature_and_permission(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.qzone_settings.signature = "——可怡"
            plugin.qzone_settings.ugc_right = 64
            ident = self.seed(plugin)
            event = BotEvent()
            await plugin._qzone_publish(ident, event)
            _, params = event.bot.api.calls[0]
            self.assertTrue(params["content"].endswith("——可怡"))
            self.assertEqual(params["ugc_right"], 64)

        self.run_async(scenario())


class RefreshTest(QzoneBase, unittest.TestCase):
    def test_refresh_updates_stats(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            plugin.diaries.mark_published(ident, "tid-9")
            api = FakeApi()
            api.feeds = {"status": "ok", "data": {"msglist": [{
                "tid": "tid-9", "content": "x", "likes": [1, 2, 3], "commentlist": [{}],
            }]}}
            plugin._qzone_api_cache = api
            data = (await self.api(plugin, "qzone/refresh", body={}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["count"], 1)
            entry = plugin.diaries.get(ident)
            self.assertEqual(entry.likes, 3)
            self.assertEqual(entry.comments, 1)

        self.run_async(scenario())

    def test_refresh_without_api(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "qzone/refresh", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("QQ", data["error"])

        self.run_async(scenario())


class CommandTest(QzoneBase, unittest.TestCase):
    def test_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_qzone(FakeEvent("#日记", umo=PRIVATE), "#日记")
            self.assertIn("空间", text)
            self.assertIn("日记", text)
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_write_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = DIARY
            text = await plugin._debug_qzone(BotEvent("#日记 写"), "#日记 写")
            self.assertIn("写好了", text)
            self.assertIn("放学", text)
            self.assertEqual(plugin.diaries.stats()["drafts"], 1)

        self.run_async(scenario())

    def test_publish_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            text = await plugin._debug_qzone(BotEvent(), f"#日记 发 {ident}")
            self.assertIn("已发到空间", text)
            self.assertEqual(plugin.diaries.get(ident).status, "published")

        self.run_async(scenario())

    def test_delete_command_and_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self.seed(plugin)
            text = await plugin._debug_qzone(FakeEvent("#日记"), f"#日记 删 {ident}")
            self.assertIn("删掉了", text)
            bad = await plugin._debug_qzone(FakeEvent("#日记"), "#日记 删")
            self.assertIn("用法", bad)

        self.run_async(scenario())

    def test_refresh_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._qzone_api_cache = FakeApi()
            text = await plugin._debug_qzone(FakeEvent("#日记"), "#日记 刷新")
            self.assertIn("0 条", text)

        self.run_async(scenario())


class DailyTest(QzoneBase, unittest.TestCase):
    def test_auto_off_does_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            api = FakeApi()
            plugin._qzone_api_cache = api
            await plugin._qzone_daily_tick()
            self.assertEqual(api.calls, [])
            self.assertEqual(plugin.diaries.stats()["total"], 0)

        self.run_async(scenario())

    def test_auto_on_writes_and_publishes(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = DIARY
            plugin.qzone_settings.auto_post = True
            plugin.qzone_settings.auto_hour = 0
            plugin.qzone_settings.auto_minute = 0
            api = FakeApi()
            plugin._qzone_api_cache = api
            await plugin._qzone_daily_tick()
            stats = plugin.diaries.stats()
            self.assertEqual(stats["published"], 1)
            self.assertIn("send_qzone_msg", [call[0] for call in api.calls])
            state = Path(plugin.data_dir) / "qzone" / "last_run.json"
            self.assertTrue(state.is_file())

        self.run_async(scenario())

    def test_auto_runs_once_per_day(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = DIARY
            plugin.qzone_settings.auto_post = True
            plugin.qzone_settings.auto_hour = 0
            plugin.qzone_settings.auto_minute = 0
            plugin._qzone_api_cache = FakeApi()
            await plugin._qzone_daily_tick()
            await plugin._qzone_daily_tick()
            self.assertEqual(plugin.diaries.stats()["total"], 1)

        self.run_async(scenario())

    def test_profile_cached(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._qzone_api_cache = FakeApi()
            profile = await plugin._qzone_profile_refresh()
            self.assertEqual(profile["nickname"], "杜可怡")
            data = (await self.api(plugin, "qzone"))["data"]
            self.assertEqual(data["profile"]["nickname"], "杜可怡")
            self.assertTrue(data["has_api"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
