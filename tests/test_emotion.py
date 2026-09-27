"""情绪内核的单元测试（完全不依赖 AstrBot）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion import (  # noqa: E402
    EMOTION_TAG_RE,
    PAD,
    Appraiser,
    EmotionEngine,
    JsonStore,
    Relation,
    Settings,
    affinity_tier,
    classify,
    detect_crisis,
    get_preset,
    heuristic_self_emotion,
    parse_custom_rule,
    resolve_prototype,
)
from mind.emotion.model import decay_scalar, intensity_level  # noqa: E402


class TestPAD(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(PAD.of(3, -9, 0.5).to_dict(), {"p": 1.0, "a": -1.0, "d": 0.5})

    def test_arithmetic(self):
        result = PAD.of(0.5, 0.5, 0.5) + PAD.of(0.2, -0.1, 0.0)
        self.assertAlmostEqual(result.p, 0.7, places=6)
        self.assertAlmostEqual(result.a, 0.4, places=6)

    def test_limit_preserves_direction(self):
        limited = PAD.of(1, 1, 1).limit(0.5)
        self.assertAlmostEqual(limited.magnitude(), 0.5, places=6)
        self.assertAlmostEqual(limited.p, limited.a, places=6)

    def test_from_any(self):
        self.assertEqual(PAD.from_any({"p": 0.2, "a": 0.3, "d": 0.4}).p, 0.2)
        self.assertEqual(PAD.from_any([0.1, 0.2, 0.3]).d, 0.3)
        self.assertEqual(PAD.from_any("乱七八糟").magnitude(), 0.0)


class TestDecay(unittest.TestCase):
    def test_half_life(self):
        # 经过一个半衰期，与目标的距离应当正好减半
        self.assertAlmostEqual(decay_scalar(1.0, 0.0, 100.0, 100.0), 0.5, places=6)

    def test_zero_dt(self):
        self.assertEqual(decay_scalar(1.0, 0.0, 100.0, 0.0), 1.0)

    def test_converges_to_target(self):
        value = 1.0
        for _ in range(200):
            value = decay_scalar(value, 0.2, 10.0, 10.0)
        self.assertAlmostEqual(value, 0.2, places=3)


class TestClassify(unittest.TestCase):
    def test_origin_is_calm(self):
        self.assertEqual(classify(PAD.of(0, 0, 0)).key, "calm")

    def test_positive_is_joyish(self):
        self.assertIn(classify(PAD.of(0.8, 0.5, 0.4)).key, {"joy", "excited", "proud"})

    def test_negative_arousal_is_angry(self):
        self.assertEqual(classify(PAD.of(-0.7, 0.8, 0.6)).key, "angry")

    def test_intensity_levels(self):
        self.assertEqual(intensity_level(PAD.of(0, 0, 0))[0], 1)
        self.assertEqual(intensity_level(PAD.of(1, 1, 1))[0], 5)


class TestCrisis(unittest.TestCase):
    def test_detects_real_signals(self):
        for text in ["我不想活了", "想自杀", "活着没意思", "写好了遗书", "撑不下去了"]:
            self.assertIsNotNone(detect_crisis(text), text)

    def test_does_not_flag_hyperbole(self):
        for text in ["想死我了", "笑死我了", "困死了", "累死", "这题难死了", "爱死你了"]:
            self.assertIsNone(detect_crisis(text), text)


class TestAppraisal(unittest.TestCase):
    def setUp(self):
        self.appraiser = Appraiser()
        self.relation = Relation(uid="1", affinity=50.0, familiarity=50.0)

    def test_praise_raises_pleasure(self):
        stim = self.appraiser.appraise("你好棒啊真的好厉害", relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertGreater(stim.delta.p, 0.2)
        self.assertGreater(stim.affinity, 0)

    def test_insult_lowers_pleasure(self):
        stim = self.appraiser.appraise("你就是个废物，滚", relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertLess(stim.delta.p, -0.3)
        self.assertLess(stim.affinity, 0)

    def test_crisis_short_circuits(self):
        stim = self.appraiser.appraise("我不想活了", relation=self.relation)
        self.assertEqual(stim.source, "crisis")
        self.assertGreater(stim.delta.a, 0.5)

    def test_neutral_message_no_stimulus(self):
        self.assertIsNone(self.appraiser.appraise("今天星期二", relation=self.relation))

    def test_scope_filters(self):
        from mind.emotion.presets import TSUNDERE_RULES
        from mind.emotion.appraisal import Rule

        special_only = Rule("t", r"宝宝", (0.5, 0.5, 0.0), scope="special")
        appraiser = Appraiser(rules=(special_only,))
        self.assertIsNone(appraiser.appraise("宝宝在吗", is_special=False))
        self.assertIsNotNone(appraiser.appraise("宝宝在吗", is_special=True))
        self.assertTrue(any(r.scope == "special" for r in TSUNDERE_RULES))

    def test_tsundere_petname_hits_hard(self):
        preset = get_preset("tsundere")
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("宝宝", is_special=True, relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertGreater(stim.delta.a, 0.3)

    def test_short_filler_reads_as_perfunctory(self):
        # 一个"嗯"在她眼里就是敷衍 —— 这是刻意的，不是误判
        stim = self.appraiser.appraise("嗯", relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertEqual(stim.key, "perfunctory")
        self.assertLess(stim.delta.p, 0)

    def test_style_override_beats_prototype(self):
        """他生病时该给"嘴毒心软"，而不是"生气"自带的阴阳怪气。"""
        preset = get_preset("tsundere")
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("我发烧了头好疼", is_special=True, relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertIn("嘴", stim.style)

    def test_jealousy_when_others_buy_gifts(self):
        preset = get_preset("tsundere")
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("我兄弟今天给我买了个礼物", is_special=True, relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertIn("别人", stim.cause)
        self.assertLess(stim.delta.p, 0)

    def test_long_neutral_text_barely_moves(self):
        # 只有"长文本"这条弱规则命中时，情绪波动应当很小
        stim = self.appraiser.appraise("x" * 140, relation=self.relation)
        self.assertIsNotNone(stim)
        self.assertLess(stim.delta.magnitude(), 0.2)


class TestCustomRules(unittest.TestCase):
    def test_parse_ok(self):
        rule = parse_custom_rule("摸鱼|划水 => 0.1,0.4,0.05 => +1 => 一起摸鱼")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.pad, (0.1, 0.4, 0.05))
        self.assertAlmostEqual(rule.affinity, 1.0)
        self.assertEqual(rule.cause, "一起摸鱼")

    def test_parse_bad(self):
        self.assertIsNone(parse_custom_rule("没有箭头"))
        self.assertIsNone(parse_custom_rule(""))


class TestPreset(unittest.TestCase):
    def test_tsundere_style_overrides_exist(self):
        preset = get_preset("tsundere")
        self.assertIn("shy", preset.style_overrides)
        self.assertIn("炸毛", preset.style_overrides["angry"] + preset.style_overrides["shy"])

    def test_unknown_preset_falls_back(self):
        self.assertEqual(get_preset("不存在的性格").key, "tsundere")


class TestSelfFeedback(unittest.TestCase):
    def test_shy_detection(self):
        hint = heuristic_self_emotion("你...你干嘛，闭嘴，别得寸进尺")
        self.assertIsNotNone(hint)
        self.assertEqual(hint[0], "shy")

    def test_jealous_detection(self):
        hint = heuristic_self_emotion("哦，你们关系挺好啊")
        self.assertIsNotNone(hint)
        self.assertEqual(hint[0], "jealous")

    def test_plain_text_no_hint(self):
        self.assertIsNone(heuristic_self_emotion(""))

    def test_tag_regex(self):
        text = "哼<emotion>害羞</emotion>才没有"
        match = EMOTION_TAG_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "害羞")
        self.assertEqual(EMOTION_TAG_RE.sub("", text), "哼才没有")


class TestRelation(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(affinity_tier(0)[0], "陌生人")
        self.assertEqual(affinity_tier(99)[0], "特别的人")

    def test_resolve_prototype(self):
        self.assertEqual(resolve_prototype("开心").key, "joy")
        self.assertEqual(resolve_prototype("有点害羞").key, "shy")
        self.assertIsNone(resolve_prototype("完全不存在的词"))


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestEngine(unittest.TestCase):
    def _make(self, tmp: str, clock: _Clock) -> EmotionEngine:
        settings = Settings.from_config(
            {"persona": {"preset": "tsundere"}, "relationship": {"special_users": ["42"]}}
        )
        engine = EmotionEngine(settings, Path(tmp), clock=clock)
        engine.load()
        return engine

    def test_decays_back_to_baseline(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.apply_stimulus(session, Appraiser().appraise("你好棒", relation=engine.relation_of(session, "1")) or None, None, clock())
            self.assertGreater(session.emotion.p, engine.settings.baseline.p)
            clock.advance(3600)  # 一小时
            engine.resolve("umo", clock())
            self.assertAlmostEqual(session.emotion.p, engine.settings.baseline.p, places=2)

    def test_special_user_flagged(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            self.assertTrue(engine.relation_of(session, "42", now=clock()).special)
            self.assertFalse(engine.relation_of(session, "7", now=clock()).special)

    def test_idle_applies_once(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            clock.advance(6 * 3600)
            self.assertTrue(engine.apply_idle(session, clock()))
            self.assertFalse(engine.apply_idle(session, clock()))  # 同一段空闲只结算一次

    def test_affinity_moves_with_praise(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            before = relation.affinity
            stim = engine.appraiser.appraise("谢谢你，你真的好棒", relation=relation, is_special=True)
            engine.apply_stimulus(session, stim, relation, clock())
            self.assertGreater(relation.affinity, before)

    def test_style_override_expires(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            stim = engine.appraiser.appraise("我发烧了", relation=relation, is_special=True)
            engine.apply_stimulus(session, stim, relation, clock())
            self.assertIn("嘴", engine.snapshot(session, relation, clock()).style)
            clock.advance(engine.settings.style_hold + 10)
            self.assertNotIn("嘴", engine.snapshot(session, relation, clock()).style)

    def test_affinity_grows_but_slows_down(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            gains = []
            for _ in range(12):
                stim = engine.appraiser.appraise("谢谢你，你真的好棒", relation=relation, is_special=True)
                before = relation.affinity
                engine.apply_stimulus(session, stim, relation, clock())
                gains.append(relation.affinity - before)
            self.assertTrue(all(g > 0 for g in gains))
            self.assertLess(gains[-1], gains[0])          # 边际递减
            self.assertLess(relation.affinity, 100.0)     # 十几条消息不该直接封顶

    def test_state_round_trip(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", "小明", clock())
            engine.touch(session, relation, clock())
            engine.apply_stimulus(session, Appraiser().appraise("你好厉害", relation=relation), relation, clock())
            self.assertTrue(engine.flush(clock()))

            reborn = self._make(tmp, clock)
            restored = reborn.resolve("umo", clock())
            self.assertAlmostEqual(restored.emotion.p, session.emotion.p, places=3)
            self.assertIn("42", restored.users)
            self.assertEqual(restored.users["42"].name, "小明")

    def test_corrupted_file_does_not_crash(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "emotion_state.json"
            target.write_text("{ 这不是合法 json", encoding="utf-8")
            engine = self._make(tmp, clock)
            self.assertEqual(engine.session_count, 0)
            self.assertTrue(engine.flush(clock()))

    def test_store_reports_failure_quietly(self):
        store = JsonStore(Path("/proc/definitely/not/writable/state.json"))
        self.assertFalse(store.save({"a": 1}))

    def test_snapshot_crisis_window(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            stim = engine.appraiser.appraise("我不想活了", relation=relation, is_special=True)
            engine.apply_stimulus(session, stim, relation, clock())
            self.assertTrue(engine.snapshot(session, relation, clock()).crisis)
            clock.advance(engine.settings.crisis_hold + 10)
            self.assertFalse(engine.snapshot(session, relation, clock()).crisis)


if __name__ == "__main__":
    unittest.main(verbosity=2)

