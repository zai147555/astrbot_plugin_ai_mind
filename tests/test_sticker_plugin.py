
"""表情包接进插件：收集、按情绪补发、命令与面板接口。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import sticker as st

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"z" * 32


class Image:
    """假图片组件（类名必须是 Image）。"""

    def __init__(self, file: str = "", url: str = "") -> None:
        self.file = file
        self.url = url
        self.text = ""


class CaptureTest(PluginHarness, unittest.TestCase):
    def _event(self, path: Path) -> FakeEvent:
        event = FakeEvent("[图片]", umo=PRIVATE, uid=OWNER)
        event.message_obj.message = [Image(file=str(path))]
        return event

    def test_collects_image(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "笑.png"
                path.write_bytes(PNG)
                await plugin.on_sticker_capture(self._event(path))
            stats = plugin.stickers.stats()
            self.assertEqual(stats["total"], 1)
            item = plugin.stickers.list()[0]
            self.assertIn("joy", item.emotions, "文件名里的「笑」应被打上 joy")

        self.run_async(scenario())

    def test_dedupe_same_image(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.png"
                path.write_bytes(PNG)
                await plugin.on_sticker_capture(self._event(path))
                await plugin.on_sticker_capture(self._event(path))
            self.assertEqual(plugin.stickers.stats()["total"], 1)

        self.run_async(scenario())

    def test_disabled_does_not_collect(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.png"
                path.write_bytes(PNG)
                await plugin.on_sticker_capture(self._event(path))
            self.assertEqual(plugin.stickers.stats()["total"], 0)

        self.run_async(scenario())


class SendTest(PluginHarness, unittest.TestCase):
    def _seed(self, plugin) -> int:
        item = plugin.stickers.add(PNG, Path(plugin.data_dir) / "stickers",
                                   name="笑.png", emotions=["joy"])
        return item.id

    def test_sends_when_enabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            plugin.sticker_settings.chance = 1.0
            self._seed(plugin)
            with mock.patch.object(plugin.context, "send_message",
                                   new=mock.AsyncMock()) as sender:
                path = await plugin._maybe_sticker(FakeEvent("嗯", umo=PRIVATE, uid=OWNER))
            self.assertTrue(path)
            self.assertGreaterEqual(sender.await_count, 1)
            self.assertEqual(plugin.stickers.list()[0].hits, 1)

        self.run_async(scenario())

    def test_respects_hourly_cap(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            plugin.sticker_settings.chance = 1.0
            plugin.sticker_settings.per_hour = 1
            self._seed(plugin)
            with mock.patch.object(plugin.context, "send_message", new=mock.AsyncMock()):
                first = await plugin._maybe_sticker(FakeEvent("嗯", umo=PRIVATE))
                second = await plugin._maybe_sticker(FakeEvent("嗯", umo=PRIVATE))
            self.assertTrue(first)
            self.assertEqual(second, "", "每小时上限要生效")

        self.run_async(scenario())

    def test_zero_chance_never_sends(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            plugin.sticker_settings.chance = 0.0
            self._seed(plugin)
            self.assertEqual(await plugin._maybe_sticker(FakeEvent("嗯", umo=PRIVATE)), "")

        self.run_async(scenario())

    def test_disabled_never_sends(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._seed(plugin)
            self.assertEqual(await plugin._maybe_sticker(FakeEvent("嗯", umo=PRIVATE)), "")

        self.run_async(scenario())


class CommandTest(PluginHarness, unittest.TestCase):
    def test_status(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_sticker("#表情")
            self.assertIn("表情包：", text)
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_on_off_tag_delete_clear(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertIn("开", plugin._debug_sticker("#表情 开"))
            self.assertTrue(plugin.sticker_settings.enabled)
            self.assertIn("关", plugin._debug_sticker("#表情 关"))
            item = plugin.stickers.add(PNG, Path(plugin.data_dir) / "s", name="a.png")
            self.assertIn("已打标", plugin._debug_sticker("#表情 标签 %d joy,shy" % item.id))
            self.assertEqual(plugin.stickers.get(item.id).emotions, ("joy", "shy"))
            self.assertIn("删掉了", plugin._debug_sticker("#表情 删 %d" % item.id))
            another = plugin.stickers.add(PNG + b"1", Path(plugin.data_dir) / "s")
            self.assertIn("清掉了 1 张", plugin._debug_sticker("#表情 清"))
            self.assertIsNone(plugin.stickers.get(another.id))

        self.run_async(scenario())


class ApiTest(PluginHarness, unittest.TestCase):
    def test_get_and_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.stickers.add(PNG, Path(plugin.data_dir) / "s", name="a.png")
            data = (await self.api(plugin, "sticker"))["data"]
            self.assertEqual(data["stats"]["total"], 1)
            self.assertEqual(len(data["items"]), 1)
            self.assertIn("image", data["items"][0])
            saved = (await self.api(plugin, "sticker/save", body={
                "enabled": True, "chance": 0.5, "per_hour": 2,
            }))["data"]
            self.assertTrue(saved["saved"])
            self.assertAlmostEqual(saved["settings"]["chance"], 0.5)
            self.assertTrue((plugin.data_dir / "sticker.json").is_file())

        self.run_async(scenario())

    def test_tag_delete_clear(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            item = plugin.stickers.add(PNG, Path(plugin.data_dir) / "s", name="a.png")
            tagged = (await self.api(plugin, "sticker/tag", body={
                "id": item.id, "emotions": ["sad"],
            }))["data"]
            self.assertTrue(tagged["tagged"])
            self.assertEqual(tagged["items"][0]["emotions"], ["sad"])
            deleted = (await self.api(plugin, "sticker/delete", body={"id": item.id}))["data"]
            self.assertTrue(deleted["deleted"])
            self.assertEqual(deleted["stats"]["total"], 0)
            plugin.stickers.add(PNG, Path(plugin.data_dir) / "s", name="b.png")
            cleared = (await self.api(plugin, "sticker/clear"))["data"]
            self.assertEqual(cleared["removed"], 1)

        self.run_async(scenario())

    def test_tag_requires_id(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            resp = await self.api(plugin, "sticker/tag", body={})
            self.assertFalse(resp.get("ok"))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

