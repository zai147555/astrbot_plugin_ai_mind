
"""刷B站（合并分镜）：打分、去重、日报、命令与面板接口。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import bili

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class SettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        conf = bili.BiliSettings()
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.candidates, 6)
        self.assertEqual(conf.storyboard_n, 2)
        self.assertEqual(conf.min_seconds, 60)

    def test_clamps_and_dedupes(self) -> None:
        conf = bili.BiliSettings.from_dict({
            "candidates": 999, "storyboard_n": -3, "rids": ["1", "1", "x", "4"],
        })
        self.assertEqual(conf.candidates, 20)
        self.assertEqual(conf.storyboard_n, 0)
        self.assertEqual(conf.rids, [1, 4])

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bili.json"
            conf = bili.BiliSettings(keywords="分镜 动画", candidates=8)
            self.assertTrue(bili.save_settings(path, conf))
            self.assertEqual(bili.load_settings(path).to_dict(), conf.to_dict())

    def test_broken_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bili.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertTrue(bili.load_settings(path).enabled)


class SeenTest(unittest.TestCase):
    def test_mark_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = bili.SeenStore(Path(tmp) / "seen.json", keep=20)
            self.assertFalse(seen.known("BV1"))
            seen.mark("BV1")
            self.assertTrue(seen.known("BV1"))
            self.assertEqual(seen.count(), 1)
            self.assertEqual(seen.clear(), 1)
            self.assertEqual(seen.count(), 0)

    def test_prunes_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = bili.SeenStore(Path(tmp) / "seen.json", keep=20)
            for index in range(30):
                seen.mark("BV%d" % index, when=index)
            self.assertLessEqual(seen.count(), 20)
            self.assertTrue(seen.known("BV29"))

    def test_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            bili.SeenStore(path).mark("BV1")
            again = bili.SeenStore(path)
            self.assertTrue(again.known("BV1"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["BV1"] > 0, True)


class NormalizeTest(unittest.TestCase):
    def test_newlist_shape(self) -> None:
        row = bili.normalize({
            "bvid": "BV1", "title": " t ", "duration": 300,
            "owner": {"name": "UP"}, "stat": {"view": 1234}, "pubdate": 1700000000,
        })
        self.assertEqual(row["title"], "t")
        self.assertEqual(row["up"], "UP")
        self.assertEqual(row["view"], 1234)
        self.assertTrue(row["url"].endswith("BV1"))

    def test_view_shape_has_cid(self) -> None:
        row = bili.normalize({
            "bvid": "BV1", "title": "x", "duration": 90,
            "owner": {"name": "UP"}, "pages": [{"cid": 42}], "stat": {"view": 5},
        })
        self.assertEqual(row["cid"], 42)


class ScoreTest(unittest.TestCase):
    def test_keyword_hit_raises_score(self) -> None:
        conf = bili.BiliSettings(keywords="分镜")
        plain = bili.score_item({"title": "随手拍", "duration": 600}, conf)
        hit = bili.score_item({"title": "教你做分镜", "duration": 600}, conf)
        self.assertGreater(hit[0], plain[0])
        self.assertTrue(any("分镜" in reason for reason in hit[1]))

    def test_too_short_is_penalised(self) -> None:
        conf = bili.BiliSettings(min_seconds=60)
        score, reasons = bili.score_item({"title": "x", "duration": 10}, conf)
        self.assertLess(score, 0)
        self.assertTrue(any("太短" in reason for reason in reasons))

    def test_subtitle_counts(self) -> None:
        conf = bili.BiliSettings()
        none = bili.score_item({"title": "x", "duration": 300}, conf)[0]
        rich = bili.score_item({"title": "x", "duration": 300, "subtitle": "字" * 300}, conf)[0]
        self.assertGreater(rich, none)


class PickTest(unittest.TestCase):
    def _items(self):
        return [
            {"bvid": "BV1", "title": "分镜教程", "duration": 600, "view": 200000},
            {"bvid": "BV2", "title": "随手拍", "duration": 600, "view": 10},
            {"bvid": "BV3", "title": "太短了", "duration": 5, "view": 10},
            {"bvid": "BV1", "title": "重复", "duration": 600, "view": 1},
        ]

    def test_filters_and_sorts(self) -> None:
        conf = bili.BiliSettings(keywords="分镜", candidates=3)
        picked = bili.pick(self._items(), conf)
        self.assertEqual(picked[0]["bvid"], "BV1")
        self.assertNotIn("BV3", [row["bvid"] for row in picked])
        self.assertTrue(all("score" in row for row in picked))

    def test_seen_items_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = bili.SeenStore(Path(tmp) / "s.json")
            seen.mark("BV1")
            picked = bili.pick(self._items(), bili.BiliSettings(), seen)
        self.assertEqual([row["bvid"] for row in picked], ["BV2"])

    def test_candidate_cap(self) -> None:
        conf = bili.BiliSettings(candidates=1)
        self.assertEqual(len(bili.pick(self._items(), conf)), 1)

    def test_run_limit_overrides_settings(self) -> None:
        conf = bili.BiliSettings(candidates=6)
        self.assertEqual(len(bili.pick(self._items(), conf, limit=1)), 1)

    def test_run_limit_never_exceeds_available(self) -> None:
        conf = bili.BiliSettings(candidates=6)
        self.assertEqual(len(bili.pick(self._items(), conf, limit=50)), 2)

    def test_discover_regions_have_names(self) -> None:
        self.assertTrue(bili.DISCOVER_RIDS)
        for rid, name in bili.DISCOVER_RIDS.items():
            self.assertIsInstance(rid, int)
            self.assertTrue(name)
        self.assertIn(4, bili.DISCOVER_RIDS)


class ReportTest(unittest.TestCase):
    def test_report_shape(self) -> None:
        items = [{
            "bvid": "BV1", "title": "教程", "up": "UP", "duration": 600,
            "view": 120000, "url": "https://b23.tv/BV1",
            "reasons": ["命中关键词"], "subtitle": "今天讲分镜", "storyboard": "9 格",
        }]
        text = bili.build_report("2026-11-04", items)
        self.assertIn("# 2026-11-04 刷B站", text)
        self.assertIn("### BV1", text)
        self.assertIn("为什么值得", text)
        self.assertIn("分镜：9 格", text)
        self.assertIn("12.0 万", text)

    def test_empty_report(self) -> None:
        self.assertIn("没有值得推的", bili.build_report("2026-11-04", []))

    def test_format_helpers(self) -> None:
        self.assertEqual(bili.format_duration(90), "1:30")
        self.assertEqual(bili.format_count(12345), "1.2 万")
        self.assertEqual(bili.format_count(123456789), "1.2 亿")


class NetworkTest(unittest.TestCase):
    def test_region_parses_list(self) -> None:
        payload = {"code": 0, "data": {"archives": [
            {"bvid": "BV1", "title": "a", "duration": 300},
            {"bvid": "BV2", "title": "b", "duration": 60},
        ]}}
        with mock.patch.object(bili, "_get_json", return_value=(payload, "")):
            rows, err = bili.fetch_region(1, 2)
        self.assertEqual(err, "")
        self.assertEqual([row["bvid"] for row in rows], ["BV1", "BV2"])

    def test_region_error_is_reported(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=(None, "接口返回 -352")):
            rows, err = bili.fetch_region(1, 2)
        self.assertEqual(rows, [])
        self.assertIn("-352", err)

    def test_pick_stream_reads_durl(self) -> None:
        payload = {"code": 0, "data": {"durl": [{"url": "https://x/1.mp4", "size": 1234}]}}
        with mock.patch.object(bili, "_get_json", return_value=(payload, "")):
            url, size, err = bili.pick_stream("BV1", 42)
        self.assertEqual((url, size, err), ("https://x/1.mp4", 1234, ""))

    def test_pick_stream_without_cid(self) -> None:
        url, size, err = bili.pick_stream("BV1", 0)
        self.assertEqual(url, "")
        self.assertIn("cid", err)

    def test_subtitle_joins_lines(self) -> None:
        payload = {"code": 0, "data": {"subtitle": {"subtitles": [
            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//sub/x.json"},
        ]}}}
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = json.dumps(
            {"body": [{"content": "第一句"}, {"content": "第二句"}]}
        ).encode()
        with mock.patch.object(bili, "_get_json", return_value=(payload, "")), \
                mock.patch("urllib.request.urlopen", return_value=body):
            text, lines, lang = bili.fetch_subtitle("BV1", 42)
        self.assertEqual(text, "第一句 第二句")
        self.assertEqual(lines, 2)
        self.assertEqual(lang, "中文")

    def test_subtitle_missing_is_empty(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=({"code": 0, "data": {}}, "")):
            self.assertEqual(bili.fetch_subtitle("BV1", 42), ("", 0, ""))

    def test_download_respects_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = mock.MagicMock()
            handler.__enter__.return_value.read.side_effect = [b"x" * 1024 * 1024 * 2, b""]
            with mock.patch("urllib.request.urlopen", return_value=handler):
                ok, err = bili.download("https://x/1.mp4", Path(tmp) / "a.mp4",
                                        max_bytes=1024 * 1024)
        self.assertFalse(ok)
        self.assertIn("上限", err)


class PluginTest(PluginHarness, unittest.TestCase):
    def _mock_network(self):
        rows = [
            {"bvid": "BV1", "title": "分镜教程", "desc": "", "up": "UP",
             "duration": 600, "view": 200000, "cid": 11, "url": "u1"},
            {"bvid": "BV2", "title": "随手拍", "desc": "", "up": "UP",
             "duration": 600, "view": 10, "cid": 22, "url": "u2"},
        ]
        return (
            mock.patch.object(self.module.bili_engine, "fetch_region", return_value=(rows, "")),
            mock.patch.object(self.module.bili_engine, "fetch_view", return_value=(rows[0], "")),
            mock.patch.object(self.module.bili_engine, "fetch_subtitle",
                              return_value=("讲分镜了", 3, "中文")),
            mock.patch.object(self.module.bili_engine, "pick_stream",
                              return_value=("https://x/1.mp4", 100, "")),
            mock.patch.object(self.module.bili_engine, "download", return_value=(True, "")),
        )

    def test_scroll_builds_report_and_storyboard(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings = bili.BiliSettings(candidates=3, storyboard_n=1)
            fake = {
                "ok": True, "summary": "片长 10:00.00｜1 张图｜共 9 格",
                "manifest": "分镜：x.mp4\n第 1 格 00:30.00",
                "sheets": [{"i": 1, "image": str(Path(self.data_dir) / "nope.png")}],
            }
            patches = self._mock_network()
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    mock.patch.object(plugin, "_storyboard_build", return_value=fake):
                result = await self.module.asyncio.to_thread(plugin._bili_scroll, 3)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(len(result["items"]), 2)
            self.assertEqual(result["items"][0]["bvid"], "BV1")
            self.assertIn("### BV1", result["report"])
            self.assertEqual(len(result["sheets"]), 1)
            self.assertTrue(Path(result["report_path"]).is_file())
            self.assertTrue(plugin.bili_seen.known("BV1"))

        self.run_async(scenario())

    def test_scroll_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.enabled = False
            result = plugin._bili_scroll(3)
            self.assertFalse(result["ok"])
            self.assertIn("关掉", result["error"])

        self.run_async(scenario())

    def test_command_prints_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "day": "2026-11-04", "items": [],
                    "report": "# 2026-11-04 刷B站\n\n今天挑了 1 条：\n\n### BV1",
                    "sheets": [], "errors": ["分区 4：接口返回 -352"]}
            with mock.patch.object(plugin, "_bili_scroll", return_value=fake):
                text = await plugin._debug_bili(FakeEvent("#刷B站", umo=PRIVATE), "#刷B站 3")
            self.assertIn("### BV1", text)
            self.assertIn("-352", text)

        self.run_async(scenario())

    def test_api_get_and_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili"))["data"]
            self.assertIn("settings", data)
            self.assertIn("seen", data)
            saved = (await self.api(plugin, "bili/save", body={
                "keywords": "分镜", "candidates": 4, "storyboard_n": 1,
            }))["data"]
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["settings"]["keywords"], "分镜")
            self.assertTrue((plugin.data_dir / "bili.json").is_file())

        self.run_async(scenario())

    def test_api_save_regions(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/save", body={
                "rids": [4, 36, 999], "candidates": 4,
            }))["data"]
            # 自定义 rid 也留着（面板只列常用的那几个，手改 json 仍可用）
            self.assertEqual(data["settings"]["rids"], [4, 36, 999])
            self.assertEqual(data["settings"]["candidates"], 4)
            self.assertTrue(data["regions"])

        self.run_async(scenario())

    def test_api_scroll_and_clear(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "day": "2026-11-04", "report": "# r",
                    "items": [{"bvid": "BV1", "title": "t", "score": 1.0}],
                    "sheets": [], "errors": []}
            with mock.patch.object(plugin, "_bili_scroll", return_value=fake):
                data = (await self.api(plugin, "bili/scroll", body={}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["items"][0]["bvid"], "BV1")
            plugin.bili_seen.mark("BV1")
            cleared = (await self.api(plugin, "bili/clear", body={}))["data"]
            self.assertEqual(cleared["removed"], 1)

        self.run_async(scenario())

    def test_api_single_storyboard(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "s.png"
                png.write_bytes(b"\x89PNG\r\n\x1a\nx")
                fake = {"ok": True, "summary": "1 张图", "manifest": "第 1 格",
                        "title": "t", "sheets": [{"i": 1, "image": str(png)}]}
                with mock.patch.object(plugin, "_bili_one_storyboard", return_value=fake):
                    data = (await self.api(plugin, "bili/storyboard", body={"bvid": "BV1"}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["bvid"], "BV1")
            self.assertTrue(data["images"][0]["image"].startswith("data:image/png;base64,"))

        self.run_async(scenario())

    def test_api_storyboard_requires_bvid(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/storyboard", body={}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

