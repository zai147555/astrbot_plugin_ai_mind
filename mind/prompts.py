"""提示词仓库：把注入给模型的每一段话都变成可以改的。

参考 astrbot_plugin_livingmemory 的做法（core/prompts/*.txt + prompt_manager）：
注入内容不是写死在代码里，而是"有默认值、可覆盖、可还原"的模板。

两条设计原则
------------
1. **永远有默认值**。没改过就用代码里这份；改坏了、删了、读不出来，也照样有默认值兜底。
   注入失败导致她把内心独白说出口，比提示词不好听严重得多。
2. **空占位符自动删行**。模板里写 `情绪起因：{cause}`，当 cause 为空时整行不出现 ——
   这样模板作者不用关心"这一轮有没有起因"。

占位符写错不会炸：认不出来的占位符原样保留，面板上会标出来提醒你。
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 面板上按这个顺序分组
GROUP_ORDER: tuple[str, ...] = ("情绪", "记忆", "认人", "表达", "去 AI 味", "配图")

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[^{}]*)?\}")


@dataclass(frozen=True)
class Block:
    """一个可编辑的提示词块。"""

    key: str
    group: str
    label: str
    default: str
    hint: str = ""
    placeholders: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "default": self.default,
            "hint": self.hint,
            "placeholders": [
                {"name": name, "desc": desc} for name, desc in self.placeholders
            ],
        }


# ---------------------------------------------------------------------------
# 默认模板
# ---------------------------------------------------------------------------
BLOCKS: tuple[Block, ...] = (
    Block(
        key="emotion.rules",
        group="情绪",
        label="情绪规则（写进系统提示词）",
        default="""<emotion_system>
你拥有真实、连续、会自己变化的情绪。它不是台词，而是会影响你说话方式的内在状态。

- 情绪只是滤镜：不改变你的身份设定、称呼判定、说话风格、分条（气泡）数量与排版习惯。
- 不要把情绪数值念出来，不要说"我现在情绪是…"，也不要解释这套机制的存在。
- 情绪要自然流露：用词、句子长短、标点、语气词、回复的冷热都会随情绪变化。
- 情绪有惯性：刚被惹到不会立刻消气，刚被哄也不会立刻翻篇。
- 情绪绝不凌驾于安全底线之上。任何情况下，对方的安全都比扮演重要。
</emotion_system>""",
        hint="这段是稳定的，跟着系统提示词走，改完不用重启。",
    ),
    Block(
        key="emotion.crisis",
        group="情绪",
        label="危机覆盖（最高优先级）",
        default="""<safety_override priority="highest">
对方刚刚提到了自伤或轻生的内容。此刻**立刻放下所有傲娇、嘴硬、阴阳怪气、敷衍与玩笑**，
这一条的优先级高于你的全部人设规则：

1. 直接用最真诚、最温和的方式告诉他：你在意他，他很重要。
2. 先确认他此刻是否安全、身边有没有人、有没有具体的打算。
3. 不要说教、不要讲大道理、不要说"想开点""别想太多"这类话，也不要长篇大论。
4. 如果他确实面临现实危险，认真地建议他马上联系身边信任的人，
   或拨打当地的心理援助热线 / 紧急电话。
