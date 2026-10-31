
"""统一记忆检索：四力打分 + BM25 + 超图传播（移植 dsh-unified-memory，MIT）。

上游那套的核心不是存储，而是**选择**：记忆不是"存了就行"，而是每轮都要重新算
「这一条现在值不值得被想起来」。四个力互相拉扯：

  relevance（相关）  BM25 / 向量 / 图上邻居
  importance（重要） 长期价值（修正、决定、约定）
  recency（新鲜）   指数衰减，但**半衰期由重要度决定**
  strength（强度）  被真正用过的次数

另外三条来自上游、这次一并补上的：
  - **分层**：pinned/档案算 L3（永不过期），fact/preference/relation 算 L2，
    event 之类算 L1；L3 每轮都在，L1 更容易掉队。
  - **BM25**：比字符二元组更会挑「罕见但命中」的词。
  - **超图传播**：两条记忆共享两个以上关键词，就算同一个话题的邻居，命中一条时邻居加分。

不做的：dispatch receipt 状态机（那是 agent 之间派活用的，AstrBot 没有这个场景）。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Sequence

#: 分层
LAYER_AXIOM = "L3"     # 永不过期（pinned / 常驻档案）
LAYER_DURABLE = "L2"   # 长期事实
LAYER_EVENT = "L1"     # 一次性经历

#: 上游的实测分档：重要度决定半衰期
HALF_LIFE_HIGH = 180.0
HALF_LIFE_MID = 60.0
HALF_LIFE_LOW = 21.0

#: 强度曲线的尺度：被用 3 次就到 63%
STRENGTH_SCALE = 3.0

#: 超图传播：至少共享几个关键词才算邻居
GRAPH_MIN_SHARED = 2


def layer_of(memory: Any) -> str:
    """这条记忆属于哪一层。"""
    if getattr(memory, "pinned", False):
        return LAYER_AXIOM
    kind = str(getattr(memory, "kind", "") or "")
    if kind in ("fact", "preference", "relation"):
        return LAYER_DURABLE
    return LAYER_EVENT


def half_life_days(importance: float, layer: str) -> float | None:
    """半衰期：档案永不过期，其余按重要度分三档（没给配置时用上游默认）。"""
    if layer == LAYER_AXIOM:
        return None
    value = max(0.0, min(1.0, float(importance or 0.0)))
    if value >= 0.80:
        return HALF_LIFE_HIGH
    if value >= 0.55:
        return HALF_LIFE_MID
    return HALF_LIFE_LOW


def recency(memory: Any, now: float, half_life: float | None) -> float:
    """0.5 ** (过了几天 / 半衰期)。没半衰期就是 1.0。"""
    if half_life is None or half_life <= 0:
        return 1.0
    reference = (
        float(getattr(memory, "last_hit", 0.0) or 0.0)
        or float(getattr(memory, "updated_at", 0.0) or 0.0)
        or float(getattr(memory, "created_at", 0.0) or 0.0)
        or now
    )
    lapse = max(0.0, (float(now) - reference) / 86400.0)
    return float(0.5 ** (lapse / float(half_life)))


def strength(hit_count: Any) -> float:
    """被用过几次 → 0..1（3 次到 63%，越多越接近 1，不会爆）。"""
    try:
        count = max(0.0, float(hit_count or 0))
    except (TypeError, ValueError):
        count = 0.0
    return float(1.0 - math.exp(-count / STRENGTH_SCALE))


def bm25(query_tokens: Iterable[str], docs: Sequence[Sequence[str]],
         k1: float = 1.5, b: float = 0.75) -> list[float]:
    """标准 BM25，返回每条文档的原始分（未归一）。"""
    words = {str(token) for token in (query_tokens or ()) if str(token).strip()}
    rows = [list(doc or ()) for doc in docs]
    if not words or not rows:
        return [0.0 for _ in rows]
    total = len(rows)
    average = sum(len(doc) for doc in rows) / max(1, total)
    document_frequency = Counter()
    for doc in rows:
        for token in set(doc):
            document_frequency[token] += 1
    scores: list[float] = []
    for doc in rows:
        term_frequency = Counter(doc)
        length = len(doc)
        score = 0.0
        for token in words:
            freq = term_frequency.get(token, 0)
            if not freq:
                continue
            seen = document_frequency.get(token, 0)
            idf = math.log(1.0 + (total - seen + 0.5) / (seen + 0.5))
            denominator = freq + k1 * (1.0 - b + b * length / max(1e-9, average))
            score += idf * (freq * (k1 + 1.0)) / max(1e-9, denominator)
        scores.append(score)
    return scores


def normalize_bm25(score: float) -> float:
    """压到 0..1，方便和别的力加权。"""
    value = max(0.0, float(score))
    return value / (1.0 + value)


def graph_bonus(seeds: Iterable[tuple[int, float]], memories: Sequence[Any],
                factor: float = 0.25,
                min_shared: int = GRAPH_MIN_SHARED) -> dict[int, float]:
    """超图传播：命中一条，同话题的邻居也沾光（只算一跳）。"""
    memo = list(memories)
    if not memo:
        return {}
    keyword_sets: dict[int, set[str]] = {}
    for item in memo:
        key = int(getattr(item, "id", 0) or 0)
        keyword_sets[key] = {str(word).lower() for word in (getattr(item, "keywords", ()) or ())}
    bonus: dict[int, float] = {}
    for seed_id, seed_score in seeds or ():
        seed_keywords = keyword_sets.get(int(seed_id), set())
        if not seed_keywords or seed_score <= 0:
            continue
        for item in memo:
            other = int(getattr(item, "id", 0) or 0)
            if other == int(seed_id):
                continue
            shared = len(seed_keywords & keyword_sets.get(other, set()))
            if shared < max(1, int(min_shared)):
                continue
            gain = float(seed_score) * float(factor) * min(1.0, shared / 4.0)
            if gain > bonus.get(other, 0.0):
                bonus[other] = gain
    return bonus


def rank(memories: Sequence[Any], *, query_tokens: Iterable[str], query_text: str,
         now: float, weights: dict[str, float], k1: float = 1.5, b: float = 0.75,
         lexical_of: Any = None, semantic_of: Any = None) -> list[tuple[Any, float, dict[str, float]]]:
    """四力打分 + BM25 + 超图传播，返回 [(记忆, 总分, 明细)]（未排序）。

    lexical_of / semantic_of 是可选的取分钩子（老公式里的词法/向量分），
    传进来就取两者较大值，保持与旧版一致的"关键词换了说法也能想起来"。
    """
    memo = list(memories)
    if not memo:
        return []
    docs = [list(str(token) for token in _tokens(item)) for item in memo]
    raw_bm25 = bm25(query_tokens, docs, k1=k1, b=b)
    scores: list[tuple[Any, float, dict[str, float]]] = []
    relevance_rows: list[tuple[int, float]] = []
    for index, item in enumerate(memo):
        relevance = normalize_bm25(raw_bm25[index])
        extra = float(lexical_of(item)) if callable(lexical_of) else 0.0
        relevance = max(relevance, min(1.0, extra))
        layer = layer_of(item)
        life = half_life_days(float(getattr(item, "importance", 0.0) or 0.0), layer)
        details = {
            "relevance": relevance,
            "bm25": raw_bm25[index],
            "importance": max(0.0, min(1.0, float(getattr(item, "importance", 0.0) or 0.0))),
            "recency": recency(item, now, life),
            "strength": strength(getattr(item, "hit_count", 0)),
            "half_life": float(life) if life is not None else 0.0,
            "layer": layer,
            "graph": 0.0,
        }
        if callable(semantic_of):
            details["semantic"] = float(semantic_of(item))
        scores.append((item, 0.0, details))
        relevance_rows.append((int(getattr(item, "id", index) or index), relevance))

    bonus = graph_bonus(relevance_rows, memo, factor=float(weights.get("graph", 0.0)) or 0.0)
    total_weight = sum(max(0.0, float(value)) for key, value in weights.items()
                       if key in ("relevance", "importance", "recency", "strength", "graph"))
    total_weight = total_weight or 1.0
    weighted: list[tuple[Any, float, dict[str, float]]] = []
    for item, _zero, details in scores:
        graph = float(bonus.get(int(getattr(item, "id", 0) or 0), 0.0))
        details["graph"] = graph
        total = (
            max(0.0, float(weights.get("relevance", 0.0))) * details["relevance"]
            + max(0.0, float(weights.get("importance", 0.0))) * details["importance"]
            + max(0.0, float(weights.get("recency", 0.0))) * details["recency"]
            + max(0.0, float(weights.get("strength", 0.0))) * details["strength"]
            + max(0.0, float(weights.get("graph", 0.0))) * graph
            + max(0.0, float(weights.get("semantic", 0.0))) * details.get("semantic", 0.0)
        ) / total_weight
        weighted.append((item, total, details))
    return weighted


def _tokens(memory: Any) -> list[str]:
    """给 BM25 用的词元：正文 + 关键词 + 主体（字符二元组，和旧版同一套）。"""
    from .model import tokenize

    return list(tokenize(memory.searchable_text()))


__all__ = [
    "LAYER_AXIOM", "LAYER_DURABLE", "LAYER_EVENT",
    "HALF_LIFE_HIGH", "HALF_LIFE_MID", "HALF_LIFE_LOW",
    "STRENGTH_SCALE", "GRAPH_MIN_SHARED",
    "layer_of", "half_life_days", "recency", "strength",
    "bm25", "normalize_bm25", "graph_bonus", "rank",
]

