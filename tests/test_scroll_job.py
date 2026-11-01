
"""后台刷站任务：启动 → 轮询 → 结果/错误。"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from tests.test_plugin_smoke import PluginHarness


class ScrollJobTest(PluginHarness, unittest.TestCase):
    def _fake(self):
        return {
            "ok": True, "day": "2026-11-01", "report": "# 日报\n\n### BV1",
            "items": [{"bvid": "BV1", "title": "t", "score": 1.0}],
            "sheets": [], "errors": [],
        }

    def test_start_then_state(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(plugin, "_bili_scroll", return_value=self._fake()), \
                    mock.patch.object(plugin, "_bili_comments", side_effect=lambda r: r):
                started = (await self.api(plugin, "bili/scroll/start", body={}))["data"]
                self.assertTrue(started["ok"])
                job = started["job"]
                state = {}
                for _ in range(100):
                    state = (await self.api(plugin, "bili/scroll/state",
                                            body={"job": job}))["data"]
                    if state.get("status") != "running":
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(state["status"], "done")
            self.assertTrue(state["result"]["ok"])
            self.assertIn("### BV1", state["result"]["report"])
            self.assertEqual(state["result"]["items"][0]["bvid"], "BV1")

        self.run_async(scenario())

    def test_failure_is_reported(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(plugin, "_bili_scroll",
                                   side_effect=RuntimeError("B站连不上")):
                started = (await self.api(plugin, "bili/scroll/start", body={}))["data"]
                job = started["job"]
                state = {}
                for _ in range(100):
                    state = (await self.api(plugin, "bili/scroll/state",
                                            body={"job": job}))["data"]
                    if state.get("status") != "running":
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(state["status"], "error")
            self.assertIn("B站连不上", state["error"])

        self.run_async(scenario())

    def test_unknown_job(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/scroll/state", body={"job": "nope"}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

