
"""模仿指定 QQ：样本采集、说话指纹、注入块、命令与端到端接线。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind import imitate
from mind.memory.store import MemoryStore

from tests.test_plugin_smoke import OWNER, PRIVATE, FakeEvent, PluginHarness


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off(self) -> None:
        conf = imitate.ImitateSettings()
        self.assertFalse(conf.enabled, "采集别人的话必须先显式打开")
        self.assertEqual(conf.targets, [])
        self.assertTrue(conf.inject)
        self.assertEqual(conf.min_samples, 20)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imitate.json"
            conf = imitate.ImitateSettings(enabled=True, targets=["42"], min_samples=5)
            self.assertTrue(imitate.save_settings(path, conf))
            again = imitate.load_settings(path)
            self.assertEqual(again.to_dict(), conf.to_dict())

    def test_missing_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(imitate.load_settings(Path(tmp) / "nope.json").enabled)

    def test_broken_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imitate.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(imitate.load_settings(path).to_dict(),
                             imitate.ImitateSettings().to_dict())

    def test_targets_accept_string_and_list(self) -> None:
        one = imitate.ImitateSettings.from_dict({"targets": "2020689845 42"})
        self.assertEqual(one.targets, ["2020689845", "42"])
        two = imitate.ImitateSettings.from_dict({"targets": ["42", " 42 ", "7"]})
        self.assertEqual(two.targets, ["42", "7"])

    def test_min_samples_is_clamped(self) -> None:
        self.assertGreaterEqual(imitate.ImitateSettings.from_dict({"min_samples": 0}).min_samples, 3)
        self.assertLessEqual(imitate.ImitateSettings.from_dict({"min_samples": 9999}).min_samples, 500)

    def test_add_and_remove_target(self) -> None:
        conf = imitate.ImitateSettings()
        self.assertTrue(conf.add_target("42"))
        self.assertFalse(conf.add_target("42"))
        self.assertTrue(conf.remove_target("42"))
        self.assertFalse(conf.remove_target("42"))


class ProfileTest(unittest.TestCase):
    def test_empty_corpus(self) -> None:
        profile = imitate.build_profile("1", [])
        self.assertEqual(profile.samples, 0)
        self.assertEqual(imitate.describe(profile), [])
        self.assertEqual(imitate.render_block(profile), "")

    def test_short_talker(self) -> None:
        profile = imitate.build_profile("1", ["行", "嗯嗯", "好", "走了啊", "你别管"])
        self.assertLess(profile.avg_len, 4)
        self.assertAlmostEqual(profile.short_ratio, 1.0)
        self.assertTrue(any("短" in line for line in imitate.describe(profile)))

    def test_long_talker(self) -> None:
        long_text = "我今天去看了那个电影，感觉还行吧，就是中间有点拖沓，不过结尾还不错。"
        profile = imitate.build_profile("1", [long_text] * 4)
        self.assertGreater(profile.avg_len, 30)
        self.assertTrue(any("长句" in line for line in imitate.describe(profile)))

    def test_punctuation_and_particles(self) -> None:
        texts = ["真的吗？", "你怎么这样啊！", "我这就去…", "行吧"]
        profile = imitate.build_profile("1", texts)
        self.assertGreater(profile.question_ratio, 0.2)
        self.assertGreater(profile.exclaim_ratio, 0.2)
        self.assertGreater(profile.ellipsis_ratio, 0.2)
        self.assertTrue(profile.particles, "「啊」「吧」该被抓出来")
        self.assertTrue(any("问号" in line for line in imitate.describe(profile)))

    def test_quiet_punctuation_is_described(self) -> None:
        profile = imitate.build_profile("1", ["走了", "好", "行", "知道了"])
        self.assertEqual(profile.exclaim_ratio, 0.0)
        self.assertTrue(any("不用感叹号" in line for line in imitate.describe(profile)))

    def test_space_habit_is_recorded_but_not_taught(self) -> None:
        profile = imitate.build_profile("1", ["切 干嘛", "你 说呢", "行吧"])
        self.assertGreater(profile.space_ratio, 0.3)
        lines = imitate.describe(profile)
        self.assertTrue(any("空格" in line and "不学" in line for line in lines))

    def test_block_keeps_the_red_lines(self) -> None:
        profile = imitate.build_profile("1", ["行吧", "啊这…", "就是就是啊", "真的假的啊？"])
        block = imitate.render_block(profile, "宝宝")
        self.assertIn("<imitate_style>", block)
        for guard in ("照抄内容", "分几条发", "不要因为学他把中文拆开加空格"):
            self.assertIn(guard, block)

    def test_block_without_samples_is_empty(self) -> None:
        self.assertEqual(imitate.render_block(imitate.build_profile("1", [])), "")


class StoreTest(unittest.TestCase):
    def _store(self, tmp: str) -> imitate.ImitateStore:
        return imitate.ImitateStore(MemoryStore(Path(tmp) / "mind.db", None), None)

    def test_add_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertGreater(store.add_sample("42", "行吧"), 0)
            self.assertEqual(store.count("42"), 1)
            self.assertEqual(store.count("7"), 0)

    def test_duplicates_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_sample("42", "行吧")
            self.assertEqual(store.add_sample("42", "行吧"), 0)
            self.assertEqual(store.count("42"), 1)

    def test_too_short_and_too_long_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertEqual(store.add_sample("42", "嗯"), 0)
            self.assertEqual(store.add_sample("42", "啊" * 500), 0)
            self.assertEqual(store.count("42"), 0)

    def test_links_are_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_sample("42", "看这个 https://example.com/x 好搞笑")
            self.assertEqual(store.samples("42"), ["看这个  好搞笑"])

    def test_profile_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for text in ("行吧", "啊这", "就是就是啊", "真的假的啊？"):
                store.add_sample("42", text)
            profile = store.profile("42")
            self.assertEqual(profile.samples, 4)
            self.assertEqual(profile.uid, "42")
            self.assertTrue(imitate.render_block(profile))

    def test_users_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_sample("42", "行吧")
            store.add_sample("7", "好啊")
            store.add_sample("7", "行啊")
            self.assertEqual(dict(store.users()), {"42": 1, "7": 2})
            stats = store.stats()
            self.assertEqual((stats["users"], stats["samples"]), (2, 3))

    def test_clear_one_and_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_sample("42", "行吧")
            store.add_sample("7", "好啊")
            self.assertEqual(store.clear("42"), 1)
            self.assertEqual(store.count("42"), 0)
            self.assertEqual(store.clear(), 1)
            self.assertEqual(store.stats()["samples"], 0)


class PluginWiringTest(PluginHarness, unittest.TestCase):
    def _enable(self, plugin, minimum: int = 3) -> None:
        plugin.imitate_settings.enabled = True
        plugin.imitate_settings.targets = [OWNER]
        plugin.imitate_settings.min_samples = minimum

    def test_capture_only_for_targets(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._enable(plugin)
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            await plugin.on_imitate_capture(FakeEvent("路人说的话", umo=PRIVATE, uid="555"))
            await plugin.on_imitate_capture(FakeEvent("/help", umo=PRIVATE, uid=OWNER))
            self.assertEqual(plugin.imitate.count(OWNER), 1)
            self.assertEqual(plugin.imitate.count("555"), 0)

        self.run_async(scenario())

    def test_capture_is_off_by_default(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            self.assertEqual(plugin.imitate.stats()["samples"], 0)

        self.run_async(scenario())

    def test_block_needs_enough_samples(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._enable(plugin, minimum=3)
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            event = FakeEvent("在吗", umo=PRIVATE, uid=OWNER)
            self.assertEqual(plugin._imitate_block(event), "", "样本不够不该注入")
            await plugin.on_imitate_capture(FakeEvent("就是就是啊", umo=PRIVATE, uid=OWNER))
            await plugin.on_imitate_capture(FakeEvent("真的假的啊？", umo=PRIVATE, uid=OWNER))
            block = plugin._imitate_block(event)
            self.assertIn("<imitate_style>", block)
            self.assertIn("不要因为学他把中文拆开加空格", block)

        self.run_async(scenario())

    def test_block_skips_other_people_and_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self._enable(plugin, minimum=1)
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            self.assertTrue(plugin._imitate_block(FakeEvent("在吗", umo=PRIVATE, uid=OWNER)))
            self.assertEqual(plugin._imitate_block(FakeEvent("在吗", umo=PRIVATE, uid="555")), "")
            plugin.imitate_settings.inject = False
            self.assertEqual(plugin._imitate_block(FakeEvent("在吗", umo=PRIVATE, uid=OWNER)), "")
            plugin.imitate_settings.inject = True
            plugin.imitate_settings.enabled = False
            self.assertEqual(plugin._imitate_block(FakeEvent("在吗", umo=PRIVATE, uid=OWNER)), "")

        self.run_async(scenario())

    def test_settings_persist_to_disk(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            text = plugin._debug_imitate("#模仿 学 " + OWNER)
            self.assertIn("开始学", text)
            path = Path(plugin.data_dir) / "imitate.json"
            self.assertTrue(path.is_file(), "命令改的设置要落盘")
            conf = imitate.load_settings(path)
            self.assertTrue(conf.enabled)
            self.assertEqual(conf.targets, [OWNER])

        self.run_async(scenario())

    def test_commands(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._debug_imitate("#模仿 学 " + OWNER)
            await plugin.on_imitate_capture(FakeEvent("行吧", umo=PRIVATE, uid=OWNER))
            report = plugin._debug_imitate("#模仿")
            self.assertIn(OWNER, report)
            self.assertIn("1 条", report)
            self.assertIn("不再学", plugin._debug_imitate("#模仿 停 " + OWNER))
            self.assertIn("关", plugin._debug_imitate("#模仿 关"))
            self.assertIn("已清掉 1 条", plugin._debug_imitate("#模仿 清空"))
            self.assertIn("用法", plugin._debug_imitate("#模仿 学"))

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()

