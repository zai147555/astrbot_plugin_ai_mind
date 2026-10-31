
"""需求先搜市场：把「提出需求 → 搜开源市场 → 有现成的就照实说」做进插件。

对应工作区规则里那条流程，现在由她执行，而不是只由写插件的人执行。

数据源是 DSH 官方插件目录（dshmarketplace.dev）：
    GET https://dshmarketplace.dev/api/v1/plugins?q=<能力词>&limit=<n>
返回 {total, results:[{fullName, summary, summaryZh, stars, license, pushedAt,
                       inRegistry, riskFlags, install, url}]}

只在**像需求**的那句话上触发（「能不能做个…」「加个…功能」「有没有插件…」），
默认只对专属用户触发，免得群里聊天被它刷屏。搜到会作为一段 <market_hits>
注入这一轮，让她说「市场里有个现成的：X」，而不是硬编一个。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

API_PATH = "/api/v1/plugins"
DEFAULT_BASE = "https://dshmarketplace.dev"
TIMEOUT = 8

#: 需求意图：名词（在说一个能力）+ 动作（想加/想找）才触发
NOUN_HINTS = ("插件", "功能", "模块", "工具")
ACTION_HINTS = ("接入", "实现", "开发", "支持")
VERB_HINTS = ("做", "加", "找", "写", "搞", "搭", "要一个", "有没有", "能不能")
#: 兼容旧名字
STRONG_HINTS = NOUN_HINTS + ACTION_HINTS
#: 太短的话不触发
MIN_CHARS = 4

#: 命中数
DEFAULT_LIMIT = 6
#: 对话里注入几条
INJECT_COUNT = 3


def looks_like_requirement(text: str) -> bool:
    """这句话是不是在提一个功能需求。"""
    raw = re.sub(r"\s+", "", str(text or ""))
    if len(raw) < MIN_CHARS:
        return False
    has_noun = any(word in raw for word in NOUN_HINTS)
    has_action = any(word in raw for word in ACTION_HINTS)
    has_verb = any(verb in raw for verb in VERB_HINTS)
    if has_noun and (has_verb or has_action):
        return True
    # 光有「接入/实现/开发/支持」也算动作意图（他说的是动词短语）
    return has_action and len(raw) >= 6


def keywords_of(text: str) -> list[str]:
    """从需求里抠检索词：去掉客套与动词，留下能力词。"""
    raw = re.sub(r"[，。！？、,.!?;；:：\"'（）()《》\[\]【】]", " ", str(text or ""))
    noise = (
        "能不能", "可不可以", "帮我", "麻烦", "一下", "给我", "我想要", "想", "你",
        "做个", "做一个", "加个", "加一个", "写个", "搞个", "搭个", "做个", "实现",
        "开发", "支持", "接入", "有没有", "找个", "看看", "谢谢", "呗", "吧", "吗", "呢",
        "的", "了", "个", "一个", "插件", "功能", "模块", "工具",
    )
    text = str(text or "")
    for word in noise:
        text = text.replace(word, " ")
    words = [piece for piece in text.split() if piece.strip()]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return seen[:6]


@dataclass
class MarketSettings:
    enabled: bool = True
    base_url: str = DEFAULT_BASE
    limit: int = DEFAULT_LIMIT
    #: 自动搜索（只在像需求的话上）
    auto: bool = True
    #: 只对专属用户自动搜（群里其他人不触发）
    owner_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "base_url": self.base_url,
            "limit": int(self.limit),
            "auto": bool(self.auto),
            "owner_only": bool(self.owner_only),
        }

    @staticmethod
    def from_dict(data: Any) -> "MarketSettings":
        raw = data if isinstance(data, dict) else {}

        def num(value: Any, fallback: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return fallback

        return MarketSettings(
            enabled=bool(raw.get("enabled", True)),
            base_url=str(raw.get("base_url") or DEFAULT_BASE).strip(),
            limit=num(raw.get("limit"), DEFAULT_LIMIT, 1, 20),
            auto=bool(raw.get("auto", True)),
            owner_only=bool(raw.get("owner_only", True)),
        )


def load_settings(path: Path | str) -> MarketSettings:
    try:
        return MarketSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return MarketSettings()


def save_settings(path: Path | str, settings: MarketSettings) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError):
        return False


#: 最近几次搜索（面板要看）
@dataclass
class MarketHistory:
    rows: list[dict[str, Any]] = field(default_factory=list)
    keep: int = 20

    def add(self, query: str, hits: list[dict[str, Any]], note: str = "") -> None:
        self.rows.insert(0, {
            "query": str(query or ""),
            "at": time.time(),
            "total": len(hits),
            "note": str(note or ""),
            "hits": [
                {
                    "fullName": hit.get("fullName", ""),
                    "summary": hit.get("summary", ""),
                    "license": hit.get("license", ""),
                    "stars": hit.get("stars", 0),
                    "url": hit.get("url", ""),
                    "riskFlags": list(hit.get("riskFlags") or []),
                }
                for hit in hits[:5]
            ],
        })
        del self.rows[self.keep:]

    def to_dict(self) -> dict[str, Any]:
        return {"rows": list(self.rows)}

    @staticmethod
    def from_dict(data: Any) -> "MarketHistory":
        raw = data if isinstance(data, dict) else {}
        rows = raw.get("rows")
        history = MarketHistory()
        if isinstance(rows, list):
            history.rows = [row for row in rows if isinstance(row, dict)][:20]
        return history


def load_history(path: Path | str) -> MarketHistory:
    try:
        return MarketHistory.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return MarketHistory()


def save_history(path: Path | str, history: MarketHistory) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(history.to_dict(), ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 目录请求
# ---------------------------------------------------------------------------


def _get(url: str, timeout: int = TIMEOUT) -> dict[str, Any]:
    """GET 一个 JSON。测试里会换掉。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "astrbot-plugin-ai-mind/3.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError("市场请求失败：%s: %s" % (type(exc).__name__, exc)) from exc


