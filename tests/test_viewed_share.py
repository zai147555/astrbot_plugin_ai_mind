
"""她真的能说出看过的视频：最近刷到 + 观看历史缓存。"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, PluginHarness,
)

COOKIE = "SESSDATA=abc; bili_jct=1; DedeUserID=2"


class ViewedCacheTest(PluginHarness, unittest.TestCase):
    def _write_cache(self, plugin, rows) -> None:
        plugin.bili_settings.cookie = COOKIE
        path = plugin._bili_viewed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": 0, "rows": rows}, ensure_ascii=False),
                        encoding="utf-8")

    def test_share_uses_history_when_no_last_run(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._write_cache(plugin, [{
                "bvid": "BV1", "title": "关于鸣潮的二三事", "up": "某UP",
                "duration": 300, "progress": 150, "url": "https://b23.tv/BV1",
            }])
            text = plugin._bili_share_block(FakeEvent("你今天都看了什么视频啊", umo=PRIVATE, uid=OWNER))
            self.assertIn("关于鸣潮", text)
            self.assertIn("b23.tv", text)
            self.assertIn("看到 50%", text)

        self.run_async(scenario())

    def test_last_run_wins_over_cache(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._write_cache(plugin, [{"title": "缓存里的", "url": "u"}])
            plugin._bili_save_last(plugin._bili_payload_from_result({
                "ok": True, "day": "d", "report": "r",
                "items": [{"bvid": "BV9", "title": "刚刷到的", "up": "UP", "url": "x"}],
                "sheets": [], "errors": [],
            }))
            text = plugin._bili_share_block(FakeEvent("看了什么视频", umo=PRIVATE, uid=OWNER))
            self.assertIn("刚刷到的", text)
            self.assertNotIn("缓存里的", text)

        self.run_async(scenario())

    def test_no_data_says_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertEqual(plugin._bili_share_block(FakeEvent("看了什么视频", umo=PRIVATE, uid=OWNER)), "")

        self.run_async(scenario())

    def test_history_tick_caches(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = COOKIE
            rows = [{"bvid": "BV1", "title": "关于鸣潮", "up": "UP",
                     "duration": 300, "progress": 30, "url": "u"}]
            with mock.patch.object(self.module.bili_engine, "fetch_history",
                                   return_value=(rows, "")) as fetch:
                await plugin._bili_history_tick()
            self.assertEqual(len(plugin._bili_viewed()), 1)
            with mock.patch.object(self.module.bili_engine, "fetch_history",
                                   return_value=(rows, "")) as fetch2:
                await plugin._bili_history_tick()
            fetch2.assert_not_called()

        self.run_async(scenario())

    def test_history_tick_needs_cookie(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.bili_engine, "fetch_history") as fetch:
                await plugin._bili_history_tick()
            fetch.assert_not_called()

        self.run_async(scenario())

    def test_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertIn("没有视频可分享", plugin._debug_viewed())
            self._write_cache(plugin, [{"title": "缓存里的", "url": "u"}])
            self.assertIn("缓存里的", plugin._debug_viewed())

        self.run_async(scenario())

    def test_injection_when_asked(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._write_cache(plugin, [{"title": "缓存里的视频", "url": "u"}])
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("你今天都看了什么视频啊", umo=PRIVATE, uid=OWNER), req)
            self.assertIn("缓存里的视频", self.all_injected(req))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
