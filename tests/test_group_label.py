
"""群里被陌生人叫「专属称呼」时：不许认、不许露。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_plugin_smoke import (  # noqa: E402
    OWNER, PRIVATE, FakeEvent, FakeProviderRequest, FakeResult, Plain, PluginHarness,
)

GROUP = "aiocqhttp:GroupMessage:445566"
STRANGER = "10086"


class GroupLabelGuardTest(PluginHarness, unittest.TestCase):
    def test_stranger_in_group_gets_discretion_block(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            text = plugin._group_label_guard(
                FakeEvent("宝宝 在吗", umo=GROUP, uid=STRANGER), GROUP
            )
            self.assertIn("群里有人用「宝宝」叫你", text)

        self.run_async(scenario())

    def test_owner_in_group_is_exempt(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            text = plugin._group_label_guard(
                FakeEvent("宝宝 在吗", umo=GROUP, uid=OWNER), GROUP
            )
            self.assertEqual(text, "")

        self.run_async(scenario())

    def test_private_never_blocks(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            text = plugin._group_label_guard(
                FakeEvent("宝宝 在吗", umo=PRIVATE, uid=STRANGER), PRIVATE
            )
            self.assertEqual(text, "")

        self.run_async(scenario())

    def test_label_missing_does_nothing(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            text = plugin._group_label_guard(
                FakeEvent("在吗", umo=GROUP, uid=STRANGER), GROUP
            )
            self.assertEqual(text, "")

        self.run_async(scenario())

    def test_discretion_off(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            plugin.settings.emotion.group_discretion = False
            text = plugin._group_label_guard(
                FakeEvent("宝宝 在吗", umo=GROUP, uid=STRANGER), GROUP
            )
            self.assertEqual(text, "")

        self.run_async(scenario())

    def test_request_injection_carries_the_block(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.settings.emotion.special_label = "宝宝"
            req = FakeProviderRequest()
            await plugin.on_llm_request(
                FakeEvent("宝宝 你好呀", umo=GROUP, uid=STRANGER), req
            )
            self.assertIn("群里有人用「宝宝」", self.all_injected(req))

        self.run_async(scenario())


class ScrubTest(PluginHarness, unittest.TestCase):
    def test_group_reply_that_leaks_is_replaced(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("宝宝", umo=GROUP, uid=STRANGER)
            event.set_result(FakeResult([Plain("在群里这么叫……")]))
            await plugin._guard_reply(event, GROUP)
            self.assertIn("嗯？", self.final(event))

        self.run_async(scenario())

    def test_group_reply_keeps_normal_part(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("宝宝", umo=GROUP, uid=STRANGER)
            event.set_result(FakeResult([Plain("在群里这么叫……？你谁啊")]))
            await plugin._guard_reply(event, GROUP)
            text = self.final(event)
            self.assertNotIn("在群里这么叫", text)
            self.assertIn("你谁啊", text)

        self.run_async(scenario())

    def test_private_reply_keeps_phrase(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("宝宝", umo=PRIVATE, uid=OWNER)
            event.set_result(FakeResult([Plain("在群里这么叫……")]))
            await plugin._guard_reply(event, PRIVATE)
            self.assertIn("在群里这么叫", self.final(event))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
