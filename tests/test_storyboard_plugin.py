
"""分镜接进插件：命令、面板接口、失败也要说人话。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import storyboard as sb

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class CommandTest(PluginHarness, unittest.TestCase):
    def test_no_argument_shows_selfcheck_and_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_storyboard(FakeEvent("#分镜", umo=PRIVATE), "#分镜")
            self.assertIn("分镜：", text)
            self.assertIn("ffmpeg", text)
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_missing_file_is_reported(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_storyboard(
                FakeEvent("#分镜 /nope/x.mp4", umo=PRIVATE), "#分镜 /nope/x.mp4"
            )
            self.assertIn("分镜失败", text)
            self.assertIn("文件不存在", text)

        self.run_async(scenario())

    def test_text_is_truncated_for_chat(self) -> None:
        result = {
            "ok": True,
            "summary": "片长 10:00.00｜2 张图",
            "manifest": "\n".join(["分镜：a.mp4"] + ["  第 %d 格 00:00.00" % i for i in range(1, 60)]),
            "sheets": [],
        }
        text = self._plugin_text(result)
        self.assertIn("还有", text)
        self.assertLessEqual(len(text.splitlines()), 45)

    @staticmethod
    def _plugin_text(result):
        from ai_mind_main import AIMindPlugin  # noqa: F401 - 只是确保模块已加载
        return AIMindPlugin._storyboard_text(result)

    def test_ok_result_mentions_manifest(self) -> None:
        result = {"ok": True, "summary": "片长 01:30.00｜1 张图｜共 9 格", "manifest": "分镜：a.mp4\n第 1 格 00:05.00", "sheets": []}
        text = self._plugin_text(result)
        self.assertIn("01:30.00", text)
        self.assertIn("第 1 格", text)


class ApiTest(PluginHarness, unittest.TestCase):
    def test_get_returns_selfcheck_and_tiers(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard"))["data"]
            self.assertIn("selfcheck", data)
            self.assertEqual(len(data["needs"]), 3)
            keys = [item["key"] for item in data["needs"]]
            self.assertEqual(keys, ["shot", "scene", "text"])
            for item in data["needs"]:
                self.assertGreaterEqual(item["per_sheet"], 1)

        self.run_async(scenario())

    def test_save_persists(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard/save", body={
                "enabled": True, "need": "text", "max_download_mb": 30,
            }))["data"]
            self.assertTrue(data["saved"])
            self.assertEqual(data["settings"]["need"], "text")
            self.assertTrue((plugin.data_dir / "storyboard.json").is_file())

        self.run_async(scenario())

    def test_save_rejects_unknown_need(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard/save", body={"need": "nonsense"}))["data"]
            self.assertEqual(data["settings"]["need"], sb.DEFAULT_NEED)

        self.run_async(scenario())

    def test_build_requires_a_path(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard/build", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("路径", data["error"])

        self.run_async(scenario())

    def test_build_returns_base64_images(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "sheet1.png"
                png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
                fake = {
                    "ok": True, "error": "", "hint": "",
                    "summary": "片长 01:30.00｜1 张图｜共 9 格",
                    "manifest": "分镜：a.mp4\n第 1 格 00:05.00",
                    "sheets": [{"i": 1, "from": 0, "to": 90, "image": str(png), "cells": [{}] * 9}],
                }
                with mock.patch.object(plugin, "_storyboard_build", return_value=fake):
                    data = (await self.api(plugin, "storyboard/build", body={"path": "a.mp4"}))["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["images"]), 1)
            self.assertTrue(data["images"][0]["image"].startswith("data:image/png;base64,"))
            self.assertEqual(data["images"][0]["cells"], 9)

        self.run_async(scenario())

    def test_build_failure_is_a_sentence(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(plugin, "_storyboard_build", return_value={
                "ok": False, "error": "没找到 ffmpeg/ffprobe", "hint": "去装 ffmpeg",
            }):
                data = (await self.api(plugin, "storyboard/build", body={"path": "a.mp4"}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("ffmpeg", data["error"])
            self.assertIn("装", data["hint"])

        self.run_async(scenario())

    def test_disabled_setting_blocks_build(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.storyboard_settings.enabled = False
            result = plugin._storyboard_build("a.mp4")
            self.assertFalse(result["ok"])
            self.assertIn("关掉", result["error"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

