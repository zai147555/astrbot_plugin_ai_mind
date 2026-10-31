
"""知识树 / 动图 / 音检接进插件：命令、注入、面板接口。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import animation, knowledge, soundcheck

from tests.test_plugin_smoke import GRP, OWNER, PRIVATE, FakeEvent, PluginHarness


class KnowledgeCommandTest(PluginHarness, unittest.TestCase):
    def test_empty_shows_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_knowledge(FakeEvent("#知识", umo=PRIVATE), "#知识")
            self.assertIn("知识树：0 棵", text)
            self.assertIn("#知识 学", text)

        self.run_async(scenario())

    def test_create_activate_add(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("#知识 学 本群规矩", umo=PRIVATE)
            self.assertIn("建好了", plugin._debug_knowledge(event, "#知识 学 本群规矩"))
            self.assertEqual(len(plugin.knowledge.trees), 1)
            self.assertIn("挂上", plugin._debug_knowledge(event, "#知识 开 本群规矩"))
            self.assertEqual(len(plugin.knowledge.active_trees(PRIVATE)), 1)
            added = plugin._debug_knowledge(event, "#知识 加 本群规矩 称呼 群里别喊真名")
            self.assertIn("已加到", added)
            self.assertTrue((plugin.data_dir / "knowledge.json").is_file())
            listed = plugin._debug_knowledge(event, "#知识")
            self.assertIn("本群规矩", listed)
            self.assertIn("*", listed)

        self.run_async(scenario())

    def test_toggle_off_and_unknown_tree(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("#知识", umo=PRIVATE)
            plugin._debug_knowledge(event, "#知识 学 t1")
            plugin._debug_knowledge(event, "#知识 开 t1")
            self.assertIn("已摘掉", plugin._debug_knowledge(event, "#知识 关 t1"))
            self.assertIn("没有这棵树", plugin._debug_knowledge(event, "#知识 开 不存在"))

        self.run_async(scenario())

    def test_review_flow(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("#知识", umo=PRIVATE)
            plugin._debug_knowledge(event, "#知识 学 t1")
            plugin._debug_knowledge(event, "#知识 加 t1 标题 原内容")
            tree = plugin.knowledge.trees[0]
            node = tree.nodes[0]
            edit = plugin.knowledge.propose(tree.id, node.id, content="改过的内容")
            listed = plugin._debug_knowledge(event, "#知识 待审")
            self.assertIn(edit.id, listed)
            self.assertIn("已批准", plugin._debug_knowledge(event, "#知识 批 " + edit.id))
            self.assertEqual(tree.get(node.id).content, "改过的内容")
            edit2 = plugin.knowledge.propose(tree.id, node.id, content="又改")
            self.assertIn("已丢弃", plugin._debug_knowledge(event, "#知识 否 " + edit2.id))
            self.assertEqual(tree.get(node.id).content, "改过的内容")

        self.run_async(scenario())

    def test_injected_only_when_activated(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = FakeEvent("#知识", umo=PRIVATE)
            plugin._debug_knowledge(event, "#知识 学 本群规矩")
            plugin._debug_knowledge(event, "#知识 加 本群规矩 称呼 群里别喊真名")
            req = await self.send(plugin, "在吗")
            self.assertNotIn("<knowledge>", self.blocks_of(req))
            plugin._debug_knowledge(event, "#知识 开 本群规矩")
            req2 = await self.send(plugin, "在吗")
            injected = self.blocks_of(req2)
            self.assertIn("<knowledge>", injected)
            self.assertIn("群里别喊真名", injected)

        self.run_async(scenario())


class MaterialCommandTest(PluginHarness, unittest.TestCase):
    def test_animation_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = await plugin._debug_animation(FakeEvent("#动图", umo=PRIVATE), "#动图")
            self.assertIn("用法", text)

        self.run_async(scenario())

    def test_animation_sends_image_privately_from_group(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "strip.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\nx")
                fake = {"ok": True, "image": str(image), "summary": "动了 8 帧",
                        "manifest": "动图：a.gif\n第 1 帧 00:00.00", "frames": []}
                with mock.patch.object(self.module.animation_engine, "build", return_value=fake), \
                        mock.patch.object(plugin.context, "send_message",
                                          new=mock.AsyncMock()) as sender:
                    text = await plugin._debug_animation(
                        FakeEvent("#动图 a.gif", umo=GRP, uid=OWNER), "#动图 a.gif"
                    )
            self.assertIn("私聊", text)
            targets = [call.args[0] for call in sender.await_args_list]
            self.assertTrue(targets)
            self.assertTrue(all("FriendMessage" in target for target in targets), targets)

        self.run_async(scenario())

    def test_animation_failure_is_a_sentence(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with mock.patch.object(self.module.animation_engine, "build", return_value={
                "ok": False, "error": "这不是动图（只有 1 帧）",
            }):
                text = await plugin._debug_animation(
                    FakeEvent("#动图 a.png", umo=PRIVATE), "#动图 a.png"
                )
            self.assertIn("不是动图", text)

        self.run_async(scenario())

    def test_soundcheck_report(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "name": "a.mp3", "duration": 213.0, "channels": 2,
                    "sample_rate": 44100, "lufs": -14.2, "lra": 8.3, "true_peak": -0.8,
                    "verdict": "响度正常。"}
            with mock.patch.object(self.module.soundcheck_engine, "analyze", return_value=fake), \
                    mock.patch.object(self.module.soundcheck_engine, "spectrum",
                                      return_value=False):
                text = await plugin._debug_soundcheck(
                    FakeEvent("#听歌 a.mp3", umo=PRIVATE), "#听歌 a.mp3"
                )
            self.assertIn("a.mp3", text)
            self.assertIn("-14.2 LUFS", text)
            self.assertIn("响度正常", text)

        self.run_async(scenario())

    def test_soundcheck_usage(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertIn("用法", await plugin._debug_soundcheck(
                FakeEvent("#听歌", umo=PRIVATE), "#听歌"))

        self.run_async(scenario())


class KnowledgeApiTest(PluginHarness, unittest.TestCase):
    def test_list_create_add_activate_review(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            empty = (await self.api(plugin, "knowledge", query={"session": PRIVATE}))["data"]
            self.assertEqual(empty["trees"], [])
            created = (await self.api(plugin, "knowledge/tree", body={
                "action": "create", "name": "本群规矩", "session": PRIVATE,
            }))["data"]
            self.assertEqual(len(created["trees"]), 1)
            added = (await self.api(plugin, "knowledge/tree", body={
                "action": "add", "tree": "本群规矩", "title": "称呼",
                "content": "群里别喊真名", "session": PRIVATE,
            }))["data"]
            self.assertIn("群里别喊真名", added["trees"][0]["outline"])
            active = (await self.api(plugin, "knowledge/active", body={
                "tree": "本群规矩", "on": True, "session": PRIVATE,
            }))["data"]
            self.assertTrue(active["trees"][0]["active"])
            tree = plugin.knowledge.trees[0]
            edit = plugin.knowledge.propose(tree.id, tree.nodes[0].id, content="新内容")
            reviewed = (await self.api(plugin, "knowledge/review", body={
                "id": edit.id, "approve": True, "session": PRIVATE,
            }))["data"]
            self.assertTrue(reviewed["done"])
            self.assertEqual(tree.nodes[0].content, "新内容")

        self.run_async(scenario())

    def test_errors(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            bad = (await self.api(plugin, "knowledge/tree", body={"action": "create", "name": ""}))
            self.assertFalse(bad.get("ok"))
            self.assertIn("树名", bad.get("message") or "")
            missing = (await self.api(plugin, "knowledge/tree", body={
                "action": "add", "tree": "没有这棵", "title": "x",
            }))
            self.assertFalse(missing.get("ok"))
            self.assertIn("这棵树", missing.get("message") or "")

        self.run_async(scenario())


class MaterialApiTest(PluginHarness, unittest.TestCase):
    def test_animation_api(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "s.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\nx")
                with mock.patch.object(self.module.animation_engine, "build", return_value={
                    "ok": True, "image": str(image), "summary": "8 帧",
                    "manifest": "第 1 帧", "frames": [],
                }):
                    data = (await self.api(plugin, "animation", body={"path": "a.gif"}))["data"]
            self.assertTrue(data["ok"])
            self.assertTrue(data["images"][0]["image"].startswith("data:image/png;base64,"))

        self.run_async(scenario())

    def test_animation_requires_path(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "animation", body={}))["data"]
            self.assertFalse(data["ok"])
            self.assertIn("路径", data["error"])

        self.run_async(scenario())

    def test_soundcheck_api(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = {"ok": True, "name": "a.mp3", "lufs": -14.2, "verdict": "正常"}
            with mock.patch.object(self.module.soundcheck_engine, "analyze", return_value=fake), \
                    mock.patch.object(self.module.soundcheck_engine, "spectrum",
                                      return_value=False):
                data = (await self.api(plugin, "soundcheck", body={"path": "a.mp3"}))["data"]
            self.assertTrue(data["ok"])
            self.assertIn("a.mp3", data["text"])

        self.run_async(scenario())

    def test_soundcheck_requires_path(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = (await self.api(plugin, "soundcheck", body={}))["data"]
            self.assertFalse(data["ok"])

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

