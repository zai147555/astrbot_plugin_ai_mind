"""去 AI 味：检测器与改写清洗的单元测试。"""

from __future__ import annotations

import unittest

from mind import antiai


class AnalyseTest(unittest.TestCase):
    def test_human_speech_scores_high(self) -> None:
        text = "哼，你怎么才来。我都等你好久了，还以为你把我忘了呢。今天去哪了呀？"
        report = antiai.analyse(text)
        self.assertGreaterEqual(report.score, 90, report.to_dict())

    def test_ai_speech_scores_low(self) -> None:
        text = "首先，我们需要明确一点。其次，这个问题值得注意。总之，希望对你有所帮助。"
        report = antiai.analyse(text)
        self.assertLess(report.score, 60)
        labels = report.top
        self.assertIn("AI 高频词", labels)
        self.assertIn("套路句型", labels)

    def test_bullets_are_penalised(self) -> None:
        report = antiai.analyse("- 第一点\n- 第二点\n- 第三点")
        self.assertIn("项目符号", report.top)

    def test_headings_are_penalised(self) -> None:
        report = antiai.analyse("# 标题\n正文内容在这里摆着")
        self.assertIn("项目符号", report.top)

    def test_dash_is_penalised(self) -> None:
        report = antiai.analyse("我想说的其实很简单——你要好好的")
        self.assertIn("长破折号", report.top)

    def test_uniform_sentences_are_penalised(self) -> None:
        report = antiai.analyse("这是一段测试。这也是一段测试。那也是一段测试。")
        self.assertIn("句长过匀", report.top)

    def test_varied_sentences_are_fine(self) -> None:
        report = antiai.analyse("哼。你来了啊，我都等了你好久好久，还以为你不来了呢。")
        self.assertNotIn("句长过匀", report.top)

    def test_idiom_pile_is_penalised(self) -> None:
        report = antiai.analyse("勤勤恳恳，兢兢业业，任劳任怨，默默无闻。")
        self.assertIn("四字格堆砌", report.top)

    def test_normal_short_segments_are_not_idioms(self) -> None:
        """「我觉得吧，这事儿」不能被当成成语堆砌。"""
        report = antiai.analyse("我觉得吧，这事儿没那么复杂，你要是真想去我陪你就是了。")
        self.assertNotIn("四字格堆砌", report.top)

    def test_missing_particles_are_penalised(self) -> None:
        report = antiai.analyse("我今天在图书馆待了一整天顺便把作业写完了")
        self.assertIn("口语碎语不足", report.top)

    def test_short_text_is_exempt(self) -> None:
        self.assertEqual(antiai.analyse("嗯。").issues, [])
        self.assertEqual(antiai.analyse("").issues, [])

    def test_repeated_openers_are_penalised(self) -> None:
        report = antiai.analyse("其实我不太想去。其实你也知道。其实算了吧。")
        self.assertIn("口癖重复", report.top)

    def test_service_talk_is_penalised(self) -> None:
        report = antiai.analyse("很抱歉给您带来不便，请问有什么可以帮您")
        self.assertIn("客服话术", report.top)

    def test_extra_words_and_allow_list(self) -> None:
        text = "绝绝子，这事儿办得漂亮"
        self.assertNotIn("AI 高频词", antiai.analyse(text).top)
        self.assertIn("AI 高频词", antiai.analyse(text, extra_ai_words=["绝绝子"]).top)
        allowed = antiai.analyse("首先我先说一句", allow_words=["首先"])
        self.assertNotIn("AI 高频词", allowed.top)

    def test_report_shape(self) -> None:
        data = antiai.analyse("首先，总之，希望对你有所帮助。").to_dict()
        for key in ("score", "length", "sentences", "issues", "checks"):
            self.assertIn(key, data)
        self.assertTrue(data["issues"])

    def test_score_is_bounded(self) -> None:
        worst = "首先，其次，总之，综上所述，希望对你有所帮助，如果还有其他问题欢迎随时告诉我。作为一个AI，我会一直陪着你。"
        report = antiai.analyse(worst)
        self.assertGreaterEqual(report.score, 0.0)
        self.assertLessEqual(report.score, 100.0)

    def test_split_sentences(self) -> None:
        self.assertEqual(antiai.split_sentences("一。二！三？"), ["一", "二", "三"])


class RewritePromptTest(unittest.TestCase):
    def test_prompt_lists_the_issues(self) -> None:
        report = antiai.analyse("首先，总之，希望对你有所帮助。")
        prompt = antiai.build_rewrite_prompt(report, "首先，总之，希望对你有所帮助。")
        self.assertIn("AI 高频词", prompt)
        self.assertIn("只输出改写后的正文", prompt)
        self.assertIn("保持原文的口吻", prompt)

    def test_prompt_works_without_issues(self) -> None:
        report = antiai.analyse("嗯。")
        self.assertIn("整体太规整", antiai.build_rewrite_prompt(report, "嗯。"))


class CleanRewriteTest(unittest.TestCase):
    ORIGINAL = "首先，我们需要明确一点。总之，希望对你有所帮助。"

    def test_strips_prefix_label(self) -> None:
        raw = "改写：哼，这事儿没那么复杂，你别想太多。"
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), "哼，这事儿没那么复杂，你别想太多。")

    def test_strips_wrapping_quotes(self) -> None:
        raw = "「哼，这事儿没那么复杂。」"
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), "哼，这事儿没那么复杂。")

    def test_strips_code_fence(self) -> None:
        fence = chr(96) * 3
        raw = fence + "\n哼，别想太多。\n" + fence
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), "哼，别想太多。")

    def test_rejects_meta_answer(self) -> None:
        raw = "作为一个AI，我无法完成这个请求。"
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), "")

    def test_rejects_too_long(self) -> None:
        raw = "啊" * 200
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), "")

    def test_rejects_empty(self) -> None:
        self.assertEqual(antiai.clean_rewrite("", self.ORIGINAL), "")
        self.assertEqual(antiai.clean_rewrite("   ", self.ORIGINAL), "")

    def test_keeps_reasonable_text(self) -> None:
        raw = "哼，你来啦。等你好久了呢。"
        self.assertEqual(antiai.clean_rewrite(raw, self.ORIGINAL), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
