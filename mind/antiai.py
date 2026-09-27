"""说话自检 · 去 AI 味。

把一段话当成"她要说出口的回复"来体检：像不像机器写的，哪里像，怎么改。
纯本地词表 + 统计，不联网、不调用模型 —— 调用模型只发生在"打回重写"那一步。

八个检测维度（对照常见的 AI 腔）：

1. AI 高频词    首先 / 其次 / 总之 / 值得注意的是 / 在当今社会 ……
2. 套路句型     不是…而是… / 让我们一起 / 希望对你有所帮助 / 作为一个AI ……
3. 客服话术     请问有什么可以帮您 / 感谢您的 / 请您 …
4. 句长过匀     连续几句字数都差不多（真人说话长短是乱的）
5. 破折号       中文里成对的长破折号基本只在书面语里出现
6. 四字格堆砌   一连串成语式短语，读起来像公文
7. 项目符号     列表项 / 编号 / Markdown 标题
8. 口语碎语不足 长句子里一个语气词都没有（啊吧呢嘛哦呀唉……）
9. 口癖重复     同一句话头反复出现

分数越高越像人话（0~100）。60 以下就值得打回重写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
#: AI 高频词：书面连接词、空话套话
AI_WORDS: tuple[str, ...] = (
    "首先", "其次", "再者", "再次", "最后一点", "总之", "综上", "综上所述",
    "总而言之", "总的来说", "总的来讲", "整体而言", "总体来说", "一般来说",
    "值得一提的是", "值得注意的是", "需要注意的是", "需要指出的是", "需要强调的是",
    "不可否认", "毫无疑问", "显而易见", "众所周知", "毋庸置疑", "不言而喻",
    "换句话说", "简而言之", "一言以蔽之", "也就是说", "与此同时", "除此之外",
    "在此基础上", "从某种意义上", "在某种程度上", "从这个角度", "从某种意义来说",
    "在当今社会", "在当今", "随着科技的发展", "随着社会的发展", "在这个快节奏",
    "在这个时代", "在这个信息爆炸", "在如今", "现代社会中", "生活中我们常常",
    "具有重要的意义", "起到了重要的作用", "发挥着重要作用", "是一个重要的",
    "不仅仅", "更重要的是", "更深层次", "本质上", "归根结底", "究其原因",
    "从而", "进而", "因此", "然而", "不仅…而且", "一方面", "另一方面",
    "无论…都", "无论你", "无论何时", "让我们", "我们可以", "我们需要",
    "希望能够", "希望能", "建议您", "建议你", "请注意", "请记住",
    "总的来说呢", "最后", "第一，", "第二，", "第三，", "其一", "其二", "其三",
)

#: 套路句型：一眼假的句式
TEMPLATES: tuple[str, ...] = (
    "不是…而是", "不是……而是", "与其说", "与其…不如",
    "让我们一起", "让我们一起来", "一起探索", "开启一段", "踏上旅程",
    "希望对你有所帮助", "希望对你有所帮助", "希望能帮到你", "希望对你有帮助",
    "如果还有其他问题", "如果还有其他", "欢迎随时", "随时告诉我", "随时找我",
    "还有什么可以帮", "还有什么需要", "有什么我可以帮",
    "作为一个AI", "作为一个 AI", "作为人工智能", "我是一个AI", "我是一个 AI",
    "我是人工智能", "我只是一个", "我没有感情", "我无法感受",
    "这是一个很好的问题", "这是个好问题", "很好的问题", "非常棒的问题",
    "让我来", "让我为你", "接下来我", "下面我", "具体来说",
    "需要注意的是", "温馨提示", "友情提示", "小贴士",
    "在这个充满", "在这个世界上", "生活总是", "人生就像",
    "我懂你的感受", "我理解你的感受", "我明白你的感受", "我感受到你的",
    "我会一直陪", "一直陪伴", "陪伴着你", "陪在你身边",
    "温暖与力量", "温暖和力量", "给你力量", "给你温暖",
    "希望你能", "愿你能", "相信你一定",
    "愿你", "祝你有个美好", "愿你被世界温柔以待",
)

#: 客服话术
SERVICE_WORDS: tuple[str, ...] = (
    "请问有什么可以帮您", "请问有什么可以帮", "有什么可以帮您", "感谢您的",
    "请您", "为您", "您的反馈", "已为您", "很抱歉给您带来",
    "感谢您的理解", "感谢您的支持", "如有疑问", "如有需要", "敬请",
    "您可以通过", "您可以在", "您可以选择", "建议您进行",
)

#: 口语碎语（语气词/拟声词）—— 有这些才像人在说话
COLLOQUIAL: tuple[str, ...] = (
    "啊", "吧", "呢", "嘛", "哦", "噢", "呀", "唉", "诶", "欸", "哼", "切",
    "哈", "嘿", "唔", "嗯", "啦", "嘞", "咯", "哒", "惹", "叭", "嗷", "呜",
    "呃", "咦", "喂", "喏", "哎", "噗", "嘻", "嘎", "嘛", "咯",
)

#: 口癖：这些词被反复用在句首，就很像模板
OPENERS: tuple[str, ...] = ("其实", "而且", "所以", "然后", "另外", "此外", "同时", "当然", "不过", "总之")

#: 四字格的常见结尾字，用来粗略识别连续的四字短语
IDIOM_TAIL = ("的", "地", "得")

DASH_PATTERNS: tuple[str, ...] = ("——", "—", "――")

BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•·]|\d+[.、)]|\(\d+\)|[一二三四五六七八九十]+[、.])\s*")
HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s")
SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…；;\n]+")
WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")

#: 句长过匀的判定阈值
UNIFORM_MIN_SENTENCES = 3
UNIFORM_STD = 2.6
UNIFORM_MIN_MEAN = 6.0

#: 短于这个字数不做"碎语不足"的判定（「嗯。」本来就没语气词）
COLLOQUIAL_MIN_LENGTH = 16


@dataclass
class Check:
    """一个检测项的结果。"""

    key: str
    label: str
    penalty: float
    hits: list[str] = field(default_factory=list)
    hint: str = ""
    detail: str = ""

    @property
    def triggered(self) -> bool:
        return self.penalty > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "penalty": round(self.penalty, 1),
            "hits": list(self.hits[:8]),
            "hint": self.hint,
            "detail": self.detail,
        }


@dataclass
class Report:
    """一份体检报告。"""

    score: float
    length: int
    sentences: int
    checks: list[Check] = field(default_factory=list)

    @property
    def issues(self) -> list[Check]:
        return [check for check in self.checks if check.triggered]

    @property
    def top(self) -> list[str]:
        return [check.label for check in sorted(self.issues, key=lambda c: -c.penalty)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "length": self.length,
            "sentences": self.sentences,
            "issues": [check.to_dict() for check in self.issues],
            "checks": [check.to_dict() for check in self.checks],
        }


def _compile(words: Iterable[str]) -> re.Pattern[str] | None:
    cleaned = sorted({str(word) for word in words if word}, key=len, reverse=True)
    if not cleaned:
        return None
    try:
        return re.compile("|".join(re.escape(word) for word in cleaned))
    except re.error:
        return None


_COMPILED: dict[str, re.Pattern[str] | None] = {}


def _pattern(
    name: str, words: Sequence[str], extras: Sequence[str] = ()
) -> re.Pattern[str] | None:
    """按名字缓存内置词表；带额外词时不缓存（否则会把上一次的结果串用）。"""
    if extras:
        return _compile(list(words) + [str(item) for item in extras if item])
    if name not in _COMPILED:
        _COMPILED[name] = _compile(words)
    return _COMPILED[name]


def _find(pattern: re.Pattern[str] | None, text: str, limit: int = 12) -> list[str]:
    if pattern is None or not text:
        return []
    out: list[str] = []
    for match in pattern.finditer(text):
        token = match.group()
        if token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(text or "") if part.strip()]


def _sentence_lengths(text: str) -> list[int]:
    return [len(sentence) for sentence in split_sentences(text)]


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return variance ** 0.5


FOUR_CHAR_RE = re.compile(r"^[\u4e00-\u9fff]{4}$")


def _idiom_runs(text: str, limit: int = 3) -> list[str]:
    """四字格堆砌：被标点隔开、连着好几个正好四个字的小段。

    「勤勤恳恳，兢兢业业，任劳任怨」才是堆砌；
    不能拿任意四字切块去猜，那样「我觉得吧这事儿」也会被误判成成语。
    """
    segments = [part.strip() for part in re.split(r"[，,、；;。！？!?\s]+", text or "") if part.strip()]
    runs: list[str] = []
    current: list[str] = []
    for segment in segments:
        if FOUR_CHAR_RE.match(segment):
            current.append(segment)
            continue
        if len(current) >= limit:
            runs.append("，".join(current))
        current = []
    if len(current) >= limit:
        runs.append("，".join(current))
    return runs


def _repeated_openers(text: str) -> list[str]:
    """同一句话头反复出现。"""
    sentences = split_sentences(text)
    if len(sentences) < 3:
        return []
    heads: dict[str, int] = {}
    for sentence in sentences:
        match = re.match(r"[\u4e00-\u9fff]{2}", sentence)
        if match is None:
            continue
        head = match.group()
        heads[head] = heads.get(head, 0) + 1
    return [head for head, count in heads.items() if count >= 3]


def analyse(
    text: str,
    *,
    extra_ai_words: Sequence[str] = (),
    extra_templates: Sequence[str] = (),
    allow_words: Sequence[str] = (),
) -> Report:
    """给一段话打分。分数越高越像人话。"""
    raw = str(text or "")
    body = raw.strip()
    allowed = {str(word) for word in allow_words if word}

    def _filtered(words: Sequence[str]) -> list[str]:
        return [word for word in words if word not in allowed]

    checks: list[Check] = []

    ai_hits = _filtered(_find(_pattern("ai", AI_WORDS, extra_ai_words), body))
    if ai_hits:
        checks.append(Check(
            key="ai_words", label="AI 高频词", penalty=min(30.0, 4.0 * len(ai_hits) + 4.0),
            hits=ai_hits, hint="换成口语说法，或者干脆删掉这些连接词",
        ))

    template_hits = _filtered(_find(_pattern("tpl", TEMPLATES, extra_templates), body))
    if template_hits:
        checks.append(Check(
            key="templates", label="套路句型", penalty=min(28.0, 8.0 * len(template_hits)),
            hits=template_hits, hint="这种句式一眼假，直接说人话",
        ))

    service_hits = _filtered(_find(_pattern("svc", list(SERVICE_WORDS)), body))
    if service_hits:
        checks.append(Check(
            key="service", label="客服话术", penalty=min(24.0, 8.0 * len(service_hits)),
            hits=service_hits, hint="你不是客服，别用敬语套话",
        ))

    dash_hits = _filtered(_find(_pattern("dash", list(DASH_PATTERNS)), body))
    if dash_hits:
        checks.append(Check(
            key="dash", label="长破折号", penalty=min(12.0, 6.0 * len(dash_hits)),
            hits=dash_hits, hint="中文聊天里几乎不用长破折号，换成逗号或者直接断句",
        ))

    lengths = _sentence_lengths(body)
    if len(lengths) >= UNIFORM_MIN_SENTENCES:
        mean = sum(lengths) / len(lengths)
        std = _std(lengths)
        if mean >= UNIFORM_MIN_MEAN and std < UNIFORM_STD:
            checks.append(Check(
                key="uniform", label="句长过匀",
                penalty=min(16.0, 6.0 + (UNIFORM_STD - std) * 3.5),
                detail=f"{len(lengths)} 句，平均 {mean:.0f} 字，波动只有 {std:.1f}",
                hint="长短句交错着来，别每句都一样长",
            ))

    runs = _idiom_runs(body)
    if runs:
        checks.append(Check(
            key="idiom", label="四字格堆砌", penalty=min(14.0, 6.0 * len(runs)),
            hits=runs, hint="成语换成大白话",
        ))

    bullets = BULLET_RE.findall(body)
    headings = HEADING_RE.findall(body)
    if bullets or headings:
        checks.append(Check(
            key="bullets", label="项目符号", penalty=min(26.0, 8.0 * (len(bullets) + len(headings)) + 6.0),
            detail=f"列表项 {len(bullets)} 个，标题 {len(headings)} 个",
            hint="聊天里不要用列表和标题，写成一句话",
        ))

    if len(body) >= COLLOQUIAL_MIN_LENGTH:
        if not _find(_pattern("coll", list(COLLOQUIAL)), body, limit=1):
            checks.append(Check(
                key="colloquial", label="口语碎语不足", penalty=8.0,
                hint="加一点语气词（啊、吧、呢、嘛、唉），别像在念稿子",
            ))

    repeats = _repeated_openers(body)
    if repeats:
        checks.append(Check(
            key="repeat", label="口癖重复", penalty=min(12.0, 5.0 * len(repeats)),
            hits=repeats, hint="同一个开头别连着用，换着说",
        ))

    penalty = sum(check.penalty for check in checks)
    score = max(0.0, min(100.0, 100.0 - penalty))
    return Report(score=score, length=len(body), sentences=len(lengths), checks=checks)


# ---------------------------------------------------------------------------
# 改写
# ---------------------------------------------------------------------------
MAX_REWRITE_RATIO = 3.0

REWRITE_SYSTEM = (
    "你是中文口语润色助手。你的任务是把一段「像机器写的」聊天回复改成真人说话的样子。"
    "只输出改写后的正文，不要任何解释、标题、前后缀、引号或代码块。"
)

REWRITE_TEMPLATE = """下面这段话是我要发给朋友的聊天回复，但读起来像机器写的。请改写它。

