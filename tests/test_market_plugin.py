
"""需求先搜市场：自动触发、注入、命令、面板接口。"""
from __future__ import annotations

import unittest
from unittest import mock

from mind import market

from tests.test_plugin_smoke import GRP, OWNER, PRIVATE, FakeEvent, PluginHarness

HITS = [{
    "fullName": "MemTensor/MemOS#memos-local-plugin",
    "summary": "分层记忆",
    "license": "MIT",
    "stars": 9,
    "inRegistry": True,
    "riskFlags": ["terminal surface"],
    "url": "https://github.com/MemTensor/MemOS",
    "install": "dsh plugin add dsh-memos",
}]


class AutoTriggerTest(PluginHarness, unittest.TestCase):
    def test_requirement_from_owner_injects_hits(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=(HITS, "")):
                req = await self.send(plugin, "能不能做个记忆插件")
            injected = self.blocks_of(req)
            self.assertIn("<market_hits>", injected)
            self.assertIn("MemOS", injected)

        self.run_async(scenario())

    def test_casual_chat_does_not_search(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search") as searcher:
                req = await self.send(plugin, "今天好累")
            searcher.assert_not_called()
            self.assertNotIn("<market_hits>", self.blocks_of(req))

        self.run_async(scenario())

    def test_stranger_does_not_trigger_by_default(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search") as searcher:
                req = await self.send(plugin, "能不能做个记忆插件", uid="555")
            searcher.assert_not_called()
            self.assertNotIn("<market_hits>", self.blocks_of(req))

        self.run_async(scenario())

    def test_owner_only_can_be_turned_off(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.market_settings.owner_only = False
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=(HITS, "")):
                req = await self.send(plugin, "能不能做个记忆插件", uid="555")
            self.assertIn("<market_hits>", self.blocks_of(req))

        self.run_async(scenario())

    def test_disabled_does_not_search(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.market_settings.enabled = False
            with mock.patch.object(self.module.market_engine, "search") as searcher:
                req = await self.send(plugin, "能不能做个记忆插件")
            searcher.assert_not_called()
            self.assertNotIn("<market_hits>", self.blocks_of(req))

        self.run_async(scenario())

    def test_empty_result_says_so(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search", return_value=([], "")):
                req = await self.send(plugin, "能不能做个量子计算插件")
            injected = self.blocks_of(req)
            self.assertIn("没有搜到现成的", injected)

        self.run_async(scenario())

    def test_network_error_is_silent(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=([], "市场请求失败：URLError")):
                req = await self.send(plugin, "能不能做个记忆插件")
            self.assertNotIn("<market_hits>", self.blocks_of(req))

        self.run_async(scenario())

    def test_search_is_cached(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=(HITS, "")) as searcher:
                await self.send(plugin, "能不能做个记忆插件")
                await self.send(plugin, "能不能做个记忆插件")
            self.assertEqual(searcher.call_count, 1, "同一句 5 分钟内不该重复打网络")

        self.run_async(scenario())


class CommandTest(PluginHarness, unittest.TestCase):
    def test_status_without_args(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_market(FakeEvent("#市场", umo=PRIVATE), "#市场")
            self.assertIn("市场：", text)
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_search_returns_list(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=(HITS, "")):
                text = await plugin._debug_market(
                    FakeEvent("#市场 记忆", umo=PRIVATE), "#市场 记忆"
                )
            self.assertIn("MemOS", text)
            self.assertIn("MIT", text)
            self.assertEqual(len(plugin.market_history.rows), 1)

        self.run_async(scenario())

    def test_failure_message(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=([], "连不上")):
                text = await plugin._debug_market(
                    FakeEvent("#市场 记忆", umo=PRIVATE), "#市场 记忆"
                )
            self.assertIn("失败", text)

        self.run_async(scenario())


class ApiTest(PluginHarness, unittest.TestCase):
    def test_get(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "market"))["data"]
            self.assertIn("settings", data)
            self.assertIn("history", data)
            self.assertTrue(data["settings"]["auto"])

        self.run_async(scenario())

    def test_search_and_history(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.market_engine, "search",
                                   return_value=(HITS, "")):
                data = (await self.api(plugin, "market/search",
                                       body={"query": "能力词"}) if False else
                        await self.api(plugin, "market/search", body={"query": "记忆"}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["hits"]), 1)
            self.assertEqual(len(data["history"]), 1)

        self.run_async(scenario())

    def test_search_requires_query(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "market/search", body={}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())

    def test_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "market/save", body={
                "auto": False, "owner_only": False, "limit": 3,
            }))["data"]
            self.assertTrue(data["saved"])
            self.assertFalse(data["settings"]["auto"])
            self.assertEqual(data["settings"]["limit"], 3)
            self.assertTrue((plugin.data_dir / "market.json").is_file())

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

