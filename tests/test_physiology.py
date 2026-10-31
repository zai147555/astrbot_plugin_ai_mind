"""生理层单测：昼夜节律、饥饿、压力、疾病、情绪偏移。"""
from __future__ import annotations

import random
import time
import unittest
from datetime import datetime

from mind.emotion import physiology as ph
from mind.emotion.model import PAD


def at(hour: float) -> float:
    """构造某一天某个整点的时间戳（本地时区）。"""
    now = datetime.now()
    base = datetime(now.year, now.month, now.day, int(hour), 0, 0)
    return base.timestamp()


class CircadianTest(unittest.TestCase):
    def test_energy_is_low_at_dawn_and_high_in_morning(self) -> None:
        dawn = ph.circadian_energy(at(4))
        morning = ph.circadian_energy(at(10))
        self.assertLess(dawn, 0.35, "凌晨四点该是最没精神的")
        self.assertGreater(morning, 0.7, "上午十点该是有精神的")
        self.assertGreater(morning, dawn)

    def test_afternoon_dip(self) -> None:
        self.assertLess(ph.circadian_energy(at(14)), ph.circadian_energy(at(11)))


class EvolveTest(unittest.TestCase):
    def test_hunger_rises_between_meals(self) -> None:
        body = ph.Body(hunger=0.2, last_update=at(9))
        ph.evolve(body, at(11), allow_illness=False)
        self.assertGreater(body.hunger, 0.2)

    def test_energy_follows_the_clock(self) -> None:
        body = ph.Body(energy=0.9, last_update=at(23))
        # 23 点熬到次日 4 点：不能直接用同一天的 at(4)（那是过去的时刻）
        ph.evolve(body, at(23) + 5 * 3600, allow_illness=False)
        self.assertLess(body.energy, 0.6, "熬夜到凌晨，精力该掉下来")

    def test_stress_decays(self) -> None:
        body = ph.Body(stress=0.9, last_update=at(10))
        ph.evolve(body, at(14), allow_illness=False)
        self.assertLess(body.stress, 0.9, "压力该随时间消退")

    def test_illness_heals_when_time_is_up(self) -> None:
        body = ph.Body(illness=0.7, illness_kind="着凉", last_update=at(10))
        body.illness_until = at(11)
        events = ph.evolve(body, at(12), allow_illness=False)
        self.assertEqual(body.illness, 0.0)
        self.assertTrue(any("好" in e for e in events))

    def test_illness_can_start_when_exhausted_and_stressed(self) -> None:
        body = ph.Body(energy=0.1, stress=0.95, last_update=at(3))
        rng = random.Random(7)
        for _ in range(300):
            # 压力是被持续消耗着的（熬夜赶作业），每轮补回高位
            body.stress = 0.95
            body.energy = 0.12
            ph.evolve(body, body.last_update + 3600, allow_illness=True, rng=rng)
            if body.illness > 0:
                break
        self.assertGreater(body.illness, 0, "累狠了+压力大，反复几小时该有着凉的可能")
        self.assertTrue(body.illness_until > body.last_update)

    def test_illness_does_not_start_when_healthy(self) -> None:
        body = ph.Body(energy=0.9, stress=0.1, last_update=at(10))
        rng = random.Random(3)
        for _ in range(50):
            ph.evolve(body, body.last_update + 3600, allow_illness=True, rng=rng)
        self.assertEqual(body.illness, 0.0, "精神好、压力小的时候不该无缘无故病")


