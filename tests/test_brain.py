
"""仿人脑三个功能：习惯化 / 共情 / 奖励预测误差。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind.emotion import brain
from mind.emotion.appraisal import Stimulus
from mind.emotion.engine import EmotionEngine, SessionMood, Settings
from mind.emotion.model import PAD


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _harsh(p: float = -0.5, key: str = "harsh") -> Stimulus:
    return Stimulus(
        delta=PAD.of(p, 0.3, -0.1),
        affinity=0.0,
        cause="被凶了",
        key=key,
        source="user",
    )


class FingerprintTest(unittest.TestCase):
    def test_rule_key_wins(self) -> None:
        stim = _harsh(key="harsh")
        self.assertEqual(brain.fingerprint(stim), "user:harsh")

    def test_falls_back_to_cause(self) -> None:
        stim = Stimulus(delta=PAD.of(-0.2, 0.1, 0.0), cause="被冷落", source="user")
        self.assertEqual(brain.fingerprint(stim), "user:被冷落")

    def test_empty_when_nothing_to_identify(self) -> None:
        self.assertEqual(brain.fingerprint(Stimulus(delta=PAD.of(-0.2, 0.1, 0.0))), "")


class HabituationTest(unittest.TestCase):
    def test_first_time_is_full_strength(self) -> None:
        factor, repeat = brain.habituation([], "user:harsh")
        self.assertEqual(factor, 1.0)
        self.assertEqual(repeat, 0)

    def test_repeats_weaken_and_stop_at_floor(self) -> None:
        recent: list[str] = []
        seen: list[float] = []
        for _ in range(12):
            factor, repeat = brain.habituation(recent, "user:harsh")
            seen.append(factor)
            brain.remember(recent, "user:harsh")
        self.assertEqual(seen[0], 1.0)
        for earlier, later in zip(seen, seen[1:]):
            self.assertLessEqual(later, earlier)
        self.assertLess(seen[-1], seen[1])
        self.assertGreaterEqual(seen[-1], brain.HABIT_FLOOR - 1e-9)

    def test_other_stimuli_do_not_count(self) -> None:
        recent = ["user:sweet", "user:sweet", "user:sweet"]
        factor, repeat = brain.habituation(recent, "user:harsh")
        self.assertEqual((factor, repeat), (1.0, 0))

    def test_window_is_bounded(self) -> None:
        recent: list[str] = []
        for _ in range(50):
            brain.remember(recent, "user:harsh")
        self.assertEqual(len(recent), brain.HABIT_SEEN)
        # 窗口满了就是地板，不会继续往负无穷滑
        factor, _ = brain.habituation(recent, "user:harsh")
        self.assertAlmostEqual(factor, brain.HABIT_FLOOR, places=6)


class EmpathyTest(unittest.TestCase):
    def test_share_rises_with_familiarity(self) -> None:
        cold = brain.empathy_share(0.0)
        warm = brain.empathy_share(100.0)
        self.assertAlmostEqual(cold, brain.EMPATHY_BASE, places=6)
        self.assertAlmostEqual(warm, brain.EMPATHY_MAX, places=6)
        self.assertGreater(warm, cold)

    def test_share_is_clamped_out_of_range(self) -> None:
        self.assertAlmostEqual(brain.empathy_share(-50), brain.EMPATHY_BASE, places=6)
        self.assertAlmostEqual(brain.empathy_share(1e6), brain.EMPATHY_MAX, places=6)

    def test_delta_is_a_fraction_of_the_stimulus(self) -> None:
        got = brain.empathy_delta(PAD.of(-0.5, 0.3, 0.0), 100.0)
        self.assertAlmostEqual(got.p, -0.5 * brain.EMPATHY_MAX, places=6)


class RewardPredictionTest(unittest.TestCase):
    def test_stranger_expects_nothing(self) -> None:
        self.assertAlmostEqual(brain.expected_valence(0.0, 0.0), 0.0, places=6)

    def test_close_friend_expects_good(self) -> None:
        self.assertGreater(brain.expected_valence(100.0, 100.0), 0.8)

    def test_expected_valence_is_clamped(self) -> None:
        self.assertLessEqual(brain.expected_valence(1e6, 100.0), 1.0)
        self.assertGreaterEqual(brain.expected_valence(-1e6, 100.0), -1.0)

    def test_rpe_sign(self) -> None:
        self.assertLess(brain.reward_prediction_error(-0.6, 0.5), 0)
        self.assertGreater(brain.reward_prediction_error(0.8, 0.0), 0)

    def test_small_gap_raises_nothing(self) -> None:
        self.assertTrue(brain.is_zero(brain.surprise_boost(0.2)))

    def test_big_negative_gap_is_worry(self) -> None:
        boost = brain.surprise_boost(-1.0)
        self.assertLess(boost.p, 0)
        self.assertGreater(boost.a, 0)
        self.assertLess(boost.d, 0)

    def test_big_positive_gap_is_pleasant_surprise(self) -> None:
        boost = brain.surprise_boost(1.0)
        self.assertGreater(boost.p, 0)
        self.assertGreater(boost.a, 0)

    def test_boost_is_bounded(self) -> None:
        for rpe in (-5.0, -1.0, 0.0, 1.0, 5.0):
            boost = brain.surprise_boost(rpe)
            self.assertLessEqual(abs(boost.a), brain.SURPRISE_AROUSAL + 1e-9)


class EngineWiringTest(unittest.TestCase):
    def _engine(self, clock: _Clock) -> tuple[EmotionEngine, str]:
        tmp = tempfile.mkdtemp(prefix="mind_brain_")
        engine = EmotionEngine(Settings(), Path(tmp), clock=clock)
        engine.load()
        return engine, tmp

    def test_repeated_harshness_hurts_less_each_time(self) -> None:
        clock = _Clock()
        engine, _ = self._engine(clock)
        session = engine.resolve("umo", clock())
        drops: list[float] = []
        for _ in range(4):
            session.emotion = PAD()
            engine.apply_stimulus(session, _harsh(), None, clock())
            drops.append(session.emotion.p)
        self.assertAlmostEqual(drops[0], -0.5, places=6)
        for earlier, later in zip(drops, drops[1:]):
            self.assertGreater(later, earlier, "同一种凶法该越来越没感觉")
        self.assertGreater(drops[-1], -0.5, "但不会完全麻木")  # 地板见单元测试

    def test_different_harm_keeps_full_strength(self) -> None:
        clock = _Clock()
        engine, _ = self._engine(clock)
        session = engine.resolve("umo", clock())
        for _ in range(3):
            engine.apply_stimulus(session, _harsh(key="harsh"), None, clock())
        session.emotion = PAD()
        engine.apply_stimulus(session, _harsh(p=-0.5, key="harsh2"), None, clock())
        self.assertAlmostEqual(session.emotion.p, -0.5, places=6)

    def test_empathy_colours_mood_only(self) -> None:
        clock = _Clock()
        engine, _ = self._engine(clock)
        close = engine.resolve("a", clock())
        distant = engine.resolve("b", clock())
        rel_close = engine.relation_of(close, "1", now=clock())
        rel_close.familiarity = 100.0
        rel_close.affinity = 0.0   # 隔离掉奖励预测误差，只看共情
        rel_far = engine.relation_of(distant, "2", now=clock())
        rel_far.familiarity = 0.0
        rel_far.affinity = 0.0
        engine.apply_stimulus(close, _harsh(), rel_close, clock())
        engine.apply_stimulus(distant, _harsh(), rel_far, clock())
        self.assertLess(close.mood.p, distant.mood.p, "熟人难过更带走我")
        self.assertAlmostEqual(close.emotion.p, distant.emotion.p, places=2)

    def test_surprise_from_a_friend_lands_harder(self) -> None:
        clock = _Clock()
        engine, _ = self._engine(clock)
        warm = engine.resolve("a", clock())
        neutral = engine.resolve("b", clock())
        rel = engine.relation_of(warm, "1", now=clock())
        rel.affinity = 100.0
        rel.familiarity = 100.0
        engine.apply_stimulus(warm, _harsh(), rel, clock())
        engine.apply_stimulus(neutral, _harsh(), None, clock())
        self.assertNotAlmostEqual(
            warm.emotion.a, neutral.emotion.a, places=2,
            msg="好感高的人突然凶我，该比陌生人更意外",
        )

    def test_working_memory_survives_a_restart(self) -> None:
        clock = _Clock()
        engine, tmp = self._engine(clock)
        session = engine.resolve("umo", clock())
        for _ in range(3):
            engine.apply_stimulus(session, _harsh(), None, clock())
        self.assertEqual(len(session.recent_stimuli), 3)
        engine.flush()
        again = EmotionEngine(Settings(), Path(tmp), clock=clock)
        again.load()
        revived = again.resolve("umo", clock())
        self.assertEqual(revived.recent_stimuli, session.recent_stimuli)

    def test_state_without_the_new_field_still_loads(self) -> None:
        settings = Settings()
        session = SessionMood.from_dict(
            "old",
            {"emotion": {"p": 0.1, "a": 0.0, "d": 0.0}, "body": {}},
            settings,
        )
        self.assertEqual(session.recent_stimuli, [])

    def test_garbage_field_does_not_crash(self) -> None:
        session = SessionMood.from_dict("bad", {"recent_stimuli": "oops"}, Settings())
        self.assertEqual(session.recent_stimuli, [])


if __name__ == "__main__":
    unittest.main()

