
"""全照搬那批新能力：WBI 搜索、yt-dlp、定时、放大、一句话。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mind import bili, storyboard as sb

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class WbiTest(unittest.TestCase):
    def test_sign_is_deterministic(self) -> None:
        one = bili.sign_wbi({"keyword": "分镜", "page": 1}, "k" * 32, wts=1700000000)
        two = bili.sign_wbi({"page": 1, "keyword": "分镜"}, "k" * 32, wts=1700000000)
        self.assertEqual(one, two)
        self.assertIn("wts=1700000000", one)
        self.assertIn("w_rid=", one)

    def test_sign_cleans_specials(self) -> None:
        signed = bili.sign_wbi({"keyword": "a!b'c(d)e*f"}, "k" * 32, wts=1)
        self.assertNotIn("%21", signed)
        self.assertIn("keyword=abcdef", signed)

    def test_sign_changes_with_key(self) -> None:
        one = bili.sign_wbi({"a": "1"}, "x" * 32, wts=1)
        two = bili.sign_wbi({"a": "1"}, "y" * 32, wts=1)
        self.assertNotEqual(one, two)


class SearchTest(unittest.TestCase):
    def _payload(self):
        return {"code": 0, "data": {"result": [
            {"bvid": "BV1", "title": "<em class=\"keyword\">分镜</em>教程",
             "description": "讲分镜", "author": "UP", "duration": "6:11",
             "play": 120000, "pubdate": 1700000000},
            {"bvid": "BV2", "title": "另一条", "duration": "96:19", "play": 5},
            {"title": "没有 bvid"},
        ]}}

    def test_search_parses_and_cleans(self) -> None:
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = json.dumps(self._payload()).encode()
        with mock.patch.object(bili, "_fetch_mixin_key", return_value="k" * 32), \
                mock.patch.object(bili, "_fetch_buvid", return_value="buvid"), \
                mock.patch("urllib.request.urlopen", return_value=body):
            rows, err = bili.search_videos("分镜", 5)
        self.assertEqual(err, "")
        self.assertEqual([row["bvid"] for row in rows], ["BV1", "BV2"])
        self.assertEqual(rows[0]["title"], "分镜教程")
        self.assertEqual(rows[0]["duration"], 371)
        self.assertEqual(rows[1]["duration"], 5779)

    def test_search_without_keyword(self) -> None:
        rows, err = bili.search_videos("  ")
        self.assertEqual(rows, [])
        self.assertIn("关键词", err)

    def test_search_without_mixin(self) -> None:
        with mock.patch.object(bili, "_fetch_mixin_key", return_value=""):
            rows, err = bili.search_videos("分镜")
        self.assertEqual(rows, [])
        self.assertIn("WBI", err)

    def test_search_api_error(self) -> None:
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = json.dumps(
            {"code": -412, "message": "请求被拦截"}
        ).encode()
        with mock.patch.object(bili, "_fetch_mixin_key", return_value="k" * 32), \
                mock.patch.object(bili, "_fetch_buvid", return_value=""), \
                mock.patch("urllib.request.urlopen", return_value=body):
            rows, err = bili.search_videos("分镜")
        self.assertEqual(rows, [])
        self.assertIn("-412", err)


class SubtitleLinesTest(unittest.TestCase):
    def test_lines_carry_timing(self) -> None:
        payload = {"code": 0, "data": {"subtitle": {"subtitles": [
            {"lan": "zh-CN", "subtitle_url": "//sub/x.json"},
        ]}}}
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = json.dumps({"body": [
            {"from": 1.0, "to": 3.0, "content": "第一句"},
            {"from": 4.0, "to": 6.5, "content": "第二句"},
        ]}).encode()
        with mock.patch.object(bili, "_get_json", return_value=(payload, "")), \
                mock.patch("urllib.request.urlopen", return_value=body):
            lines = bili.fetch_subtitle_lines("BV1", 42)
        self.assertEqual(len(lines), 2)
        self.assertAlmostEqual(lines[1]["from"], 4.0)
        self.assertEqual(lines[1]["text"], "第二句")

    def test_no_cid_is_empty(self) -> None:
        self.assertEqual(bili.fetch_subtitle_lines("BV1", 0), [])


class YtdlpTest(unittest.TestCase):
    def test_missing_binary(self) -> None:
        with mock.patch.object(bili, "ytdlp_path", return_value=""):
            ok, err = bili.download_with_ytdlp("u", "/tmp/x.mp4")
        self.assertFalse(ok)
        self.assertIn("yt-dlp", err)

    def test_command_shape_and_success(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen.append(list(cmd))
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"mp4")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            with mock.patch("subprocess.run", fake_run):
                ok, err = bili.download_with_ytdlp("https://b/x", out, bin_path="yt-dlp")
            self.assertTrue(ok, err)
            cmd = seen[-1]
            self.assertIn("--no-playlist", cmd)
            self.assertTrue(any("height<=480" in part for part in cmd))
            self.assertIn(str(out), cmd)

    def test_failure_message(self) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, b"", b"ERROR: boom")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("subprocess.run", fake_run):
                ok, err = bili.download_with_ytdlp("u", Path(tmp) / "v.mp4", bin_path="yt-dlp")
        self.assertFalse(ok)
        self.assertIn("boom", err)


class DueTest(unittest.TestCase):
    def _stamp(self, hour: int) -> float:
        return time.mktime((2026, 11, 4, hour, 30, 0, 0, 0, -1))

    def test_hits_the_right_hour(self) -> None:
        self.assertTrue(bili.due_today(8, now=self._stamp(8), last_run=0))
        self.assertFalse(bili.due_today(8, now=self._stamp(9), last_run=0))

    def test_only_once_a_day(self) -> None:
        ran = self._stamp(8)
        self.assertFalse(bili.due_today(8, now=self._stamp(8), last_run=ran))
        # 昨天跑过，今天该再跑
        yesterday = self._stamp(8) - 86400
        self.assertTrue(bili.due_today(8, now=self._stamp(8), last_run=yesterday))

    def test_disabled(self) -> None:
        self.assertFalse(bili.due_today(-1, now=self._stamp(8)))


class OneLinerReportTest(unittest.TestCase):
    def test_report_prefers_its_own_sentence(self) -> None:
        text = bili.build_report("2026-11-04", [{
            "bvid": "BV1", "title": "标题", "duration": 600, "view": 1000,
            "url": "u", "reasons": [], "subtitle": "字幕原文", "oneself": "一句话概括",
        }])
        self.assertIn("它说：一句话概括", text)
        self.assertIn("字幕摘录：字幕原文", text)

    def test_report_falls_back_to_subtitle(self) -> None:
        text = bili.build_report("2026-11-04", [{
            "bvid": "BV1", "title": "t", "duration": 60, "view": 1, "url": "u",
            "reasons": [], "subtitle": "只有字幕",
        }])
        self.assertIn("它说：只有字幕", text)


class PluginExtrasTest(PluginHarness, unittest.TestCase):
    def test_zoom_without_previous_storyboard(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_zoom(FakeEvent("#分镜放大 3", umo=PRIVATE), "#分镜放大 3")
            self.assertIn("还没有分镜", text)
            self.assertIn("用法", await plugin._debug_zoom(
                FakeEvent("#分镜放大", umo=PRIVATE), "#分镜放大"))

        self.run_async(scenario())

    def test_zoom_uses_last_storyboard(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "z.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\nx")
                plugin._last_storyboard = {
                    "source": "a.mp4", "plan": sb.plan(90), "workdir": tmp, "at": time.time(),
                }
                fake = {"ok": True, "cell": 3, "frames": 3, "image": str(image),
                        "lines": ["第 3 格放大：00:20.00–00:30.00（3 帧）"], "text": "台词"}
                with mock.patch.object(self.module.storyboard_engine, "zoom",
                                       return_value=fake):
                    text = await plugin._debug_zoom(
                        FakeEvent("#分镜放大 3", umo=PRIVATE), "#分镜放大 3")
            self.assertIn("第 3 格放大", text)
            self.assertIn("台词", text)

        self.run_async(scenario())

    def test_one_liner_updates_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.provider.responder = lambda prompt: "这条讲了怎么拍分镜"
            self.provider.response = "这条讲了怎么拍分镜"
            result = {
                "ok": True, "day": "2026-11-04",
                "items": [{"bvid": "BV1", "title": "t", "duration": 600, "view": 1,
                           "url": "u", "reasons": ["命中"], "subtitle": "字幕"}],
                "report": "旧", "report_path": str(Path(self.data_dir) / "r.md"),
            }
            result = await plugin._bili_comments(result)
            self.assertEqual(result["items"][0]["oneself"], "这条讲了怎么拍分镜")
            self.assertIn("它说：这条讲了怎么拍分镜", result["report"])

        self.run_async(scenario())

    def test_one_liner_skipped_when_off(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.one_liner = False
            result = {"ok": True, "items": [{"bvid": "BV1"}], "report": "旧"}
            same = await plugin._bili_comments(result)
            self.assertNotIn("oneself", same["items"][0])

        self.run_async(scenario())

    def test_daily_tick_sends_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.daily_hour = time.localtime().tm_hour
            plugin._bili_target = PRIVATE
            fake = {"ok": True, "day": "2026-11-04", "report": "# 日报", "items": [],
                    "sheets": [], "errors": []}
            with mock.patch.object(plugin, "_bili_scroll", return_value=fake), \
                    mock.patch.object(plugin, "_bili_comments", return_value=fake), \
                    mock.patch.object(plugin.context, "send_message",
                                      new=mock.AsyncMock()) as sender:
                await plugin._bili_daily_tick()
            self.assertTrue(sender.await_count >= 1)
            state = Path(plugin.data_dir) / "bili" / "last_run.json"
            self.assertTrue(state.is_file())
            # 同一天再跑一次：不该重复发
            with mock.patch.object(plugin, "_bili_scroll", return_value=fake), \
                    mock.patch.object(plugin, "_bili_comments", return_value=fake), \
                    mock.patch.object(plugin.context, "send_message",
                                      new=mock.AsyncMock()) as again:
                await plugin._bili_daily_tick()
            self.assertEqual(again.await_count, 0)

        self.run_async(scenario())

    def test_storyboard_settings_expose_new_switches(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard"))["data"]
            settings = data["settings"]
            for key in ("annotate", "audio_bar", "cache", "font_path"):
                self.assertIn(key, settings)
            self.assertTrue(settings["annotate"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

