"""拟人分段：把一条回复拆成几口气，一段一段说出来。

这个模块照着 astrbot_plugin_splitter（作者 nuomicici）的做法重新实现，
把它的核心能力并进本插件：智能分段、保护规则、拟人延迟、组件策略、替换规则。
区别在于：

- 不依赖任何第三方包，也不额外要一份配置文件，和情绪/记忆共用同一份配置与面板；
- 分段引擎是纯函数（只认「类名是 Plain 的组件」和「有 .text 的组件」），
  所以没有 astrbot 也能单测；
- 发送动作留在 main.py：前面的分段用 context.send_message 主动发出去，
  最后一段留在 result.chain 里交回框架，
  这样引用回复、TTS、平台特有能力都还走原来的路。

为什么要「先发前面的、最后一段交回框架」：on_decorating_result 只能改一条链，
没法让它变成两条；主动发送是唯一能在同一个事件里连续吐多条消息的口子。
"""

from __future__ import annotations

import fnmatch
import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .memory.config import cfg_get

#: 代码块围栏（三个反引号）。不直接写出来，免得被各种引号层吃掉。
FENCE = chr(96) * 3
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

#: 默认切分点：句末标点 + 换行
#: （换行也算：模型想分点时，最常用的办法就是换行）
DEFAULT_SPLIT_REGEX = "[。？！?!…；;~～\n]+|[.．]{2,}"
DEFAULT_SPLIT_CHARS = ["。", "？", "！", "?", "!", "；", ";", "…", "\n", "~"]

#: 一段太长时的二次切分点（逗号、顿号、冒号、空白）
SECONDARY_PATTERN = re.compile("[，,、:：;；]+")

#: 停顿：中文省略号、两个以上的点、破折号、波浪号。
#: 它们是「换气」而不是「断句」—— 犹豫本身就该分成两条发，
#: 所以遇到它不看长度门槛（见 _should_split）。
PAUSE_DELIM_RE = re.compile("^(?:…+|[.．]{2,}|—{2,}|~{2,})$")

QUOTE_CHARS = set("“”‘’「」『』《》〈〉\"'")
PAIR_MAP = {
    "(": ")", "（": "）", "[": "]", "【": "】", "{": "}",
    "〔": "〕", "《": "》", "〈": "〉", "「": "」", "『": "』",
    "“": "”", "‘": "’",
}

STRATEGY_ALONE = "单独"
STRATEGY_NEXT = "跟随下段"
STRATEGY_PREV = "跟随上段"
STRATEGY_EMBED = "嵌入"
STRATEGIES = (STRATEGY_ALONE, STRATEGY_NEXT, STRATEGY_PREV, STRATEGY_EMBED)
STRATEGY_ALIASES = {
    "alone": STRATEGY_ALONE, "separate": STRATEGY_ALONE, "solo": STRATEGY_ALONE,
    "单独": STRATEGY_ALONE, "单发": STRATEGY_ALONE,
    "next": STRATEGY_NEXT, "after": STRATEGY_NEXT, "below": STRATEGY_NEXT,
    "跟随下段": STRATEGY_NEXT, "接下文": STRATEGY_NEXT,
    "prev": STRATEGY_PREV, "before": STRATEGY_PREV, "above": STRATEGY_PREV,
    "跟随上段": STRATEGY_PREV, "接上文": STRATEGY_PREV,
    "embed": STRATEGY_EMBED, "inline": STRATEGY_EMBED, "嵌入": STRATEGY_EMBED,
}

DELAY_STRATEGIES = ("linear", "log", "random", "fixed")
DELAY_LABELS = {
    "linear": "线性：基础值 + 下一段字数 x 系数",
    "log": "对数：长段慢一点，封顶更自然",
    "random": "随机：在最小/最大之间抖一下",
    "fixed": "固定：每次都等同样久",
}

#: 回复里出现这些标记 = 有语音插件要整条转语音，这时候绝不能分段
#: [EMO 开头的是 TTS 情绪路由那类插件的标记（[EMO:happy] / 【EMO：开心】）
DEFAULT_VOICE_TAGS = ["[TTS]", "[tts]", "【语音】", "[语音]", "<tts>", "</tts>", "[EMO", "【EMO"]

#: 消息链里同时有语音和文字时怎么办
VOICE_KEEP_VOICE = "voice_only"
VOICE_KEEP_TEXT = "text_only"
VOICE_THEN_TEXT = "voice_then_text"
VOICE_KEEP_BOTH = "both"
VOICE_POLICIES = (VOICE_KEEP_VOICE, VOICE_KEEP_TEXT, VOICE_THEN_TEXT, VOICE_KEEP_BOTH)
VOICE_POLICY_LABELS = {
    VOICE_KEEP_VOICE: "仅发语音：丢掉重复的文字（推荐）",
    VOICE_KEEP_TEXT: "只保留文字：丢掉语音",
    VOICE_THEN_TEXT: "语音 + 整条文字：各发一条，文字不分段",
    VOICE_KEEP_BOTH: "文字照常分段 + 语音（旧行为，会刷屏）",
}

#: 框架内置 TTS 的概率怎么掷
TTS_MODE_SINGLE = "single"
TTS_MODE_PER_SEGMENT = "per_segment"
TTS_MODES = (TTS_MODE_SINGLE, TTS_MODE_PER_SEGMENT)
TTS_MODE_LABELS = {
    TTS_MODE_SINGLE: "整条统一：要么全语音、要么全文字（推荐）",
    TTS_MODE_PER_SEGMENT: "逐段独立：概率不中就文字，中了就语音",
}

