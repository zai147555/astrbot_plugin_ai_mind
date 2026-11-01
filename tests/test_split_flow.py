
"""分段：空格清理、旧实例判定、诊断命令。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind.textfix import tidy_cjk_spaces  # noqa: E402
from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, FakeResult, Plain, PluginHarness,
)


class TextFixTest(unittest.TestCase):
    def test_space_after_ellipsis_removed(self):
        self.assertEqual(tidy_cjk_spaces("我想静静…… 好吗？"), "我想静静……好吗？")

    def test_cjk_space_removed(self):
        self.assertEqual(tidy_cjk_spaces("今 天 很 累"), "今天很累")

    def test_english_space_kept(self):
        self.assertEqual(tidy_cjk_spaces("用 AstrBot 挺好"), "用 AstrBot 挺好")

    def test_punct_before_space_removed(self):
        self.assertEqual(tidy_cjk_spaces("行 。"), "行。")


class SplitFlowTest(PluginHarness, unittest.TestCase):
    def _split(self, plugin, event):
        plugin._mark_llm(event)
        event.set_result(FakeResult([Plain("今天 有点 累。你 别 烦我！我 想 静 静…… 好吗？")]))
        return event

    def test_segments_and_spaces(self):
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.splitter.enabled = True
            plugin.splitter.max_segments = 3
            plugin.splitter.delay_strategy = "fixed"
            plugin.splitter.fixed_delay = 0.0
            event = self._split(plugin, FakeEvent("嗯", umo=PRIVATE, uid=OWNER))
            plugin.context.sent = []
            await plugin.on_decorating_result(event)
            sent = ["".join(getattr(c, "text", "") for c in getattr(ch, "chain", ch))
                    for _umo, ch in plugin.context.sent]
            final = "".join(getattr(c, "text", "") for c in event.get_result().chain)
            all_text = "".join(sent) + final
            self.assertNotIn(" ", all_text)
            self.assertIn("今天有点累", all_text)
            self.assertGreaterEqual(len(sent) + 1, 2)
            self.assertTrue(plugin._last_split.get("segments"))

        self.run_async(scenario())


class SplitDiagnosticsTest(PluginHarness, unittest.TestCase):
    def test_report_after_no_split(self):
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.splitter.enabled = False
            event = FakeEvent("嗯", umo=PRIVATE, uid=OWNER)
            plugin._mark_llm(event)
            event.set_result(FakeResult([Plain("嗯。")]))
            await plugin.on_decorating_result(event)
            text = plugin._debug_split("#分段")
            self.assertIn("分段：关", text)

        self.run_async(scenario())

    def test_preview(self):
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_split("#分段 试 今天好累。你别烦我！我想静静")
            self.assertIn("试拆", text)
            self.assertIn("段", text)

        self.run_async(scenario())

    def test_last_split_recorded(self):
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.splitter.enabled = True
            plugin.splitter.max_segments = 3
            plugin.splitter.fixed_delay = 0.0
            event = FakeEvent("嗯", umo=PRIVATE, uid=OWNER)
            plugin._mark_llm(event)
            event.set_result(FakeResult([Plain("第一句。第二句。第三句。")]))
            await plugin.on_decorating_result(event)
            text = plugin._debug_split("#分段")
            self.assertIn("上一次", text)

        self.run_async(scenario())


class StaleLogicTest(PluginHarness, unittest.TestCase):
    def test_newer_instance_runs_even_before_initialize(self) -> None:
        async def scenario() -> None:
            first = self.make_plugin()
            await first.initialize()
            second = self.make_plugin()
            self.assertFalse(second._is_stale())
            self.assertFalse(first._is_stale())
            await second.initialize()
            self.assertTrue(first._is_stale())
            self.assertFalse(second._is_stale())

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