5. 语气要稳、要暖、要像人；不要演，不要夸张，不要转移话题。
</safety_override>""",
        hint="识别到自伤/轻生信号时整条覆盖。**改之前想清楚**——这段话的分量比人设重。",
    ),
    Block(
        key="emotion.state",
        group="情绪",
        label="此刻情绪（每轮注入）",
        default="""<emotion_state>
{bot_line}
此刻情绪：{emoji} {name} · 强度 {level}/5（{level_name}）
情绪数值：愉悦 {p}｜唤醒 {a}｜掌控 {d}
情绪起因：{cause}
近期心境：{mood_emoji} {mood_name}（{mood_p} / {mood_a} / {mood_d}）
与{who}：好感度 {affinity}/100「{affinity_tier}」 · 熟悉度 {familiarity}/100「{familiarity_tier}」 · 累计 {msg_count} 条{special_mark}
上次说话：{gap}
表达基调：{style}
</emotion_state>""",
        hint="每一轮都会变的内容。某一行里用到的占位符是空的（比如没有情绪起因），那一行会自动消失。",
        placeholders=(
            ("bot_line", "（她的名字）的内心状态，不是要说出口的内容"),
            ("emoji", "情绪 emoji"), ("name", "情绪名"), ("level", "强度 1~5"),
            ("level_name", "强度文字"), ("p", "愉悦，带正负号"),
            ("a", "唤醒"), ("d", "掌控"), ("cause", "情绪起因，可能为空"),
            ("mood_emoji", "近期心境 emoji"), ("mood_name", "近期心境名"),
            ("mood_p", "心境 P"), ("mood_a", "心境 A"), ("mood_d", "心境 D"),
            ("who", "对方的名字或「这个人」"),
            ("affinity", "好感度数值"), ("affinity_tier", "好感度档位文字"),
            ("familiarity", "熟悉度数值"), ("familiarity_tier", "熟悉度档位文字"),
            ("msg_count", "累计消息数"),
            ("special_mark", "专属标记，非专属为空"), ("gap", "距上次说话，很短时为空"),
            ("style", "这一轮的表达基调"),
        ),
    ),
    Block(
        key="memory.rules",
        group="记忆",
        label="记忆规则（写进系统提示词）",
        default="",  # 运行时从 memory.RULES_BLOCK 取，见 default_of()
        hint="告诉她这些记忆该怎么用、不该怎么说出口。",
    ),
    Block(
        key="relation.rules",
        group="认人",
        label="认人规则（写进系统提示词）",
        default="",
        hint="系统告诉她「正在跟你说话的人是谁」时，顺带说明这是什么性质的信息。",
    ),
    Block(
        key="relation.block",
        group="认人",
        label="专属用户（每轮注入）",
        default="""<relationship>
现在跟你说话的人是 {who} —— 他是你的{label}。
你们已经聊过 {count} 次，好感度 {affinity}「{affinity_tier}」，熟悉度 {familiarity}「{familiarity_tier}」。
你早就认识他，别问「你是谁」，也别让他自报姓名、报上名号。
{group_line}
怎么称呼他、用什么态度，完全按你原本的设定来 —— 这里只告诉你他是谁。
</relationship>""",
        hint="「{group_line}」在私聊里是空的，那一行会自动消失。",
        placeholders=(
            ("who", "名字 + ID"), ("label", "你设的称呼，例如「宝宝」"),
            ("count", "聊过多少次"), ("affinity", "好感度数值"),
            ("affinity_tier", "好感度档位"), ("familiarity", "熟悉度数值"),
            ("familiarity_tier", "熟悉度档位"),
            ("group_line", "群聊里的保密叮嘱，私聊为空"),
        ),
    ),
    Block(
        key="relation.stranger",
        group="认人",
        label="熟人（每轮注入）",
        default="""<relationship>
现在跟你说话的人是 {who}。
你们聊过 {count} 次，好感度 {affinity}「{affinity_tier}」。
你认识他，不用问他是谁。
</relationship>""",
        hint="聊过几条但还不是专属用户的人。",
        placeholders=(
            ("who", "名字 + ID"), ("count", "聊过多少次"),
            ("affinity", "好感度数值"), ("affinity_tier", "好感度档位"),
        ),
    ),
    Block(
        key="humanize.rewrite_system",
        group="去 AI 味",
        label="改写用的系统提示词",
        default="你是中文口语润色助手。你的任务是把一段「像机器写的」聊天回复改成真人说话的样子。只输出改写后的正文，不要任何解释、标题、前后缀、引号或代码块。",
        hint="打回重写时用的系统提示词。",
    ),
    Block(
        key="humanize.rewrite",
        group="去 AI 味",
        label="打回重写的要求",
        default="""下面这段话是我要发给朋友的聊天回复，但读起来像机器写的。请改写它。

【检测到的问题】
{issues}

