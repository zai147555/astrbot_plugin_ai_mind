
"""日程按人格生成 / 按日程刷B站 / 分享看过的视频。"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import bili as bili_engine  # noqa: E402
from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, PluginHarness,
)

SCHEDULE_JSON = json.dumps({
    "note": "测试日",
    "table": [
        {"start": "00:00", "end": "07:00", "activity": "睡觉", "state": "sleeping", "tone": "困"},
        {"start": "07:00", "end": "12:00", "activity": "上课", "state": "busy", "tone": "短"},
        {"start": "12:00", "end": "17:00", "activity": "下午上课", "state": "busy", "tone": "敷衍"},
        {"start": "17:00", "end": "23:00", "activity": "写作业刷手机", "state": "free", "tone": "话多"},
        {"start": "23:00", "end": "24:00", "activity": "睡觉", "state": "sleeping", "tone": "赶你睡"},
    ],
}, ensure_ascii=False)


class DueByScheduleTest(unittest.TestCase):
    def _hour(self, hour: int) -> float:
        import datetime

        return datetime.datetime(2026, 11, 1, hour, 0).timestamp()

    def test_free_and_gap_ok(self) -> None:
        now = self._hour(15)
        self.assertTrue(bili_engine.due_by_schedule(True, "free", now, now - 7 * 3600))

    def test_busy_skips(self) -> None:
        now = self._hour(15)
        self.assertFalse(bili_engine.due_by_schedule(True, "busy", now, 0))

    def test_sleeping_skips(self) -> None:
        now = self._hour(3)
        self.assertFalse(bili_engine.due_by_schedule(True, "free", now, 0))

    def test_recent_run_skips(self) -> None:
        now = self._hour(15)
        self.assertFalse(bili_engine.due_by_schedule(True, "free", now, now - 3600))

    def test_disabled(self) -> None:
        now = self._hour(15)
        self.assertFalse(bili_engine.due_by_schedule(False, "free", now, 0))


class GeneratedScheduleTest(PluginHarness, unittest.TestCase):
    def test_generates_and_uses_it(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = SCHEDULE_JSON
            await plugin._schedule_tick()
            gen = plugin._load_generated_schedule()
            self.assertIsNotNone(gen)
            self.assertEqual(gen.get("note"), "测试日")
            block = plugin._schedule_block()
            self.assertIn("测试日", block)
            self.assertIn("她在：", block)
            state, activity = plugin._current_schedule_state()
            self.assertIn(state, ("busy", "free", "sleeping"))

        self.run_async(scenario())

    def test_bad_json_falls_back_to_template(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = "我不是 JSON"
            await plugin._schedule_tick()
            self.assertIsNone(plugin._load_generated_schedule())
            self.assertIn("<daily_schedule>", plugin._schedule_block())

        self.run_async(scenario())

    def test_auto_generate_off(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"schedule": {"auto_generate": False}})
            await plugin.initialize()
            self.provider.response = SCHEDULE_JSON
            await plugin._schedule_tick()
            self.assertIsNone(plugin._load_generated_schedule())

        self.run_async(scenario())

    def test_saved_to_disk(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = SCHEDULE_JSON
            await plugin._schedule_tick()
            path = plugin._schedule_path()
            self.assertTrue(path.is_file())
            self.assertIn("table", json.loads(path.read_text(encoding="utf-8")))

        self.run_async(scenario())

    def test_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.response = SCHEDULE_JSON
            text = await plugin._debug_schedule("#日程 重")
            self.assertIn("已重新生成", text)
            report = await plugin._debug_schedule("#日程")
            self.assertIn("测试日", report)

        self.run_async(scenario())


class ShareVideosTest(PluginHarness, unittest.TestCase):
    def _payload(self, plugin) -> None:
        plugin._bili_save_last(plugin._bili_payload_from_result({
            "ok": True, "day": "2026-11-01", "report": "x",
            "items": [{"bvid": "BV1", "title": "关于鸣潮的二三事", "up": "某UP"},
                      {"bvid": "BV2", "title": "独立游戏推荐", "up": "另一个UP"}],
            "sheets": [], "errors": [],
        }))

    def test_asks_about_videos_gets_block(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._payload(plugin)
            text = plugin._bili_share_block(FakeEvent("你最近看了什么视频", umo=PRIVATE, uid=OWNER))
            self.assertIn("recent_videos", text)
            self.assertIn("关于鸣潮", text)

        self.run_async(scenario())

    def test_unrelated_message_gets_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._payload(plugin)
            self.assertEqual(plugin._bili_share_block(FakeEvent("今天吃什么", umo=PRIVATE, uid=OWNER)), "")

        self.run_async(scenario())

    def test_no_history_gets_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertEqual(plugin._bili_share_block(FakeEvent("你看了什么视频", umo=PRIVATE, uid=OWNER)), "")

        self.run_async(scenario())

    def test_injected_on_request(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._payload(plugin)
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("你刷到什么好看的视频了", umo=PRIVATE, uid=OWNER), req)
            self.assertIn("关于鸣潮", self.all_injected(req))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
