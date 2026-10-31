
"""扫码登录：协议解析、状态映射、面板接口与落盘。"""
from __future__ import annotations

import unittest
from unittest import mock

from mind import bili

from tests.test_plugin_smoke import PRIVATE, PluginHarness

COOKIE = "SESSDATA=s1; bili_jct=j1; DedeUserID=42"


class QrProtocolTest(unittest.TestCase):
    def test_generate_ok(self) -> None:
        payload = {"code": 0, "data": {"url": "https://passport.bilibili.com/h5-app/x",
                                       "qrcode_key": "KEY123"}}
        with mock.patch.object(bili, "_get_json_headers",
                               return_value=(payload, [], "")) as getter:
            report = bili.qr_generate()
        self.assertTrue(report["ok"])
        self.assertEqual(report["key"], "KEY123")
        self.assertIn("passport", report["url"])
        self.assertIn("PASSPORT" if False else "passport-login/web/qrcode/generate",
                      getter.call_args.args[0])

    def test_generate_without_data(self) -> None:
        with mock.patch.object(bili, "_get_json_headers", return_value=({"code": 0, "data": {}}, [], "")):
            report = bili.qr_generate()
        self.assertFalse(report["ok"])

    def test_generate_error(self) -> None:
        with mock.patch.object(bili, "_get_json_headers", return_value=(None, [], "HTTP 500")):
            report = bili.qr_generate()
        self.assertFalse(report["ok"])
        self.assertIn("500", report["error"])

    def test_poll_states(self) -> None:
        cases = {
            86101: "waiting",
            86090: "scanned",
            86038: "expired",
        }
        for code, expected in cases.items():
            payload = {"code": 0, "data": {"code": code, "message": ""}}
            with mock.patch.object(bili, "_get_json_headers", return_value=(payload, [], "")):
                report = bili.qr_poll("KEY")
            self.assertEqual(report["status"], expected, code)
            self.assertEqual(report["cookie"], "")

    def test_poll_success_collects_cookie(self) -> None:
        payload = {"code": 0, "data": {"code": 0, "message": ""}}
        headers = [
            "SESSDATA=s1; Path=/; HttpOnly; Secure",
            "bili_jct=j1; Path=/",
            "DedeUserID=42; Path=/",
            "unrelated=nope; Path=/",
        ]
        with mock.patch.object(bili, "_get_json_headers", return_value=(payload, headers, "")):
            report = bili.qr_poll("KEY")
        self.assertEqual(report["status"], "success")
        self.assertIn("SESSDATA=s1", report["cookie"])
        self.assertIn("bili_jct=j1", report["cookie"])
        self.assertIn("DedeUserID=42", report["cookie"])
        self.assertNotIn("unrelated", report["cookie"])

    def test_poll_without_key(self) -> None:
        self.assertEqual(bili.qr_poll("")["status"], "error")

    def test_cookie_from_set_cookie_order(self) -> None:
        jar = bili.cookie_from_set_cookie([
            "DedeUserID=42; Path=/", "SESSDATA=s1; Path=/", "bili_jct=j1; Path=/",
        ])
        self.assertTrue(jar.startswith("SESSDATA="))
        self.assertIn("DedeUserID=42", jar)


class QrApiTest(PluginHarness, unittest.TestCase):
    def test_start(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.bili_engine, "qr_generate", return_value={
                "ok": True, "url": "https://passport.bilibili.com/x", "key": "K1", "error": "",
            }):
                data = (await self.api(plugin, "bili/qr/start", body={}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["key"], "K1")

        self.run_async(scenario())

    def test_poll_success_saves_cookie(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.bili_engine, "qr_poll", return_value={
                "status": "success", "message": "登录成功", "cookie": COOKIE, "code": 0,
            }), mock.patch.object(self.module.bili_engine, "login_check", return_value={
                "ok": True, "is_login": True, "uname": "挽风", "message": "已登录：挽风",
            }):
                data = (await self.api(plugin, "bili/qr/poll", body={"key": "K1"}))["data"]
            self.assertEqual(data["status"], "success")
            self.assertTrue(data["saved"])
            self.assertEqual(data["login"]["uname"], "挽风")
            self.assertEqual(plugin.bili_settings.cookie, COOKIE)
            self.assertTrue((plugin.data_dir / "bili.json").is_file())

        self.run_async(scenario())

    def test_poll_waiting_does_not_save(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.bili_engine, "qr_poll", return_value={
                "status": "waiting", "message": "等着扫码…", "cookie": "", "code": 86101,
            }):
                data = (await self.api(plugin, "bili/qr/poll", body={"key": "K1"}))["data"]
            self.assertFalse(data["saved"])
            self.assertEqual(plugin.bili_settings.cookie, "")

        self.run_async(scenario())

    def test_poll_requires_key(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "bili/qr/poll", body={}))["data"]
            self.assertEqual(data["status"], "error")

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

