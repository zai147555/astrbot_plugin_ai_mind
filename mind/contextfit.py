
"""上下文压缩：聊久了不能把整段历史原样塞给模型。

原则：**先删废话，保留要紧句**。
- 要紧句：他说的、带标记的（记住 / 别忘 / 以后 / 生日 / 学校 / 生病 / 难过 / 危机词…）；
- 最近 keep_recent 轮原样保留（接着聊得下去）；
- 被删的部分压成几行提要（原文摘一句），贴在请求尾部 —— 不破坏前缀缓存；
- 全程不调模型，只是选句子，所以不花 token。
"""
from __future__ import annotations

import re
from typing import Any

_CJK = re.compile(r"[\u4e00-\u9fff]")

#: 出现这些词的一句，值得从旧历史里挑出来留着
IMPORTANT_WORDS = (
    "记住", "别忘了", "别忘", "以后", "答应", "约定", "生日", "名字", "学校", "住址",
    "生病", "药", "医院", "难过", "哭", "吵架", "对不起", "喜欢", "讨厌", "害怕",
    "考试", "比赛", "搬家", "分手", "兼职", "工作", "妈妈", "爸爸", "家里",
)
#: 危机句必须留（宁多留一句，也不能漏）
CRISIS_WORDS = ("自杀", "轻生", "不想活", "去死", "自伤", "伤害自己")

MARKER = "（更早的"


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。"""
    blob = str(text or "")
    if not blob:
        return 0
    cjk = len(_CJK.findall(blob))
    other = max(0, len(blob) - cjk)
    return int(cjk + other / 4 + 0.5)


def _content(row: Any) -> str:
    if isinstance(row, dict):
        value = row.get("content")
    else:
        value = getattr(row, "content", "")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(value or "")


def _role(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("role") or "")
    return str(getattr(row, "role", "") or "")


def is_important(row: Any) -> bool:
    """这句值不值得从旧历史里留下。"""
    text = _content(row)
    if not text.strip():
        return False
    role = _role(row)
    if any(word in text for word in CRISIS_WORDS):
        return True
    if role == "user" and any(word in text for word in IMPORTANT_WORDS):
        return True
    # 「他」问了具体的事（带问号 / 包含「？」），后面可能还要接着答
    if role == "user" and ("？" in text or "?" in text) and len(text) >= 12:
        return True
    if role == "assistant" and any(word in text for word in CRISIS_WORDS):
        return True
    return False


def _snippet(text: str, limit: int = 48) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _label(role: str) -> str:
    return "他" if role == "user" else ("她" if role == "assistant" else "·")


def plan(contexts: Any, max_turns: int = 40, keep_recent: int = 16,
         keep_important: int = 6, digest_lines: int = 8) -> dict[str, Any]:
    """决定这一轮的历史怎么砍。

    返回 {contexts, dropped, kept, digest, saved_tokens}；没超上限就原样返回。
    """
    rows = list(contexts or [])
    total = len(rows)
    limit = max(4, int(max_turns or 40))
    if total <= limit:
        return {"contexts": rows, "dropped": 0, "kept": total,
                "digest": "", "saved_tokens": 0}
    recent_n = max(2, min(total - 1, int(keep_recent or 16)))
    older = rows[: total - recent_n]
    recent = rows[total - recent_n:]
    important_idx = [index for index, row in enumerate(older) if is_important(row)]
    keep_n = max(0, min(len(important_idx), int(keep_important or 6)))
    keep_set = set(important_idx[-keep_n:]) if keep_n else set()
    kept_older = [row for index, row in enumerate(older) if index in keep_set]
    dropped = [row for index, row in enumerate(older) if index not in keep_set]

    lines: list[str] = []
    for row in dropped:
        text = _content(row)
        if len(text.strip()) < 8:
            continue
        lines.append("· %s：%s" % (_label(_role(row)), _snippet(text)))
        if len(lines) >= max(1, int(digest_lines or 8)):
            break
    dropped_tokens = sum(estimate_tokens(_content(row)) for row in dropped)
    head = "%s %d 轮已省略；留下要紧的几句）" % (MARKER, len(dropped))
    digest = head + ("\n" + "\n".join(lines) if lines else "")
    return {
        "contexts": kept_older + recent,
        "dropped": len(dropped),
        "kept": len(kept_older) + len(recent),
        "digest": digest,
        "saved_tokens": max(0, dropped_tokens - estimate_tokens(digest)),
    }
