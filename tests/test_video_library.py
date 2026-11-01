
"""视频库：标题 + 链接存下来，问到就能直接报。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, PluginHarness,
)


class LibraryTest(PluginHarness, unittest.TestCase):
    def test_add_and_dedupe(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            added = plugin._bili_library_add([
                {"bvid": "BV1", "title": "关于鸣潮", "up": "UP", "url": "https://b23.tv/BV1"},
                {"bvid": "BV1", "title": "关于鸣潮（重复）", "up": "UP", "url": "x"},
                {"bvid": "BV2", "title": "第二个视频", "up": "UP2", "url": "https://b23.tv/BV2"},
            ])
            self.assertEqual(added, 2)
            rows = plugin._bili_library()
            self.assertEqual([row["bvid"] for row in rows], ["BV1", "BV2"])
            path = plugin._bili_library_path()
            self.assertTrue(path.is_file())
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 2)

        self.run_async(scenario())

    def test_cap_40(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._bili_library_add([
                {"bvid": "BV%d" % i, "title": "视频 %d" % i, "url": "u%d" % i}
                for i in range(50)
            ])
            self.assertLessEqual(len(plugin._bili_library()), 40)

        self.run_async(scenario())

    def test_share_uses_library_when_no_last_run(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._bili_library_add([{
                "bvid": "BV1", "title": "读博到最后应有尽有",
                "up": "UP", "url": "https://www.bilibili.com/video/BV1xx",
            }])
            text = plugin._bili_share_block(FakeEvent("链接发我", umo=PRIVATE, uid=OWNER))
            self.assertIn("读博到最后应有尽有", text)
            self.assertIn("bilibili.com/video/BV1xx", text)
            self.assertIn("不许说「自己搜去」", text)

        self.run_async(scenario())

    def test_link_words_trigger(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._bili_library_add([{"bvid": "BV1", "title": "t", "url": "u"}])
            for text in ("链接", "发我地址", "网址呢"):
                self.assertTrue(plugin._bili_share_block(FakeEvent(text, umo=PRIVATE, uid=OWNER)),
                                text)

        self.run_async(scenario())

    def test_command_shows_links(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._bili_library_add([{"bvid": "BV1", "title": "标题", "url": "https://b23.tv/x"}])
            text = plugin._debug_viewed()
            self.assertIn("视频库", text)
            self.assertIn("b23.tv/x", text)

        self.run_async(scenario())

    def test_run_result_feeds_library(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            payload = plugin._bili_payload_from_result({
                "ok": True, "day": "d", "report": "r",
                "items": [{"bvid": "BV7", "title": "刷到的", "up": "UP", "url": "u7"}],
                "sheets": [], "errors": [],
            })
            plugin._bili_save_last(payload)
            plugin._bili_library_add(payload["items"])
            self.assertEqual(plugin._bili_library()[0]["bvid"], "BV7")

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
