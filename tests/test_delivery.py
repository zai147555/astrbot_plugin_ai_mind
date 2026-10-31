
"""结果投递：群里发指令 → 大块结果进指令 QQ 的私聊。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import delivery

from tests.test_plugin_smoke import GRP, OWNER, PRIVATE, FakeEvent, PluginHarness


class PrivateUmoTest(unittest.TestCase):
    def test_group_becomes_friend(self) -> None:
        self.assertEqual(
            delivery.private_umo("aiocqhttp:GroupMessage:778899", "42"),
            "aiocqhttp:FriendMessage:42",
        )

    def test_platform_override(self) -> None:
        self.assertEqual(
            delivery.private_umo("x:GroupMessage:1", "42", "telegram"),
            "telegram:FriendMessage:42",
        )

    def test_empty_inputs(self) -> None:
        self.assertEqual(delivery.private_umo("", "42"), "")
        self.assertEqual(delivery.private_umo("aiocqhttp:GroupMessage:1", ""), "")

    def test_is_group(self) -> None:
        self.assertTrue(delivery.is_group("aiocqhttp:GroupMessage:1"))
        self.assertFalse(delivery.is_group("aiocqhttp:FriendMessage:1"))


class SettingsTest(unittest.TestCase):
    def test_default_is_on(self) -> None:
        self.assertTrue(delivery.DeliverySettings().bulk_to_private)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.json"
            conf = delivery.DeliverySettings(bulk_to_private=False)
            self.assertTrue(delivery.save_settings(path, conf))
            self.assertFalse(delivery.load_settings(path).bulk_to_private)

    def test_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertTrue(delivery.load_settings(path).bulk_to_private)


class BulkDeliveryTest(PluginHarness, unittest.TestCase):
    def _storyboard_result(self, directory: Path) -> dict:
        image = directory / "sheet1.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nx")
        return {
            "ok": True, "error": "", "hint": "",
            "summary": "片长 01:30.00｜1 张图｜共 9 格",
            "manifest": "分镜：a.mp4\n第 1 格 00:05.00",
            "sheets": [{"i": 1, "from": 0, "to": 90, "image": str(image), "cells": [{}] * 9}],
        }

    def test_group_command_goes_to_private(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                fake = self._storyboard_result(Path(tmp))
                with mock.patch.object(plugin, "_storyboard_build", return_value=fake), \
                        mock.patch.object(plugin.context, "send_message",
                                          new=mock.AsyncMock()) as sender:
                    text = await plugin._debug_storyboard(
                        FakeEvent("#分镜 a.mp4", umo=GRP, uid=OWNER), "#分镜 a.mp4"
                    )
            self.assertIn("私聊", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(targets)
            self.assertTrue(all("FriendMessage" in target for target in targets), targets)
            self.assertTrue(all(target.endswith(OWNER) for target in targets), targets)

        self.run_async(scenario())

    def test_private_command_stays_in_place(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                fake = self._storyboard_result(Path(tmp))
                with mock.patch.object(plugin, "_storyboard_build", return_value=fake), \
                        mock.patch.object(plugin.context, "send_message",
                                          new=mock.AsyncMock()) as sender:
                    text = await plugin._debug_storyboard(
                        FakeEvent("#分镜 a.mp4", umo=PRIVATE, uid=OWNER), "#分镜 a.mp4"
                    )
            self.assertIn("第 1 格", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(targets)
            self.assertTrue(all(target == PRIVATE for target in targets), targets)

        self.run_async(scenario())

    def test_switch_off_keeps_it_in_the_group(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.delivery_settings.bulk_to_private = False
            with tempfile.TemporaryDirectory() as tmp:
                fake = self._storyboard_result(Path(tmp))
                with mock.patch.object(plugin, "_storyboard_build", return_value=fake), \
                        mock.patch.object(plugin.context, "send_message",
                                          new=mock.AsyncMock()) as sender:
                    text = await plugin._debug_storyboard(
                        FakeEvent("#分镜 a.mp4", umo=GRP, uid=OWNER), "#分镜 a.mp4"
                    )
            self.assertIn("第 1 格", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(targets)
            self.assertTrue(all(target == GRP for target in targets), targets)

        self.run_async(scenario())

    def test_failed_storyboard_stays_in_chat(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(plugin, "_storyboard_build", return_value={
                "ok": False, "error": "没找到 ffmpeg/ffprobe", "hint": "去装",
            }), mock.patch.object(plugin.context, "send_message",
                                  new=mock.AsyncMock()) as sender:
                text = await plugin._debug_storyboard(
                    FakeEvent("#分镜 a.mp4", umo=GRP, uid=OWNER), "#分镜 a.mp4"
                )
            self.assertIn("ffmpeg", text)
            self.assertEqual(sender.await_count, 0, "报错就该原地回，别让人去私聊找")

        self.run_async(scenario())

    def test_bili_report_goes_to_private(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "day": "2026-11-04", "report": "# 日报\n\n### BV1",
                    "items": [], "sheets": [], "errors": []}
            with mock.patch.object(plugin, "_bili_scroll", return_value=fake), \
                    mock.patch.object(plugin, "_bili_comments", side_effect=lambda r: r), \
                    mock.patch.object(plugin.context, "send_message",
                                      new=mock.AsyncMock()) as sender:
                text = await plugin._debug_bili(FakeEvent("#刷B站", umo=GRP, uid=OWNER), "#刷B站")
            self.assertIn("私聊", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(targets)
            self.assertTrue(all("FriendMessage" in target for target in targets), targets)

        self.run_async(scenario())

    def test_api_exposes_and_saves_the_switch(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "storyboard"))["data"]
            self.assertIn("delivery", data)
            self.assertTrue(data["delivery"]["bulk_to_private"])
            saved = (await self.api(plugin, "storyboard/save", body={
                "bulk_to_private": False,
            }))["data"]
            self.assertFalse(saved["delivery"]["bulk_to_private"])
            self.assertTrue((plugin.data_dir / "delivery.json").is_file())
            again = (await self.api(plugin, "bili"))["data"]
            self.assertFalse(again["delivery"]["bulk_to_private"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

