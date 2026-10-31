"""仿人脑的记忆唤醒：遗忘曲线从「上次想起」算，且越回想越牢固。"""
from __future__ import annotations

import unittest

from mind.memory.model import (
    Memory,
    ScoreWeights,
    activation,
    score_memory,
    tokenize,
)

BASE = 1_700_000_000.0
DAY = 86400.0


class ActivationTest(unittest.TestCase):
    def test_fresh_memory_is_full(self) -> None:
        memory = Memory(content="他喜欢冰美式", created_at=BASE)
        self.assertAlmostEqual(activation(memory, now=BASE, half_life_days=30), 1.0, places=2)

    def test_decays_by_half_life(self) -> None:
        memory = Memory(content="x", created_at=BASE)
        thirty_days = activation(memory, now=BASE + 30 * DAY, half_life_days=30)
        sixty_days = activation(memory, now=BASE + 60 * DAY, half_life_days=30)
        self.assertAlmostEqual(thirty_days, 0.5, places=1)
        self.assertAlmostEqual(sixty_days, 0.25, places=1)

    def test_recall_resets_the_clock(self) -> None:
        memory = Memory(content="x", created_at=BASE)
        stale = activation(memory, now=BASE + 60 * DAY, half_life_days=30)
        memory.last_hit = BASE + 60 * DAY          # 刚刚被想起
        refreshed = activation(memory, now=BASE + 60 * DAY, half_life_days=30)
        self.assertGreater(refreshed, stale, "想起来的旧事应该重新变「热」")
        self.assertAlmostEqual(refreshed, 1.0, places=1)

    def test_spacing_effect_slows_forgetting(self) -> None:
        never = Memory(content="x", created_at=BASE)
        often = Memory(content="x", created_at=BASE, hit_count=6, last_hit=BASE)
        now = BASE + 60 * DAY
        weak = activation(never, now=now, half_life_days=30)
        strong = activation(often, now=now, half_life_days=30)
        self.assertGreater(strong, weak * 2, "想过六次的事，该忘得慢得多")

    def test_spacing_is_capped(self) -> None:
        """想过一万次也不该永不遗忘 —— 半衰期有封顶。"""
        hoarder = Memory(content="x", created_at=BASE, hit_count=10000, last_hit=BASE)
        now = BASE + 3650 * DAY
        self.assertLess(activation(hoarder, now=now, half_life_days=30), 1.0)

    def test_old_data_behaves_like_before(self) -> None:
        """没被想起过的老记忆：hit_count=0、last_hit=0，行为与旧逻辑一致。"""
        memory = Memory(content="x", created_at=BASE, updated_at=BASE)
        by_activation = activation(memory, now=BASE + 10 * DAY, half_life_days=30)
        from mind.memory.model import recency_score

        by_recency = recency_score(memory.age_days(BASE + 10 * DAY), 30)
        self.assertAlmostEqual(by_activation, by_recency, places=2)


class ScoreUsesActivationTest(unittest.TestCase):
    def _score(self, memory: Memory, now: float) -> float:
        total, _detail = score_memory(
            memory, tokenize("猫"), weights=ScoreWeights(), now=now
        )
        return total

    def test_frequently_recalled_memory_wins(self) -> None:
        now = BASE + 60 * DAY
        forgotten = Memory(content="猫", created_at=BASE)
        remembered = Memory(content="猫", created_at=BASE, hit_count=5, last_hit=BASE + 59 * DAY)
        self.assertGreater(
            self._score(remembered, now),
            self._score(forgotten, now),
            "同样内容，常被想起的那条该排前面",
        )

    def test_detail_exposes_activation(self) -> None:
        _total, detail = score_memory(
            Memory(content="猫", created_at=BASE), tokenize("猫"),
            weights=ScoreWeights(), now=BASE,
        )
        self.assertIn("activation", detail)
        self.assertIn("hits", detail)


if __name__ == "__main__":
    unittest.main()

