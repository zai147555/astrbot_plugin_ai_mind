"""傻瓜模式的自检与推荐配置。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

_spec = importlib.util.spec_from_file_location(
    "smoke_wizard", PLUGIN_DIR / "tests" / "test_plugin_smoke.py"
)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_wizard"] = smoke
_spec.loader.exec_module(smoke)


class WizardHarness(smoke.PluginHarness, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

    async def payload(self, plugin, query=None):
        res = await self.api(plugin, "wizard", query=query or {})
        return res["data"]

    def levels(self, data) -> dict:
        return {item["key"]: item["level"] for item in data["checks"]}


class WizardTest(WizardHarness):
    def test_missing_owner_is_an_error(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"emotion": {"relationship": {"special_users": []}}})
            await plugin.initialize()
            data = await self.payload(plugin)
            self.assertEqual(self.levels(data).get("owner"), "error")
            self.assertGreaterEqual(data["errors"], 1)
            self.assertIn("必须修", data["summary"])
            item = [c for c in data["checks"] if c["key"] == "owner"][0]
            self.assertIn("人格定制", item["fix"])
            self.assertEqual(item["action"], "goto:persona")
            await plugin.terminate()

        self.run_async(scenario())

    def test_configured_owner_is_ok(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            data = await self.payload(plugin)
            self.assertEqual(self.levels(data).get("owner_ok"), "ok")
            self.assertEqual(data["errors"], 0)
            await plugin.terminate()

        self.run_async(scenario())

    def test_disabled_plugin_is_reported(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"enabled": False})
            await plugin.initialize()
            data = await self.payload(plugin)
            self.assertEqual(self.levels(data).get("disabled"), "error")
            await plugin.terminate()

        self.run_async(scenario())

    def test_warns_about_splitter_and_style_backlog(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"splitter": {"max_segments": 7}})
            await plugin.initialize()
            from mind import StyleExample

            for index in range(6):
                plugin.styles.add(StyleExample(
                    user_text="他问的第 %d 句话" % index,
                    reply_text="她回答的第 %d 句话" % index,
                    grams=tuple(["问%d" % index]),
                ))
            data = await self.payload(plugin)
            levels = self.levels(data)
            self.assertEqual(levels.get("split_many"), "warn")
            self.assertEqual(levels.get("style_pending"), "warn")
            self.assertGreaterEqual(data["warns"], 2)
            await plugin.terminate()

        self.run_async(scenario())

    def test_checks_come_back_sorted(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"emotion": {"relationship": {"special_users": []}}})
            await plugin.initialize()
            data = await self.payload(plugin)
            order = {"error": 0, "warn": 1, "ok": 2}
            ranks = [order.get(item["level"], 3) for item in data["checks"]]
            self.assertEqual(ranks, sorted(ranks))
            await plugin.terminate()

        self.run_async(scenario())

    def test_apply_writes_overrides(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "wizard/apply", body={})
            self.assertTrue(res["ok"], res)
            applied = res["data"]["applied"]
            self.assertIn("humanize", applied)
            self.assertIn("style", applied)
            self.assertIn("guard", applied)
            self.assertIn("splitter", applied)
            self.assertTrue((plugin.data_dir / "humanize_overrides.json").is_file())
            self.assertTrue((plugin.data_dir / "style_overrides.json").is_file())
            self.assertTrue((plugin.data_dir / "splitter_overrides.json").is_file())
            # 推荐值真的生效了
            self.assertEqual(plugin.settings.humanize.mode, "check")
            self.assertFalse(plugin.settings.style.auto_approve)
            self.assertEqual(plugin.splitter.max_segments, 3)
            await plugin.terminate()

        self.run_async(scenario())

    def test_apply_can_be_limited(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "wizard/apply", body={"groups": ["splitter"]})
            self.assertEqual(res["data"]["applied"], ["splitter"])
            self.assertFalse((plugin.data_dir / "humanize_overrides.json").is_file())
            await plugin.terminate()

        self.run_async(scenario())

    def test_mode_round_trip(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "wizard/mode", body={})
            self.assertEqual(res["data"]["mode"], "simple", "默认应该是傻瓜模式")
            res = await self.api(plugin, "wizard/mode", body={"mode": "expert"})
            self.assertEqual(res["data"]["mode"], "expert")
            res = await self.api(plugin, "wizard/mode", body={"mode": "胡说"})
            self.assertEqual(res["data"]["mode"], "expert", "非法值不该改掉当前模式")
            await plugin.terminate()

        self.run_async(scenario())

    def test_recommended_keys_are_valid(self) -> None:
        """推荐配置里的键必须真的存在，否则一键套用会静默写一堆没用的东西。"""
        from mind.config import GuardSettings, HumanizeSettings, StyleSettings
        from mind.splitter import SplitterSettings

        plugin = self.make_plugin()
        rec = plugin._wizard_payload()["recommended"]
        allowed = {
            "humanize": set(HumanizeSettings().to_dict()),
            "style": set(StyleSettings().to_dict()),
            "guard": set(GuardSettings().to_dict()),
            "splitter": set(SplitterSettings.from_mapping({}).to_dict()),
        }
        for group, keys in allowed.items():
            self.assertIn(group, rec, group)
            for key in rec.get(group, {}):
                self.assertIn(key, keys, f"{group}.{key} 不是有效配置项")
        self.assertTrue(set(rec["emotion"]) <= {"lexicon", "lexicon_gain", "trusted"})

    def test_tools_are_registered(self) -> None:
        plugin = self.make_plugin()
        self.assertTrue(callable(getattr(plugin, "tool_recall", None)))
        self.assertTrue(callable(getattr(plugin, "tool_memorize", None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

