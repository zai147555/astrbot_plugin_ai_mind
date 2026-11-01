
"""对方没听懂 / 说你有 bug 时：先解释，不许骂人。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, FakeResult, Plain, PluginHarness,
)


class DetectTest(PluginHarness, unittest.TestCase):
    def test_question_marks(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertTrue(plugin._wants_clarify(FakeEvent("？？？", umo=PRIVATE, uid=OWNER)))
            self.assertTrue(plugin._wants_clarify(FakeEvent("什么意思", umo=PRIVATE, uid=OWNER)))
            self.assertTrue(plugin._wants_clarify(FakeEvent("这段有 bug", umo=PRIVATE, uid=OWNER)))
            self.assertFalse(plugin._wants_clarify(FakeEvent("今天吃什么", umo=PRIVATE, uid=OWNER)))
            self.assertFalse(plugin._wants_clarify(FakeEvent("嗯", umo=PRIVATE, uid=OWNER)))

        self.run_async(scenario())

    def test_guard_text(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._clarify_guard(FakeEvent("？？？", umo=PRIVATE, uid=OWNER))
            self.assertIn("clarify_mode", text)
            self.assertIn("说明白", text)
            self.assertEqual(plugin._clarify_guard(FakeEvent("在吗", umo=PRIVATE, uid=OWNER)), "")

        self.run_async(scenario())


class InjectionTest(PluginHarness, unittest.TestCase):
    def test_clarify_block_injected(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("？？？", umo=PRIVATE, uid=OWNER), req)
            self.assertIn("clarify_mode", self.all_injected(req))

        self.run_async(scenario())

    def test_normal_message_has_no_clarify_block(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = FakeProviderRequest()
            await plugin.on_llm_request(FakeEvent("今天好累", umo=PRIVATE, uid=OWNER), req)
            self.assertNotIn("clarify_mode", self.all_injected(req))

        self.run_async(scenario())

    def test_persona_rule_against_insults(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._persona_anchor(FakeProviderRequest())
            self.assertIn("不许骂「笨蛋」", text)

        self.run_async(scenario())


class ScrubTest(PluginHarness, unittest.TestCase):
    def test_rude_words_removed_in_clarify(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("？？？", umo=PRIVATE, uid=OWNER)
            event.set_result(FakeResult([Plain("笨蛋")]))
            await plugin._guard_reply(event, PRIVATE)
            self.assertNotIn("笨蛋", self.final(event))
            self.assertIn("？", self.final(event))

        self.run_async(scenario())

    def test_keeps_explanation(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("什么意思", umo=PRIVATE, uid=OWNER)
            event.set_result(FakeResult([Plain("我说的是能上网，问什么问，就是查资料。")]))
            await plugin._guard_reply(event, PRIVATE)
            text = self.final(event)
            self.assertNotIn("问什么问", text)
            self.assertIn("查资料", text)

        self.run_async(scenario())

    def test_normal_message_keeps_tone(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("在吗", umo=PRIVATE, uid=OWNER)
            event.set_result(FakeResult([Plain("笨蛋，在啊。")]))
            await plugin._guard_reply(event, PRIVATE)
            self.assertIn("笨蛋", self.final(event))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