def normalize(item: dict[str, Any]) -> dict[str, Any]:
    """把目录条目抹平成插件要用的形状（summary 优先中文）。"""
    summary = str(item.get("summaryZh") or item.get("summary") or "").strip()
    return {
        "fullName": str(item.get("fullName") or ""),
        "summary": summary,
        "category": str(item.get("category") or ""),
        "stars": int(item.get("stars") or 0),
        "license": str(item.get("license") or "none detected"),
        "pushedAt": str(item.get("pushedAt") or ""),
        "inRegistry": bool(item.get("inRegistry")),
        "riskFlags": [str(flag) for flag in (item.get("riskFlags") or [])],
        "install": str(item.get("install") or ""),
        "url": str(item.get("url") or ""),
    }


def search(query: str, settings: MarketSettings,
           timeout: int = TIMEOUT) -> tuple[list[dict[str, Any]], str]:
    """搜市场，返回 (结果, 错误说明)。"""
    word = str(query or "").strip()
    if not word:
        return [], "没有检索词"
    from urllib.parse import urlencode

    base = str(settings.base_url or DEFAULT_BASE).rstrip("/")
    url = base + API_PATH + "?" + urlencode({"q": word, "limit": int(settings.limit)})
    try:
        payload = _get(url, timeout)
    except ValueError as exc:
        return [], str(exc)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return [], "目录返回格式变了"
    return [normalize(item) for item in results if isinstance(item, dict)], ""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _risk_text(hit: dict[str, Any]) -> str:
    flags = hit.get("riskFlags") or []
    return ("风险：" + "、".join(flags)) if flags else ""


def render_hits(hits: Iterable[dict[str, Any]], count: int = INJECT_COUNT) -> str:
    """给人读的紧凑列表（命令、面板共用）。"""
    rows = list(hits)[: max(1, int(count))]
    if not rows:
        return ""
    lines: list[str] = []
    for index, hit in enumerate(rows, 1):
        bits = [hit.get("fullName") or "?"]
        meta = []
        if hit.get("license"):
            meta.append(str(hit["license"]))
        if hit.get("stars"):
            meta.append("%d★" % int(hit["stars"]))
        if hit.get("inRegistry"):
            meta.append("社区登记")
        if meta:
            bits.append("（" + "，".join(meta) + "）")
        lines.append("%d. %s" % (index, "".join(bits)))
        if hit.get("summary"):
            lines.append("   " + str(hit["summary"])[:120])
        risk = _risk_text(hit)
        if risk:
            lines.append("   " + risk)
        if hit.get("url"):
            lines.append("   " + str(hit["url"]))
    return "\n".join(lines)


def render_block(query: str, hits: list[dict[str, Any]]) -> str:
    """注入给模型的一段：告诉她市场里有没有现成的。"""
    if not hits:
        return (
            "<market_hits>\n"
            "主人刚提了个需求，我替他搜了开源插件市场（关键词：%s），**没有搜到现成的**。\n"
            "就按自己的判断答，别说「市场里有」，也别编插件名。\n"
            "</market_hits>" % str(query or "")
        )
    body = render_hits(hits, INJECT_COUNT)
    return (
        "<market_hits>\n"
        "主人刚提了个需求，我替他去开源插件市场搜了一圈（关键词：%s），有这些现成的：\n"
        "%s\n"
        "怎么用这些信息：如果里有能直接用的，就照实说「市场里有个现成的：X」；\n"
        "如果都不合适，就说没找到合适的，不要硬推；不要编造结果里没有的插件。\n"
        "</market_hits>" % (str(query or ""), body)
    )


def render_chat(query: str, hits: list[dict[str, Any]], error: str = "") -> str:
    """命令回复。"""
    if error:
        return "搜市场失败：" + error
    if not hits:
        return "市场里没有搜到「%s」。换几个说法再试试（用能力词，别用产品名）。" % (query or "")
    return "市场里搜到 %d 个（关键词：%s）：\n%s" % (len(hits), query or "", render_hits(hits, 5))


__all__ = [
    "API_PATH", "DEFAULT_BASE", "NOUN_HINTS", "ACTION_HINTS", "VERB_HINTS",
    "STRONG_HINTS", "DEFAULT_LIMIT",
    "INJECT_COUNT", "MarketSettings", "MarketHistory",
    "looks_like_requirement", "keywords_of", "load_settings", "save_settings",
    "load_history", "save_history", "normalize", "search",
    "render_hits", "render_block", "render_chat",
]

