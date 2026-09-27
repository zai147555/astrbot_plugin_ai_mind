"""消息防抖：把用户连发的几条消息攒成一轮再交给 LLM。

参考 astrbot_plugin_debounce（作者 advent259141）的做法，但把判断核心换掉了：

- 原版下载一个微调过的 BERT（onnxruntime + transformers + modelscope）来判断
  「这句话说完了没」。本插件坚持零第三方依赖，所以这里换成**本地规则**：
  看结尾标点、看是不是停在连接词上、看长度够不够。规则不如模型准，
  但不用下模型、不用推理、当场出结果，而且**判不准时默认放行**
  （宁可早回一句，也不能把用户的话吞了）。
- 原版用「stop_event + 伪造事件回灌 EventBus」做超时补发。这里改成
  **在 on_llm_request 里睡一个短窗口**：AstrBot 的 on_waiting_llm_request
  在会话锁之前触发，所以睡的时候用户接着说的一句已经被记下来了，
  醒来直接合并即可 —— 不伪造事件，也没有缓冲区清空的竞态。

判断分三档：

| 情况 | 等几个窗口 |
| --- | --- |
| 结尾是句末标点 / 语气词，或者已经够长 | 0，立刻发 |
| 短句、没有标点（拿不准） | 1 |
| 结尾是逗号，或者停在「然后」「因为」这种连接词上 | 2 |

连续几个窗口都没有新消息进来就放弃等待，直接发，所以最坏情况的延迟是
「窗口数 x grace_seconds」，由 max_wait_seconds 兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 结尾是这些 = 明显还没说完
CONTINUE_PUNCT = "，,、：:；;"

#: 结尾是这些 = 一句话说完了
END_PUNCT = "。！？!?～~"

#: 结尾是这些语气词 = 说完了
END_PARTICLES = "吗吧啊呀哦嘛啦哈咯嘞哟喔噢诶哎嗯"

#: 结尾停在这些词上 = 后面明显还有话。
#: 只收多字词和介词 —— 「的」「了」「就」这种单字太常见，收进来会误判。
CONTINUE_TAILS = (
    "然后", "而且", "所以", "但是", "可是", "不过", "因为", "如果", "要是",
    "虽然", "还有", "另外", "以及", "或者", "要么", "就是", "那个", "这个",
    "其实", "感觉", "觉得", "应该", "可能", "顺便", "反正", "不然", "否则",
    "于是", "接着", "后来", "跟", "和", "对", "给", "让", "把", "被", "向", "从",
)

SCOPE_BOTH = "both"
SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"
SCOPES = (SCOPE_BOTH, SCOPE_PRIVATE, SCOPE_GROUP)
SCOPE_LABELS = {
    SCOPE_BOTH: "私聊和群聊都防抖",
    SCOPE_PRIVATE: "只在私聊防抖",
    SCOPE_GROUP: "只在群聊防抖",
}

#: 等几个窗口
WINDOW_NONE = 0
WINDOW_ONE = 1
WINDOW_TWO = 2


def _b(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    return bool(value)


def _i(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _f(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


@dataclass
class DebounceSettings:
    """防抖参数。"""

    #: 默认关：它会给消息加一点点延迟，这种「时间上的意外」不该默认塞给所有人
    enabled: bool = False
    #: both / private / group
    scope: str = SCOPE_BOTH
    #: 一个等待窗口的长度（秒）
    grace_seconds: float = 1.5
    #: 一轮最多等多久（秒），硬上限
    max_wait_seconds: float = 6.0
    #: 一轮最多合并几条，超过就直接发
    max_messages: int = 5
    #: 长于这个字数就当成「说完了」，不再等
    long_enough: int = 12

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "DebounceSettings":
        raw: dict[str, Any] = {}
        if isinstance(config, Mapping):
            block = config.get("debounce")
            if isinstance(block, Mapping):
                raw = dict(block)
        if isinstance(overrides, Mapping):
            raw.update({k: v for k, v in overrides.items() if v is not None})
        scope = _s(raw.get("scope"), SCOPE_BOTH).lower()
        if scope not in SCOPES:
            scope = SCOPE_BOTH
        return DebounceSettings(
            enabled=_b(raw.get("enabled"), False),
            scope=scope,
            grace_seconds=_f(raw.get("grace_seconds"), 1.5, 0.2, 10.0),
            max_wait_seconds=_f(raw.get("max_wait_seconds"), 6.0, 0.5, 60.0),
            max_messages=_i(raw.get("max_messages"), 5, 2, 20),
            long_enough=_i(raw.get("long_enough"), 12, 4, 200),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "scope": self.scope,
            "grace_seconds": float(self.grace_seconds),
            "max_wait_seconds": float(self.max_wait_seconds),
            "max_messages": int(self.max_messages),
            "long_enough": int(self.long_enough),
        }


def is_incomplete(text: str) -> bool:
    """明显还没说完：结尾是逗号/顿号，或者停在一个连接词上。"""

    tail = _s(text)
    if not tail:
        return False
    if tail[-1] in END_PUNCT:
        return False  # 「然后。」已经说完了
    if tail[-1] in CONTINUE_PUNCT:
        return True
    return any(tail.endswith(word) for word in CONTINUE_TAILS)


def is_complete(text: str, settings: DebounceSettings | None = None) -> bool:
    """明显说完了：结尾是句末标点或语气词，再不然就是已经够长了。"""

    conf = settings or DebounceSettings()
    tail = _s(text)
    if not tail:
        return False
    if tail[-1] in END_PUNCT or tail[-1] in END_PARTICLES:
        return True
    return len(tail) >= int(conf.long_enough)


def wait_windows(text: str, settings: DebounceSettings | None = None) -> int:
    """这句话要等几个 grace 窗口再发。0 = 立刻发。"""

    conf = settings or DebounceSettings()
    tail = _s(text)
    if not tail:
        return WINDOW_NONE
    if is_incomplete(tail):
        return WINDOW_TWO
    if is_complete(tail, conf):
        return WINDOW_NONE
    return WINDOW_ONE


def _needs_space(left: str, right: str) -> bool:
    """只有两边都是英文/数字时才补空格，中文之间不补。"""

    return bool(
        left
        and right
        and left[-1].isascii()
        and left[-1].isalnum()
        and right[0].isascii()
        and right[0].isalnum()
    )


def merge_texts(parts: Sequence[str]) -> str:
    """把连发的几条拼成一句。

    中文之间不加空格 —— 加了就变成「今天晚上 吃什么」，一眼假。
    """

    out = ""
    for raw in parts or ():
        piece = _s(raw)
        if not piece:
            continue
        if not out:
            out = piece
            continue
        out += (" " if _needs_space(out, piece) else "") + piece
    return out


def scope_allows(settings: DebounceSettings, is_group: bool) -> bool:
    """这个会话要不要防抖。"""

    scope = (settings.scope or SCOPE_BOTH).lower()
    if scope == SCOPE_PRIVATE:
        return not is_group
    if scope == SCOPE_GROUP:
        return is_group
    return True


def looks_like_command(text: str) -> bool:
    """指令不参与防抖 —— 它本来就不该等。"""

    head = (text or "").strip()[:1]
    return head in {"/", "!", "！", "／", "#"}


def describe(settings: DebounceSettings) -> str:
    """一句话描述当前设置，面板顶部直接显示。"""

    if not settings.enabled:
        return "消息防抖：关"
    return "消息防抖：开（%s，窗口 %.1fs，最多合并 %d 条，上限 %.0fs）" % (
        SCOPE_LABELS.get(settings.scope, settings.scope),
        float(settings.grace_seconds),
        int(settings.max_messages),
        float(settings.max_wait_seconds),
    )


def preview(text: str, settings: DebounceSettings | None = None) -> dict[str, Any]:
    """给面板用：判断一段文字会不会被等，以及等几个窗口。"""

    conf = settings or DebounceSettings()
    windows = wait_windows(text, conf)
    if not conf.enabled:
        verdict = "disabled"
        note = "防抖关着，所有消息都是立刻发。"
    elif windows == WINDOW_NONE:
        verdict = "send"
        note = "看着说完了，立刻发。"
    elif windows == WINDOW_ONE:
        verdict = "grace"
        note = "短句又没有句末标点，拿不准 —— 等 1 个窗口，期间有下一条就合并。"
    else:
        verdict = "hold"
        note = "结尾没说完（逗号或连接词），等 2 个窗口；期间有下一条就合并。"
    return {
        "verdict": verdict,
        "windows": windows,
        "grace_seconds": float(conf.grace_seconds),
        "wait_seconds": round(float(conf.grace_seconds) * windows, 2),
        "incomplete": is_incomplete(text),
        "complete": is_complete(text, conf),
        "text": _s(text),
        "note": note,
    }


def panel_schema() -> dict[str, Any]:
    """面板渲染选项表需要的枚举，免得前端硬编码中文。"""

    return {
        "scopes": [{"value": key, "label": label} for key, label in SCOPE_LABELS.items()],
    }


__all__ = [
    "CONTINUE_PUNCT",
    "CONTINUE_TAILS",
    "END_PARTICLES",
    "END_PUNCT",
    "SCOPES",
    "SCOPE_LABELS",
    "WINDOW_NONE",
    "WINDOW_ONE",
    "WINDOW_TWO",
    "DebounceSettings",
    "describe",
    "is_complete",
    "is_incomplete",
    "looks_like_command",
    "merge_texts",
    "panel_schema",
    "preview",
    "scope_allows",
    "wait_windows",
]

