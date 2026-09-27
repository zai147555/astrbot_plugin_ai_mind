"""人格预设系统的测试。

重点是两件事：
1. **内置的傲娇预设必须原样不动** —— 那是给特定人设调好的，不能被"定制化"改动碰到。
2. 自定义预设能存能读能删，并且能认出坏输入。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion.presets import (  # noqa: E402
    PRESETS,
    all_presets,
    delete_custom_preset,
    get_preset,
    load_custom_presets,
    preset_from_dict,
    preset_to_dict,
    save_custom_preset,
)

#: 内置傲娇预设的"指纹"。任何改动碰坏它，这里就会红。
TSUNDERE_FINGERPRINT = {
    "label": "傲娇（嘴硬心软）",
    "baseline": {"p": 0.08, "a": 0.22, "d": 0.3},
    "emotion_half_life": 240.0,
    "mood_half_life": 5400.0,
    "mood_coupling": 0.1,
    "affinity_initial": 8.0,
}


class TestBuiltinUntouched(unittest.TestCase):
    def test_tsundere_fingerprint(self):
        preset = PRESETS["tsundere"]
        self.assertEqual(preset.label, TSUNDERE_FINGERPRINT["label"])
        self.assertEqual(preset.baseline.to_dict(), TSUNDERE_FINGERPRINT["baseline"])
        self.assertEqual(preset.emotion_half_life, TSUNDERE_FINGERPRINT["emotion_half_life"])
        self.assertEqual(preset.mood_half_life, TSUNDERE_FINGERPRINT["mood_half_life"])
        self.assertEqual(preset.mood_coupling, TSUNDERE_FINGERPRINT["mood_coupling"])
        self.assertEqual(preset.affinity_initial, TSUNDERE_FINGERPRINT["affinity_initial"])

    def test_tsundere_rules_and_styles_intact(self):
        preset = PRESETS["tsundere"]
        # 不写死条数（改预设时会变），而是要求这个规模量级和关键规则都在
        self.assertGreaterEqual(len(preset.extra_rules), 12)
        keys = {rule.key for rule in preset.extra_rules}
        for expected in ("special_petname", "special_confess", "special_call_master",
                         "special_care_sick", "special_other_person"):
            self.assertIn(expected, keys)
        styles = preset.style_overrides or {}
        self.assertIn("炸毛", styles["shy"])
        self.assertIn("阴阳怪气", styles["angry"])
        self.assertIn("旁敲侧击", styles["jealous"])

    def test_custom_cannot_shadow_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, _msg = save_custom_preset(tmp, "tsundere", {"label": "假的"})
            self.assertFalse(ok)
            self.assertEqual(get_preset("tsundere", tmp).label, TSUNDERE_FINGERPRINT["label"])


class TestCustomPresets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _data(self, **over):
        data = {
            "label": "温柔的人设",
            "baseline": {"p": 0.3, "a": 0.1, "d": 0.2},
            "styles": {"shy": "低头小声说话", "angry": "语气变冷"},
            "rules": ["摸鱼 => 0.1,0.4,0.05 => +1 => 一起摸鱼"],
            "emotion_half_life": 600,
            "mood_half_life": 3600,
            "mood_coupling": 0.2,
            "affinity_initial": 5,
        }
        data.update(over)
        return data

    def test_save_and_load(self):
        ok, message = save_custom_preset(self.root, "gentle", self._data())
        self.assertTrue(ok, message)
        preset = get_preset("gentle", self.root)
        self.assertEqual(preset.label, "温柔的人设")
        self.assertEqual(preset.baseline.p, 0.3)
        self.assertEqual(preset.emotion_half_life, 600.0)
        self.assertEqual(len(preset.extra_rules), 1)
        self.assertEqual(preset.style_overrides["shy"], "低头小声说话")

    def test_file_is_readable_json(self):
        save_custom_preset(self.root, "gentle", self._data())
        path = Path(self.root) / "presets" / "gentle.json"
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["label"], "温柔的人设")

    def test_export_import_round_trip(self):
        save_custom_preset(self.root, "gentle", self._data())
        exported = preset_to_dict(get_preset("gentle", self.root))
        with tempfile.TemporaryDirectory() as other:
            ok, _msg = save_custom_preset(other, "copied", exported)
            self.assertTrue(ok)
            copied = get_preset("copied", other)
            self.assertEqual(copied.baseline.to_dict(), exported["baseline"])
            self.assertEqual(copied.style_overrides, get_preset("gentle", self.root).style_overrides)

    def test_delete(self):
        save_custom_preset(self.root, "gentle", self._data())
        self.assertEqual(list(load_custom_presets(self.root)), ["gentle"])
        self.assertTrue(delete_custom_preset(self.root, "gentle"))
        self.assertEqual(list(load_custom_presets(self.root)), [])

    def test_rejects_bad_names(self):
        for name in ("", "../escape", "a/b", "x" * 40, "带 空格"):
            ok, _msg = save_custom_preset(self.root, name, self._data())
            self.assertFalse(ok, name)

    def test_accepts_chinese_name(self):
        ok, message = save_custom_preset(self.root, "我的人设", self._data())
        self.assertTrue(ok, message)
        self.assertIsNotNone(get_preset("我的人设", self.root))

    def test_broken_file_is_skipped(self):
        save_custom_preset(self.root, "good", self._data())
        (Path(self.root) / "presets" / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
        presets = load_custom_presets(self.root)
        self.assertIn("good", presets)
        self.assertNotIn("broken", presets)

    def test_defaults_fill_missing_fields(self):
        ok, _msg = save_custom_preset(self.root, "minimal", {"label": "极简"})
        self.assertTrue(ok)
        preset = get_preset("minimal", self.root)
        self.assertEqual(preset.label, "极简")
        self.assertGreater(preset.emotion_half_life, 0)
        self.assertIsNone(preset.style_overrides)

    def test_bad_rules_are_dropped_not_fatal(self):
        ok, _msg = save_custom_preset(
            self.root, "mixed", self._data(rules=["没有箭头", "摸鱼 => 0.1,0.4,0.0 => +1 => 摸鱼"])
        )
        self.assertTrue(ok)
        preset = get_preset("mixed", self.root)
        self.assertEqual(len(preset.extra_rules), 1)

    def test_all_presets_merges(self):
        save_custom_preset(self.root, "gentle", self._data())
        merged = all_presets(self.root)
        self.assertIn("tsundere", merged)
        self.assertIn("neutral", merged)
        self.assertIn("gentle", merged)

    def test_from_dict_rejects_garbage(self):
        self.assertIsNone(preset_from_dict("x", "not a dict"))
        self.assertIsNone(preset_from_dict("x", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
