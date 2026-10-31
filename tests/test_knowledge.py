
"""知识树：建树、激活、注入、模型改件待审。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind import knowledge


def sample() -> knowledge.KnowledgeBase:
    kb = knowledge.KnowledgeBase()
    tree = kb.add_tree("本群规矩", with_root="" )
    root = kb.add_node(tree, "说话方式", "中文之间不留空格")
    kb.add_node(tree, "称呼", "群里别喊真名", parent=root.id)
    kb.add_node(tree, "作息", "零点后基本不在")
    return kb


class TreeTest(unittest.TestCase):
    def test_add_and_read(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        self.assertEqual(tree.name, "本群规矩")
        self.assertEqual(len(tree.nodes), 3)
        self.assertEqual([n.title for n in tree.children("")], ["说话方式", "作息"])
        self.assertEqual(kb.by_name("本群规矩").id, tree.id)

    def test_node_cap(self) -> None:
        kb = knowledge.KnowledgeBase()
        tree = kb.add_tree("大")
        for index in range(knowledge.MAX_NODES):
            self.assertIsNotNone(kb.add_node(tree, "第%d条" % index))
        self.assertIsNone(kb.add_node(tree, "超了"))

    def test_content_is_truncated(self) -> None:
        kb = knowledge.KnowledgeBase()
        tree = kb.add_tree("t")
        node = kb.add_node(tree, "长", "x" * 5000)
        self.assertEqual(len(node.content), knowledge.MAX_CONTENT)


class RenderTest(unittest.TestCase):
    def test_outline_has_indent_and_content(self) -> None:
        text = knowledge.render_tree(sample().trees[0])
        self.assertIn("【本群规矩】", text)
        self.assertIn("- 说话方式：中文之间不留空格", text)
        self.assertIn("  - 称呼：群里别喊真名", text)

    def test_render_is_capped(self) -> None:
        kb = knowledge.KnowledgeBase()
        tree = kb.add_tree("大")
        for index in range(60):
            kb.add_node(tree, "条目%d" % index, "内容" * 40)
        text = knowledge.render_tree(tree, max_chars=400)
        self.assertLessEqual(len(text), 420)

    def test_empty_tree_renders_nothing(self) -> None:
        kb = knowledge.KnowledgeBase()
        self.assertEqual(knowledge.render_tree(kb.add_tree("空")), "")

    def test_active_session_block(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        self.assertEqual(knowledge.render_active(kb, "s1"), "")
        kb.activate("s1", tree.id)
        text = knowledge.render_active(kb, "s1")
        self.assertIn("<knowledge>", text)
        self.assertIn("本群规矩", text)
        self.assertIn("不要重新整理", text)
        self.assertEqual(knowledge.render_active(kb, "s2"), "")


class ActiveTest(unittest.TestCase):
    def test_activate_and_deactivate(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        self.assertTrue(kb.activate("s1", tree.id))
        self.assertFalse(kb.activate("s1", tree.id), "同一棵别重复挂")
        self.assertEqual([t.id for t in kb.active_trees("s1")], [tree.id])
        self.assertTrue(kb.deactivate("s1", tree.id))
        self.assertEqual(kb.active_trees("s1"), [])

    def test_unknown_tree(self) -> None:
        kb = knowledge.KnowledgeBase()
        self.assertFalse(kb.activate("s1", "nope"))

    def test_empty_session(self) -> None:
        kb = sample()
        self.assertFalse(kb.activate("", kb.trees[0].id))


class ReviewTest(unittest.TestCase):
    def test_propose_then_approve(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        node = tree.nodes[0]
        edit = kb.propose(tree.id, node.id, content="新的说话方式", note="模型补充")
        self.assertEqual(len(kb.pending), 1)
        self.assertTrue(kb.approve(edit.id))
        self.assertEqual(tree.get(node.id).content, "新的说话方式")
        self.assertEqual(tree.get(node.id).origin, "model")
        self.assertEqual(kb.pending, [])

    def test_reject_keeps_original(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        node = tree.nodes[0]
        before = node.content
        edit = kb.propose(tree.id, node.id, content="不该改")
        self.assertTrue(kb.reject(edit.id))
        self.assertEqual(tree.get(node.id).content, before)
        self.assertEqual(kb.pending, [])

    def test_approve_unknown(self) -> None:
        self.assertFalse(knowledge.KnowledgeBase().approve("nope"))
        self.assertFalse(knowledge.KnowledgeBase().reject("nope"))

    def test_title_change_applies(self) -> None:
        kb = sample()
        tree = kb.trees[0]
        node = tree.nodes[0]
        edit = kb.propose(tree.id, node.id, title="新的标题")
        self.assertTrue(kb.approve(edit.id))
        self.assertEqual(tree.get(node.id).title, "新的标题")


class StorageTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        kb = sample()
        kb.activate("s1", kb.trees[0].id)
        kb.propose(kb.trees[0].id, kb.trees[0].nodes[0].id, content="改动")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.json"
            self.assertTrue(knowledge.save(path, kb))
            again = knowledge.load(path)
        self.assertEqual(len(again.trees), 1)
        self.assertEqual(len(again.pending), 1)
        self.assertEqual(again.active["s1"], [kb.trees[0].id])

    def test_broken_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k.json"
            path.write_text("{nope", encoding="utf-8")
            kb = knowledge.load(path)
        self.assertEqual(kb.trees, [])

    def test_summary(self) -> None:
        kb = sample()
        kb.activate("s1", kb.trees[0].id)
        stats = knowledge.summarize(kb)
        self.assertEqual((stats["trees"], stats["nodes"], stats["sessions"]), (1, 3, 1))


class SearchTest(unittest.TestCase):
    def test_find_nodes(self) -> None:
        hits = knowledge.find_nodes(sample(), "真名")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1].title, "称呼")

    def test_empty_keyword(self) -> None:
        self.assertEqual(knowledge.find_nodes(sample(), "  "), [])


if __name__ == "__main__":
    unittest.main()

