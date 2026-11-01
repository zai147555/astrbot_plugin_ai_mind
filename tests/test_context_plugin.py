
"""上下文压缩 / 人格锚点 / 省 token 遥测（插件层）。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, PluginHarness,
)


class PersonaAnchorTest(PluginHarness, unittest.TestCase):
    def test_anchor_is_injected(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("在吗", umo=PRIVATE, uid=OWNER), req)
            prompt = req.system_prompt or ""
            self.assertIn("<persona_anchor>", prompt)
            self.assertIn("说话底线", prompt)
            self.assertIn("不因情绪", prompt)

        self.run_async(scenario())

    def test_anchor_text_prefers_preset_style(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            try:
                plugin.engine.preset.style_note = "说话松散一点"
            except Exception:
                self.skipTest("引擎预设不可写")
            text = plugin._persona_anchor()
            self.assertIn("说话松散一点", text)

        self.run_async(scenario())


class ContextTrimTest(PluginHarness, unittest.TestCase):
    def _long(self, n: int = 60):
        return [{"role": "user", "content": "闲聊第%d句，说点事情" % i} for i in range(n)]

    def test_long_history_is_trimmed(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            req.contexts = self._long(60)
            await plugin.on_llm_request(FakeEvent("今天好累", umo=PRIVATE, uid=OWNER), req)
            self.assertLess(len(req.contexts), 60)
            self.assertIn("更早的", self.all_injected(req))

        self.run_async(scenario())

    def test_short_history_untouched(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            req.contexts = self._long(6)
            await plugin.on_llm_request(FakeEvent("嗯", umo=PRIVATE, uid=OWNER), req)
            self.assertEqual(len(req.contexts), 6)

        self.run_async(scenario())

    def test_important_line_survives(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.memory.context_keep_recent = 4
            plugin.settings.memory.context_max_turns = 10
            rows = [{"role": "user", "content": "记住我生日是五月三号"}] + self._long(40)
            req = FakeProviderRequest()
            req.contexts = rows
            await plugin.on_llm_request(FakeEvent("在吗", umo=PRIVATE, uid=OWNER), req)
            joined = "".join(str((row or {}).get("content") or "") for row in req.contexts)
            self.assertIn("生日", joined)

        self.run_async(scenario())

    def test_can_be_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.memory.context_enabled = False
            req = FakeProviderRequest()
            req.contexts = self._long(60)
            await plugin.on_llm_request(FakeEvent("嗯", umo=PRIVATE, uid=OWNER), req)
            self.assertEqual(len(req.contexts), 60)

        self.run_async(scenario())


class TokenStatsTest(PluginHarness, unittest.TestCase):
    def test_wizard_exposes_learn_and_tokens(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("今天累", umo=PRIVATE, uid=OWNER), req)
            data = (await self.api(plugin, "wizard", query={"session": PRIVATE}))["data"]
            self.assertIn("auto_learn", data)
            self.assertIn("context", data)
            self.assertIn("tokens", data)
            self.assertGreater(data["tokens"]["last"], 0)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
