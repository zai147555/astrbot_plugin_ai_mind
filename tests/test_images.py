"""关键词配图的内核测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.memory.store import MemoryStore  # noqa: E402
from mind.images import (  # noqa: E402
    MODE_CONTAINS,
    MODE_EXACT,
    MODE_FUZZY,
    SCOPE_GLOBAL,
    SCOPE_USER,
    Cooldown,
    ImageStore,
    char_jaccard,
    guess_extension,
    human_size,
    match_score,
)


class TestMatchScore(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(match_score("晚安", "晚安", MODE_EXACT), 1.0)
        self.assertEqual(match_score("晚安啦", "晚安", MODE_EXACT), 0.0)

    def test_contains(self):
        self.assertEqual(match_score("我睡了晚安", "晚安", MODE_CONTAINS), 1.0)
        self.assertEqual(match_score("早点休息", "晚安", MODE_CONTAINS), 0.0)

    def test_fuzzy_handles_reordered_and_padded(self):
        # 这是"模糊"存在的意义：二元组完全对不上，但字符集合一致
        self.assertEqual(match_score("安晚", "晚安", MODE_FUZZY), 1.0)
        self.assertGreater(match_score("晚安啦", "晚安", MODE_FUZZY), 0.6)

    def test_fuzzy_rejects_unrelated(self):
        self.assertEqual(match_score("今天天气不错", "晚安", MODE_FUZZY), 0.0)

    def test_empty_inputs(self):
        self.assertEqual(match_score("", "晚安", MODE_FUZZY), 0.0)
        self.assertEqual(match_score("晚安", "", MODE_CONTAINS), 0.0)

    def test_char_jaccard(self):
        self.assertEqual(char_jaccard("晚安", "安晚"), 1.0)
        self.assertEqual(char_jaccard("晚安", "abcd"), 0.0)


class TestHelpers(unittest.TestCase):
    def test_guess_extension_from_mime(self):
        self.assertEqual(guess_extension("image/png"), ".png")
        self.assertEqual(guess_extension("image/jpeg"), ".jpg")

    def test_guess_extension_from_filename(self):
        self.assertEqual(guess_extension("", "a.PNG"), ".png")
        self.assertEqual(guess_extension("", "a.jpeg"), ".jpg")

    def test_guess_extension_rejects_non_image(self):
        self.assertEqual(guess_extension("text/plain", "a.txt"), "")

    def test_human_size(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertTrue(human_size(2048).endswith("KB"))


class TestImageStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name) / "m.db")
        self.images = ImageStore(self.store.connection, Path(self._tmp.name))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _add(self, name="a.png", size=100):
        path = self.images.path_of(name)
        path.write_bytes(b"x" * size)
        return self.images.add_image(stored_name=name, original_name=name, mime="image/png", size=size)

    def test_add_and_list(self):
        image_id = self._add()
        self.assertGreater(image_id, 0)
        listed = self.images.list_images()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].stored_name, "a.png")

    def test_trigger_lifecycle(self):
        image_id = self._add()
        trigger_id = self.images.add_trigger(keyword="晚安", image_id=image_id, mode=MODE_CONTAINS)
        self.assertGreater(trigger_id, 0)
        self.assertEqual(len(self.images.list_triggers()), 1)
        self.assertTrue(self.images.update_trigger(trigger_id, enabled=False))
        self.assertFalse(self.images.list_triggers()[0].enabled)
        self.assertTrue(self.images.delete_trigger(trigger_id))
        self.assertEqual(self.images.list_triggers(), [])

    def test_delete_image_removes_file_and_triggers(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.assertTrue(self.images.path_of("a.png").is_file())
        self.assertTrue(self.images.delete_image(image_id))
        self.assertFalse(self.images.path_of("a.png").is_file())
        self.assertEqual(self.images.list_triggers(), [])

    def test_delete_many(self):
        ids = [self._add(f"i{i}.png") for i in range(3)]
        for image_id in ids:
            self.images.add_trigger(keyword=f"k{image_id}", image_id=image_id)
        self.assertEqual(self.images.delete_many(ids[:2]), 2)
        self.assertEqual(len(self.images.list_images()), 1)
        self.assertEqual(len(self.images.list_triggers()), 1)

    def test_delete_many_ignores_garbage(self):
        image_id = self._add()
        self.assertEqual(self.images.delete_many([0, None, "x"]), 0)
        self.assertEqual(len(self.images.list_images()), 1)
        self.assertEqual(self.images.delete_many([image_id]), 1)

    def test_update_triggers_of_images(self):
        first = self._add("a.png")
        second = self._add("b.png")
        self.images.add_trigger(keyword="晚安", image_id=first)
        self.images.add_trigger(keyword="早安", image_id=first)
        self.images.add_trigger(keyword="生气", image_id=second)
        self.assertEqual(self.images.update_triggers_of_images([first], enabled=False), 2)
        by_keyword = {t.keyword: t for t in self.images.list_triggers()}
        self.assertFalse(by_keyword["晚安"].enabled)
        self.assertFalse(by_keyword["早安"].enabled)
        self.assertTrue(by_keyword["生气"].enabled)

    def test_update_triggers_of_images_mode_and_scope(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, scope=SCOPE_USER, owner_id="42")
        self.assertEqual(
            self.images.update_triggers_of_images([image_id], scope=SCOPE_GLOBAL, owner_id=""), 1
        )
        trigger = self.images.list_triggers()[0]
        self.assertEqual(trigger.scope, SCOPE_GLOBAL)
        self.assertEqual(trigger.owner_id, "")
        self.assertEqual(self.images.update_triggers_of_images([image_id], mode=MODE_FUZZY), 1)
        self.assertEqual(self.images.list_triggers()[0].mode, MODE_FUZZY)

    def test_update_triggers_rejects_unknown_fields(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.assertEqual(self.images.update_triggers_of_images([image_id], hacker="yes"), 0)
        self.assertEqual(self.images.update_triggers_of_images([], enabled=False), 0)

    def test_stats(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.images.add_trigger(keyword="早安", image_id=image_id)
        stats = self.images.stats()
        self.assertEqual((stats["images"], stats["triggers"], stats["active"]), (1, 2, 2))

    def test_match_picks_longest_keyword_on_tie(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, mode=MODE_CONTAINS)
        self.images.add_trigger(keyword="晚安啦", image_id=image_id, mode=MODE_CONTAINS)
        found = self.images.match("晚安啦", fuzzy_threshold=0.6)
        self.assertIsNotNone(found)
        self.assertEqual(found.keyword, "晚安啦")

    def test_match_skips_disabled(self):
        image_id = self._add()
        trigger_id = self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.images.update_trigger(trigger_id, enabled=False)
        self.assertIsNone(self.images.match("晚安"))

    def test_match_scope_user_filters_by_sender(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, scope=SCOPE_USER, owner_id="42")
        self.assertIsNotNone(self.images.match("晚安", sender_id="42"))
        self.assertIsNone(self.images.match("晚安", sender_id="7"))

    def test_match_scope_global_matches_anyone(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, scope=SCOPE_GLOBAL)
        self.assertIsNotNone(self.images.match("晚安", sender_id="任何人"))

    def test_match_respects_fuzzy_threshold(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, mode=MODE_FUZZY)
        self.assertIsNotNone(self.images.match("晚安啦", fuzzy_threshold=0.6))
        self.assertIsNone(self.images.match("晚安啦", fuzzy_threshold=0.9))

    def test_resolve_exact_keyword(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        found = self.images.resolve("晚安")
        self.assertIsNotNone(found)
        self.assertEqual(found.keyword, "晚安")

    def test_resolve_fuzzy_keyword(self):
        """模型经常把关键词记成近义说法，模糊兜一下。"""
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.assertIsNotNone(self.images.resolve("晚安啦", fuzzy_threshold=0.6))

    def test_resolve_by_note(self):
        """模型说的是"这张图是干嘛的"，而不是关键词本身。"""
        image_id = self._add()
        self.images.add_trigger(keyword="zzz", image_id=image_id, note="睡前道晚安")
        found = self.images.resolve("睡前道晚安", fuzzy_threshold=0.6)
        self.assertIsNotNone(found)
        self.assertEqual(found.keyword, "zzz")

    def test_resolve_unknown_returns_none(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.assertIsNone(self.images.resolve("完全不相干的东西"))

    def test_resolve_respects_scope(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id, scope=SCOPE_USER, owner_id="42")
        self.assertIsNotNone(self.images.resolve("晚安", sender_id="42"))
        self.assertIsNone(self.images.resolve("晚安", sender_id="7"))

    def test_resolve_skips_disabled(self):
        image_id = self._add()
        trigger_id = self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.images.update_trigger(trigger_id, enabled=False)
        self.assertIsNone(self.images.resolve("晚安"))

    def test_enabled_triggers_for_gallery(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.images.add_trigger(keyword="早安", image_id=image_id)
        self.assertEqual(len(self.images.enabled_triggers()), 2)

    def test_match_empty_text(self):
        image_id = self._add()
        self.images.add_trigger(keyword="晚安", image_id=image_id)
        self.assertIsNone(self.images.match(""))


class TestPicTags(unittest.TestCase):
    def test_extract_both_syntaxes(self):
        from mind.images import extract_pic_tags

        self.assertEqual(extract_pic_tags("哼<pic>晚安</pic>"), ["晚安"])
        self.assertEqual(extract_pic_tags("[pic:生气]"), ["生气"])
        self.assertEqual(extract_pic_tags("a<pic>x</pic>b[pic:y]c"), ["x", "y"])
        self.assertEqual(extract_pic_tags("没有标记"), [])

    def test_strip_leaves_only_text(self):
        from mind.images import strip_pic_tags

        self.assertEqual(strip_pic_tags("哼<pic>晚安</pic>我睡了"), "哼我睡了")
        self.assertEqual(strip_pic_tags("<pic>晚安</pic>"), "")

    def test_has_pic_tag(self):
        from mind.images import has_pic_tag

        self.assertTrue(has_pic_tag("x<pic>y</pic>"))
        self.assertFalse(has_pic_tag("x y"))

    def test_gallery_lists_keywords_and_notes(self):
        from mind.images import ImageTrigger, render_available_images

        text = render_available_images([
            ImageTrigger(keyword="晚安", note="睡前"),
            ImageTrigger(keyword="生气"),
        ])
        self.assertIn("<available_images>", text)
        self.assertIn("晚安（睡前）", text)
        self.assertIn("生气", text)
        self.assertIn("<pic>关键词</pic>", text)

    def test_gallery_empty_when_no_triggers(self):
        from mind.images import render_available_images

        self.assertEqual(render_available_images([]), "")


class TestCooldown(unittest.TestCase):
    def test_blocks_within_window(self):
        cool = Cooldown(30)
        self.assertTrue(cool.allow("s1", 1, now=1000.0))
        self.assertFalse(cool.allow("s1", 1, now=1010.0))
        self.assertTrue(cool.allow("s1", 1, now=1040.0))

    def test_different_sessions_are_independent(self):
        cool = Cooldown(30)
        self.assertTrue(cool.allow("s1", 1, now=1000.0))
        self.assertTrue(cool.allow("s2", 1, now=1000.0))

    def test_zero_disables(self):
        cool = Cooldown(0)
        self.assertTrue(cool.allow("s1", 1, now=1000.0))
        self.assertTrue(cool.allow("s1", 1, now=1000.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)

