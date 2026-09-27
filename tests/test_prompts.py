"""提示词仓库：模板渲染、空行折叠、覆盖与还原。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mind import prompts
from mind.prompts import PromptStore, render


class RenderTest(unittest.TestCase):
    def test_plain_substitution(self) -> None:
        self.assertEqual(render("你好{who}", {"who": "他"}), "你好他")

    def test_empty_placeholder_drops_the_line(self) -> None:
        text = "第一行\n情绪起因：{cause}\n最后一行"
        self.assertEqual(render(text, {"cause": ""}), "第一行\n最后一行")

    def test_unknown_placeholder_is_kept(self) -> None:
        self.assertIn("{nobody}", render("你好{nobody}", {}))

    def test_broken_format_spec_does_not_raise(self) -> None:
        self.assertEqual(render("值 {a:!!!}", {"a": 1}), "值 {a:!!!}")

    def test_format_spec_is_applied(self) -> None:
        self.assertEqual(render("{p:+.2f}", {"p": 0.5}), "+0.50")

    def test_empty_template(self) -> None:
        self.assertEqual(render("", {"a": 1}), "")


class BlockRegistryTest(unittest.TestCase):
    def test_blocks_are_unique(self) -> None:
        keys = [block.key for block in prompts.BLOCKS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_block_has_a_default(self) -> None:
        for block in prompts.BLOCKS:
            self.assertTrue(prompts.default_of(block.key).strip(), block.key)

    def test_groups_are_ordered(self) -> None:
        groups = {block.group for block in prompts.BLOCKS}
        self.assertTrue(groups <= set(prompts.GROUP_ORDER), groups)

    def test_memory_and_relation_rules_come_from_code(self) -> None:
        from mind.emotion.prompt import RELATION_RULES_BLOCK
        from mind.memory.prompt import RULES_BLOCK

        self.assertEqual(prompts.default_of("memory.rules"), RULES_BLOCK)
        self.assertEqual(prompts.default_of("relation.rules"), RELATION_RULES_BLOCK)

    def test_check_placeholders(self) -> None:
        self.assertEqual(prompts.check_placeholders("relation.block", "{who} {label}"), [])
        self.assertEqual(
            prompts.check_placeholders("relation.block", "{who} {nope}"), ["nope"]
        )


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = PromptStore(self.dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_when_file_missing(self) -> None:
        self.assertEqual(self.store.get("emotion.rules"), prompts.default_of("emotion.rules"))
        self.assertFalse(self.store.is_custom("emotion.rules"))
        self.assertEqual(self.store.payload()["custom_count"], 0)

    def test_save_and_reload(self) -> None:
        self.assertTrue(self.store.save({"emotion.rules": "改过的规则"}))
        self.assertTrue(self.store.path.is_file())
        again = PromptStore(self.dir)
        self.assertEqual(again.get("emotion.rules"), "改过的规则")
        self.assertTrue(again.is_custom("emotion.rules"))

    def test_equal_to_default_is_not_stored(self) -> None:
        self.assertTrue(self.store.save({"emotion.rules": prompts.default_of("emotion.rules")}))
        self.assertFalse(self.store.is_custom("emotion.rules"))

    def test_empty_is_not_stored(self) -> None:
        self.assertTrue(self.store.save({"emotion.rules": "   "}))
        self.assertFalse(self.store.is_custom("emotion.rules"))

    def test_unknown_key_is_ignored(self) -> None:
        self.assertTrue(self.store.save({"没这个块": "x"}))
        self.assertEqual(self.store.overrides, {})

    def test_reset_all_and_single(self) -> None:
        self.store.save({"emotion.rules": "A", "memory.rules": "B"})
        self.store.reset(["emotion.rules"])
        self.assertFalse(self.store.is_custom("emotion.rules"))
        self.assertTrue(self.store.is_custom("memory.rules"))
        self.store.reset()
        self.assertEqual(self.store.overrides, {})

    def test_broken_file_falls_back_to_defaults(self) -> None:
        self.store.path.write_text("{ 这不是 JSON", encoding="utf-8")
        again = PromptStore(self.dir)
        self.assertEqual(again.overrides, {})
        self.assertEqual(again.get("emotion.rules"), prompts.default_of("emotion.rules"))

    def test_payload_shape(self) -> None:
        data = self.store.payload()
        self.assertIn("groups", data)
        self.assertEqual(data["block_count"], len(prompts.BLOCKS))
        keys = [b["key"] for group in data["groups"] for b in group["blocks"]]
        self.assertEqual(sorted(keys), sorted(prompts.BLOCK_BY_KEY))

    def test_preview_uses_sample_values(self) -> None:
        text = self.store.preview("relation.block")
        self.assertIn("宝宝", text)
        self.assertNotIn("{label}", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

