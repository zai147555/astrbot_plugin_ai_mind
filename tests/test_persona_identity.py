
"""人格：别一会儿叫流莹一会儿叫桃子。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind.memory import Memory, SCOPE_GLOBAL  # noqa: E402
from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, PluginHarness,
)


class AnchorTest(PluginHarness, unittest.TestCase):
    def test_no_name_means_no_invented_name(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.bot_name = ""
            req = FakeProviderRequest()
            text = plugin._persona_anchor(req)
            self.assertIn("不要自报名字", text)
            self.assertNotIn("你是她", text)

        self.run_async(scenario())

    def test_detects_declared_name(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            req.system_prompt = "你是流莹，一个安静的女生。"
            text = plugin._persona_anchor(req)
            self.assertIn("流莹", text)

        self.run_async(scenario())

    def test_conflicting_names_are_called_out(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.bot_name = "桃子"
            req = FakeProviderRequest()
            req.system_prompt = "你叫流莹，记住这个设定。"
            text = plugin._persona_anchor(req)
            self.assertIn("多个名字", text)
            self.assertIn("流莹", text)
            self.assertIn("桃子", text)

        self.run_async(scenario())

    def test_identity_rule_present(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._persona_anchor(FakeProviderRequest())
            self.assertIn("不要自创第二个名字", text)

        self.run_async(scenario())


class MemoryIdentityTest(PluginHarness, unittest.TestCase):
    def test_filter_drops_foreign_name(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            rows = [Memory(content="我叫流莹", scope=SCOPE_GLOBAL),
                    Memory(content="我叫桃子", scope=SCOPE_GLOBAL),
                    Memory(content="我叫小怡", scope=SCOPE_GLOBAL)]
            kept = plugin._filter_identity_memories(rows, {"小怡"})
            self.assertEqual([row.content for row in kept], ["我叫小怡"])

        self.run_async(scenario())

    def test_filter_keeps_memories_without_claim(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            rows = [Memory(content="他喜欢喝咖啡", scope=SCOPE_GLOBAL)]
            kept = plugin._filter_identity_memories(rows, {"小怡"})
            self.assertEqual(len(kept), 1)

        self.run_async(scenario())

    def test_injection_skips_conflicting_memory(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.bot_name = "小怡"
            plugin.store.add(Memory(content="我叫流莹，请记住", scope=SCOPE_GLOBAL,
                                    importance=0.9))
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("你还记得你叫什么吗", umo=PRIVATE, uid=OWNER), req)
            injected = self.all_injected(req)
            self.assertNotIn("流莹", injected)
            self.assertGreaterEqual(int(plugin._diag.get("identity_skipped") or 0), 1)

        self.run_async(scenario())


class PersonaCommandTest(PluginHarness, unittest.TestCase):
    def test_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_persona()
            self.assertIn("配置里的名字", text)
            self.assertIn("运行中的人格预设", text)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
