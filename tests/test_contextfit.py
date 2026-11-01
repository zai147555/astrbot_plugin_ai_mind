
"""上下文压缩：只砍废话，留住要紧的句子。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import contextfit as cf  # noqa: E402


def rows(n, **kw):
    out = [{"role": "user", "content": "闲聊%d" % i} for i in range(n)]
    for index, row in kw.items():
        out[int(index.split("_")[1])] = row
    return out


class EstimateTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cf.estimate_tokens(""), 0)

    def test_cjk_and_latin(self):
        self.assertEqual(cf.estimate_tokens("你好 hello"), 4)

    def test_none(self):
        self.assertEqual(cf.estimate_tokens(None), 0)


class ImportantTest(unittest.TestCase):
    def test_remember(self):
        self.assertTrue(cf.is_important({"role": "user", "content": "记住我生日是五月三号"}))

    def test_crisis(self):
        self.assertTrue(cf.is_important({"role": "user", "content": "我不想活了"}))

    def test_question(self):
        self.assertTrue(cf.is_important({"role": "user", "content": "你觉得我该不该去那个兼职？"}))

    def test_assistant_crisis(self):
        self.assertTrue(cf.is_important({"role": "assistant", "content": "别做伤害自己的事"}))

    def test_chit_chat(self):
        self.assertFalse(cf.is_important({"role": "user", "content": "哈哈"}))

    def test_empty(self):
        self.assertFalse(cf.is_important({"role": "user", "content": "   "}))


class PlanTest(unittest.TestCase):
    def test_short_history_untouched(self):
        data = rows(5)
        plan = cf.plan(data, max_turns=40)
        self.assertEqual(plan["dropped"], 0)
        self.assertEqual(plan["contexts"], data)
        self.assertEqual(plan["digest"], "")

    def test_drops_and_keeps_recent(self):
        data = [{"role": "user", "content": "第%d句，说点事情" % i} for i in range(30)]
        plan = cf.plan(data, max_turns=10, keep_recent=4, keep_important=0)
        self.assertEqual(plan["kept"], 4)
        self.assertEqual(plan["dropped"], 26)
        self.assertEqual(plan["contexts"][-1], data[-1])

    def test_keeps_important_old_line(self):
        data = [{"role": "user", "content": "记住我生日是五月三号"}] + \
               [{"role": "user", "content": "闲聊第%d句" % i} for i in range(29)]
        plan = cf.plan(data, max_turns=10, keep_recent=4, keep_important=1)
        texts = [row["content"] for row in plan["contexts"]]
        self.assertTrue(any("生日" in text for text in texts))
        self.assertIn("要紧", plan["digest"])

    def test_digest_quotes_dropped_lines(self):
        data = [{"role": "user", "content": "今天加班到十点，好累啊"}] + \
               [{"role": "user", "content": "闲聊第%d句" % i} for i in range(20)]
        plan = cf.plan(data, max_turns=6, keep_recent=2, keep_important=0)
        self.assertIn("更早的", plan["digest"])
        self.assertIn("他：", plan["digest"])

    def test_saved_tokens_not_negative(self):
        data = [{"role": "user", "content": "字" * 200} for _ in range(20)]
        plan = cf.plan(data, max_turns=4, keep_recent=2, keep_important=0)
        self.assertGreaterEqual(plan["saved_tokens"], 0)

    def test_list_content(self):
        data = [{"role": "user", "content": [{"type": "text", "text": "记住别熬夜"}]}] + \
               [{"role": "user", "content": "闲聊第%d句" % i} for i in range(20)]
        plan = cf.plan(data, max_turns=6, keep_recent=2, keep_important=1)
        texts = [cf._content(row) for row in plan["contexts"]]
        self.assertTrue(any("别熬夜" in text for text in texts))

    def test_minimum_window(self):
        data = rows(10)
        plan = cf.plan(data, max_turns=1, keep_recent=1, keep_important=0)
        self.assertLess(plan["kept"], len(data))
        self.assertGreaterEqual(len(plan["contexts"]), 2)


if __name__ == "__main__":
    unittest.main()
