"""消息防抖的单元测试（纯规则，不依赖 astrbot）。"""

from __future__ import annotations

import unittest

from mind import debounce as d


def conf(**kwargs) -> d.DebounceSettings:
    base = {"enabled": True}
    base.update(kwargs)
    return d.DebounceSettings(**base)


class WaitWindowTest(unittest.TestCase):
    """判断「这句话说完了没」。"""

    def test_sentence_end_does_not_wait(self) -> None:
        for text in ("随便你。", "你吃了吗", "在吗", "去WebUI里看嘛。", "好吧", "走啦", "这样啊"):
            self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_NONE, text)

    def test_trailing_comma_waits_two(self) -> None:
        for text in ("今天晚上，", "我跟你、", "首先：", "然后;", "还有,"):
            self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_TWO, text)

    def test_conjunction_tail_waits_two(self) -> None:
        for text in ("然后", "因为", "我跟", "而且"):
            self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_TWO, text)

    def test_particle_with_question_tail_waits_one(self) -> None:
        # 「所以呢」结尾是「呢」，归到「拿不准」那档
        self.assertEqual(d.wait_windows("所以呢", conf()), d.WINDOW_ONE)

    def test_long_text_does_not_wait(self) -> None:
        text = "今天晚上吃什么好呢我想想再说"
        self.assertGreaterEqual(len(text), 12)
        self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_NONE)

    def test_short_without_punct_waits_one(self) -> None:
        for text in ("晚上吃什么", "我跟你", "好的"):
            self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_ONE, text)

    def test_filler_word_waits_two(self) -> None:
        """「那个」「就是」本身就是说半截的填词，不能当成一句完整的话。"""
        for text in ("那个", "就是", "我跟"):
            self.assertEqual(d.wait_windows(text, conf()), d.WINDOW_TWO, text)

    def test_empty_text_never_waits(self) -> None:
        self.assertEqual(d.wait_windows("", conf()), d.WINDOW_NONE)
        self.assertEqual(d.wait_windows("   ", conf()), d.WINDOW_NONE)

    def test_sentence_end_beats_conjunction(self) -> None:
        """「然后。」是说完的 —— 句末标点优先。"""
        self.assertFalse(d.is_incomplete("然后。"))
        self.assertEqual(d.wait_windows("然后。", conf()), d.WINDOW_NONE)

    def test_long_enough_is_configurable(self) -> None:
        self.assertEqual(d.wait_windows("晚上吃什么", conf(long_enough=4)), d.WINDOW_NONE)
        self.assertEqual(d.wait_windows("晚上吃什么", conf(long_enough=40)), d.WINDOW_ONE)

    def test_ellipsis_is_ambiguous(self) -> None:
        """「……」是拖着尾音，既不等于说完也不算没说完。"""
        self.assertEqual(d.wait_windows("你好……", conf()), d.WINDOW_ONE)


class MergeTextTest(unittest.TestCase):
    """合并连发的几条。"""

    def test_chinese_joins_without_space(self) -> None:
        self.assertEqual(d.merge_texts(["今天晚上", "吃什么"]), "今天晚上吃什么")
        self.assertEqual(d.merge_texts(["晚上好。", "随便你"]), "晚上好。随便你")
        self.assertEqual(d.merge_texts(["我跟你，", "说个事"]), "我跟你，说个事")

    def test_english_joins_with_space(self) -> None:
        self.assertEqual(d.merge_texts(["hello", "world"]), "hello world")
        self.assertEqual(d.merge_texts(["v1.2", "beta"]), "v1.2 beta")

    def test_mixed_does_not_add_space(self) -> None:
        self.assertEqual(d.merge_texts(["hello", "你好"]), "hello你好")
        self.assertEqual(d.merge_texts(["你好", "hello"]), "你好hello")

    def test_blank_pieces_are_skipped(self) -> None:
        self.assertEqual(d.merge_texts(["  ", "你好", ""]), "你好")
        self.assertEqual(d.merge_texts([]), "")
        self.assertEqual(d.merge_texts(["", ""]), "")

    def test_single_piece_is_itself(self) -> None:
        self.assertEqual(d.merge_texts(["嗯"]), "嗯")


class ScopeAndCommandTest(unittest.TestCase):
    def test_scope(self) -> None:
        self.assertTrue(d.scope_allows(conf(scope="both"), is_group=True))
        self.assertTrue(d.scope_allows(conf(scope="both"), is_group=False))
        self.assertFalse(d.scope_allows(conf(scope="private"), is_group=True))
        self.assertTrue(d.scope_allows(conf(scope="private"), is_group=False))
        self.assertTrue(d.scope_allows(conf(scope="group"), is_group=True))
        self.assertFalse(d.scope_allows(conf(scope="group"), is_group=False))

    def test_command_is_never_debounced(self) -> None:
        for text in ("/情绪", "!help", "！帮助", "／记忆", "#测试"):
            self.assertTrue(d.looks_like_command(text), text)
        self.assertFalse(d.looks_like_command("情绪怎么样"))
        self.assertFalse(d.looks_like_command(""))


class ConfigTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        self.assertFalse(d.DebounceSettings.from_config({}).enabled)

    def test_reads_block(self) -> None:
        parsed = d.DebounceSettings.from_config(
            {"debounce": {"enabled": True, "scope": "group", "grace_seconds": 2.5}}
        )
        self.assertTrue(parsed.enabled)
        self.assertEqual(parsed.scope, "group")
        self.assertEqual(parsed.grace_seconds, 2.5)

    def test_clamps_and_falls_back(self) -> None:
        parsed = d.DebounceSettings.from_config(
            {"debounce": {"grace_seconds": 999, "max_messages": 100, "scope": "???", "long_enough": -5}}
        )
        self.assertLessEqual(parsed.grace_seconds, 10.0)
        self.assertEqual(parsed.max_messages, 20)
        self.assertEqual(parsed.scope, "both")
        self.assertGreaterEqual(parsed.long_enough, 4)

    def test_overrides_win(self) -> None:
        parsed = d.DebounceSettings.from_config(
            {"debounce": {"enabled": True}}, {"enabled": False}
        )
        self.assertFalse(parsed.enabled)

    def test_round_trip(self) -> None:
        parsed = d.DebounceSettings.from_config({"debounce": {"enabled": True}})
        again = d.DebounceSettings.from_config({"debounce": parsed.to_dict()})
        self.assertEqual(parsed.to_dict(), again.to_dict())


class PreviewTest(unittest.TestCase):
    def test_verdicts(self) -> None:
        on = conf()
        self.assertEqual(d.preview("你吃了吗", on)["verdict"], "send")
        self.assertEqual(d.preview("晚上吃什么", on)["verdict"], "grace")
        self.assertEqual(d.preview("今天晚上，", on)["verdict"], "hold")
        self.assertEqual(d.preview("你吃了吗", d.DebounceSettings())["verdict"], "disabled")

    def test_wait_seconds(self) -> None:
        got = d.preview("今天晚上，", conf(grace_seconds=2.0))
        self.assertEqual(got["windows"], d.WINDOW_TWO)
        self.assertEqual(got["wait_seconds"], 4.0)

    def test_describe(self) -> None:
        self.assertIn("关", d.describe(d.DebounceSettings()))
        self.assertIn("1.5", d.describe(conf()))


if __name__ == "__main__":
    unittest.main()
