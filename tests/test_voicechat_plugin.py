
"""AI 声聊接进插件：先文字后语音、平台/群/冷却门禁。"""
from __future__ import annotations

import unittest
from unittest import mock

from mind import voicechat as vc

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness

GROUP = "aiocqhttp:GroupMessage:778899"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.characters = [{"character_id": "c1", "character_name": "小怡"}]

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.fail:
            raise RuntimeError("qq 不支持")
        if action == "get_ai_characters":
            return {"status": "ok", "data": self.characters}
        return {"status": "ok"}


class FakeBot:
    def __init__(self) -> None:
        self.api = FakeApi()


class BotEvent(FakeEvent):
    def __init__(self, text="你好", umo=GROUP, uid=OWNER) -> None:
        super().__init__(text, umo=umo, uid=uid)
        self.bot = FakeBot()


class VoiceFlowTest(PluginHarness, unittest.TestCase):
    async def _run(self, plugin, event, text="今天也想你", group="778899"):
        return await plugin._maybe_voice(event, text)

    def test_sends_voice_after_text(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.voice_settings.enabled = True
            plugin.voice_settings.character = ""
            event = BotEvent()
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                spoken = await self._run(plugin, event)
            self.assertTrue(spoken)
            actions = [call[0] for call in event.bot.api.calls]
            self.assertIn("send_group_ai_record", actions)
            sent = [call for call in event.bot.api.calls if call[0] == "send_group_ai_record"][0][1]
            self.assertEqual(sent["character"], "c1")
            self.assertIn("今天也想你", sent["text"])

        self.run_async(scenario())

    def test_cleans_voice_text(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.voice_settings.enabled = True
            plugin.voice_settings.character = "c1"
            event = BotEvent()
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                spoken = await self._run(plugin, event, "看这个 https://x.y/z （笑）你好😊")
            self.assertNotIn("http", spoken)
            self.assertNotIn("（笑）", spoken)
            self.assertIn("你好", spoken)

        self.run_async(scenario())

    def test_disabled_sends_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = BotEvent()
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                self.assertEqual(await self._run(plugin, event), "")
            self.assertEqual(event.bot.api.calls, [])

        self.run_async(scenario())

    def test_non_group_skipped(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.voice_settings.enabled = True
            event = BotEvent(umo=PRIVATE)
            self.assertEqual(await self._run(plugin, event), "")
            self.assertEqual(event.bot.api.calls, [])

        self.run_async(scenario())

    def test_cooldown(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.voice_settings.enabled = True
            plugin.voice_settings.cooldown = 60
            plugin.voice_settings.character = "c1"
            event = BotEvent()
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                self.assertTrue(await self._run(plugin, event))
                self.assertEqual(await self._run(plugin, event), "", "冷却里不该再发")

        self.run_async(scenario())

    def test_api_failure_is_silent(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.voice_settings.enabled = True
            plugin.voice_settings.character = "c1"
            event = BotEvent()
            event.bot.api.fail = True
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                self.assertEqual(await self._run(plugin, event), "")

        self.run_async(scenario())


class VoiceCommandTest(PluginHarness, unittest.TestCase):
    def test_status_and_toggle(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_voice(BotEvent(), "#声聊")
            self.assertIn("AI 声聊", text)
            self.assertIn("用法", text)
            self.assertIn("开", await plugin._debug_voice(BotEvent(), "#声聊 开"))
            self.assertTrue(plugin.voice_settings.enabled)
            self.assertIn("关", await plugin._debug_voice(BotEvent(), "#声聊 关"))
            self.assertFalse(plugin.voice_settings.enabled)

        self.run_async(scenario())

    def test_characters_and_use(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = BotEvent()
            with mock.patch.object(plugin, "_event_group_id", return_value="778899"):
                listed = await plugin._debug_voice(event, "#声聊 人物")
            self.assertIn("c1", listed)
            self.assertIn("音色设成", await plugin._debug_voice(event, "#声聊 用 c1"))
            self.assertEqual(plugin.voice_settings.character, "c1")

        self.run_async(scenario())


class VoiceApiTest(PluginHarness, unittest.TestCase):
    def test_get_and_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "voice"))["data"]
            self.assertFalse(data["settings"]["enabled"])
            saved = (await self.api(plugin, "voice/save", body={
                "enabled": True, "mode": "chance", "chance": 0.4,
                "character": "c9", "groups": ["100"],
            }))["data"]
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["settings"]["mode"], "chance")
            self.assertEqual(saved["settings"]["character"], "c9")
            self.assertTrue((plugin.data_dir / "voice.json").is_file())

        self.run_async(scenario())

    def test_characters_api_requires_group(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "voice/characters", body={}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