#: 找语音标记时只看开头/结尾这么多字符
VOICE_TAG_EDGE = 24

SCOPES = ("both", "private", "group")
SCOPE_LABELS = {"both": "所有会话", "private": "只在私聊", "group": "只在群聊"}

MODE_REGEX = "regex"
MODE_SIMPLE = "simple"
MODES = (MODE_REGEX, MODE_SIMPLE)
MODE_LABELS = {MODE_REGEX: "正则切分（推荐）", MODE_SIMPLE: "字符切分（简单）"}

CJK_RE = re.compile("[\\u4e00-\\u9fff\\u3400-\\u4dbf\\uf900-\\ufaff]")
LATIN_RE = re.compile("[a-zA-Z0-9]")
ASCII_EDGE_RE = re.compile("^[a-zA-Z0-9 \\t.?!,;:\\-']$")
NEUTRAL_DELIM_RE = re.compile("^[ \\t.?!,;:\\-']+$")
TRAILING_SPACE_DELIM_RE = re.compile("^[ \\t]+$")
LEADING_BLANK_RE = re.compile("^(?:[ \\t]*\\r?\\n)+")
TRAILING_BLANK_RE = re.compile("(?:\\r?\\n[ \\t]*)+$")

#: 分段发送时，框架要求的最小非空判断（空格、制表、零宽空格都算空）
BLANK_CHARS = " \t\r\n\u200b\u3000"


# ----------------------------------------------------------------------
# 组件适配层
# ----------------------------------------------------------------------
class LocalPlain:
    """没有 astrbot 时的替身，只为了让纯函数能被单测。"""

    __slots__ = ("text",)

    def __init__(self, text: str = "") -> None:
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Plain(" + repr(self.text) + ")"


_plain_factory: Any = LocalPlain


def set_plain_factory(factory: Any) -> None:
    """插件启动时把真正的 Plain 交进来；测试里不调用就走替身。"""
    global _plain_factory
    if factory is not None:
        _plain_factory = factory


def make_plain(text: str) -> Any:
    return _plain_factory(text)


def comp_kind(comp: Any) -> str:
    return type(comp).__name__.lower()


def is_plain(comp: Any) -> bool:
    if comp is None:
        return False
    if isinstance(comp, LocalPlain):
        return True
    return type(comp).__name__ == "Plain"


def plain_text(comp: Any) -> str:
    if comp is None:
        return ""
    if not is_plain(comp):
        return ""
    text = getattr(comp, "text", "")
    return text if isinstance(text, str) else ""


def set_plain_text(comp: Any, text: str) -> bool:
    try:
        comp.text = text
        return True
    except Exception:  # noqa: BLE001 - 第三方组件可能只读
        return False


def is_reply(comp: Any) -> bool:
    return "reply" in comp_kind(comp)


def is_record(comp: Any) -> bool:
    """语音组件。框架内置 TTS 转出来的 Record 会把原文放在 .text 里，
    所以这里只认类名，绝不能把它的 text 当成正文。"""
    return "record" in comp_kind(comp) or "voice" in comp_kind(comp)


def chain_has_voice(chain: Sequence[Any]) -> bool:
    return any(is_record(comp) for comp in chain)


def chain_has_text(chain: Sequence[Any]) -> bool:
    return any(is_plain(comp) and plain_text(comp).strip(BLANK_CHARS) for comp in chain)


def has_media(segment: Sequence[Any]) -> bool:
    return any(not is_plain(comp) for comp in segment)


def segment_text(segment: Sequence[Any]) -> str:
    return "".join(plain_text(comp) for comp in segment)


def segment_has_content(segment: Sequence[Any]) -> bool:
    """这一段有东西可发吗？

    只有引用、或者只有空白文字的段不算 —— 发出去就是一条空消息。
    """
    for comp in segment:
        if is_plain(comp):
            if plain_text(comp).strip(BLANK_CHARS):
                return True
        elif not is_reply(comp):
            return True
    return False


def is_blank_segment(segment: Sequence[Any]) -> bool:
    return not segment_has_content(segment)


def merge_content_less_segments(segments: Sequence[Sequence[Any]]) -> list[list[Any]]:
    """把「没内容可发」的段并进相邻段。

    典型场景：链是 [引用, 语音]，引用被单独切成一段 ——
    直接发会多出一条只有引用的空消息。
    """
    merged: list[list[Any]] = []
    pending: list[Any] = []
    for segment in segments:
        if segment_has_content(segment):
            merged.append(pending + list(segment))
            pending = []
        else:
            pending.extend(segment)
    if pending:
        if merged:
            merged[-1].extend(pending)
        elif any(not is_plain(comp) and not is_reply(comp) for comp in pending):
            # 前面一段都没有、手里只剩引用和空白：那还有点东西可发（例如引用）
            merged.append(pending)
    return merged


def find_voice_tag(chain: Sequence[Any], tags: Sequence[str]) -> str:
    """回复开头/结尾有没有语音标记？返回命中的那个。

    [TTS] / 【语音】 这类标记意味着有语音插件要拿整条回复去合成语音；
    这时候分段会把标记当正文发出去、语音插件又只拿到最后一段，
    最后变成「语音念一句 + 文字刷一片」。
    """
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return ""
    for comp in chain:
        if not is_plain(comp):
            continue
        text = plain_text(comp).strip()
        if not text:
            continue
        head = text[:VOICE_TAG_EDGE]
        tail = text[-VOICE_TAG_EDGE:]
        for tag in tags:
            tag = str(tag or "")
            if tag and (tag in head or tag in tail):
                return tag
    return ""