class PadOffsetTest(unittest.TestCase):
    def test_healthy_body_is_neutral(self) -> None:
        offset = ph.pad_offset(ph.Body(energy=0.8, hunger=0.2, stress=0.1))
        self.assertAlmostEqual(offset.p, 0.0, places=2)
        self.assertAlmostEqual(offset.a, 0.0, places=2)

    def test_exhaustion_lowers_pleasure_and_arousal(self) -> None:
        offset = ph.pad_offset(ph.Body(energy=0.08, hunger=0.2, stress=0.1))
        self.assertLess(offset.p, 0)
        self.assertLess(offset.a, 0)

    def test_hunger_makes_her_edgy(self) -> None:
        offset = ph.pad_offset(ph.Body(energy=0.7, hunger=0.9, stress=0.1))
        self.assertLess(offset.p, 0)
        self.assertGreater(offset.a, 0, "饿的时候唤醒反而更高（烦躁）")

    def test_illness_drags_all_three_down(self) -> None:
        offset = ph.pad_offset(ph.Body(illness=0.8))
        self.assertLess(offset.p, 0)
        self.assertLess(offset.a, 0)
        self.assertLess(offset.d, 0)


class DescribeTest(unittest.TestCase):
    def test_healthy_says_nothing(self) -> None:
        self.assertEqual(ph.describe(ph.Body(energy=0.8, hunger=0.2, stress=0.1)), "")

    def test_illness_is_described(self) -> None:
        text = ph.describe(ph.Body(illness=0.8, illness_kind="着凉"))
        self.assertIn("病", text)

    def test_round_trip_serialisation(self) -> None:
        body = ph.Body(energy=0.42, hunger=0.66, stress=0.33, illness=0.5,
                       illness_kind="着凉", illness_until=time.time() + 60)
        back = ph.Body.from_dict(body.to_dict())
        self.assertAlmostEqual(back.energy, 0.42, places=3)
        self.assertAlmostEqual(back.hunger, 0.66, places=3)
        self.assertAlmostEqual(back.illness, 0.5, places=3)
        self.assertEqual(back.illness_kind, "着凉")


if __name__ == "__main__":
    unittest.main()


class EngineWiringTest(unittest.TestCase):
    """接线：身体必须真的把情绪拖过去，而不是只改一句提示词。

    这里踩过一个坑：一开始做成「每次 tick 加一点身体偏移」，
    结果那点偏移被情绪衰减直接冲掉 —— 熬到天亮情绪还是 0。
    正确做法是让身体改「衰减目标」（基线 + 身体偏移）。"""

    def test_staying_up_all_night_drags_emotion_down(self) -> None:
        import tempfile

        from mind.emotion import engine as eng
        from mind.emotion.model import PAD

        workdir = tempfile.mkdtemp(prefix="mind_body_")
        engine = eng.EmotionEngine(eng.Settings(), workdir)
        session = engine.resolve("wire", at(9))
        session.emotion = PAD.of(0.20, 0.30, 0.10)
        session.body = ph.Body(energy=0.85, hunger=0.15, stress=0.1, last_update=at(9))
        session.last_tick = at(9)

        moment = at(9)
        for _ in range(38):          # 09:00 → 次日 04:00，每半小时 tick
            moment += 1800
            session.tick(moment, engine.settings)

        self.assertLess(session.emotion.p, -0.10, "熬通宵该明显不高兴")
        self.assertLess(session.body.energy, 0.35, "力气的确耗下去了")
        offset = ph.pad_offset(session.body)
        self.assertAlmostEqual(session.emotion.p, offset.p, places=1,
                               msg="情绪该收敛到「基线 + 身体偏移」")

    def test_healthy_body_keeps_emotion_before_midnight(self) -> None:
        import tempfile

        from mind.emotion import engine as eng
        from mind.emotion.model import PAD

        workdir = tempfile.mkdtemp(prefix="mind_body_")
        # 这条只测「身体物理」，把生理周期关掉 —— 否则它会随真实日期漂：
        # 今天恰好是经前的话，身体偏移就不是 0，测试自己会不稳。
        original = ph.CYCLE_ENABLED
        ph.CYCLE_ENABLED = False
        try:
            engine = eng.EmotionEngine(eng.Settings(), workdir)
            session = engine.resolve("ok", at(9))
            session.emotion = PAD.of(0.30, 0.40, 0.20)
            session.body = ph.Body(energy=0.85, hunger=0.15, stress=0.1, last_update=at(9))
            session.last_tick = at(9)
            moment = at(9)
            for _ in range(4):           # 只到 11:00，还是精神好的时候
                moment += 1800
                session.tick(moment, engine.settings)
            offset = ph.pad_offset(session.body)
            # 情绪本来就会按半衰期回落，所以不能断言「还是正数」；
            # 真正的不变量是：健康时身体偏移接近 0，没有额外往下拖。
            self.assertGreater(offset.p, -0.05, "上午身体不该有负面偏移")
            self.assertAlmostEqual(offset.a, 0.0, delta=0.1)
        finally:
            ph.CYCLE_ENABLED = original

