"""拟人分段引擎的单元测试。

这一层不需要 astrbot：分段内核只认「类名是 Plain 的组件」，
所以这里用几个同名假组件就能把切分、保护、延迟、替换全跑一遍。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from mind import splitter as sp

FENCE = chr(96) * 3


class Image:
    def __init__(self, file: str = "a.png") -> None:
        self.file = file


class At:
    def __init__(self, qq: str = "123") -> None:
        self.qq = qq


class Record:
    """语音组件。真框架转出来的 Record 会把原文放在 .text 里。"""

    def __init__(self, file: str = "a.mp3", text: str = "") -> None:
        self.file = file
        self.url = file
        self.text = text


class Face:
    def __init__(self, id: int = 1) -> None:
        self.id = id


class Reply:
    def __init__(self, id: str = "9") -> None:
        self.id = id


def texts(segments) -> list[str]:
    return [sp.segment_text(seg) for seg in segments]


def settings(**kwargs) -> sp.SplitterSettings:
    base = {"max_segments": 0, "delay_strategy": "fixed", "fixed_delay": 0.0}
    base.update(kwargs)
    return sp.SplitterSettings.from_mapping(base)


class SplitBasicsTest(unittest.TestCase):
    def test_splits_at_sentence_end(self) -> None:
        segs = sp.split_chain([sp.make_plain("哼。你来了。走吧。")], settings())
        self.assertEqual(texts(segs), ["哼。", "你来了。", "走吧。"])

    def test_keeps_trailing_text_without_punctuation(self) -> None:
        segs = sp.split_chain([sp.make_plain("哼。还没写完")], settings())
        self.assertEqual(texts(segs), ["哼。", "还没写完"])

    def test_leading_punctuation_does_not_make_a_bubble(self) -> None:
        """开头就是一串标点时，不能吐出一个只有标点的空气泡。"""
        segs = sp.split_chain([sp.make_plain("。。。哼。你来了。")], settings())
        self.assertEqual(texts(segs), ["。。。哼。", "你来了。"])

    def test_empty_input(self) -> None:
        self.assertEqual(sp.split_chain([], settings()), [])
        self.assertEqual(sp.split_chain([sp.make_plain("")], settings()), [])
        self.assertEqual(sp.split_chain([sp.make_plain("   ")], settings()), [])

    def test_caps_segment_count(self) -> None:
        text = "一。二。三。四。五。六。"
        segs = sp.split_chain([sp.make_plain(text)], settings(max_segments=2))
        self.assertEqual(len(segs), 2)
        self.assertEqual("".join(texts(segs)), text)

    def test_cap_keeps_every_character(self) -> None:
        text = "哼，你怎么才来。我都等好久了。今天去哪了呀？说嘛说嘛！"
        segs = sp.split_chain([sp.make_plain(text)], settings(max_segments=3))
        self.assertLessEqual(len(segs), 3)
        self.assertEqual("".join(texts(segs)), text)

    def test_simple_mode_uses_configured_chars(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("哼|你来了|走吧")],
            settings(split_mode="simple", split_chars=["|"]),
        )
        self.assertEqual(texts(segs), ["哼|", "你来了|", "走吧"])

    def test_simple_mode_understands_escaped_newline(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("哼\n你来了")],
            settings(split_mode="simple", split_chars=["\\n"]),
        )
        self.assertEqual(texts(segs), ["哼", "你来了"])
        loose = sp.split_chain(
            [sp.make_plain("哼\n你来了")],
            settings(split_mode="simple", split_chars=["\\n"], trim_edge_blank_lines=False),
        )
        self.assertEqual(texts(loose), ["哼\n", "你来了"])

    def test_broken_regex_falls_back_to_default(self) -> None:
        segs = sp.split_chain([sp.make_plain("哼。你来了。")], settings(split_regex="[("))
        self.assertEqual(texts(segs), ["哼。", "你来了。"])

    def test_adjacent_plain_components_get_merged(self) -> None:
        chain = [sp.make_plain("哼。"), sp.make_plain("你来了。")]
        segs = sp.split_chain(chain, settings())
        self.assertEqual(texts(segs), ["哼。", "你来了。"])

    def test_multiple_plain_blocks_share_one_buffer(self) -> None:
        """上一条消息链里 Plain 被拆成好几块是常事，不能因此凭空多切一段。"""
        chain = [sp.make_plain("哼。你"), sp.make_plain("来了。")]
        segs = sp.split_chain(chain, settings())
        self.assertEqual(texts(segs), ["哼。", "你来了。"])


class ProtectionTest(unittest.TestCase):
    def test_code_fence_is_kept_whole(self) -> None:
        text = "看这个：\n" + FENCE + "python\nprint(1)\nprint(2)\n" + FENCE + "\n懂了没？"
        segs = sp.split_chain([sp.make_plain(text)], settings())
        joined = texts(segs)
        self.assertTrue(any(FENCE in item for item in joined))
        block = [item for item in joined if FENCE in item][0]
        self.assertIn("print(1)", block)
        self.assertIn("print(2)", block)

    def test_think_block_is_kept_whole(self) -> None:
        text = "嗯。\n<think>他在问我今天干嘛。要不要说实话呢。</think>\n那就说吧。"
        segs = sp.split_chain([sp.make_plain(text)], settings())
        joined = texts(segs)
        block = [item for item in joined if "<think>" in item][0]
        self.assertIn("要不要说实话呢。", block)
        self.assertIn("</think>", block)

    def test_markdown_table_is_kept_whole(self) -> None:
        text = "给你看：\n| 项目 | 值 |\n| --- | --- |\n| 心情 | 不好 |\n看完了没？"
        segs = sp.split_chain([sp.make_plain(text)], settings())
        joined = texts(segs)
        block = [item for item in joined if "| 项目 |" in item][0]
        self.assertIn("| 心情 | 不好 |", block)

    def test_does_not_split_inside_quotes(self) -> None:
        text = "他说「你不要乱说。我在忙。」然后就走了。哼。"
        segs = sp.split_chain([sp.make_plain(text)], settings())
        joined = texts(segs)
        self.assertTrue(
            any("「你不要乱说。我在忙。」" in item for item in joined),
            "引号里的句子被切开了：" + repr(joined),
        )

    def test_does_not_split_before_protected_word(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("哼。宝宝你在干嘛。")],
            settings(no_split_around=["宝宝"]),
        )
        self.assertEqual(texts(segs), ["哼。宝宝你在干嘛。"])

    def test_protected_word_only_blocks_its_own_boundary(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("哼。宝宝来了。好玩。")],
            settings(no_split_around=["宝宝"]),
        )
        self.assertEqual(texts(segs), ["哼。宝宝来了。", "好玩。"])

    def test_english_period_is_not_a_split_point(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("Wait. I don't think so. 算了。")],
            settings(),
        )
        joined = texts(segs)
        self.assertTrue(any("Wait. I don't think so." in item for item in joined), joined)

    def test_space_between_cjk_and_latin_is_not_a_split_point(self) -> None:
        segs = sp.split_chain([sp.make_plain("我叫 Du Keyi 你呢")], settings(split_regex="[ \\n]+"))
        joined = texts(segs)
        self.assertTrue(any("Du Keyi" in item for item in joined), joined)

    def test_newline_always_splits(self) -> None:
        segs = sp.split_chain([sp.make_plain("哼\n\n你来了")], settings())
        self.assertEqual(texts(segs), ["哼", "你来了"])

    def test_newline_is_kept_when_trimming_is_off(self) -> None:
        segs = sp.split_chain(
            [sp.make_plain("哼\n\n你来了")], settings(trim_edge_blank_lines=False)
        )
        self.assertEqual(texts(segs), ["哼\n\n", "你来了"])

    def test_trim_edge_blank_lines(self) -> None:
        segs = sp.split_chain([sp.make_plain("\n\n哼。\n\n你来了。\n\n")], settings())
        self.assertEqual(texts(segs), ["哼。", "你来了。"])


class ComponentPolicyTest(unittest.TestCase):
    def test_image_goes_out_alone(self) -> None:
        chain = [sp.make_plain("给你看。"), Image(), sp.make_plain("好看吗？")]
        segs = sp.split_chain(chain, settings())
        self.assertEqual(len(segs), 3)
        self.assertIsInstance(segs[1][0], Image)
        self.assertFalse(sp.is_blank_segment(segs[1]))

    def test_image_can_follow_next_segment(self) -> None:
        chain = [sp.make_plain("给你看。"), Image(), sp.make_plain("好看吗？")]
        segs = sp.split_chain(chain, settings(image_strategy=sp.STRATEGY_NEXT))
        self.assertEqual(texts(segs)[1], "好看吗？")
        self.assertIsInstance(segs[1][0], Image)

    def test_image_can_follow_previous_segment(self) -> None:
        chain = [sp.make_plain("给你看。"), Image(), sp.make_plain("好看吗？")]
        segs = sp.split_chain(chain, settings(image_strategy=sp.STRATEGY_PREV))
        self.assertIsInstance(segs[0][-1], Image)

    def test_embed_keeps_media_in_text_order(self) -> None:
        """嵌入 = 图片跟着它前后那句一起发，不单独占一个气泡。"""
        chain = [sp.make_plain("给你看"), Image(), sp.make_plain("好看吗？")]
        segs = sp.split_chain(chain, settings(image_strategy=sp.STRATEGY_EMBED))
        self.assertEqual(len(segs), 1)
        self.assertTrue(sp.is_plain(segs[0][0]))
        self.assertIsInstance(segs[0][1], Image)
        self.assertEqual(sp.segment_text(segs[0]), "给你看好看吗？")

    def test_embed_after_a_sentence_break_starts_the_next_bubble(self) -> None:
        chain = [sp.make_plain("给你看。"), Image(), sp.make_plain("好看吗？")]
        segs = sp.split_chain(chain, settings(image_strategy=sp.STRATEGY_EMBED))
        self.assertEqual([sp.segment_text(s) for s in segs], ["给你看。", "好看吗？"])
        self.assertIsInstance(segs[1][0], Image)

    def test_reply_component_rides_the_first_segment(self) -> None:
        chain = [Reply(), sp.make_plain("哼。你来了。")]
        segs = sp.split_chain(chain, settings())
        self.assertIsInstance(segs[0][0], Reply)

    def test_reply_can_be_dropped(self) -> None:
        chain = [Reply(), sp.make_plain("哼。你来了。")]
        segs = sp.split_chain(chain, settings(enable_reply=False))
        self.assertEqual(len(segs[0]), 1)
        self.assertTrue(sp.is_plain(segs[0][0]))

    def test_blank_media_only_segment_survives(self) -> None:
        segs = sp.split_chain([Image()], settings())
        self.assertEqual(len(segs), 1)
        self.assertTrue(sp.has_media(segs[0]))
        self.assertFalse(sp.is_blank_segment(segs[0]))

    def test_strategy_aliases(self) -> None:
        self.assertEqual(sp.normalise_strategy("alone"), sp.STRATEGY_ALONE)
        self.assertEqual(sp.normalise_strategy("接下文"), sp.STRATEGY_NEXT)
        self.assertEqual(sp.normalise_strategy("embed"), sp.STRATEGY_EMBED)
        self.assertEqual(sp.normalise_strategy("胡说"), sp.STRATEGY_NEXT)

    def test_strategy_for_known_kinds(self) -> None:
        conf = settings(
            image_strategy=sp.STRATEGY_ALONE,
            at_strategy=sp.STRATEGY_NEXT,
            face_strategy=sp.STRATEGY_EMBED,
            other_strategy=sp.STRATEGY_PREV,
        )
        self.assertEqual(conf.strategy_for("image"), sp.STRATEGY_ALONE)
        self.assertEqual(conf.strategy_for("record"), sp.STRATEGY_ALONE)
        self.assertEqual(conf.strategy_for("at"), sp.STRATEGY_NEXT)
        self.assertEqual(conf.strategy_for("atall"), sp.STRATEGY_NEXT)
        self.assertEqual(conf.strategy_for("face"), sp.STRATEGY_EMBED)
        self.assertEqual(conf.strategy_for("unknownthing"), sp.STRATEGY_PREV)


class DelayTest(unittest.TestCase):
    def test_linear(self) -> None:
        conf = settings(delay_strategy="linear", linear_base=1.0, linear_factor=0.1)
        self.assertAlmostEqual(sp.calculate_delay(conf, "12345"), 1.5, places=3)

    def test_log_grows_with_length(self) -> None:
        conf = settings(delay_strategy="log", log_base=0.0, log_factor=1.0, max_delay=99)
        short = sp.calculate_delay(conf, "啊")
        long = sp.calculate_delay(conf, "啊" * 30)
        self.assertLess(short, long)

    def test_fixed(self) -> None:
        conf = settings(delay_strategy="fixed", fixed_delay=2.5)
        self.assertAlmostEqual(sp.calculate_delay(conf, "随便多长"), 2.5, places=3)

    def test_random_stays_in_range(self) -> None:
        conf = settings(delay_strategy="random", random_min=1.0, random_max=2.0)
        for _ in range(50):
            value = sp.calculate_delay(conf, "随便")
            self.assertGreaterEqual(value, 1.0)
            self.assertLessEqual(value, 2.0)

    def test_random_swapped_bounds_still_work(self) -> None:
        conf = settings(delay_strategy="random", random_min=3.0, random_max=1.0)
        value = sp.calculate_delay(conf, "随便")
        self.assertGreaterEqual(value, 1.0)
        self.assertLessEqual(value, 3.0)

    def test_max_delay_clamps(self) -> None:
        conf = settings(delay_strategy="linear", linear_base=0.0, linear_factor=10.0, max_delay=4.0)
        self.assertAlmostEqual(sp.calculate_delay(conf, "啊" * 100), 4.0, places=3)

    def test_unknown_strategy_falls_back_to_linear(self) -> None:
        self.assertEqual(sp._delay_strategy("nonsense"), "linear")


class ReplaceAndCleanTest(unittest.TestCase):
    def test_parse_dict_rules(self) -> None:
        rules = sp.parse_replace_rules([{"find": "a", "replace": "b"}])
        self.assertEqual(rules, [("a", "b")])

    def test_parse_arrow_rules(self) -> None:
        rules = sp.parse_replace_rules(["作为AI=>", "喵==>汪"])
        self.assertEqual(rules, [("作为AI", ""), ("喵", "汪")])

    def test_parse_ignores_junk(self) -> None:
        self.assertEqual(sp.parse_replace_rules(""), [])
        self.assertEqual(sp.parse_replace_rules(None), [])
        self.assertEqual(sp.parse_replace_rules([{"replace": "x"}, 5, ""]), [])

    def test_unescape_newline(self) -> None:
        self.assertEqual(sp.unescape_replace_str("a\\nb"), "a" + chr(10) + "b")

    def test_apply_replace_rules(self) -> None:
        text = sp.apply_replace_rules("作为AI我很抱歉", [("作为AI", "")])
        self.assertEqual(text, "我很抱歉")

    def test_clean_before_and_after(self) -> None:
        conf = settings(
            clean_before_items=["[SYS]"],
            clean_after_items=["~~"],
            clean_after_regex="\\s+$",
        )
        self.assertEqual(sp.clean_text("[SYS]哼", conf, "before"), "哼")
        self.assertEqual(sp.clean_text("哼~~", conf, "after"), "哼")

    def test_broken_clean_regex_is_ignored(self) -> None:
        conf = settings(clean_before_regex="[(")
        self.assertEqual(sp.clean_text("哼", conf, "before"), "哼")


class FilterTest(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        self.assertFalse(sp.should_split(settings(enabled=False), text="哼。你来了。"))

    def test_llm_only(self) -> None:
        conf = settings(llm_only=True)
        self.assertFalse(sp.should_split(conf, is_llm=False, text="哼。"))
        self.assertTrue(sp.should_split(conf, is_llm=True, text="哼。"))

    def test_scope(self) -> None:
        self.assertTrue(sp.should_split(settings(scope="group"), is_group=True, text="哼。"))
        self.assertFalse(sp.should_split(settings(scope="group"), is_group=False, text="哼。"))
        self.assertTrue(sp.should_split(settings(scope="private"), is_group=False, text="哼。"))
        self.assertFalse(sp.should_split(settings(scope="private"), is_group=True, text="哼。"))
        self.assertTrue(sp.should_split(settings(scope="both"), is_group=True, text="哼。"))

    def test_length_gates(self) -> None:
        conf = settings(min_length=10, max_length=20)
        self.assertFalse(sp.should_split(conf, text="短"))
        self.assertTrue(sp.should_split(conf, text="啊" * 15))
        self.assertFalse(sp.should_split(conf, text="啊" * 30))

    def test_blacklist_and_whitelist(self) -> None:
        conf = settings(blacklist=["aiocqhttp:GroupMessage:999*"])
        self.assertFalse(sp.should_split(conf, session_id="aiocqhttp:GroupMessage:9991", text="哼。"))
        self.assertTrue(sp.should_split(conf, session_id="aiocqhttp:GroupMessage:1", text="哼。"))
        conf = settings(whitelist=["aiocqhttp:FriendMessage:*"])
        self.assertTrue(sp.should_split(conf, session_id="aiocqhttp:FriendMessage:1", text="哼。"))
        self.assertFalse(sp.should_split(conf, session_id="aiocqhttp:GroupMessage:1", text="哼。"))

    def test_unknown_scope_falls_back_to_both(self) -> None:
        self.assertEqual(sp.SplitterSettings.from_mapping({"scope": "??"}).scope, "both")

    def test_scope_aliases(self) -> None:
        self.assertEqual(sp.SplitterSettings.from_mapping({"scope": "全部"}).scope, "both")


class ConfigTest(unittest.TestCase):
    def test_defaults_are_sane(self) -> None:
        conf = sp.SplitterSettings.from_mapping({})
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.max_segments, 3)
        self.assertTrue(conf.smart)

    def test_config_section_and_overrides(self) -> None:
        config = {"splitter": {"enabled": False, "max_segments": 5}}
        conf = sp.SplitterSettings.from_config(config)
        self.assertFalse(conf.enabled)
        self.assertEqual(conf.max_segments, 5)
        conf = sp.SplitterSettings.from_config(config, {"enabled": True, "max_segments": 2})
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.max_segments, 2)

    def test_overrides_ignore_none(self) -> None:
        conf = sp.SplitterSettings.from_config({}, {"max_segments": None})
        self.assertEqual(conf.max_segments, 3)

    def test_clamps_out_of_range(self) -> None:
        conf = sp.SplitterSettings.from_mapping({"max_segments": 999, "max_delay": -5})
        self.assertEqual(conf.max_segments, 30)
        self.assertEqual(conf.max_delay, 0.0)

    def test_to_dict_round_trips(self) -> None:
        conf = sp.SplitterSettings.from_mapping({"max_segments": 4, "blacklist": ["a"]})
        again = sp.SplitterSettings.from_mapping(conf.to_dict())
        self.assertEqual(again.to_dict(), conf.to_dict())

    def test_panel_schema_shape(self) -> None:
        schema = sp.panel_schema()
        for key in ("scopes", "strategies", "delay_strategies", "modes", "scope_labels"):
            self.assertIn(key, schema)
        self.assertEqual(set(schema["scope_labels"]), set(schema["scopes"]))


class PreviewTest(unittest.TestCase):
    def test_preview_reports_segments_and_delays(self) -> None:
        conf = settings(delay_strategy="fixed", fixed_delay=1.0)
        result = sp.preview("哼。你来了。走吧。", conf)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 3)
        self.assertEqual([s["text"] for s in result["segments"]], ["哼。", "你来了。", "走吧。"])
        self.assertAlmostEqual(result["segments"][0]["delay_before_next"], 1.0, places=2)
        self.assertAlmostEqual(result["segments"][-1]["delay_before_next"], 0.0, places=2)

    def test_preview_empty_text(self) -> None:
        result = sp.preview("", settings())
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["segments"], [])

    def test_preview_reports_over_limit(self) -> None:
        result = sp.preview("一。二。三。四。", settings(max_segments=2))
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["over_limit"])

    def test_preview_applies_replace_rules(self) -> None:
        conf = settings(replace_rules=[{"find": "作为AI", "replace": ""}])
        result = sp.preview("作为AI我不知道。真的。", conf)
        self.assertEqual(result["segments"][0]["text"], "我不知道。")
        self.assertEqual(result["rule_count"], 1)

    def test_preview_pattern_is_reported(self) -> None:
        result = sp.preview("哼。", settings(split_regex="[。]+"))
        self.assertEqual(result["pattern"], "[。]+")

    def test_describe_mentions_state(self) -> None:
        self.assertIn("已关闭", sp.describe(settings(enabled=False)))
        self.assertIn("3 段", sp.describe(settings(max_segments=3)))


class PanelWiringTest(unittest.TestCase):
    """面板上的分段表单和后端设置必须对得上。

    真实踩过的坑：HTML 里写错一个 data-sp 名字，前端照常显示、照常"保存成功"，
    但那个开关永远不会生效 —— 从截图上看不出来，只能靠这里卡住。
    """

    HTML_PATH = Path(__file__).resolve().parent.parent / "pages" / "mind" / "index.html"

    @classmethod
    def setUpClass(cls):
        cls.html = cls.HTML_PATH.read_text(encoding="utf-8")
        cls.defaults = sp.SplitterSettings.from_mapping({}).to_dict()

    def fields(self):
        pattern = re.compile(r'<([a-zA-Z]+)([^>]*\sdata-sp="([a-z_]+)"[^>]*)>')
        out = []
        for match in pattern.finditer(self.html):
            out.append((match.group(1).lower(), match.group(2), match.group(3)))
        return out

    def test_form_has_fields(self):
        self.assertGreaterEqual(len(self.fields()), 15)

    def test_every_field_maps_to_a_setting(self):
        for tag, _attrs, key in self.fields():
            self.assertIn(key, self.defaults, "面板上的 data-sp=%r 后端不认识" % key)

    def test_control_type_matches_setting_type(self):
        for tag, attrs, key in self.fields():
            default = self.defaults[key]
            if isinstance(default, bool):
                self.assertIn('type="checkbox"', attrs, "%s 是开关，应当用 checkbox" % key)
                continue
            if "data-rules" in attrs:
                self.assertEqual(key, "replace_rules")
                self.assertIsInstance(default, list)
                continue
            if "data-list" in attrs:
                self.assertIsInstance(default, list, "%s 不是列表，不该标 data-list" % key)
                continue
            self.assertNotIsInstance(default, list, "%s 是列表，得标 data-list" % key)
            if isinstance(default, (int, float)):
                self.assertEqual(tag, "input")
                self.assertIn('type="number"', attrs, "%s 是数字，应当用 number 输入框" % key)

    def test_no_duplicate_fields(self):
        keys = [key for _tag, _attrs, key in self.fields()]
        self.assertEqual(len(keys), len(set(keys)), "同一个设置出现了两个控件")

    def test_script_only_touches_declared_ids(self):
        """脚本里 $("xxx") 引用的 id 必须真的在页面上。

        写错一个字母的后果是「按钮点了没反应」，从截图上完全看不出来。
        """
        script = self.html.split("<script>")[-1]
        used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', script))
        declared = set(re.findall(r'id="([a-z0-9-]+)"', self.html))
        # 有的元素是脚本自己建的（createElement 之后 .id = "xxx"），这些也算声明过。
        declared |= set(re.findall(r'[.]id\s*=\s*"([^"]+)"', script))
        # innerHTML 字符串里写的 id='xxx' / id="xxx" 同样是脚本自己建的
        declared |= set(re.findall(r"id\s*=\s*['\"]([a-z0-9-]+)['\"]", script))
        self.assertTrue(used, "脚本里没引用任何控件，八成是接线断了")
        self.assertEqual(used - declared, set(), "脚本引用了不存在的 id")

    def test_relationship_card_is_wired(self):
        """认人那张卡片：控件、接口、脚本三边都要对得上。"""
        script = self.html.split("<script>")[-1]
        for name in ("rl-list", "rl-candidates", "rl-label", "rl-add", "rl-addbtn",
                     "rl-save", "rl-reset", "rl-inject", "rl-discretion", "rl-status"):
            self.assertIn('id="' + name + '"', self.html, "缺少 " + name)
        self.assertIn('request("relationship"', script)
        self.assertIn('request("relationship/save"', script)
        self.assertIn('request("relationship/reset"', script)

    def test_config_schema_matches_settings(self):
        """_conf_schema.json 里写的键必须真的被后端读。

        写错一个键的后果很隐蔽：WebUI 上那个开关能改、能存，但插件根本不看它。
        """
        import json

        schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("splitter", schema)
        block = schema["splitter"]
        self.assertIn("hint", block)
        self.assertTrue(block.get("items"), "分段配置块是空的")
        for key, spec in block["items"].items():
            self.assertIn(key, self.defaults, "_conf_schema 里的 splitter.%s 后端不认" % key)
            self.assertIn("description", spec, "splitter.%s 缺少说明" % key)
            self.assertIn(spec.get("type"), ("bool", "int", "float", "string", "list", "object"))
        # 默认值要和代码里的默认值一致：两边不一致时，用户看到的和实际跑的不是一回事
        for key in ("enabled", "llm_only", "scope", "max_segments", "delay_strategy", "smart",
                    "voice_tags", "voice_conflict_policy", "tts_for_segments",
                    "tts_probability_mode", "record_strategy"):
            self.assertEqual(block["items"][key]["default"], self.defaults[key],
                             "splitter.%s 的默认值和代码里对不上" % key)

    def test_tab_and_view_are_wired(self):
        for name in ("tab-split", "view-split"):
            self.assertIn('id="' + name + '"', self.html, "缺少 " + name)
        script = self.html.split("<script>")[-1]
        self.assertIn('$("tab-split").onclick', script)
        self.assertIn('split: "view-split"', script, "setView 里没登记分段视图")


class VoiceCompatTest(unittest.TestCase):
    """语音兼容：标记识别、语音/文字去重、空分段合并。"""

    def test_is_record_and_chain_helpers(self) -> None:
        self.assertTrue(sp.is_record(Record()))
        self.assertFalse(sp.is_record(Image()))
        self.assertTrue(sp.chain_has_voice([Image(), Record()]))
        self.assertFalse(sp.chain_has_voice([Image(), sp.make_plain("嗯")]))
        self.assertTrue(sp.chain_has_text([Record(), sp.make_plain("嗯")]))
        self.assertFalse(sp.chain_has_text([Record(), sp.make_plain("   ")]))

    def test_record_carries_its_text_but_is_not_plain(self) -> None:
        """框架转出来的 Record 会把原文放在 .text 里，绝不能被当成正文。"""
        voice = Record(text="我念的是这句")
        self.assertFalse(sp.is_plain(voice))
        self.assertEqual(sp.plain_text(voice), "")
        self.assertEqual(sp.segment_text([voice]), "")

    def test_record_goes_out_alone_by_default(self) -> None:
        chain = [sp.make_plain("哼。"), Record(), sp.make_plain("你来了。")]
        segs = sp.split_chain(chain, settings())
        self.assertEqual([sp.segment_text(s) for s in segs], ["哼。", "", "你来了。"])
        self.assertTrue(sp.is_record(segs[1][0]))

    def test_record_can_follow_next_segment(self) -> None:
        chain = [sp.make_plain("哼。"), Record(), sp.make_plain("你来了。")]
        segs = sp.split_chain(chain, settings(record_strategy=sp.STRATEGY_NEXT))
        self.assertEqual([sp.segment_text(s) for s in segs], ["哼。", "你来了。"])
        self.assertTrue(sp.is_record(segs[1][0]))

    def test_find_voice_tag_at_the_edges(self) -> None:
        self.assertEqual(sp.find_voice_tag([sp.make_plain("[TTS]你好呀。")], ["[TTS]"]), "[TTS]")
        self.assertEqual(sp.find_voice_tag([sp.make_plain("你好呀。【语音】")], ["【语音】"]), "【语音】")
        # 只看开头/结尾各 24 个字：埋在长句子中间的不算
        middle = "啊" * 30 + "[TTS]" + "啊" * 30
        self.assertEqual(sp.find_voice_tag([sp.make_plain(middle)], ["[TTS]"]), "")
        self.assertEqual(sp.find_voice_tag([sp.make_plain("你好呀。")], sp.DEFAULT_VOICE_TAGS), "")
        self.assertIn("<tts>", sp.DEFAULT_VOICE_TAGS)

    def test_find_voice_tag_skips_non_plain(self) -> None:
        self.assertEqual(sp.find_voice_tag([Record(text="[TTS]别看我")], ["[TTS]"]), "")

    def test_find_voice_tag_accepts_a_bare_string(self) -> None:
        self.assertEqual(sp.find_voice_tag([sp.make_plain("[TTS]喂")], "[TTS]"), "[TTS]")

    def test_voice_policy_normalisation(self) -> None:
        self.assertEqual(sp._voice_policy("text_only"), sp.VOICE_KEEP_TEXT)
        self.assertEqual(sp._voice_policy("胡说"), sp.VOICE_KEEP_VOICE)
        self.assertEqual(sp._voice_policy(None), sp.VOICE_KEEP_VOICE)
        for key, label in sp.VOICE_POLICY_LABELS.items():
            self.assertEqual(sp._voice_policy(label), key, "中文标签没认出来")

    def test_tts_mode_normalisation(self) -> None:
        self.assertEqual(sp._tts_mode("per_segment"), sp.TTS_MODE_PER_SEGMENT)
        self.assertEqual(sp._tts_mode("逐段独立"), sp.TTS_MODE_PER_SEGMENT)
        self.assertEqual(sp._tts_mode("胡说"), sp.TTS_MODE_SINGLE)

    def test_merge_pulls_lone_reply_into_the_next_segment(self) -> None:
        segments = [[Reply()], [Record()]]
        merged = sp.merge_content_less_segments(segments)
        self.assertEqual(len(merged), 1)
        self.assertEqual([type(c).__name__ for c in merged[0]], ["Reply", "Record"])

    def test_merge_drops_a_trailing_blank_segment(self) -> None:
        merged = sp.merge_content_less_segments([[sp.make_plain("哼。")], [sp.make_plain("   ")]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sp.segment_text(merged[0]), "哼。   ")

    def test_merge_drops_a_lone_blank_segment(self) -> None:
        self.assertEqual(sp.merge_content_less_segments([[sp.make_plain("   ")]]), [])
        self.assertEqual(sp.merge_content_less_segments([[Reply()]]), [])

    def test_segment_has_content(self) -> None:
        self.assertFalse(sp.segment_has_content([Reply()]))
        self.assertFalse(sp.segment_has_content([sp.make_plain("  ")]))
        self.assertTrue(sp.segment_has_content([sp.make_plain("嗯")]))
        self.assertTrue(sp.segment_has_content([Record()]))
        self.assertTrue(sp.segment_has_content([Image()]))

    def test_split_chain_keeps_reply_with_voice(self) -> None:
        segs = sp.split_chain([Reply(), Record()], settings())
        self.assertEqual(len(segs), 1)
        self.assertEqual([type(c).__name__ for c in segs[0]], ["Reply", "Record"])

    def test_segment_fingerprint_distinguishes_voice_from_text(self) -> None:
        text_print = sp.segment_fingerprint([sp.make_plain("嗯")])
        voice_print = sp.segment_fingerprint([Record(file="a.mp3", text="嗯")])
        self.assertNotEqual(text_print, voice_print)
        self.assertEqual(text_print, sp.segment_fingerprint([sp.make_plain("嗯")]))
        self.assertEqual(sp.segment_fingerprint([Reply()]), ())

    def test_voice_settings_round_trip(self) -> None:
        conf = sp.SplitterSettings.from_mapping({
            "voice_conflict_policy": "voice_then_text",
            "voice_tags": ["[TTS]"],
            "tts_for_segments": False,
            "tts_probability_mode": "per_segment",
            "record_strategy": "嵌入",
        })
        self.assertEqual(conf.voice_conflict_policy, sp.VOICE_THEN_TEXT)
        self.assertEqual(conf.voice_tags, ["[TTS]"])
        self.assertFalse(conf.tts_for_segments)
        self.assertEqual(conf.tts_probability_mode, sp.TTS_MODE_PER_SEGMENT)
        self.assertEqual(conf.record_strategy, sp.STRATEGY_EMBED)
        again = sp.SplitterSettings.from_mapping(conf.to_dict())
        self.assertEqual(again.to_dict(), conf.to_dict())

    def test_voice_defaults(self) -> None:
        conf = sp.SplitterSettings.from_mapping({})
        self.assertEqual(conf.voice_conflict_policy, sp.VOICE_KEEP_VOICE)
        self.assertTrue(conf.voice_tag_disable_split)
        self.assertEqual(conf.voice_tags, sp.DEFAULT_VOICE_TAGS)
        self.assertTrue(conf.tts_for_segments)

    def test_panel_schema_has_voice_options(self) -> None:
        schema = sp.panel_schema()
        self.assertEqual(set(schema["voice_policy_labels"]), set(schema["voice_policies"]))
        self.assertEqual(set(schema["tts_mode_labels"]), set(schema["tts_modes"]))

    def test_describe_mentions_voice_tag_guard(self) -> None:
        self.assertIn("语音标记", sp.describe(settings(voice_tag_disable_split=True)))

if __name__ == "__main__":
    unittest.main(verbosity=2)


class PunctuationBoundaryTest(unittest.TestCase):
    """回归：用户实际看到的「气泡以逗号结尾」。"""

    def test_bubble_never_ends_with_comma(self) -> None:
        text = "下午好。晚上随便你，反正我会在的。"
        for cap in (2, 3, 4):
            segs = sp.split_chain([sp.make_plain(text)], settings(max_segments=cap))
            got = texts(segs)
            self.assertEqual("".join(got), text, "不能丢字")
            for seg in got:
                self.assertFalse(
                    seg.endswith("，") or seg.endswith("、"),
                    "气泡不能以逗号结尾：" + repr(got),
                )

    def test_sentence_end_wins_over_comma(self) -> None:
        text = "下午好。晚上随便你，反正我会在的。"
        segs = sp.split_chain([sp.make_plain(text)], settings(max_segments=2))
        self.assertEqual(texts(segs), ["下午好。", "晚上随便你，反正我会在的。"])

    def test_space_is_not_a_split_point(self) -> None:
        """空格不能当切点，否则中英夹杂的话会被切成两半。"""
        text = "去 WebUI 里看嘛。我又不是工具箱，哼。"
        for cap in (2, 3):
            segs = sp.split_chain([sp.make_plain(text)], settings(max_segments=cap))
            for seg in texts(segs):
                self.assertFalse(seg.endswith(" "), "不能在空格上断句")
