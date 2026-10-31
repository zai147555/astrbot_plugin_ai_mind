
"""统一记忆：四力、BM25、分层、超图传播，以及接进检索器的行为。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind.memory import unified
from mind.memory.config import Settings
from mind.memory.model import Memory, ScoreWeights, score_memory, tokenize
from mind.memory.retriever import Retriever
from mind.memory.store import MemoryStore

BASE = 1_700_000_000.0
DAY = 86400.0


def memo(**kwargs) -> Memory:
    base = dict(content="他喜欢冰美式", keywords=("冰美式", "咖啡"),
                kind="preference", importance=0.9, created_at=BASE, updated_at=BASE)
    base.update(kwargs)
    return Memory(**base)


class LayerTest(unittest.TestCase):
    def test_layers(self) -> None:
        self.assertEqual(unified.layer_of(memo(pinned=True)), unified.LAYER_AXIOM)
        for kind in ("fact", "preference", "relation"):
            self.assertEqual(unified.layer_of(memo(kind=kind, pinned=False)),
                             unified.LAYER_DURABLE)
        self.assertEqual(unified.layer_of(memo(kind="event", pinned=False)),
                         unified.LAYER_EVENT)

    def test_half_life_by_importance(self) -> None:
        self.assertIsNone(unified.half_life_days(0.99, unified.LAYER_AXIOM))
        self.assertEqual(unified.half_life_days(0.9, unified.LAYER_DURABLE), 180.0)
        self.assertEqual(unified.half_life_days(0.8, unified.LAYER_DURABLE), 180.0)
        self.assertEqual(unified.half_life_days(0.6, unified.LAYER_DURABLE), 60.0)
        self.assertEqual(unified.half_life_days(0.2, unified.LAYER_EVENT), 21.0)

    def test_half_life_clamps(self) -> None:
        self.assertEqual(unified.half_life_days(5.0, unified.LAYER_DURABLE), 180.0)
        self.assertEqual(unified.half_life_days(-5.0, unified.LAYER_EVENT), 21.0)


class ForceTest(unittest.TestCase):
    def test_recency_halves(self) -> None:
        memory = memo()
        fresh = unified.recency(memory, BASE, 21.0)
        half = unified.recency(memory, BASE + 21 * DAY, 21.0)
        double = unified.recency(memory, BASE + 42 * DAY, 21.0)
        self.assertAlmostEqual(fresh, 1.0, places=2)
        self.assertAlmostEqual(half, 0.5, places=2)
        self.assertAlmostEqual(double, 0.25, places=2)

    def test_recency_prefers_last_hit(self) -> None:
        memory = memo(last_hit=BASE + 100 * DAY)
        self.assertAlmostEqual(unified.recency(memory, BASE + 100 * DAY, 21.0), 1.0, places=2)

    def test_axiom_never_decays(self) -> None:
        memory = memo(pinned=True)
        self.assertEqual(unified.recency(memory, BASE + 3650 * DAY, None), 1.0)

    def test_strength_is_saturating(self) -> None:
        values = [unified.strength(n) for n in (0, 1, 3, 10, 100)]
        self.assertEqual(values[0], 0.0)
        for earlier, later in zip(values, values[1:]):
            self.assertLessEqual(earlier, later)
        self.assertLess(values[-1], 1.0)
        self.assertGreater(values[-1], 0.99)

    def test_strength_handles_garbage(self) -> None:
        self.assertEqual(unified.strength(None), 0.0)
        self.assertEqual(unified.strength("nope"), 0.0)


class Bm25Test(unittest.TestCase):
    def test_rare_term_wins(self) -> None:
        docs = [["咖啡", "冰美式", "好喝"], ["咖啡", "学习", "考试"]]
        scores = unified.bm25({"冰美式"}, docs)
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(scores[1], 0.0)

    def test_common_term_gets_low_idf(self) -> None:
        docs = [["咖啡"] for _ in range(10)]
        scores = unified.bm25({"咖啡"}, docs)
        self.assertGreater(scores[0], 0.0)
        self.assertLess(scores[0], 0.1, "每条都有的词不该有大权重")

    def test_empty_inputs(self) -> None:
        self.assertEqual(unified.bm25(set(), [["a"]]), [0.0])
        self.assertEqual(unified.bm25({"a"}, []), [])

    def test_normalize_bounded(self) -> None:
        for raw in (0.0, 1.0, 10.0, 1000.0):
            value = unified.normalize_bm25(raw)
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 1.0)


class GraphTest(unittest.TestCase):
    def test_shared_keywords_give_bonus(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("冰美式", "咖啡", "早晨"))]
        bonus = unified.graph_bonus([(1, 0.8)], rows, factor=0.5)
        self.assertIn(2, bonus)
        self.assertGreater(bonus[2], 0.0)

    def test_one_shared_keyword_is_not_enough(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("咖啡", "早晨"))]
        self.assertEqual(unified.graph_bonus([(1, 0.8)], rows, factor=0.5), {})

    def test_never_boosts_itself(self) -> None:
        rows = [memo(id=1)]
        self.assertEqual(unified.graph_bonus([(1, 0.9)], rows, factor=1.0), {})

    def test_zero_factor_is_noop(self) -> None:
        rows = [memo(id=1), memo(id=2, keywords=("冰美式", "咖啡"))]
        self.assertEqual(unified.graph_bonus([(1, 1.0)], rows, factor=0.0), {})


class RankTest(unittest.TestCase):
    def _weights(self, **over):
        base = {"relevance": 0.55, "importance": 0.2, "recency": 0.15, "strength": 0.1}
        base.update(over)
        return base

    def test_relevance_dominates(self) -> None:
        hit = memo(id=1, content="冰美式", keywords=("冰美式",), importance=0.1,
                   created_at=BASE)
        miss = memo(id=2, content="考试", keywords=("考试",), importance=0.9,
                    created_at=BASE)
        ranked = unified.rank([hit, miss], query_tokens={"冰美式"}, query_text="冰美式",
                              now=BASE, weights=self._weights(),
                              lexical_of=lambda item: 0.0)
        self.assertEqual(ranked[0][0].id, 1)
        self.assertIn("layer", ranked[0][2])
        self.assertIn("half_life", ranked[0][2])
        self.assertGreater(ranked[0][2]["strength"], -0.1)

    def test_no_query_keeps_importance_and_freshness(self) -> None:
        important = memo(id=1, keywords=(), importance=0.95)
        trivial = memo(id=2, keywords=(), importance=0.1)
        ranked = unified.rank([important, trivial], query_tokens=set(), query_text="",
                              now=BASE, weights=self._weights())
        self.assertEqual(ranked[0][0].id, 1)

    def test_empty_pool(self) -> None:
        self.assertEqual(unified.rank([], query_tokens={"a"}, query_text="a",
                                      now=BASE, weights=self._weights()), [])

    def test_semantic_hook_is_used(self) -> None:
        rows = [memo(id=1, embedding=[1.0, 0.0])]
        ranked = unified.rank(rows, query_tokens=set(), query_text="", now=BASE,
                              weights=self._weights(semantic=0.5),
                              semantic_of=lambda item: 1.0)
        self.assertGreater(ranked[0][2]["semantic"], 0.0)
        self.assertGreater(ranked[0][1], 0.0)


class RetrieverIntegrationTest(unittest.TestCase):
    def _store(self, tmp: str) -> MemoryStore:
        store = MemoryStore(Path(tmp) / "mind.db", None)
        store.add(Memory(content="他喜欢冰美式", keywords=("冰美式", "咖啡"),
                         kind="preference", importance=0.9, scope="session",
                         session_id="s1", created_at=BASE, updated_at=BASE))
        store.add(Memory(content="他提过一句要买伞", keywords=("伞",),
                         kind="event", importance=0.2, scope="session",
                         session_id="s1", created_at=BASE, updated_at=BASE))
        return store

    def _settings(self, unified_on: bool) -> Settings:
        settings = Settings.from_config({
            "retrieval": {"unified": {"enabled": unified_on}, "recent_limit": 0},
            "capture": {"batch_turns": 999},
        })
        settings.pinned_limit = 0
        return settings

    def test_unified_finds_the_relevant_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            retriever = Retriever(self._settings(True))
            got = retriever.retrieve(store, query="冰美式", session_id="s1",
                                     sender_id="u1", group=False, now=BASE + DAY)
        self.assertTrue(got.relevant)
        self.assertIn("冰美式", got.relevant[0].content)

    def test_disabled_keeps_old_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            retriever = Retriever(self._settings(False))
            got = retriever.retrieve(store, query="冰美式", session_id="s1",
                                     sender_id="u1", group=False, now=BASE + DAY)
        self.assertTrue(got.relevant)
        self.assertIn("冰美式", got.relevant[0].content)

    def test_unrelated_query_returns_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            retriever = Retriever(self._settings(True))
            got = retriever.retrieve(store, query="量子力学", session_id="s1",
                                     sender_id="u1", group=False, now=BASE + DAY)
        self.assertEqual(got.relevant, [])


class DetailTest(unittest.TestCase):
    def test_detail_carries_layer_and_half_life(self) -> None:
        _total, detail = score_memory(
            memo(hit_count=2), tokenize("咖啡"), weights=ScoreWeights(), now=BASE
        )
        self.assertEqual(detail["layer"], unified.LAYER_DURABLE)
        self.assertEqual(detail["half_life"], 180.0)
        self.assertGreater(detail["strength"], 0.0)

    def test_pinned_detail_is_axiom(self) -> None:
        _total, detail = score_memory(
            memo(pinned=True), tokenize("咖啡"), weights=ScoreWeights(), now=BASE
        )
        self.assertEqual(detail["layer"], unified.LAYER_AXIOM)
        self.assertEqual(detail["half_life"], 0.0)


if __name__ == "__main__":
    unittest.main()

