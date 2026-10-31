
"""价值回传与经验结晶。"""
from __future__ import annotations

import unittest

from mind.memory import evolve
from mind.memory.model import Memory


def memo(**kwargs) -> Memory:
    base = dict(content="他喜欢冰美式", keywords=("冰美式", "咖啡"),
                importance=0.5, kind="preference")
    base.update(kwargs)
    return Memory(**base)


class FeedbackTest(unittest.TestCase):
    def test_positive(self) -> None:
        for text in ("对", "没错", "就是这个", "记住了", "真准"):
            self.assertEqual(evolve.detect_feedback(text)[0], 1, text)

    def test_negative(self) -> None:
        for text in ("不对", "记错了", "不是这样", "我没说", "乱说"):
            self.assertEqual(evolve.detect_feedback(text)[0], -1, text)

    def test_negative_wins_over_positive(self) -> None:
        self.assertEqual(evolve.detect_feedback("不是不对")[0], -1)

    def test_long_text_is_ignored(self) -> None:
        self.assertEqual(evolve.detect_feedback("今天聊了挺多，不过你说的那个不对的地方我们自己再想想吧")[0], 0)

    def test_plain_text(self) -> None:
        self.assertEqual(evolve.detect_feedback("今天好累"), (0, ""))
        self.assertEqual(evolve.detect_feedback(""), (0, ""))


class ApplyTest(unittest.TestCase):
    def test_positive_raises_target(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("考试",))]
        changes = evolve.apply_value(rows, [1], 1)
        self.assertAlmostEqual(changes[1], 0.62, places=2)

    def test_negative_lowers_more(self) -> None:
        rows = [memo(id=1, importance=0.5)]
        changes = evolve.apply_value(rows, [1], -1)
        self.assertAlmostEqual(changes[1], 0.32, places=2)

    def test_neighbor_gets_a_slice(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("冰美式", "咖啡", "早晨"))]
        changes = evolve.apply_value(rows, [1], 1)
        self.assertIn(2, changes)
        self.assertLess(changes[2], changes[1])

    def test_unrelated_stays_untouched(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("考试",))]
        self.assertNotIn(2, evolve.apply_value(rows, [1], 1))

    def test_no_signal_no_change(self) -> None:
        self.assertEqual(evolve.apply_value([memo(id=1)], [1], 0), {})
        self.assertEqual(evolve.apply_value([memo(id=1)], [], 1), {})

    def test_clamped(self) -> None:
        rows = [memo(id=1, importance=0.99)]
        self.assertEqual(evolve.apply_value(rows, [1], 1)[1], 1.0)
        rows = [memo(id=1, importance=0.1)]
        self.assertGreaterEqual(evolve.apply_value(rows, [1], -1)[1], evolve.VALUE_FLOOR)

    def test_unchanged_is_not_returned(self) -> None:
        rows = [memo(id=1, importance=1.0)]
        self.assertEqual(evolve.apply_value(rows, [1], 1), {})


class ClusterTest(unittest.TestCase):
    def test_clusters_by_shared_keywords(self) -> None:
        rows = [
            memo(id=1, keywords=("咖啡", "冰美式")),
            memo(id=2, keywords=("咖啡", "冰美式", "早晨")),
            memo(id=3, keywords=("考试", "复习")),
        ]
        groups = evolve.clusters(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_single_shared_keyword_not_enough(self) -> None:
        rows = [memo(id=1, keywords=("咖啡",)), memo(id=2, keywords=("咖啡", "早晨"))]
        self.assertEqual(evolve.clusters(rows), [])


class CrystallizeTest(unittest.TestCase):
    def _group(self, values=(0.9, 0.85, 0.82)):
        return [
            memo(id=index + 1, importance=value,
                 keywords=("咖啡", "冰美式", "早晨")[:2 + (index % 2)],
                 content="第%d条" % (index + 1))
            for index, value in enumerate(values)
        ]

    def test_crystallizes_high_value_cluster(self) -> None:
        cards = evolve.crystallize(self._group())
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["kind"], "policy")
        self.assertIn("经验（3 条记忆）", card["content"])
        self.assertEqual(len(card["source_ids"]), 3)
        self.assertIn("咖啡", card["keywords"])

    def test_low_value_is_skipped(self) -> None:
        self.assertEqual(evolve.crystallize(self._group((0.3, 0.4, 0.5))), [])

    def test_too_few_is_skipped(self) -> None:
        self.assertEqual(evolve.crystallize(self._group((0.9, 0.9))), [])

    def test_existing_policy_is_not_duplicated(self) -> None:
        existing = [memo(id=9, kind="policy", keywords=("咖啡", "冰美式", "早晨"))]
        self.assertEqual(evolve.crystallize(self._group(), existing), [])


if __name__ == "__main__":
    unittest.main()

