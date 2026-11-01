
"""观看历史：解析、登录门槛、刷站优先用它、命令与接口。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import bili

from tests.test_plugin_smoke import GRP, OWNER, PRIVATE, FakeEvent, PluginHarness

COOKIE = "SESSDATA=s1; bili_jct=j1; DedeUserID=42"

HISTORY_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {"title": "关于鸣潮", "author_name": "某个UP", "duration": 120,
             "progress": 30, "history": {"bvid": "BV1xx", "cid": 11}, "view_at": 1700000000},
            {"title": "没有 bvid 的一条", "duration": 60},
        ],
        "cursor": {"is_end": False},
    },
}


class FetchHistoryTest(unittest.TestCase):
    def test_parse(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=(HISTORY_PAYLOAD, "")) as getter:
            rows, err = bili.fetch_history(COOKIE, 5)
        self.assertEqual(err, "")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["bvid"], "BV1xx")
        self.assertEqual(row["cid"], 11)
        self.assertEqual(row["duration"], 120)
        self.assertEqual(row["progress"], 30)
        self.assertTrue(row["from_history"])
        self.assertEqual(row["up"], "某个UP")
        self.assertIn("history/cursor", getter.call_args.args[0])

    def test_requires_login(self) -> None:
        rows, err = bili.fetch_history("")
        self.assertEqual(rows, [])
        self.assertIn("登录", err)

    def test_api_error(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=(None, "接口返回 -101")):
            rows, err = bili.fetch_history(COOKIE)
        self.assertEqual(rows, [])
        self.assertIn("-101", err)

    def test_bad_shape(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=({"code": 0, "data": {"list": "nope"}}, "")):
            rows, err = bili.fetch_history(COOKIE)
        self.assertEqual(rows, [])
        self.assertIn("格式", err)

    def test_history_gets_a_score_bonus(self) -> None:
        row = {"bvid": "BV1", "title": "x", "duration": 300, "progress": 120,
               "from_history": True}
        conf = bili.BiliSettings()
        normal = bili.score_item({"bvid": "BV2", "title": "x", "duration": 300}, conf)[0]
        boosted = bili.score_item(row, conf)[0]
        self.assertGreater(boosted, normal)
        reasons = bili.score_item(row, conf)[1]
        self.assertTrue(any("最近在看" in reason for reason in reasons))


class PluginHistoryTest(PluginHarness, unittest.TestCase):
    def test_command_without_cookie(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_history(FakeEvent("#在看", umo=PRIVATE), "#在看")
            self.assertIn("还没登录", text)

        self.run_async(scenario())

    def test_command_lists_history(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = COOKIE
            rows = [{"bvid": "BV1", "title": "关于鸣潮", "up": "UP",
                     "duration": 120, "progress": 30, "url": "u"}]
            with mock.patch.object(self.module.bili_engine, "fetch_history",
                                   return_value=(rows, "")):
                text = await plugin._debug_history(FakeEvent("#在看", umo=PRIVATE), "#在看 5")
            self.assertIn("关于鸣潮", text)
            self.assertIn("看到 25%", text)

        self.run_async(scenario())

    def test_api(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = COOKIE
            with mock.patch.object(self.module.bili_engine, "fetch_history",
                                   return_value=([{"bvid": "BV1", "title": "t", "up": "u",
                                                    "duration": 60, "progress": 10,
                                                    "url": "x"}], "")):
                data = (await self.api(plugin, "bili/history", body={"limit": 5}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["items"][0]["bvid"], "BV1")

        self.run_async(scenario())

    def test_api_requires_login(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/history", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("登录", data["error"])

        self.run_async(scenario())

    def test_scroll_uses_history_first(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = COOKIE
            plugin.bili_settings.use_history = True
            plugin.bili_settings.use_search = False
            history = [{"bvid": "BVH", "title": "我在看的", "up": "UP", "duration": 600,
                        "progress": 100, "view": 0, "cid": 5, "from_history": True,
                        "url": "u", "desc": ""}]
            with mock.patch.object(self.module.bili_engine, "fetch_history",
                                   return_value=(history, "")), \
                    mock.patch.object(self.module.bili_engine, "fetch_region",
                                      return_value=([], "")), \
                    mock.patch.object(self.module.bili_engine, "fetch_view",
                                      return_value=({"bvid": "BVH", "title": "我在看的",
                                                     "duration": 600, "cid": 5}, "")), \
                    mock.patch.object(self.module.bili_engine, "fetch_subtitle",
                                      return_value=("", 0, "")), \
                    mock.patch.object(self.module.bili_engine, "pick_stream",
                                      return_value=("", 0, "跳过下载")), \
                    mock.patch.object(plugin, "_storyboard_build",
                                      return_value={"ok": False, "error": "x"}):
                result = await self.module.asyncio.to_thread(plugin._bili_scroll, 3)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(result["items"])
            self.assertEqual(result["items"][0]["bvid"], "BVH")
            self.assertIn("最近在看", result["report"])

        self.run_async(scenario())

    def test_scroll_skips_history_without_login(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = ""
            with mock.patch.object(self.module.bili_engine, "fetch_history") as history, \
                    mock.patch.object(self.module.bili_engine, "fetch_region",
                                      return_value=([], "")), \
                    mock.patch.object(self.module.bili_engine, "search_videos",
                                      return_value=([], "")):
                await self.module.asyncio.to_thread(plugin._bili_scroll, 3)
            history.assert_not_called()

        self.run_async(scenario())

    def test_last_run_persisted_and_reloaded(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            payload = plugin._bili_payload_from_result({
                "ok": True, "day": "2026-11-01", "report": "今天挑了 1 条",
                "items": [{"bvid": "BV1", "title": "关于鸣潮", "up": "UP",
                           "duration": 120, "score": 9, "reasons": ["时长合适"]}],
                "sheets": [], "errors": [],
            })
            plugin._bili_save_last(payload)
            last = plugin._bili_payload()["last"]
            self.assertTrue(last["ok"])
            self.assertEqual(last["items"][0]["bvid"], "BV1")
            self.assertIn("今天挑了", last["report"])
            self.assertIn("saved_at", last)

        self.run_async(scenario())

    def test_last_run_failure_is_kept(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            payload = plugin._bili_payload_from_result(
                {"ok": False, "error": "一条都没拉到。分区 0：超时", "items": []}
            )
            plugin._bili_save_last(payload)
            last = plugin._bili_payload()["last"]
            self.assertFalse(last["ok"])
            self.assertIn("没拉到", last["error"])

        self.run_async(scenario())

    def test_last_run_rebuilds_images(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "sheet.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"s" * 16)
                payload = plugin._bili_payload_from_result({
                    "ok": True, "day": "2026-11-01", "report": "x", "items": [],
                    "sheets": [{"bvid": "BV1", "title": "t", "images": [str(image)]}],
                    "errors": [],
                })
                plugin._bili_save_last(payload)
                last = plugin._bili_payload()["last"]
            self.assertTrue(last["images"])
            self.assertTrue(last["images"][0]["image"].startswith("data:image"))
            self.assertEqual(last["images"][0]["bvid"], "BV1")

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