def segment_fingerprint(segment: Sequence[Any]) -> tuple:
    """给一段做个指纹，用来防同一条回复里重复发同样的内容。"""
    parts: list[tuple] = []
    for comp in segment:
        if is_plain(comp):
            parts.append(("text", plain_text(comp).strip()))
        elif is_record(comp):
            parts.append(("voice", str(getattr(comp, "file", "") or getattr(comp, "url", "") or "")))
        elif is_reply(comp):
            continue
        else:
            parts.append((comp_kind(comp), str(getattr(comp, "file", "") or "")))
    return tuple(parts)


def merge_adjacent_plain(segment: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for comp in segment:
        if out and is_plain(comp) and is_plain(out[-1]):
            out[-1] = make_plain(plain_text(out[-1]) + plain_text(comp))
        else:
            out.append(comp)
    return out


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def _b(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def _f(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if result != result:  # NaN
        result = default
    return max(low, min(high, result))


def _i(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split("[,\\n]", value)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    out.append(text)
                elif item:
                    # 纯空白项不能丢：split_chars 里的换行/制表符就是这么写的
                    out.append(item)
                continue
            text = _s(item)
            if text:
                out.append(text)
        return out
    return []


def normalise_strategy(value: Any, default: str = STRATEGY_NEXT) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text in STRATEGIES:
            return text
        alias = STRATEGY_ALIASES.get(text.lower())
        if alias:
            return alias
    return default


def parse_replace_rules(raw: Any) -> list[tuple[str, str]]:
    """把配置里的替换规则统一成 (find, replace)。

    支持两种写法：字典 {"find": ..., "replace": ...}，
    或者一行字符串 "find=>replace"（也认 ==> 和 →）。
    """
    rules: list[tuple[str, str]] = []
    if not raw:
        return rules
    if isinstance(raw, str):
        raw = [line for line in raw.split("\n")]
    if not isinstance(raw, (list, tuple)):
        return rules
    for item in raw:
        if isinstance(item, Mapping):
            find = unescape_replace_str(item.get("find", ""))
            replace = unescape_replace_str(item.get("replace", ""))
        elif isinstance(item, str):
            text = item
            find = text
            replace = ""
            for token in ("==>", "=>", "→"):
                if token in text:
                    head, _, tail = text.partition(token)
                    find, replace = head, tail
                    break
            find = unescape_replace_str(find)
            replace = unescape_replace_str(replace)
        else:
            continue
        if find:
            rules.append((find, replace))
    return rules


def unescape_replace_str(value: Any) -> str:
    """把配置里写的 \\n、\\t 还原成真的换行/制表符。"""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    return value.replace("\\n", "\n").replace("\\t", "\t")


@dataclass
class SplitterSettings:
    """分段行为的所有旋钮。"""

    enabled: bool = True
    #: 只在 LLM 的回复上分段（指令回执、插件自己的报错就不切了）
    llm_only: bool = True
    #: both / private / group
    scope: str = "both"
    #: 命中黑名单或不在白名单里就不分段（支持 * 通配）
    blacklist: list[str] = field(default_factory=list)
    whitelist: list[str] = field(default_factory=list)
    #: 回复短于这个字数就不折腾（0 = 不限）
    min_length: int = 0
    #: 回复长于这个字数就不分段（0 = 不限，防止把长文刷成一片）
    max_length: int = 0

    #: regex / simple
    split_mode: str = MODE_REGEX
    split_regex: str = DEFAULT_SPLIT_REGEX
    split_chars: list[str] = field(default_factory=lambda: list(DEFAULT_SPLIT_CHARS))

    #: 智能模式：保护代码块/表格/引号，并尽量把长度匀开
    smart: bool = True
    balanced_mode: bool = True
    balanced_ratio_min: float = 0.4
    balanced_ratio_max: float = 0.9
    #: 一段最多几个字（均分模式的目标下限）
    min_segment_length: int = 8
    #: 一条回复最多几个气泡
    max_segments: int = 3

    #: 延迟
    delay_strategy: str = "linear"
    fixed_delay: float = 1.2
    linear_base: float = 0.4
    linear_factor: float = 0.05
    log_base: float = 0.4
    log_factor: float = 0.5
    random_min: float = 0.6
    random_max: float = 1.8
    max_delay: float = 4.0

    #: 给第一段挂上引用回复
    enable_reply: bool = True
    #: 段首段尾的空行抹掉
    trim_edge_blank_lines: bool = True

    #: 分段前的替换 / 清理
    replace_rules: list[Any] = field(default_factory=list)
    clean_before_items: list[str] = field(default_factory=list)
    clean_before_regex: str = ""
    clean_after_items: list[str] = field(default_factory=list)
    clean_after_regex: str = ""

    #: 这些词前面不切（例如「宝宝」「主人」）
    no_split_around: list[str] = field(default_factory=list)

    #: 消息链里同时有语音和文字时怎么办（见 VOICE_POLICY_LABELS）
    voice_conflict_policy: str = VOICE_KEEP_VOICE
    #: 回复里出现语音标记（[TTS] 等）就不分段，整条交给语音插件
    voice_tag_disable_split: bool = True
    voice_tags: list[str] = field(default_factory=lambda: list(DEFAULT_VOICE_TAGS))
    #: 分段也走框架内置 TTS（自己把文字转成语音再发，避免只有尾段变语音）
    tts_for_segments: bool = True
    #: 框架 TTS 的概率决策方式
    tts_probability_mode: str = TTS_MODE_SINGLE

    #: 媒体组件怎么跟
    image_strategy: str = STRATEGY_ALONE
    #: 语音组件怎么跟（默认单独成条）
    record_strategy: str = STRATEGY_ALONE
    at_strategy: str = STRATEGY_NEXT
    face_strategy: str = STRATEGY_EMBED
    other_strategy: str = STRATEGY_NEXT

    @staticmethod
    def from_mapping(raw: Mapping[str, Any] | None) -> "SplitterSettings":
        data: Mapping[str, Any] = raw or {}
        scope = _s(data.get("scope"), "both").lower()
        if scope in ("all", "全部", "所有"):
            scope = "both"
        if scope not in SCOPES:
            scope = "both"
        mode = _s(data.get("split_mode"), MODE_REGEX).lower()
        if mode not in MODES:
            mode = MODE_REGEX
        chars = _str_list(data.get("split_chars"))
        if not chars:
            chars = list(DEFAULT_SPLIT_CHARS)
        return SplitterSettings(
            enabled=_b(data.get("enabled"), True),
            llm_only=_b(data.get("llm_only"), True),
            scope=scope,
            blacklist=_str_list(data.get("blacklist")),
            whitelist=_str_list(data.get("whitelist")),
            min_length=_i(data.get("min_length"), 0, 0, 100000),
            max_length=_i(data.get("max_length"), 0, 0, 1000000),
            split_mode=mode,
            split_regex=_s(data.get("split_regex"), DEFAULT_SPLIT_REGEX),
            split_chars=chars,
            smart=_b(data.get("smart"), True),
            balanced_mode=_b(data.get("balanced_mode"), True),
            balanced_ratio_min=_f(data.get("balanced_ratio_min"), 0.4, 0.05, 1.0),
            balanced_ratio_max=_f(data.get("balanced_ratio_max"), 0.9, 0.05, 1.0),
            min_segment_length=_i(data.get("min_segment_length"), 8, 0, 500),
            max_segments=_i(data.get("max_segments"), 3, 0, 30),
            delay_strategy=_delay_strategy(data.get("delay_strategy")),
            fixed_delay=_f(data.get("fixed_delay"), 1.2, 0.0, 60.0),
            linear_base=_f(data.get("linear_base"), 0.4, 0.0, 60.0),
            linear_factor=_f(data.get("linear_factor"), 0.05, 0.0, 2.0),
            log_base=_f(data.get("log_base"), 0.4, 0.0, 60.0),
            log_factor=_f(data.get("log_factor"), 0.5, 0.0, 10.0),
            random_min=_f(data.get("random_min"), 0.6, 0.0, 60.0),
            random_max=_f(data.get("random_max"), 1.8, 0.0, 120.0),
            max_delay=_f(data.get("max_delay"), 4.0, 0.0, 120.0),
            enable_reply=_b(data.get("enable_reply"), True),
            trim_edge_blank_lines=_b(data.get("trim_edge_blank_lines"), True),
            replace_rules=list(data.get("replace_rules") or []),
            clean_before_items=_str_list(data.get("clean_before_items")),
            clean_before_regex=_s(data.get("clean_before_regex"), ""),
            clean_after_items=_str_list(data.get("clean_after_items")),
            clean_after_regex=_s(data.get("clean_after_regex"), ""),
            no_split_around=_str_list(data.get("no_split_around")),
            voice_conflict_policy=_voice_policy(data.get("voice_conflict_policy")),
            voice_tag_disable_split=_b(data.get("voice_tag_disable_split"), True),
            voice_tags=_str_list(data.get("voice_tags")) or list(DEFAULT_VOICE_TAGS),
            tts_for_segments=_b(data.get("tts_for_segments"), True),
            tts_probability_mode=_tts_mode(data.get("tts_probability_mode")),
            image_strategy=normalise_strategy(data.get("image_strategy"), STRATEGY_ALONE),
            record_strategy=normalise_strategy(data.get("record_strategy"), STRATEGY_ALONE),
            at_strategy=normalise_strategy(data.get("at_strategy"), STRATEGY_NEXT),
            face_strategy=normalise_strategy(data.get("face_strategy"), STRATEGY_EMBED),
            other_strategy=normalise_strategy(data.get("other_strategy"), STRATEGY_NEXT),
        )

    @staticmethod
    def from_config(
        config: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None = None
    ) -> "SplitterSettings":
        raw = dict(cfg_get(config, "splitter", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        return SplitterSettings.from_mapping(raw)

    def strategy_for(self, kind: str) -> str:
        key = (kind or "").lower()
        if "record" in key or "voice" in key or "audio" in key:
            return self.record_strategy
        if "image" in key or "video" in key or "file" in key:
            return self.image_strategy
        if key == "at" or "atall" in key:
            return self.at_strategy
        if "face" in key or "emoji" in key:
            return self.face_strategy
        return self.other_strategy

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "llm_only": bool(self.llm_only),
            "scope": self.scope,
            "blacklist": list(self.blacklist),
            "whitelist": list(self.whitelist),
            "min_length": int(self.min_length),
            "max_length": int(self.max_length),
            "split_mode": self.split_mode,
            "split_regex": self.split_regex,
            "split_chars": list(self.split_chars),
            "smart": bool(self.smart),
            "balanced_mode": bool(self.balanced_mode),
            "balanced_ratio_min": float(self.balanced_ratio_min),
            "balanced_ratio_max": float(self.balanced_ratio_max),
            "min_segment_length": int(self.min_segment_length),
            "max_segments": int(self.max_segments),
            "delay_strategy": self.delay_strategy,
            "fixed_delay": float(self.fixed_delay),
            "linear_base": float(self.linear_base),
            "linear_factor": float(self.linear_factor),
            "log_base": float(self.log_base),
            "log_factor": float(self.log_factor),
            "random_min": float(self.random_min),
            "random_max": float(self.random_max),
            "max_delay": float(self.max_delay),
            "enable_reply": bool(self.enable_reply),
            "trim_edge_blank_lines": bool(self.trim_edge_blank_lines),
            "replace_rules": [dict(r) if isinstance(r, Mapping) else r for r in self.replace_rules],
            "clean_before_items": list(self.clean_before_items),
            "clean_before_regex": self.clean_before_regex,
            "clean_after_items": list(self.clean_after_items),
            "clean_after_regex": self.clean_after_regex,
            "no_split_around": list(self.no_split_around),
            "voice_conflict_policy": self.voice_conflict_policy,
            "voice_tag_disable_split": bool(self.voice_tag_disable_split),
            "voice_tags": list(self.voice_tags),
            "tts_for_segments": bool(self.tts_for_segments),
            "tts_probability_mode": self.tts_probability_mode,
            "image_strategy": self.image_strategy,
            "record_strategy": self.record_strategy,
            "at_strategy": self.at_strategy,
            "face_strategy": self.face_strategy,
            "other_strategy": self.other_strategy,
        }


def _delay_strategy(value: Any) -> str:
    text = _s(value, "linear").lower()
    if text not in DELAY_STRATEGIES:
        return "linear"
    return text


def _voice_policy(value: Any) -> str:
    text = _s(value, VOICE_KEEP_VOICE)
    if text in VOICE_POLICIES:
        return text
    for key, label in VOICE_POLICY_LABELS.items():
        if text == label:
            return key
    return VOICE_KEEP_VOICE


def _tts_mode(value: Any) -> str:
    text = _s(value, TTS_MODE_SINGLE).lower()
    if text in TTS_MODES:
        return text
    if text in ("整条统一", "single", "once"):
        return TTS_MODE_SINGLE
    if text in ("逐段独立", "per_segment", "each"):
        return TTS_MODE_PER_SEGMENT
    return TTS_MODE_SINGLE


def panel_schema() -> dict[str, Any]:
    """面板渲染表单需要的选项表，免得前端硬编码中文。"""
    return {
        "scopes": list(SCOPES),
        "scope_labels": dict(SCOPE_LABELS),
        "strategies": list(STRATEGIES),
        "delay_strategies": list(DELAY_STRATEGIES),
        "delay_labels": dict(DELAY_LABELS),
        "modes": list(MODES),
        "mode_labels": dict(MODE_LABELS),
        "voice_policies": list(VOICE_POLICIES),
        "voice_policy_labels": dict(VOICE_POLICY_LABELS),
        "tts_modes": list(TTS_MODES),
        "tts_mode_labels": dict(TTS_MODE_LABELS),
    }


# ----------------------------------------------------------------------
# 切分引擎
# ----------------------------------------------------------------------
def compile_pattern(settings: SplitterSettings) -> str:
    if settings.split_mode == MODE_SIMPLE:
        pieces: list[str] = []
        for raw in settings.split_chars or []:
            text = unescape_replace_str(raw)
            if not text:
                continue
            pieces.append(re.escape(text))
        if not pieces:
            return "[\\n]+"
        pieces.sort(key=len, reverse=True)
        return "(?:" + "|".join(pieces) + ")+"
    pattern = _s(settings.split_regex, "") or DEFAULT_SPLIT_REGEX
    try:
        re.compile(pattern)
    except re.error:
        pattern = DEFAULT_SPLIT_REGEX
    return pattern


def _compiled(settings: SplitterSettings) -> re.Pattern[str]:
    return re.compile(compile_pattern(settings))


def _plain_weight(chain: Iterable[Any]) -> int:
    total = 0
    for comp in chain:
        if is_plain(comp):
            total += len(plain_text(comp).replace(" ", ""))
    return total


def ideal_length(chain: Sequence[Any], settings: SplitterSettings) -> int:
    """均分模式的目标段长：把字数和单独成段的媒体一起考虑。"""
    if not settings.balanced_mode:
        return 0
    limit = int(settings.max_segments or 0)
    if limit <= 0:
        return 0
    solo = 0
    for comp in chain:
        if is_plain(comp) or is_reply(comp):
            continue
        if settings.strategy_for(comp_kind(comp)) == STRATEGY_ALONE:
            solo += 1
    target = max(1, limit - solo)
    weight = _plain_weight(chain)
    if weight <= 0:
        return 0
    return max(int(math.ceil(weight / target)), int(settings.min_segment_length))


def _scan_past_spaces(text: str, index: int) -> int:
    n = len(text)
    while index < n and text[index] in " \t":
        index += 1
    return index


def _next_starts_with_protected(parts: Sequence[str], index: int, protected: Sequence[str]) -> bool:
    after = ""
    for k in range(index + 1, len(parts)):
        if parts[k]:
            after = parts[k]
            break
    stripped = after.lstrip(" \t")
    if not stripped:
        return False
    return any(word and stripped.startswith(word) for word in protected)


def _protected_span(text: str, index: int) -> int:
    """从 index 开始是不是一整块要原样保留的东西？是就返回它的长度。

    代码块、think 块、Markdown 表格都要整块跳过，不然会被切得七零八落。
    """
    if index > 0 and text[index - 1] != "\n":
        return 0
    n = len(text)
    if text.startswith(FENCE, index):
        end = text.find(FENCE, index + len(FENCE))
        return n - index if end == -1 else end + len(FENCE) - index
    if text.startswith(THINK_OPEN, index):
        end = text.find(THINK_CLOSE, index + len(THINK_OPEN))
        return n - index if end == -1 else end + len(THINK_CLOSE) - index
    if text[index] == "|":
        end = index
        pos = index
        while pos < n:
            line_end = text.find("\n", pos)
            if line_end == -1:
                line_end = n
            line = text[pos:line_end].strip()
            if line.startswith("|") or (line and all(ch in "-| :" for ch in line)):
                end = line_end + 1 if line_end < n else n
                pos = end
            else:
                break
        if end > index + 1:
            return end - index
    return 0


def _delim_weight(delim: str) -> int:
    """这个标点自己占多少重量（换行/空格不算）。"""
    return sum(1 for ch in delim if not ch.isspace())


def _should_split(
    text: str,
    index: int,
    delim: str,
    depth: int,
    weight: int,
    ideal: int,
    settings: SplitterSettings,
) -> bool:
    if depth > 0:
        return False  # 引号/括号没闭合，先别切
    # 停顿是天然的换气点：她停一下、再接着说，本来就该分两条发。
    # 这里不受长度门槛限制 —— 否则「你...你这什么造型啊」这种十来字的短句
    # 永远切不开，读起来是一口气说完，犹豫的感觉全没了。
    if _delim_weight(delim) >= 1 and PAUSE_DELIM_RE.match(delim):
        # 开头就停顿（「...那就再陪你一小会儿」）也要单独成一条：
        # 那一拍犹豫本身就是内容，本来就该自己占一个气泡。
        # 以前要求「两边都得有字」，正好把这一种挡在门外。
        if (index == 0 or weight >= 1) and index + len(delim) < len(text):
            return True
    # 均分模式：这一段还没攒够就先别切。
    # 标点自己的长度也要算进去 —— 否则「下午好。」这种四个字的短句会被判成「不够长」，
    # 切点被迫后移到下一个逗号上，气泡就变成以「，」结尾。
    if ideal > 0 and weight + _delim_weight(delim) < ideal * settings.balanced_ratio_min:
        return False
    n = len(text)
    if "\n" not in delim and NEUTRAL_DELIM_RE.match(delim):
        prev_char = text[index - 1] if index > 0 else ""
        next_index = index + len(delim)
        next_char = text[next_index] if next_index < n else ""
        if prev_char and next_char and ASCII_EDGE_RE.match(prev_char) and ASCII_EDGE_RE.match(next_char):
            return False  # 英文句子里的 ". " 和 ", " 不切
        if prev_char and next_char:
            prev_cjk = bool(CJK_RE.match(prev_char))
            prev_latin = bool(LATIN_RE.match(prev_char))
            next_cjk = bool(CJK_RE.match(next_char))
            next_latin = bool(LATIN_RE.match(next_char))
            if (prev_cjk and next_latin) or (prev_latin and next_cjk):
                return False  # 「中文 English」中间的空格不切
    protected = settings.no_split_around
    if protected:
        scan = _scan_past_spaces(text, index + len(delim))
        for word in protected:
            if word and text.startswith(word, scan):
                return False
    return True


def _has_body(chunk: str, compiled: re.Pattern[str]) -> bool:
    """这一段除了标点和空白，还有别的东西吗？"""
    return bool(compiled.sub("", chunk).strip(BLANK_CHARS))


def _flush(
    chunk: str,
    compiled: re.Pattern[str],
    segments: list[list[Any]],
    buffer: list[Any],
) -> bool:
    """攒够一段就交出去。只有标点/空白的不算一段，不能吐出空气泡。"""
    if not _has_body(chunk, compiled):
        return False
    buffer.append(make_plain(chunk))
    segments.append(buffer[:])
    buffer.clear()
    return True


def _process_text_simple(
    text: str,
    compiled: re.Pattern[str],
    settings: SplitterSettings,
    segments: list[list[Any]],
    buffer: list[Any],
) -> None:
    try:
        splitter = re.compile("(" + compiled.pattern + ")")
    except re.error:
        splitter = re.compile("(" + DEFAULT_SPLIT_REGEX + ")")
    parts = splitter.split(text)
    chunk = ""
    for index, part in enumerate(parts):
        if not part:
            continue
        if compiled.fullmatch(part):
            if _next_starts_with_protected(parts, index, settings.no_split_around):
                chunk += part
                continue
            chunk += part
            if _flush(chunk, compiled, segments, buffer):
                chunk = ""
            continue
        chunk += part
    if chunk:
        buffer.append(make_plain(chunk))


def _process_text_smart(
    text: str,
    compiled: re.Pattern[str],
    settings: SplitterSettings,
    ideal: int,
    segments: list[list[Any]],
    buffer: list[Any],
    weight: int,
) -> int:
    stack: list[str] = []
    chunk = ""
    index = 0
    last_break = 0  # 上一个「本可以切但没切」的位置
    n = len(text)
    while index < n:
        span = _protected_span(text, index)
        if span > 0:
            piece = text[index : index + span]
            chunk += piece
            weight += sum(1 for ch in piece if not ch.isspace())
            index += span
            continue
        match = compiled.match(text, pos=index)
        if match:
            delim = match.group()
            if _should_split(text, index, delim, len(stack), weight, ideal, settings):
                chunk += delim
                if _flush(chunk, compiled, segments, buffer):
                    chunk = ""
                    weight = 0
                last_break = 0
            else:
                chunk += delim
                weight += _delim_weight(delim)
                last_break = len(chunk)  # 这里本来可以切，先记下来
            index += len(delim)
            continue
        if ideal > 0 and weight >= ideal * settings.balanced_ratio_max and not stack:
            if SECONDARY_PATTERN.match(text, pos=index):
                # 攒够长度了，但脚下是逗号/顿号这种「话还没说完」的标点。
                # 直接在这儿切，气泡就会以「，」结尾（用户看到的就是这个）。
                # 改成退回这一口气里最后一个句末标点处切；找不到就不切，
                # 让这一口气接着说下去——宁可长一点，也不能把句子拦腰截断。
                if last_break > 0:
                    head, tail = chunk[:last_break], chunk[last_break:]
                    if _has_body(head, compiled):
                        buffer.append(make_plain(head))
                        segments.append(buffer[:])
                        buffer.clear()
                        chunk = tail
                        weight = sum(1 for ch in tail if not ch.isspace())
                        last_break = 0
                        continue
        char = text[index]
        if char in QUOTE_CHARS:
            if stack and stack[-1] == char:
                stack.pop()
            elif not stack:
                stack.append(char)
        elif not stack and char in PAIR_MAP:
            stack.append(char)
        elif stack and char == PAIR_MAP.get(stack[-1]):
            stack.pop()
        chunk += char
        index += 1
        if not char.isspace():
            weight += 1
    if chunk:
        buffer.append(make_plain(chunk))
    return weight


def cap_segments(segments: list[list[Any]], max_segments: int) -> list[list[Any]]:
    """超过上限的段全部并进最后一段，防止一条回复刷成一片。"""
    limit = int(max_segments or 0)
    if limit <= 0 or len(segments) <= limit:
        return segments
    keep = [list(seg) for seg in segments[: limit - 1]]
    tail: list[Any] = []
    for seg in segments[limit - 1 :]:
        tail.extend(seg)
    keep.append(merge_adjacent_plain(tail))
    return keep


def split_chain(chain: Sequence[Any], settings: SplitterSettings) -> list[list[Any]]:
    """把一条消息链拆成若干段（每段还是一条链）。"""
    segments: list[list[Any]] = []
    buffer: list[Any] = []
    compiled = _compiled(settings)
    ideal = ideal_length(chain, settings)
    weight = 0

    for comp in chain or []:
        if is_plain(comp):
            text = plain_text(comp)
            if not text:
                continue
            if settings.smart:
                weight = _process_text_smart(text, compiled, settings, ideal, segments, buffer, weight)
            else:
                _process_text_simple(text, compiled, settings, segments, buffer)
                weight = 0
            continue
        if is_reply(comp):
            if settings.enable_reply:
                buffer.append(comp)
            continue
        strategy = settings.strategy_for(comp_kind(comp))
        if strategy == STRATEGY_ALONE:
            if buffer:
                segments.append(buffer[:])
                buffer.clear()
            segments.append([comp])
            weight = 0
        elif strategy == STRATEGY_PREV:
            if buffer:
                buffer.append(comp)
                segments.append(buffer[:])
                buffer.clear()
                weight = 0
            elif segments:
                segments[-1].append(comp)
            else:
                segments.append([comp])
        elif strategy == STRATEGY_EMBED:
            buffer.append(comp)
        else:  # 跟随下段
            if buffer:
                segments.append(buffer[:])
                buffer.clear()
                weight = 0
            buffer.append(comp)

    if buffer:
        segments.append(buffer)

    cleaned: list[list[Any]] = []
    for seg in segments:
        if not seg:
            continue
        if settings.trim_edge_blank_lines:
            trim_edge_blank_lines(seg)
        cleaned.append(merge_adjacent_plain(seg))
    # 只有引用/空白的段并进邻居，别让它单独成一条空消息
    cleaned = merge_content_less_segments(cleaned)
    return cap_segments(cleaned, settings.max_segments)


def trim_edge_blank_lines(segment: Sequence[Any]) -> Sequence[Any]:
    plains = [comp for comp in segment if is_plain(comp)]
    if not plains:
        return segment
    head = plain_text(plains[0])
    if head:
        set_plain_text(plains[0], LEADING_BLANK_RE.sub("", head))
    tail = plain_text(plains[-1])
    if tail:
        set_plain_text(plains[-1], TRAILING_BLANK_RE.sub("", tail))
    return segment


def calculate_delay(settings: SplitterSettings, text: str = "") -> float:
    """下一段之前等多久（秒）。"""
    length = len(text or "")
    strategy = (settings.delay_strategy or "linear").lower()
    if strategy == "random":
        low = min(settings.random_min, settings.random_max)
        high = max(settings.random_min, settings.random_max)
        delay = random.uniform(low, high)
    elif strategy == "log":
        delay = settings.log_base + settings.log_factor * math.log(length + 1)
    elif strategy == "fixed":
        delay = settings.fixed_delay
    else:
        delay = settings.linear_base + length * settings.linear_factor
    return max(0.0, min(float(delay), float(settings.max_delay)))


def apply_replace_rules(text: str, rules: Sequence[tuple[str, str]]) -> str:
    if not text or not rules:
        return text
    for find, replace in rules:
        if find:
            text = text.replace(find, replace)
    return text


def clean_text(text: str, settings: SplitterSettings, stage: str) -> str:
    """stage = before / after：分段前后的批量清理。"""
    if not text:
        return text
    if stage == "before":
        items = settings.clean_before_items
        pattern = settings.clean_before_regex
    else:
        items = settings.clean_after_items
        pattern = settings.clean_after_regex
    for item in items or []:
        if item:
            text = text.replace(item, "")
    if pattern:
        try:
            text = re.sub(pattern, "", text, flags=re.DOTALL)
        except re.error:
            pass
    return text


def scope_allows(settings: SplitterSettings, is_group: bool) -> bool:
    scope = (settings.scope or "both").lower()
    if scope == "group":
        return bool(is_group)
    if scope == "private":
        return not is_group
    return True


def session_allowed(settings: SplitterSettings, session_id: str) -> bool:
    key = str(session_id or "")
    for pattern in settings.blacklist or []:
        if pattern and fnmatch.fnmatchcase(key, pattern):
            return False
    white = [p for p in (settings.whitelist or []) if p]
    if white:
        return any(fnmatch.fnmatchcase(key, p) for p in white)
    return True


def should_split(
    settings: SplitterSettings,
    session_id: str = "",
    is_group: bool = True,
    text: str = "",
    is_llm: bool = True,
) -> bool:
    if not settings.enabled:
        return False
    if settings.llm_only and not is_llm:
        return False
    if not scope_allows(settings, is_group):
        return False
    if not session_allowed(settings, session_id):
        return False
    length = len(text or "")
    if settings.min_length > 0 and length < settings.min_length:
        return False
    if settings.max_length > 0 and length > settings.max_length:
        return False
    return True


# ----------------------------------------------------------------------
# 给面板用：干跑一遍，只报结果不发消息
# ----------------------------------------------------------------------
def preview(text: str, settings: SplitterSettings) -> dict[str, Any]:
    raw = str(text or "")
    chain = [make_plain(raw)] if raw else []
    if chain:
        chain = [make_plain(clean_text(raw, settings, "before"))]
        rules = parse_replace_rules(settings.replace_rules)
        if rules:
            set_plain_text(chain[0], apply_replace_rules(plain_text(chain[0]), rules))
    segments = split_chain(chain, settings)
    out: list[dict[str, Any]] = []
    total = len(segments)
    for index, seg in enumerate(segments):
        after = seg
        if settings.clean_after_items or settings.clean_after_regex:
            after = [comp for comp in seg]
            for comp in after:
                if is_plain(comp):
                    set_plain_text(comp, clean_text(plain_text(comp), settings, "after"))
        text_value = segment_text(after)
        next_text = ""
        if index + 1 < total:
            next_text = segment_text(segments[index + 1])
        delay = calculate_delay(settings, next_text) if index + 1 < total else 0.0
        out.append(
            {
                "index": index + 1,
                "text": text_value,
                "chars": len(text_value),
                "delay_before_next": round(delay, 2),
                "kinds": [type(comp).__name__ for comp in after],
                "has_media": has_media(after),
            }
        )
    return {
        "ok": True,
        "count": total,
        "limit": int(settings.max_segments),
        "over_limit": bool(settings.max_segments and total > settings.max_segments),
        "total_chars": len(raw),
        "pattern": compile_pattern(settings),
        "rule_count": len(parse_replace_rules(settings.replace_rules)),
        "segments": out,
    }


def describe(settings: SplitterSettings) -> str:
    """一句话描述当前设置，面板顶部直接显示。"""
    if not settings.enabled:
        return "分段：已关闭"
    bits = [
        "最多 %d 段" % int(settings.max_segments) if settings.max_segments else "不限制段数",
        "延迟 " + DELAY_LABELS.get(settings.delay_strategy, settings.delay_strategy),
        SCOPE_LABELS.get(settings.scope, settings.scope),
    ]
    if settings.smart:
        bits.append("智能保护")
    if settings.voice_tag_disable_split:
        bits.append("带语音标记不分段")
    return "分段：" + "，".join(bits)


__all__ = [
    "BLANK_CHARS",
    "DEFAULT_VOICE_TAGS",
    "TTS_MODE_LABELS",
    "TTS_MODES",
    "TTS_MODE_PER_SEGMENT",
    "TTS_MODE_SINGLE",
    "VOICE_KEEP_BOTH",
    "VOICE_KEEP_TEXT",
    "VOICE_KEEP_VOICE",
    "VOICE_POLICIES",
    "VOICE_POLICY_LABELS",
    "VOICE_THEN_TEXT",
    "chain_has_text",
    "chain_has_voice",
    "find_voice_tag",
    "is_record",
    "merge_content_less_segments",
    "segment_fingerprint",
    "segment_has_content",
    "DELAY_LABELS",
    "DELAY_STRATEGIES",
    "DEFAULT_SPLIT_CHARS",
    "DEFAULT_SPLIT_REGEX",
    "FENCE",
    "LocalPlain",
    "MODE_LABELS",
    "MODE_REGEX",
    "MODE_SIMPLE",
    "MODES",
    "SCOPE_LABELS",
    "SCOPES",
    "STRATEGIES",
    "STRATEGY_ALONE",
    "STRATEGY_EMBED",
    "STRATEGY_NEXT",
    "STRATEGY_PREV",
    "SplitterSettings",
    "apply_replace_rules",
    "calculate_delay",
    "cap_segments",
    "clean_text",
    "comp_kind",
    "compile_pattern",
    "describe",
    "has_media",
    "ideal_length",
    "is_blank_segment",
    "is_plain",
    "is_reply",
    "make_plain",
    "merge_adjacent_plain",
    "normalise_strategy",
    "panel_schema",
    "parse_replace_rules",
    "plain_text",
    "preview",
    "scope_allows",
    "segment_text",
    "session_allowed",
    "set_plain_factory",
    "set_plain_text",
    "should_split",
    "split_chain",
    "trim_edge_blank_lines",
    "unescape_replace_str",
]
