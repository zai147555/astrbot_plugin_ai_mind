
"""戳一戳接进插件：处理器、冷却、面板接口、表情工具。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from mind.memory.model import Memory

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"q" * 32


class PokeEvent(FakeEvent):
    """带 raw_message / request_llm 的假事件。"""

    def __init__(self, *, target: str = "999", sender: str = OWNER,
                 group: str = "123456", self_id: str = "999", **kwargs):
        super().__init__("[戳一戳]", umo=PRIVATE, uid=sender, **kwargs)
        self.message_obj.raw_message = {
            "post_type": "notice", "notice_type": "poke", "sub_type": "poke",
            "target_id": target, "user_id": sender, "group_id": group,
            "self_id": self_id,
        }
        self.requested: list[dict] = []

    def get_self_id(self) -> str:
        return "999"

    def get_group_id(self) -> str:
        return str(self.message_obj.raw_message.get("group_id") or "")

    def request_llm(self, **kwargs):
        self.requested.append(kwargs)
        return ("llm", kwargs.get("prompt", ""))


class PokeHandlerTest(PluginHarness, unittest.TestCase):
    async def _collect(self, plugin, event):
        out = []
        async for item in plugin.on_poke(event):
            out.append(item)
        return out

    def test_poke_me_triggers_llm(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = PokeEvent(target="999")
            out = await self._collect(plugin, event)
            self.assertEqual(len(out), 1)
            self.assertIn("戳了你一下", out[0][1])
            self.assertIn("群聊", out[0][1])

        self.run_async(scenario())

    def test_poke_someone_else_is_ignored(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertEqual(await self._collect(plugin, PokeEvent(target="777")), [])

        self.run_async(scenario())

    def test_cooldown_blocks_second_poke(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.poke_settings.user_cooldown = 30
            plugin.poke_gate.settings = plugin.poke_settings
            first = PokeEvent(target="999")
            self.assertEqual(len(await self._collect(plugin, first)), 1)
            second = PokeEvent(target="999")
            self.assertEqual(await self._collect(plugin, second), [], "冷却里不该再回")

        self.run_async(scenario())

    def test_blacklisted_user_is_ignored(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.poke_settings.blacklist = [OWNER]
            plugin.poke_gate.settings = plugin.poke_settings
            self.assertEqual(await self._collect(plugin, PokeEvent(target="999")), [])

        self.run_async(scenario())

    def test_disabled_ignored(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.poke_settings.enabled = False
            self.assertEqual(await self._collect(plugin, PokeEvent(target="999")), [])

        self.run_async(scenario())

    def test_non_poke_event_ignored(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("你好", umo=PRIVATE, uid=OWNER)
            self.assertEqual(await self._collect(plugin, event), [])

        self.run_async(scenario())


class PokeApiTest(PluginHarness, unittest.TestCase):
    def test_get_and_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "poke"))["data"]
            self.assertTrue(data["settings"]["enabled"])
            saved = (await self.api(plugin, "poke/save", body={
                "user_cooldown": 3, "group_cooldown": 0, "blacklist": ["42"],
                "in_groups": False,
            }))["data"]
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["settings"]["user_cooldown"], 3)
            self.assertFalse(saved["settings"]["in_groups"])
            self.assertEqual(saved["settings"]["blacklist"], ["42"])
            self.assertTrue((plugin.data_dir / "poke.json").is_file())
            self.assertEqual(plugin.poke_gate.settings.user_cooldown, 3)

        self.run_async(scenario())


class StickerToolTest(PluginHarness, unittest.TestCase):
    def test_send_and_search(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.sticker_settings.enabled = True
            item = plugin.stickers.add(PNG, Path(plugin.data_dir) / "s",
                                       name="笑.png", emotions=["joy"])
            event = FakeEvent("嗯", umo=PRIVATE, uid=OWNER)
            with mock.patch.object(plugin.context, "send_message", new=mock.AsyncMock()) as sender:
                text = await plugin.tool_send_sticker(event, mood="joy")
            self.assertIn("已经发了一张表情", text)
            self.assertEqual(sender.await_count, 1)
            self.assertEqual(plugin.stickers.get(item.id).hits, 1)
            found = await plugin.tool_search_sticker(event, keyword="joy")
            self.assertIn("有 1 张", found)
            self.assertIn("#%d" % item.id, found)

        self.run_async(scenario())

    def test_send_when_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin.tool_send_sticker(FakeEvent("嗯", umo=PRIVATE), mood="joy")
            self.assertIn("没开", text)

        self.run_async(scenario())

    def test_search_empty(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin.tool_search_sticker(FakeEvent("嗯", umo=PRIVATE), keyword="zzz")
            self.assertIn("没有相关", text)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