class CycleTest(unittest.TestCase):
    """生理周期：按时间算，同一天永远一样；经期确实会累、会难受。"""

    def test_phases(self) -> None:
        self.assertEqual(ph.cycle_phase(1), "经期")
        self.assertEqual(ph.cycle_phase(ph.PERIOD_DAYS), "经期")
        self.assertEqual(ph.cycle_phase(ph.PERIOD_DAYS + 1), "经期后")
        self.assertEqual(ph.cycle_phase(14), "排卵期")
        self.assertEqual(ph.cycle_phase(26), "经前")

    def test_day_wraps_and_is_deterministic(self) -> None:
        anchor = ph.CYCLE_ANCHOR
        self.assertEqual(ph.cycle_day(anchor), 1)
        self.assertEqual(ph.cycle_day(anchor + 27 * 86400), 28)
        self.assertEqual(ph.cycle_day(anchor + 28 * 86400), 1, "满一轮回到第 1 天")
        self.assertEqual(ph.cycle_day(anchor + 3 * 86400), ph.cycle_day(anchor + 3 * 86400),
                         "同一时刻必须算出同一天（不能随机跳）")

    def test_period_lowers_energy_and_mood(self) -> None:
        now = ph.CYCLE_ANCHOR + 86400          # 第 2 天 = 经期
        body = ph.Body(energy=0.9, hunger=0.1, stress=0.0, last_update=now - 3600)
        ph.evolve(body, now, allow_illness=False)
        self.assertEqual(body.cycle_phase, "经期")
        offset = ph.pad_offset(body)
        self.assertLess(offset.p, -0.05, "经期该明显不舒服")
        self.assertLess(offset.d, -0.05, "掌控感也会掉")
        self.assertIn("生理期", ph.describe(body))

    def test_pms_is_edgy(self) -> None:
        now = ph.CYCLE_ANCHOR + 25 * 86400       # 第 26 天 = 经前
        body = ph.Body(energy=0.7, hunger=0.2, stress=0.1, last_update=now - 3600)
        ph.evolve(body, now, allow_illness=False)
        self.assertEqual(body.cycle_phase, "经前")
        offset = ph.pad_offset(body)
        self.assertLess(offset.p, 0.0)
        self.assertGreater(offset.a, 0.0, "经前是烦躁，唤醒往上")

    def test_energy_ceiling_drops_during_period(self) -> None:
        anchor = ph.CYCLE_ANCHOR
        period_now = anchor + 86400              # 经期
        calm_now = anchor + 8 * 86400            # 经期后（第 9 天）
        during = ph.Body(energy=0.95, hunger=0.1, stress=0.0, last_update=period_now - 3 * 3600)
        after = ph.Body(energy=0.95, hunger=0.1, stress=0.0, last_update=calm_now - 3 * 3600)
        ph.evolve(during, period_now, allow_illness=False)
        ph.evolve(after, calm_now, allow_illness=False)
        self.assertLess(during.energy, after.energy, "经期能调动的精力上限更低")

    def test_can_be_disabled(self) -> None:
        original = ph.CYCLE_ENABLED
        try:
            ph.CYCLE_ENABLED = False
            self.assertEqual(ph.cycle_phase(1), "")
            body = ph.Body(energy=0.8, last_update=0.0)
            ph.evolve(body, ph.CYCLE_ANCHOR + 86400, allow_illness=False)
            self.assertEqual(body.cycle_phase, "", "关掉之后不该有任何周期影响")
        finally:
            ph.CYCLE_ENABLED = original
