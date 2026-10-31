
"""记忆的价值回传与经验结晶（移植 MemOS 的 Reflect2Evolve 思路，MIT）。

上游给了四个层：L1 轨迹 / L2 策略 / L3 世界模型 / 技能。对一个 QQ 上的陪伴人格，
能搬的是它的**反馈闭环**，而不是整条 agent 反思流水线：

  - **反馈**：用户说「对」「不是这样」就是对记忆的一次评价；
  - **价值回传**：评价不只用在那一条上，还按超图邻居（共享关键词）按比例带过去；
  - **经验结晶**：同一话题下一批高价值记忆，压成一条 kind=policy 的「经验」，
    以后检索到它就等于一次想起整组。

价值载体就用记忆自己的 importance（不新增表、不改库结构）：
  - 正向 +0.12 / 负向 −0.18（记错比记对代价大）
  - 邻居按 0.4 传播，且只传一跳
  - 地板 0.05、天花板 1.0
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

#: 正向反馈（先判负向，避免「不是不对」被算成肯定）
POSITIVE_WORDS = ("记住了", "记得对", "没错", "对的", "就是这个", "是这样", "真准", "厉害了", "对的呀")
NEGATIVE_WORDS = ("不对", "错了", "记错", "不是这样", "不是这个", "我没说", "乱说", "才不是", "谁跟你说")

#: 整句就是这几个词时也算反馈（单字「对」只有单独成句才算）
POSITIVE_EXACT = ("对", "嗯对", "是", "是的", "嗯", "好", "行")
NEGATIVE_EXACT = ("错", "不是", "没有")

#: 只对短消息做反馈判定（长句里的「不对」多半是在说别的事）
MAX_FEEDBACK_CHARS = 16
#: 价值调整幅度
POSITIVE_STEP = 0.12
NEGATIVE_STEP = -0.18
#: 邻居传播比例与最少共享关键词数
NEIGHBOR_FACTOR = 0.4
NEIGHBOR_MIN_SHARED = 2
#: 价值上下限
VALUE_FLOOR = 0.05
VALUE_CEIL = 1.0

#: 结晶参数：同一话题至少几条、平均价值多高
CLUSTER_MIN = 3
CLUSTER_MIN_VALUE = 0.8


def detect_feedback(text: str) -> tuple[int, str]:
    """这句话是在肯定还是否定上一条记忆。返回 (符号, 命中词)，没命中是 (0, "")。"""
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw or len(raw) > MAX_FEEDBACK_CHARS:
        return 0, ""
    lowered = raw.lower()
    for word in NEGATIVE_WORDS:
        if word in lowered:
            return -1, word
    for word in POSITIVE_WORDS:
        if word in lowered:
            return 1, word
    if lowered in {word.lower() for word in NEGATIVE_EXACT}:
        return -1, lowered
    if lowered in {word.lower() for word in POSITIVE_EXACT}:
        return 1, lowered
    return 0, ""


def _clamp(value: float) -> float:
    return max(VALUE_FLOOR, min(VALUE_CEIL, float(value)))


def shared_keywords(left: Any, right: Any) -> int:
    a = {str(word).lower() for word in (getattr(left, "keywords", ()) or ())}
    b = {str(word).lower() for word in (getattr(right, "keywords", ()) or ())}
    return len(a & b)


def apply_value(memories: Sequence[Any], targets: Iterable[int], sign: int,
                factor: float = NEIGHBOR_FACTOR) -> dict[int, float]:
    """把一次评价回传到目标记忆和它的邻居，返回 {记忆 id: 新价值}。

    只返回**变了**的那些；调用方拿它去 update_many。
    """
    if sign == 0:
        return {}
    step = POSITIVE_STEP if sign > 0 else NEGATIVE_STEP
    rows = [item for item in memories if getattr(item, "id", 0)]
    wanted = {int(item) for item in targets or () if str(item).strip().lstrip("-").isdigit()}
    if not rows or not wanted:
        return {}

    changes: dict[int, float] = {}
    for memory in rows:
        if int(memory.id) in wanted:
            changes[int(memory.id)] = _clamp(float(memory.importance or 0.5) + step)
    for memory in rows:
        if int(memory.id) in wanted:
            continue
        if any(shared_keywords(memory, seed) >= NEIGHBOR_MIN_SHARED
               for seed in rows if int(seed.id) in wanted):
            changes[int(memory.id)] = _clamp(
                float(memory.importance or 0.5) + step * float(factor)
            )
    # 去掉没真的变的（避免白写库）
    return {
        key: value for key, value in changes.items()
        if abs(value - float(next(
            (item.importance for item in rows if int(item.id) == key), value
        ))) > 1e-6
    }


def clusters(memories: Sequence[Any], min_shared: int = NEIGHBOR_MIN_SHARED) -> list[list[Any]]:
    """按共享关键词把记忆聚成话题簇（只做一跳，够用了）。"""
    rows = [item for item in memories if getattr(item, "id", 0)]
    used: set[int] = set()
    groups: list[list[Any]] = []
    for index, seed in enumerate(rows):
        if int(seed.id) in used:
            continue
        group = [seed]
        for other in rows[index + 1:]:
            if int(other.id) in used:
                continue
            if shared_keywords(seed, other) >= min_shared:
                group.append(other)
        if len(group) > 1:
            used.update(int(item.id) for item in group)
            groups.append(group)
    return groups


def policy_card(group: Sequence[Any]) -> dict[str, Any] | None:
    """把一组记忆压成一张「经验」卡（内容 + 关键词 + 价值）。"""
    rows = [item for item in group if getattr(item, "id", 0)]
    if len(rows) < CLUSTER_MIN:
        return None
    values = [float(getattr(item, "importance", 0) or 0) for item in rows]
    if sum(values) / len(values) < CLUSTER_MIN_VALUE:
        return None
    keywords: list[str] = []
    for item in rows:
        for word in (getattr(item, "keywords", ()) or ()):
            text = str(word).strip()
            if text and text not in keywords:
                keywords.append(text)
    keywords = keywords[:8]
    if not keywords:
        return None
    bodies = [str(getattr(item, "content", "") or "").strip() for item in rows]
    summary = "；".join(body for body in bodies[:3] if body)[:200]
    return {
        "content": "经验（%d 条记忆）：%s" % (len(rows), summary),
        "keywords": tuple(keywords),
        "kind": "policy",
        "importance": min(0.9, round(sum(values) / len(values), 2)),
        "source_ids": [int(item.id) for item in rows],
    }


def crystallize(memories: Sequence[Any],
                existing: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """找出可以结晶的话题簇，返回待写入的经验卡（不落库）。"""
    taken: set[frozenset[str]] = set()
    for item in existing or ():
        words = frozenset(str(word).lower() for word in (getattr(item, "keywords", ()) or ()))
        if words:
            taken.add(words)
    cards: list[dict[str, Any]] = []
    for group in clusters(memories):
        card = policy_card(group)
        if card is None:
            continue
        signature = frozenset(str(word).lower() for word in card["keywords"])
        if signature in taken:
            continue
        taken.add(signature)
        cards.append(card)
    return cards


__all__ = [
    "POSITIVE_WORDS", "NEGATIVE_WORDS", "POSITIVE_EXACT", "NEGATIVE_EXACT",
    "MAX_FEEDBACK_CHARS",
    "POSITIVE_STEP", "NEGATIVE_STEP", "NEIGHBOR_FACTOR", "NEIGHBOR_MIN_SHARED",
    "VALUE_FLOOR", "VALUE_CEIL", "CLUSTER_MIN", "CLUSTER_MIN_VALUE",
    "detect_feedback", "shared_keywords", "apply_value", "clusters",
    "policy_card", "crystallize",
]