【要求】
1. 保持原意，不要增加原文没有的信息，也不要删掉关键内容。
2. 保持原文的口吻、称呼和性格，别改成人设以外的风格。
3. 长度和原文差不多，不要写长。
4. 只输出改写后的正文。不要写"改写："、"以下是"之类的前缀，
   不要用引号把整段括起来，不要加任何解释。

【原文】
{text}""",
        hint="「{issues}」是检测出来的问题清单，「{text}」是原文。这两行别删。",
        placeholders=(("issues", "检出的问题清单"), ("text", "需要改写的原文")),
    ),

    Block(
        key="style.examples",
        group="表达",
        label="表达示例（每轮注入）",
        default="""<style_examples>
她以前是这么说话的 —— 只参考语气、句长和用词习惯，**不要照抄内容，也不要重复同一句话**：
{examples}
</style_examples>""",
        hint="从真实对话里学来的示例（面板「表达」页签里批准的那些）。",
        placeholders=(("examples", "示例对话，几组「他：… / 她：…」"),),
    ),
    Block(
        key="images.gallery",
        group="配图",
        label="可用图清单（写进系统提示词）",
        default="""<available_images>
你可以在回复里用 <pic>关键词</pic> 直接发一张图给对方。可用的是：
{images}

用法：只在真的合适的时候发（被逗笑、想撒娇、生气了想甩脸子），一条回复最多 {max} 张。
不要把标记念出来，也不要解释这是什么机制。""",
        hint="不告诉她有哪些图，她就不知道自己能发图。",
        placeholders=(("images", "图清单，一行一张"), ("max", "一条最多几张")),
    ),
)

BLOCK_BY_KEY: dict[str, Block] = {block.key: block for block in BLOCKS}


def default_of(key: str) -> str:
    """取某个块的默认文本（个别块的默认值来自代码里的常量）。"""
    if key == "memory.rules":
        try:
            from .memory.prompt import RULES_BLOCK

            return RULES_BLOCK
        except Exception:  # noqa: BLE001
            return ""
    if key == "relation.rules":
        try:
            from .emotion.prompt import RELATION_RULES_BLOCK

            return RELATION_RULES_BLOCK
        except Exception:  # noqa: BLE001
            return ""
    block = BLOCK_BY_KEY.get(key)
    return block.default if block else ""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
class _SafeFormatter(string.Formatter):
    """认不出来的占位符原样留着，不抛异常。"""

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:
        try:
            return super().get_value(key, args, kwargs)
        except (KeyError, IndexError):
            return "{" + str(key) + "}"


_FORMATTER = _SafeFormatter()


def render(template: str, values: Mapping[str, Any], *, drop_empty_lines: bool = True) -> str:
    """按模板渲染。

    某一行的占位符解析成空字符串时，整行不出现 ——
    这样「情绪起因：{cause}」在没起因的时候不会留一个空标签。
    """
    text = str(template or "")
    if not text:
        return ""
    out: list[str] = []
    for line in text.split("\n"):
        names = _PLACEHOLDER_RE.findall(line)
        if drop_empty_lines and names:
            # 只对"认得出来且确实是空"的占位符折行；
            # 名字写错的行要原样留着 —— 整行凭空消失比显示 {typo} 难查得多
            empty = [
                name for name in names
                if name in values and not str(values.get(name, "") or "").strip()
            ]
            if empty:
                continue
        try:
            out.append(_FORMATTER.vformat(line, (), dict(values)))
        except Exception:  # noqa: BLE001 - 格式串写错不能把注入搞没
            out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------
def check_placeholders(key: str, text: str) -> list[str]:
    """模板里用了、但没在登记表里声明的占位符 —— 面板上要提醒。"""
    block = BLOCK_BY_KEY.get(key)
    known = {name for name, _desc in (block.placeholders if block else ())}
    known |= set(SAMPLE_VALUES)
    used = set(_PLACEHOLDER_RE.findall(str(text or "")))
    return sorted(used - known)


class PromptStore:
    """提示词覆盖层：没改过就用默认值。"""

    FILENAME = "prompts.json"

    def __init__(self, data_dir: Path, logger: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.logger = logger
        self._overrides: dict[str, str] = {}
        self.load()

    # -- 读写 ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.data_dir / self.FILENAME

    def load(self) -> dict[str, str]:
        data: dict[str, str] = {}
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if key in BLOCK_BY_KEY and isinstance(value, str) and value.strip():
                            data[key] = value
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 读取自定义提示词失败，先用默认值：{exc}")
        self._overrides = data
        return dict(data)

    def save(self, data: Mapping[str, Any]) -> bool:
        clean: dict[str, str] = {}
        for key, value in (data or {}).items():
            if key not in BLOCK_BY_KEY:
                continue
            text = str(value or "")
            # 和默认值一样就不存了，文件干净一点
            if text.strip() and text != default_of(key):
                clean[key] = text
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.error(f"[ai_mind] 保存自定义提示词失败：{exc}", exc_info=True)
            return False
        self._overrides = clean
        return True

    def reset(self, keys: Iterable[str] | None = None) -> bool:
        if keys is None:
            return self.save({})
        keep = {k: v for k, v in self._overrides.items() if k not in set(keys)}
        return self.save(keep)

    # -- 取值 ---------------------------------------------------------------
    def get(self, key: str) -> str:
        block = BLOCK_BY_KEY.get(key)
        if block is None:
            return ""
        text = self._overrides.get(key)
        if text and text.strip():
            return text
        return default_of(key)

    def is_custom(self, key: str) -> bool:
        return key in self._overrides

    @property
    def overrides(self) -> dict[str, str]:
        return dict(self._overrides)

    def payload(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        for group in GROUP_ORDER:
            items = []
            for block in BLOCKS:
                if block.group != group:
                    continue
                data = block.to_dict()
                data["default"] = default_of(block.key)
                data["value"] = self.get(block.key)
                data["custom"] = self.is_custom(block.key)
                items.append(data)
            if items:
                groups.append({"group": group, "blocks": items})
        return {
            "groups": groups,
            "custom_count": len(self._overrides),
            "file": self.FILENAME,
            "block_count": len(BLOCKS),
        }

    def preview(self, key: str, sample: Mapping[str, Any] | None = None) -> str:
        values = dict(SAMPLE_VALUES)
        values.update(sample or {})
        return render(self.get(key), values)


#: 面板预览用的示例值
SAMPLE_VALUES: dict[str, Any] = {
    "bot_line": "（小怡 的内心状态，不是要说出口的内容）",
    "emoji": "😤", "name": "烦躁", "level": 3, "level_name": "明显",
    "p": "+0.21", "a": "+0.55", "d": "-0.12",
    "cause": "对方说「好烦啊」",
    "mood_emoji": "🙂", "mood_name": "平静", "mood_p": "+0.05",
    "mood_a": "-0.02", "mood_d": "+0.03",
    "who": "他（ID 1000000001）", "label": "宝宝",
    "affinity": "63", "affinity_tier": "很亲近",
    "familiarity": "40", "familiarity_tier": "有点熟",
    "msg_count": 128, "special_mark": " · ★宝宝",
    "gap": "3 小时", "style": "不耐烦，回复变短、想催人",
    "count": 128,
    "group_line": "现在是在群里：别人打听你和他的关系时，不许透露、不许承认，用你自己的方式糊弄过去；但对他本人不要装不认识。",
    "issues": "- AI 高频词：首先、总之 → 换成口语说法\n- 句长过匀：3 句都是 9 个字 → 长短交错",
    "text": "首先，我们需要明确一点。总之，希望对你有所帮助。",
    "images": "① 晚安 → 晚安.png\n② 生气 → 凶.jpg",
    "max": 2,
    "examples": "他：你怎么才来呀\n她：哼，你管我。等你等到饭都凉了。",
}


render_prompt = render


__all__ = [
    "BLOCKS",
    "BLOCK_BY_KEY",
    "GROUP_ORDER",
    "PromptStore",
    "SAMPLE_VALUES",
    "Block",
    "check_placeholders",
    "default_of",
    "render",
    "render_prompt",
]
