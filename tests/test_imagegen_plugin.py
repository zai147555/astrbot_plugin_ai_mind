
"""生图接进插件：命令、投递、面板接口。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import imagegen as ig

from tests.test_plugin_smoke import GRP, OWNER, PRIVATE, FakeEvent, PluginHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


class CommandTest(PluginHarness, unittest.TestCase):
    def test_status_without_args(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_imagegen(FakeEvent("#画", umo=PRIVATE), "#画")
            self.assertIn("生图：", text)
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_disabled_refuses(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_imagegen(FakeEvent("#画 猫", umo=PRIVATE), "#画 猫")
            self.assertIn("关着", text)

        self.run_async(scenario())

    def test_generate_sends_privately_from_group(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.imagegen_settings.enabled = True
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "a.png"
                image.write_bytes(PNG)
                fake = {"ok": True, "images": [str(image)], "note": "", "size": "1024x1024"}
                with mock.patch.object(self.module.imagegen_engine, "generate",
                                       return_value=fake), \
                        mock.patch.object(plugin.context, "send_message",
                                          new=mock.AsyncMock()) as sender:
                    text = await plugin._debug_imagegen(
                        FakeEvent("#画 猫", umo=GRP, uid=OWNER), "#画 猫"
                    )
            self.assertIn("私聊", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(all("FriendMessage" in target for target in targets), targets)

        self.run_async(scenario())

    def test_edit_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.imagegen_settings.enabled = True
            text = await plugin._debug_imagegen(FakeEvent("#画 改", umo=PRIVATE), "#画 改")
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_edit_calls_engine_with_ref(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.imagegen_settings.enabled = True
            with tempfile.TemporaryDirectory() as tmp:
                ref = Path(tmp) / "ref.png"
                ref.write_bytes(PNG)
                out = Path(tmp) / "out.png"
                out.write_bytes(PNG)
                fake = {"ok": True, "images": [str(out)], "note": "", "error": ""}
                with mock.patch.object(self.module.imagegen_engine, "edit",
                                       return_value=fake) as editor:
                    await plugin._debug_imagegen(
                        FakeEvent("#画 改 " + str(ref) + " 背景换灰", umo=PRIVATE),
                        "#画 改 " + str(ref) + " 背景换灰",
                    )
            args = editor.call_args.args
            self.assertEqual(args[0], "背景换灰")
            self.assertEqual(args[3], [str(ref)])

        self.run_async(scenario())


class ApiTest(PluginHarness, unittest.TestCase):
    def test_get_lists_adapters(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "imagegen"))["data"]
            keys = [item["key"] for item in data["adapters"]]
            self.assertIn("gpt-image", keys)
            self.assertIn("dashscope-wanx", keys)
            self.assertNotIn("api_key", data["settings"])

        self.run_async(scenario())

    def test_save_keeps_key_when_blank(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.api(plugin, "imagegen/save", body={
                "enabled": True, "base_url": "https://api.openai.com/v1",
                "model": "gpt-image-1", "api_key": "sk-1",
            })
            self.assertEqual(plugin.imagegen_settings.api_key, "sk-1")
            data = (await self.api(plugin, "imagegen/save", body={"api_key": ""}))["data"]
            self.assertEqual(plugin.imagegen_settings.api_key, "sk-1")
            cleared = (await self.api(plugin, "imagegen/save", body={"clear_key": True}))["data"]
            self.assertEqual(plugin.imagegen_settings.api_key, "")
            self.assertFalse(cleared["settings"]["has_key"])
            self.assertTrue((plugin.data_dir / "imagegen.json").is_file())

        self.run_async(scenario())

    def test_generate_returns_base64(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "a.png"
                image.write_bytes(PNG)
                with mock.patch.object(self.module.imagegen_engine, "generate",
                                       return_value={"ok": True, "images": [str(image)],
                                                     "note": "", "error": ""}):
                    data = (await self.api(plugin, "imagegen/generate",
                                           body={"prompt": "猫"}))["data"]
            self.assertTrue(data["ok"])
            self.assertTrue(data["images_b64"][0]["image"].startswith("data:image/png;base64,"))
            self.assertIn("画好了", data["text"])

        self.run_async(scenario())

    def test_generate_requires_prompt(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "imagegen/generate", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("画什么", data["error"])

        self.run_async(scenario())

    def test_edit_requires_ref_and_prompt(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "imagegen/edit",
                                   body={"prompt": "改", "refs": []}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("参考图", data["error"])

        self.run_async(scenario())

    def test_recent_lists_generated_files(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            folder = Path(plugin.data_dir) / "imagegen" / "out"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "one.png").write_bytes(PNG)
            data = (await self.api(plugin, "imagegen"))["data"]
            self.assertEqual(len(data["recent"]), 1)
            self.assertTrue(data["recent"][0]["image"].startswith("data:image/png;base64,"))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

