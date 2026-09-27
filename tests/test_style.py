"""表达示例：抽词、检索、待审队列。

不需要 astrbot：给它一个真的 SQLite 连接就能测。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind import style
from mind.memory.store import MemoryStore


def make_store() -> style.StyleStore:
    tmp = tempfile.mkdtemp(prefix="ai_mind_style_")
    memory = MemoryStore(Path(tmp) / "mind.db")
    return style.StyleStore(memory.connection)


def example(user: str, reply: str, **kwargs) -> style.StyleExample:
    data = dict(
        user_text=user,
        reply_text=reply,
        created_at=kwargs.pop("created_at", 0.0),
        keywords=tuple(style.keywords_of(user)),
        grams=tuple(style.ngrams(user)),
    )
    data.update(kwargs)
    return style.StyleExample(**data)


class TokenTest(unittest.TestCase):
    def test_ngrams_ignore_punctuation(self) -> None:
        self.assertEqual(style.ngrams("你好，世界"), ["你好", "好世", "世界"])

    def test_ngrams_drop_stopwords(self) -> None:
        self.assertNotIn("的我", style.ngrams("我的我"))

    def test_short_text_gives_nothing(self) -> None:
        self.assertEqual(style.ngrams("啊"), [])
        self.assertEqual(style.ngrams(""), [])

    def test_keywords_are_deduped(self) -> None:
        words = style.keywords_of("咖啡咖啡咖啡店")
        self.assertTrue(words)
        self.assertFalse(any(a in b for a in words for b in words if a != b))

    def test_similarity(self) -> None:
        self.assertEqual(style.similarity(["ab", "bc"], ["ab", "bc"]), 1.0)
        self.assertEqual(style.similarity(["ab"], ["cd"]), 0.0)
        self.assertEqual(style.similarity([], ["cd"]), 0.0)

    def test_followup_detection(self) -> None:
        self.assertTrue(style.is_followup("哦"))
        self.assertTrue(style.is_followup("/情绪"))
        self.assertFalse(style.is_followup("你今天去哪了呀"))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()

    def test_add_and_count(self) -> None:
        self.assertTrue(self.store.add(example("你怎么才来", "哼，你管我。")))
        counts = self.store.counts()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["auto"], 1)

    def test_duplicate_is_ignored(self) -> None:
        self.assertTrue(self.store.add(example("你怎么才来", "哼，你管我。")))
        self.assertEqual(self.store.add(example("你怎么才来", "哼，你管我。")), 0)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_status_flow_and_rollback(self) -> None:
        new_id = self.store.add(example("你今天去哪了", "关你什么事。"))
        self.assertEqual(self.store.set_status([new_id], style.STATUS_APPROVED), 1)
        self.assertEqual(self.store.counts()["approved"], 1)
        # 回滚：从"已批准"退回"待审"
        self.assertEqual(self.store.set_status([new_id], style.STATUS_PENDING), 1)
        self.assertEqual(self.store.counts()["pending"], 1)

    def test_bad_status_is_rejected(self) -> None:
        new_id = self.store.add(example("abc def", "reply here"))
        self.assertEqual(self.store.set_status([new_id], "胡说"), 0)

    def test_delete_and_clear(self) -> None:
        first = self.store.add(example("你怎么才来", "哼，你管我。"))
        self.store.add(example("我今天好累", "那就早点睡。"))
        self.assertEqual(self.store.delete([first]), 1)
        self.assertEqual(self.store.counts()["total"], 1)
        self.assertEqual(self.store.clear(only_auto=True), 1)

    def test_list_filters(self) -> None:
        self.store.add(example("你怎么才来", "哼，你管我。", status=style.STATUS_APPROVED))
        self.store.add(example("我今天好累", "那就早点睡。"))
        approved, _t = self.store.list(status=style.STATUS_APPROVED)
        self.assertEqual(len(approved), 1)
        found, _t = self.store.list(query="好累")
        self.assertEqual(len(found), 1)

    def test_matched_picks_the_closest(self) -> None:
        good = self.store.add(example(
            "你怎么才来呀", "哼，你管我。等你等到饭都凉了。",
            status=style.STATUS_APPROVED, score=95,
        ))
        self.store.add(example(
            "我今天好累", "那你就早点睡，别硬撑着跟我聊。",
            status=style.STATUS_APPROVED, score=90,
        ))
        hits = self.store.matched("你怎么才来")
        self.assertTrue(hits)
        self.assertEqual(hits[0].id, good)

    def test_only_approved_is_matched(self) -> None:
        self.store.add(example("你怎么才来呀", "哼，你管我。"))
        self.assertEqual(self.store.matched("你怎么才来"), [])

    def test_scene_filter(self) -> None:
        self.store.add(example("你怎么才来呀", "哼，你管我。",
                               status=style.STATUS_APPROVED, scene=style.SCENE_GROUP))
        self.assertEqual(self.store.matched("你怎么才来", scene=style.SCENE_PRIVATE), [])
        self.assertTrue(self.store.matched("你怎么才来", scene=style.SCENE_GROUP))

    def test_touch_counts_hits(self) -> None:
        new_id = self.store.add(example("你怎么才来呀", "哼，你管我。",
                                        status=style.STATUS_APPROVED))
        self.store.touch([new_id])
        row = self.store.list(status=style.STATUS_APPROVED)[0][0]
        self.assertEqual(row.hit_count, 1)

    def test_render_examples(self) -> None:
        text = style.render_examples([example("你怎么才来呀", "哼，你管我。")])
        self.assertIn("他：你怎么才来呀", text)
        self.assertIn("她：哼，你管我。", text)

    def test_broken_connection_degrades(self) -> None:
        broken = style.StyleStore(None)
        self.assertEqual(broken.counts()["total"], 0)
        self.assertEqual(broken.list()[0], [])
        self.assertEqual(broken.matched("随便"), [])
        self.assertEqual(broken.add(example("abc", "def")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

