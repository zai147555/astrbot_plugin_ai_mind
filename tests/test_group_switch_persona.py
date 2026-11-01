
"""换群 / 人格 / 表情：自定义人格别退回旧的，表情失败要说得出原因。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import sticker as st  # noqa: E402
from mind.emotion.engine import EmotionEngine  # noqa: E402
from mind.emotion.presets import save_custom_preset  # noqa: E402
from tests.test_plugin_smoke import OWNER, FakeEvent, PluginHarness  # noqa: E402


def _emotion_settings(raw, root=None):
    from mind.config import EmotionSettings

    return EmotionSettings.from_config(raw, root)

GROUP = "aiocqhttp:GroupMessage:556677"
STRANGER = "10010"
PNG = b"\x89PNG\r\n\x1a\n" + b"k" * 32


class CustomPresetRestartTest(unittest.TestCase):
    def test_custom_preset_survives_engine_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, message = save_custom_preset(root, "我的她", {
                "label": "我的她",
                "style_note": "说话慢一点，爱用省略号",
            })
            self.assertTrue(ok, message)
            settings = _emotion_settings({"persona": {"preset": "我的她"}}, root)
            engine = EmotionEngine(settings, root)
            self.assertEqual(engine.preset.key, "我的她")
            self.assertIn("省略号", engine.preset.style_note)

    def test_unknown_preset_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _emotion_settings({"persona": {"preset": "不存在"}}, Path(tmp))
            engine = EmotionEngine(settings, Path(tmp))
            self.assertTrue(engine.preset.key)


class StickerSendTest(PluginHarness, unittest.TestCase):
    def _seed(self, plugin) -> int:
        item = plugin.stickers.add(PNG, Path(plugin.data_dir) / "stickers",
                                   name="笑.png", emotions=["joy"])
        return item.id

    def test_send_ok(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._seed(plugin)
            event = FakeEvent("嗯", umo=GROUP, uid=OWNER)
            with mock.patch.object(plugin.context, "send_message",
                                   new=mock.AsyncMock()) as sender:
                result = await plugin._send_sticker(event)
            self.assertTrue(result["ok"], result)
            sender.assert_awaited()

        self.run_async(scenario())

    def test_empty_pool_says_why(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            result = await plugin._send_sticker(FakeEvent("嗯", umo=GROUP, uid=OWNER))
            self.assertFalse(result["ok"])
            self.assertIn("空", result["error"])

        self.run_async(scenario())

    def test_missing_file_says_why(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            ident = self._seed(plugin)
            item = plugin.stickers.get(ident)
            Path(item.path).unlink()
            result = await plugin._send_sticker(FakeEvent("嗯", umo=GROUP, uid=OWNER))
            self.assertFalse(result["ok"])
            self.assertIn("文件", result["error"])

        self.run_async(scenario())

    def test_platform_rejection_is_reported(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._seed(plugin)
            event = FakeEvent("嗯", umo=GROUP, uid=OWNER)
            with mock.patch.object(plugin.context, "send_message",
                                   new=mock.AsyncMock(side_effect=RuntimeError("不支持"))):
                result = await plugin._send_sticker(event)
            self.assertFalse(result["ok"])
            self.assertIn("平台", result["error"])

        self.run_async(scenario())


class GroupDiagnosticsTest(PluginHarness, unittest.TestCase):
    def test_report_lists_state(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_group(FakeEvent("嗯", umo=GROUP, uid=OWNER))
            self.assertIn("群开关", text)
            self.assertIn("人格", text)
            self.assertIn("表情", text)
            self.assertIn("专属用户", text)

        self.run_async(scenario())

    def test_closed_group_says_it(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.groupswitch.set_one("556677", False)
            text = plugin._debug_group(FakeEvent("嗯", umo=GROUP, uid=OWNER))
            self.assertIn("被关掉了", text)

        self.run_async(scenario())


class StaleInstanceTest(PluginHarness, unittest.TestCase):
    def test_old_instance_does_not_inject(self) -> None:
        async def scenario() -> None:
            first = self.make_plugin()
            await first.initialize()
            second = self.make_plugin()
            await second.initialize()
            from tests.test_plugin_smoke import FakeProviderRequest

            req = FakeProviderRequest()
            await first.on_llm_request(FakeEvent("嗯", umo=GROUP, uid=OWNER), req)
            self.assertEqual(req.system_prompt or "", "", "旧实例不该再注入")

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
