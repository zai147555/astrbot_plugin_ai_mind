
"""价值回传接进插件：隐含反馈、手动调整、经验结晶。"""
from __future__ import annotations

import unittest

from mind.memory.model import Memory

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class FeedbackWiringTest(PluginHarness, unittest.TestCase):
    def _seed(self, plugin, **kwargs) -> int:
        base = dict(content="他喜欢冰美式", keywords=("冰美式", "咖啡"),
                    kind="preference", scope="session", session_id=PRIVATE,
                    importance=0.5)
        base.update(kwargs)
        return plugin.store.add(Memory(**base))

    def test_positive_feedback_raises_value(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            memory_id = self._seed(plugin)
            await self.send(plugin, "我喜欢冰美式")
            self.assertIn(memory_id, plugin._last_memory_ids.get(PRIVATE, []))
            await self.send(plugin, "对")
            value = plugin.store.get(memory_id).importance
            self.assertGreater(value, 0.5, "肯定之后价值该涨")
            self.assertGreaterEqual(plugin._feedback_applied, 1)

        self.run_async(scenario())

    def test_negative_feedback_lowers_value(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            memory_id = self._seed(plugin)
            await self.send(plugin, "我喜欢冰美式")
            await self.send(plugin, "不对")
            self.assertLess(plugin.store.get(memory_id).importance, 0.5)

        self.run_async(scenario())

    def test_long_message_is_not_treated_as_feedback(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            memory_id = self._seed(plugin)
            await self.send(plugin, "我喜欢冰美式")
            await self.send(plugin, "今天聊了挺多，不过你说的那个不对的地方我们自己再想想吧")
            self.assertAlmostEqual(plugin.store.get(memory_id).importance, 0.5, places=2)

        self.run_async(scenario())

    def test_no_memories_means_no_feedback(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "对")
            self.assertEqual(plugin._feedback_applied, 0)
            self.assertNotIn(PRIVATE, plugin._last_memory_ids)

        self.run_async(scenario())

    def test_manual_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            memory_id = self._seed(plugin)
            text = plugin._debug_memory("#记忆 对 %d" % memory_id)
            self.assertIn("已按「对」调整", text)
            self.assertGreater(plugin.store.get(memory_id).importance, 0.5)
            out = plugin._debug_memory("#记忆 错 %d" % memory_id)
            self.assertIn("已按「错」调整", out)
            self.assertLess(plugin.store.get(memory_id).importance, 0.6)
            self.assertIn("没调整到", plugin._debug_memory("#记忆 对 99999"))

        self.run_async(scenario())

    def test_help_line_shows_counter(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            memory_id = self._seed(plugin)
            plugin._debug_memory("#记忆 对 %d" % memory_id)
            text = plugin._debug_memory("#记忆")
            self.assertIn("价值回传", text)

        self.run_async(scenario())


class CrystallizeWiringTest(PluginHarness, unittest.TestCase):
    def test_makes_policy_card(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            for index in range(3):
                plugin.store.add(Memory(
                    content="第%d条关于咖啡的习惯" % (index + 1),
                    keywords=("咖啡", "冰美式"),
                    kind="preference", scope="session", session_id=PRIVATE,
                    importance=0.9,
                ))
            made = plugin._crystallize_memories()
            self.assertEqual(made, 1)
            policies = [m for m in plugin.store.list_memories(limit=50) if m.kind == "policy"]
            self.assertEqual(len(policies), 1)
            self.assertIn("经验（3 条记忆）", policies[0].content)
            self.assertGreaterEqual(policies[0].importance, 0.8)

        self.run_async(scenario())

    def test_no_duplicate_policy(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            for index in range(3):
                plugin.store.add(Memory(
                    content="第%d条" % index, keywords=("咖啡", "冰美式"),
                    kind="preference", scope="session", session_id=PRIVATE,
                    importance=0.9,
                ))
            self.assertEqual(plugin._crystallize_memories(), 1)
            self.assertEqual(plugin._crystallize_memories(), 0, "同一簇不该重复结晶")

        self.run_async(scenario())

    def test_low_value_cluster_is_skipped(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            for index in range(3):
                plugin.store.add(Memory(
                    content="第%d条" % index, keywords=("咖啡", "冰美式"),
                    kind="preference", scope="session", session_id=PRIVATE,
                    importance=0.3,
                ))
            self.assertEqual(plugin._crystallize_memories(), 0)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