【检测到的问题】
{issues}

【要求】
1. 保持原意，不要增加原文没有的信息，也不要删掉关键内容。
2. 保持原文的口吻、称呼和性格，别改成人设以外的风格。
3. 长度和原文差不多，不要写长。
4. 只输出改写后的正文。不要写"改写："、"以下是"之类的前缀，
   不要用引号把整段括起来，不要加任何解释。

【原文】
{text}"""


def build_rewrite_prompt(report: Report, text: str, template: str = None) -> str:
    lines = []
    for check in sorted(report.issues, key=lambda c: -c.penalty):
        detail = f"（{check.detail}）" if check.detail else ""
        hits = f"：{'、'.join(check.hits[:6])}" if check.hits else ""
        lines.append(f"- {check.label}{hits}{detail} → {check.hint}")
    if not lines:
        lines.append("- 整体太规整，读起来像书面语 → 说得随意一点")
    body = template.strip() if isinstance(template, str) and template.strip() else REWRITE_TEMPLATE
    try:
        return body.format(issues=chr(10).join(lines), text=text.strip())
    except (KeyError, IndexError, ValueError):
        return REWRITE_TEMPLATE.format(issues=chr(10).join(lines), text=text.strip())


_PREFIX_RE = re.compile(
    r"^\s*(?:改写|修改|润色|版本\s*\d*|结果|输出|以下是|下面是)[^\n]{0,12}?[：:]\s*"
)
_FENCE_RE = re.compile(r"^\s*" + chr(96) * 3 + r"[a-zA-Z]*\s*|\s*" + chr(96) * 3 + r"\s*$")
_QUOTE_PAIRS = (("「", "」"), ("“", "”"), ("『", "』"), ('"', '"'), ("'", "'"))


def clean_rewrite(raw: str, original: str = "") -> str:
    """把模型返回的东西洗成能直接发出去的正文。

    只做保守清理：去掉前缀标签、代码块围栏、整段包裹的引号；
    一旦发现它变成了"解释"或者长得离谱，就返回空串，让调用方保留原文。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    # 模型有时会先写一段分析再给正文，取最后一个前缀标签之后的内容
    matches = list(_PREFIX_RE.finditer(text))
    if matches:
        text = text[matches[-1].end():].strip()
    for left, right in _QUOTE_PAIRS:
        if text.startswith(left) and text.endswith(right) and len(text) > len(left) + len(right):
            text = text[len(left) : -len(right)].strip()
            break
    text = text.strip()
    if not text:
        return ""
    if "\n\n" in text and len(text.split("\n\n")[0]) < 6:
        text = text.split("\n\n", 1)[1].strip()
    if original:
        limit = max(40, int(len(original.strip()) * MAX_REWRITE_RATIO))
        if len(text) > limit:
            return ""
    # 拒绝"我不行/我不能"这类跑偏的回答
    if re.search(r"(?:作为(?:一个)?\s*AI|我是人工智能|我无法|我不能|抱歉，我)", text):
        return ""
    return text


__all__ = [
    "AI_WORDS",
    "BULLET_RE",
    "COLLOQUIAL",
    "Check",
    "DASH_PATTERNS",
    "Report",
    "REWRITE_SYSTEM",
    "SERVICE_WORDS",
    "TEMPLATES",
    "analyse",
    "build_rewrite_prompt",
    "clean_rewrite",
    "split_sentences",
]
