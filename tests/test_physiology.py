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

