
"""内置 ffmpeg + B站登录凭据：接口与面板接线。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import bili, ffmpeg_setup

from tests.test_plugin_smoke import PRIVATE, FakeEvent, PluginHarness

COOKIE = "SESSDATA=abc123; bili_jct=def456; buvid3=xyz789; DedeUserID=42"


class CookieTest(unittest.TestCase):
    def test_normalize_strips_prefix_and_newlines(self) -> None:
        self.assertEqual(bili.normalize_cookie("Cookie: a=1;\n b=2;"), "a=1; b=2")
        self.assertEqual(bili.normalize_cookie("  "), "")

    def test_cookie_value(self) -> None:
        self.assertEqual(bili.cookie_value(COOKIE, "SESSDATA"), "abc123")
        self.assertEqual(bili.cookie_value(COOKIE, "buvid3"), "xyz789")
        self.assertEqual(bili.cookie_value(COOKIE, "nope"), "")

    def test_get_json_attaches_cookie(self) -> None:
        seen = {}

        class Resp:
            status = 200

            def read(self):
                return json.dumps({"code": 0, "data": {}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            seen["cookie"] = request.headers.get("Cookie")
            seen["url"] = request.full_url
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            payload, err = bili._get_json("/x/web-interface/nav", None, cookie=COOKIE)
        self.assertEqual(err, "")
        self.assertEqual(seen["cookie"], "SESSDATA=abc123; bili_jct=def456; buvid3=xyz789; DedeUserID=42")
        self.assertIn("/x/web-interface/nav", seen["url"])

    def test_get_json_without_cookie(self) -> None:
        seen = {}

        class Resp:
            def read(self):
                return b"{\"code\":0}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            seen["cookie"] = request.headers.get("Cookie")
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            bili._get_json("/x/web-interface/view", {"bvid": "BV1"})
        self.assertIsNone(seen["cookie"])

    def test_login_check_ok(self) -> None:
        payload = {"code": 0, "data": {"isLogin": True, "uname": "挽风", "mid": 42,
                                       "level_info": {"current_level": 6}}}
        with mock.patch.object(bili, "_get_json", return_value=(payload, "")):
            report = bili.login_check(COOKIE)
        self.assertTrue(report["ok"])
        self.assertTrue(report["is_login"])
        self.assertEqual(report["uname"], "挽风")
        self.assertIn("已登录", report["message"])

    def test_login_check_not_logged_in(self) -> None:
        with mock.patch.object(bili, "_get_json", return_value=({"code": 0, "data": {"isLogin": False}}, "")):
            report = bili.login_check(COOKIE)
        self.assertTrue(report["ok"])
        self.assertFalse(report["is_login"])
        self.assertIn("无效", report["message"])

    def test_login_check_without_cookie(self) -> None:
        self.assertFalse(bili.login_check("")["ok"])

    def test_pick_stream_prefers_hq_when_logged_in(self) -> None:
        attempts = []

        def fake_get(path, params=None, timeout=0, cookie=""):
            attempts.append((params or {}).get("qn"))
            if len(attempts) == 1:
                return None, "没有可下载的直链"
            return {"code": 0, "data": {"durl": [{"url": "https://x/hd.mp4", "size": 999}]}}, ""

        with mock.patch.object(bili, "_get_json", fake_get):
            url, size, err = bili.pick_stream("BV1", 42, cookie=COOKIE, prefer_hq=True)
        self.assertEqual(url, "https://x/hd.mp4")
        self.assertEqual(attempts[0], 80)

    def test_pick_stream_anonymous_uses_default(self) -> None:
        attempts = []

        def fake_get(path, params=None, timeout=0, cookie=""):
            attempts.append((params or {}).get("qn"))
            return {"code": 0, "data": {"durl": [{"url": "https://x/sd.mp4"}]}}, ""

        with mock.patch.object(bili, "_get_json", fake_get):
            bili.pick_stream("BV1", 42, quality=32)
        self.assertEqual(attempts, [32])

    def test_masked_hides_cookie(self) -> None:
        conf = bili.BiliSettings(cookie=COOKIE, prefer_hq=False)
        shown = conf.masked()
        self.assertTrue(shown["has_cookie"])
        self.assertFalse(shown["prefer_hq"])
        self.assertNotIn(COOKIE, json.dumps(shown))
        self.assertNotIn('"cookie"', json.dumps(shown))

    def test_roundtrip_keeps_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bili.json"
            conf = bili.BiliSettings(cookie=COOKIE)
            self.assertTrue(bili.save_settings(path, conf))
            self.assertEqual(bili.load_settings(path).cookie, COOKIE)


class FfmpegApiTest(PluginHarness, unittest.TestCase):
    def test_install_updates_settings(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "message": "装好了：ffmpeg version 7.1",
                    "ffmpeg": "/tmp/data/bin/ffmpeg", "ffprobe": "/tmp/data/bin/ffprobe"}
            with mock.patch.object(self.module.ffmpeg_setup, "install", return_value=fake), \
                    mock.patch.object(self.module.ffmpeg_setup, "status", return_value={
                        "ok": True, "message": "ffmpeg version 7.1", "source": "bundled",
                        "version": "ffmpeg version 7.1", "bundled": True,
                    }):
                data = (await self.api(plugin, "storyboard/install-ffmpeg", body={}))["data"]
            self.assertTrue(data["ok"])
            self.assertIn("装好了", data["message"])
            self.assertEqual(plugin.storyboard_settings.ffmpeg_path, "/tmp/data/bin/ffmpeg")
            self.assertTrue((plugin.data_dir / "storyboard.json").is_file())

        self.run_async(scenario())

    def test_install_failure_is_reported(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.ffmpeg_setup, "install", return_value={
                "ok": False, "message": "所有下载源都不通", "ffmpeg": "", "ffprobe": "",
            }):
                data = (await self.api(plugin, "storyboard/install-ffmpeg", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("不通", data["message"])

        self.run_async(scenario())

    def test_storyboard_payload_exposes_ffmpeg(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard"))["data"]
            self.assertIn("ffmpeg", data)
            self.assertIn("ok", data["ffmpeg"])

        self.run_async(scenario())


class BiliLoginApiTest(PluginHarness, unittest.TestCase):
    def test_save_keeps_cookie_when_blank(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            first = (await self.api(plugin, "bili/save", body={"cookie": COOKIE}))["data"]
            self.assertTrue(first["settings"]["has_cookie"])
            self.assertEqual(plugin.bili_settings.cookie, COOKIE)
            again = (await self.api(plugin, "bili/save", body={"cookie": ""}))["data"]
            self.assertTrue(again["settings"]["has_cookie"], "留空不该抹掉凭据")
            cleared = (await self.api(plugin, "bili/save", body={"clear_cookie": True}))["data"]
            self.assertFalse(cleared["settings"]["has_cookie"])
            self.assertEqual(plugin.bili_settings.cookie, "")

        self.run_async(scenario())

    def test_login_check_api(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.bili_settings.cookie = COOKIE
            with mock.patch.object(self.module.bili_engine, "login_check", return_value={
                "ok": True, "is_login": True, "uname": "挽风", "mid": 42,
                "level": 6, "message": "已登录：挽风",
            }) as checker:
                data = (await self.api(plugin, "bili/login", body={}))["data"]
            self.assertTrue(data["is_login"])
            self.assertEqual(data["uname"], "挽风")
            checker.assert_called_once_with(COOKIE)

        self.run_async(scenario())

    def test_prefer_hq_saves(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/save", body={"prefer_hq": False}))["data"]
            self.assertFalse(data["settings"]["prefer_hq"])
            self.assertFalse(plugin.bili_settings.prefer_hq)

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

