"""面板数据组装：把内核状态整理成前端能直接画的 JSON。

曲线是**服务端解析重建**的，不是把采样点直连起来画
--------------------------------------------------
情绪在两次事件之间是按指数衰减走的，把采样点用直线连起来会失真。
但我们也不需要为此存密集的点：只要拿到"每个事件之后的情绪值"，
就能用半衰期公式把中间任意时刻的值算出来。

    value(t) = baseline + (v0 - baseline) × 0.5 ^ ((t - t0) / half_life)

所以前端拿到的是已经算好的、平滑的曲线，而不是原始噪声点。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from .emotion.model import (
    PAD,
    affinity_tier,
    classify,
    familiarity_title,
    intensity_level,
    intensity_of,
)
from .memory.model import humanize_ago
from .samples import Sample

MAX_CURVE_POINTS = 900


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def split_session_key(key: str) -> tuple[str, str, str]:
    """把 unified_msg_origin 拆成 (平台, 类型, 会话 ID)。"""
    parts = str(key or "").split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], ":".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", str(key or "")


def describe_session(key: str) -> str:
    platform, kind, ident = split_session_key(key)
    lowered = kind.lower()
    if "group" in lowered:
        label = "群聊"
    elif "friend" in lowered or "private" in lowered:
        label = "私聊"
    elif kind:
        label = kind
    else:
        label = "会话"
    return f"{label} · {ident}" if ident else label


def is_group(key: str) -> bool:
    from .memory.privacy import is_group_session

    return is_group_session(key)


def session_entries(
    *,
    engine: Any,
    sample_store: Any,
    memory_store: Any,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """所有已知会话的摘要列表，供面板顶部切换。"""
    now = time.time() if now is None else now
    keys: dict[str, dict[str, Any]] = {}

    def ensure(key: str) -> dict[str, Any]:
        entry = keys.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": ("👥 " if is_group(key) else "💬 ") + describe_session(key),
                "group": is_group(key),
                "samples": 0,
                "users": 0,
                "messages": 0,
                "last_active": 0.0,
                "emotion": None,
                "emotion_emoji": "",
                "emotion_level": 0,
            }
            keys[key] = entry
        return entry

    for key, session in getattr(engine, "sessions", {}).items():
        entry = ensure(key)
        entry["users"] = len(session.users)
        entry["messages"] = sum(int(r.msg_count) for r in session.users.values())
        entry["last_active"] = max(entry["last_active"], session.last_interaction, session.last_tick)
        proto = classify(session.emotion)
        entry["emotion"] = proto.name
        entry["emotion_emoji"] = proto.emoji
        entry["emotion_level"] = intensity_level(session.emotion)[0]

    # 有记忆、但还没产生情绪状态的群/会话也要列出来，否则面板上根本选不到它
    if memory_store is not None:
        try:
            for key in memory_store.distinct_sessions():
                ensure(key)
        except Exception:
            pass

    # 采样表里也留着会话 id。记忆库万一不可用，至少这些会话还能选到，
    # 否则面板顶部的会话列表会凭空少掉一群群聊。
    if sample_store is not None:
        try:
            for key in sample_store.sessions():
                ensure(key)
        except Exception:
            pass
    for key in sample_store.sessions() if sample_store is not None else []:
        entry = ensure(key)
        entry["samples"] = sample_store.count(key)
        latest = sample_store.latest(key)
        if latest is not None:
            entry["last_active"] = max(entry["last_active"], latest.t)
            if entry["emotion"] is None:
                proto = classify(latest.pad())
                entry["emotion"] = proto.name
                entry["emotion_emoji"] = proto.emoji
                entry["emotion_level"] = intensity_level(latest.pad())[0]

    for key in keys:
        entry = keys[key]
        entry["last_active_ago"] = humanize_ago(now - entry["last_active"]) if entry["last_active"] else "-"

    return sorted(keys.values(), key=lambda item: item["last_active"], reverse=True)


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------


def build_curve(
    samples: Sequence[Sample],
    *,
    baseline: PAD,
    half_life: float,
    since: float,
    until: float,
    points: int = 400,
) -> list[dict[str, float]]:
    """按指数衰减公式重建区间内的平滑曲线。"""
    points = max(2, min(int(points), MAX_CURVE_POINTS))
    if until <= since:
        return []
    ordered = sorted((s for s in samples if s.t <= until), key=lambda s: s.t)
    if not ordered:
        # 一条采样点都没有：诚实地返回空，
        # 否则前端会画出一条贴在基线上的假直线，看起来像"她一直很平静"。
        return []
    step = (until - since) / (points - 1)
    curve: list[dict[str, float]] = []
    index = 0
    for i in range(points):
        t = since + i * step
        while index < len(ordered) - 1 and ordered[index + 1].t <= t:
            index += 1
        current = ordered[index]
        if current.t > t or half_life <= 0:
            value = baseline if current.t > t else current.pad()
        else:
            value = current.pad().decay_toward(baseline, half_life, max(0.0, t - current.t))
        curve.append(
            {"t": round(t, 2), "p": round(value.p, 4), "a": round(value.a, 4), "d": round(value.d, 4)}
        )
    return curve


def build_curve_from_history(
    history: list[Any],
    *,
    current: PAD,
    baseline: PAD,
    half_life: float,
    since: float,
    until: float,
    points: int = 400,
) -> list[dict[str, float]]:
    """采样表是空的时候，用情绪事件倒推一条近似曲线。

    每个事件记的是"这次变化了多少"（delta）。从**现在的情绪**往回减，
    就能还原出每个事件刚发生完时的情绪值；再用衰减公式把这些点铺开。

    这是近似值（两次事件之间的衰减没有精确重建），但比"什么都没有"有用得多，
    尤其对刚装上插件、采样点还没攒起来的会话。
    """
    events = [e for e in (history or []) if getattr(e, "t", 0) > 0]
    if not events:
        return []
    events = sorted(events[-200:], key=lambda e: e.t)

    value = PAD.from_any(current).clamped()
    marks: list[tuple[float, PAD]] = [(float(until), value)]
    for event in reversed(events):
        delta = event.delta or (0.0, 0.0, 0.0)
        value = PAD(
            value.p - delta[0], value.a - delta[1], value.d - delta[2]
        ).clamped()
        marks.append((float(event.t), value))
    marks.reverse()

    samples = [
        Sample(t=t, p=pad.p, a=pad.a, d=pad.d, kind="estimated")
        for t, pad in marks
        if t <= until
    ]
    if not samples:
        return []
    return build_curve(
        samples, baseline=baseline, half_life=half_life,
        since=since, until=until, points=points,
    )


def emotion_payload(
    *,
    engine: Any,
    sample_store: Any,
    session_key: str,
    hours: float,
    max_points: int,
    now: float | None = None,
) -> dict[str, Any]:
    """情绪页需要的全部数据。"""
    now = time.time() if now is None else now
    settings = engine.settings
    session = engine.resolve(session_key, now)
    relation = None
    latest_sender = ""
    if session.users:
        latest_sender = max(session.users.values(), key=lambda r: r.last_seen or 0).uid
        relation = session.users.get(latest_sender)

    snapshot = engine.snapshot(session, relation, now)
    since = now - max(3600.0, hours * 3600.0)
    samples = sample_store.since(session_key, since, limit=max_points) if sample_store is not None else []

    curve = build_curve(
        samples,
        baseline=settings.baseline,
        half_life=settings.emotion_half_life,
        since=since,
        until=now,
        points=min(max_points, MAX_CURVE_POINTS),
    )
    curve_source = "samples" if curve else "none"
    if not curve:
        # 采样表还是空的：用情绪事件倒推一条近似曲线，
        # 免得刚装上插件的人只看到一片空白
        curve = build_curve_from_history(
            session.history,
            current=session.emotion,
            baseline=settings.baseline,
            half_life=settings.emotion_half_life,
            since=since,
            until=now,
            points=min(max_points, MAX_CURVE_POINTS),
        )
        if curve:
            curve_source = "history"

    events = []
    for event in session.history[-120:]:
        events.append(
            {
                "t": round(event.t, 2),
                "label": event.label,
                "emoji": event.emoji,
                "cause": event.cause,
                "source": event.source,
                "delta": [round(v, 3) for v in event.delta],
            }
        )

    level, level_name = intensity_level(session.emotion)
    mood_proto = classify(session.mood)

    return {
        "session": {
            "key": session_key,
            "label": describe_session(session_key),
            "group": is_group(session_key),
        },
        "now": now,
        "since": since,
        "hours": hours,
        "current": {
            "p": round(session.emotion.p, 4),
            "a": round(session.emotion.a, 4),
            "d": round(session.emotion.d, 4),
            "label": snapshot.proto.name,
            "emoji": snapshot.proto.emoji,
            "level": level,
            "level_name": level_name,
            "intensity": round(intensity_of(session.emotion), 4),
            "style": snapshot.style,
            "cause": snapshot.cause,
            "crisis": snapshot.crisis,
        },
        "mood": {
            "p": round(session.mood.p, 4),
            "a": round(session.mood.a, 4),
            "d": round(session.mood.d, 4),
            "label": mood_proto.name,
            "emoji": mood_proto.emoji,
        },
        "baseline": {
            "p": settings.baseline.p,
            "a": settings.baseline.a,
            "d": settings.baseline.d,
        },
        "params": {
            "emotion_half_life": settings.emotion_half_life,
            "mood_half_life": settings.mood_half_life,
            "preset": settings.preset_key,
            "bot_name": settings.bot_name,
        },
        "curve": curve,
        "curve_source": curve_source,
        "sample_count": sample_store.count(session_key) if sample_store is not None else 0,
        "sample_total": sample_store.count() if sample_store is not None else 0,
        "history_count": len(session.history),
        "samples": [s.to_dict() for s in samples[-600:]],
        "events": events,
        "users": [
            {
                "uid": r.uid,
                "name": r.name,
                "affinity": round(r.affinity, 2),
                "familiarity": round(r.familiarity, 2),
                "msg_count": r.msg_count,
                "special": r.special,
                "tier": affinity_tier(r.affinity)[0],
                "familiarity_title": familiarity_title(r.familiarity),
                "last_seen_ago": humanize_ago(now - r.last_seen) if r.last_seen else "-",
            }
            for r in sorted(session.users.values(), key=lambda x: -x.affinity)[:20]
        ],
        "idle_seconds": round(max(0.0, now - session.last_interaction), 2),
    }


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


def memory_to_dict(memory: Any, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {
        "id": memory.id,
        "content": memory.content,
        "keywords": list(memory.keywords),
        "kind": memory.kind,
        "subject": memory.subject,
        "scope": memory.scope,
        "session_id": memory.session_id,
        "owner_id": memory.owner_id,
        "importance": round(memory.importance, 3),
        "pinned": bool(memory.pinned),
        "sensitive": bool(memory.sensitive),
        "sensitive_reasons": list(memory.sensitive_reasons),
        "source": memory.source,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "hit_count": memory.hit_count,
        "ago": humanize_ago(now - (memory.updated_at or memory.created_at or now)),
        "embedded": memory.embedding is not None,
    }


def memory_payload(
    *,
    store: Any,
    query: str = "",
    scope: str = "",
    kind: str = "",
    pinned: Any = None,
    sensitive: Any = None,
    limit: int = 50,
    offset: int = 0,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    items, total = store.query_page(
        q=query,
        scope=scope,
        kind=kind,
        pinned=pinned,
        sensitive=sensitive,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [memory_to_dict(m, now) for m in items],
        "total": total,
        "offset": offset,
        "limit": limit,
        "stats": store.stats(),
    }


__all__ = [
    "MAX_CURVE_POINTS",
    "build_curve_from_history",
    "build_curve",
    "describe_session",
    "emotion_payload",
    "is_group",
    "memory_payload",
    "memory_to_dict",
    "session_entries",
    "split_session_key",
]

# ---------------------------------------------------------------------------
# 理论基础 + 词表命中（面板「理论」页签）
# ---------------------------------------------------------------------------
#: 三块理论出处。面板上照实写清楚，也方便别人照着查。
THEORY_SOURCES: tuple[dict[str, str], ...] = (
    {
        "key": "pad",
        "title": "PAD 三维情感模型",
        "from": "Mehrabian & Russell, 1974",
        "use": "愉悦 P / 唤醒 A / 支配 D 三个连续维度，把情绪变成可以计算、可以衰减的向量",
        "how": "本插件用它当**唯一的情绪状态**：规则命中和词表命中都换算成 PAD 偏移，"
               "再按半衰期往基线衰减。曲线、心境、强度分级全是它的衍生。",
    },
    {
        "key": "plutchik",
        "title": "Plutchik 情绪轮",
        "from": "Robert Plutchik, 1980",
        "use": "8 条基本情绪轴、强度分层、对立关系（喜↔悲、信任↔厌恶、惧↔怒、惊↔期待）",
        "how": "本插件的 23 个情绪原型都归属到 8 条轴上；强度分层用 PAD 向量长度换算成 1~5 级；"
               "对立轴用来给面板上的关系图定位，也用于「此刻情绪」的对比说明。",
    },
    {
        "key": "lexicon",
        "title": "中文情感词汇本体库",
        "from": "徐琳宏、林鸿飞等《情感词汇本体的构造》，大连理工大学，2008",
        "use": "7 大类 21 小类，每个词带情感强度（1/3/5/7/9）与极性（中性/褒义/贬义/兼有）",
        "how": "手写规则一条都没命中时，用它兜底评估情绪 —— 这是「开放词表」相对正则穷举的优势。"
               "冲击力明显弱于手写规则，只负责让她对日常表达有反应。",
    },
)


def theory_payload(
    lexicon_store: Any = None,
    *,
    enabled: bool = True,
    gain: float = 0.25,
    recent_limit: int = 30,
    top_limit: int = 24,
    window_hours: float = 72.0,
    now: float | None = None,
) -> dict[str, Any]:
    """把三块理论 + 词表命中情况整理成面板要的数据。"""
    from .emotion import lexicon as lexicon_mod
    from .emotion.model import PLUTCHIK, PLUTCHIK_AXES, PROTOTYPES

    stamp = float(now if now is not None else time.time())
    since = stamp - max(1.0, float(window_hours)) * 3600.0

    proto_by_key = {proto.key: proto for proto in PROTOTYPES}

    # -- 7 大类 21 小类 --------------------------------------------------
    groups: list[dict[str, Any]] = []
    for family_name, (family_key, family_note) in lexicon_mod.FAMILIES.items():
        subs = []
        for sub in lexicon_mod.SUBCATEGORIES:
            if sub.family != family_name:
                continue
            words = lexicon_mod.words_of(sub.code)
            proto = proto_by_key.get(sub.proto)
            subs.append({
                "code": sub.code,
                "name": sub.name,
                "polarity": sub.polarity,
                "polarity_label": lexicon_mod.POLARITY_LABELS.get(sub.polarity, ""),
                "pad": [round(v, 2) for v in sub.pad],
                "proto": sub.proto,
                "proto_name": proto.name if proto else sub.proto,
                "proto_emoji": proto.emoji if proto else "",
                "opposite": sub.opposite,
                "opposite_name": (
                    lexicon_mod.SUBCATEGORY_BY_CODE[sub.opposite].name
                    if sub.opposite in lexicon_mod.SUBCATEGORY_BY_CODE else ""
                ),
                "outward": bool(sub.outward),
                "word_count": len(words),
                "samples": [word for word, _strength in words[:6]],
            })
        groups.append({
            "family": family_name,
            "key": family_key,
            "note": family_note,
            "subcategories": subs,
            "word_count": sum(item["word_count"] for item in subs),
        })

    # -- Plutchik 8 轴 ---------------------------------------------------
    family_buckets: dict[str, list[dict[str, Any]]] = {}
    for proto in PROTOTYPES:
        if not proto.family:
            continue
        family_buckets.setdefault(proto.family, []).append({
            "key": proto.key,
            "name": proto.name,
            "emoji": proto.emoji,
            "pad": [round(v, 2) for v in proto.pad],
        })
    plutchik_families = []
    for key, (name, emoji, opposite) in PLUTCHIK.items():
        plutchik_families.append({
            "key": key,
            "name": name,
            "emoji": emoji,
            "opposite": opposite,
            "opposite_name": PLUTCHIK.get(opposite, ("", "", ""))[0],
            "prototypes": family_buckets.get(key, []),
        })

    payload: dict[str, Any] = {
        "theory": [dict(item) for item in THEORY_SOURCES],
        "lexicon": {
            "enabled": bool(enabled),
            "gain": float(gain),
            "size": lexicon_mod.vocabulary_size(),
            "family_count": len(lexicon_mod.FAMILIES),
            "subcategory_count": len(lexicon_mod.SUBCATEGORIES),
            "intensity_steps": list(lexicon_mod.INTENSITY_STEPS),
            "polarity_labels": dict(lexicon_mod.POLARITY_LABELS),
            "fallback_only": True,
            "categories": groups,
            "default_gain": lexicon_mod.DEFAULT_GAIN,
            "max_delta": lexicon_mod.MAX_DELTA,
        },
        "plutchik": {"families": plutchik_families, "axes": [list(a) for a in PLUTCHIK_AXES]},
        "prototypes": [
            {
                "key": proto.key,
                "name": proto.name,
                "emoji": proto.emoji,
                "family": proto.family,
                "family_name": PLUTCHIK.get(proto.family, ("（中性）", "", ""))[0] if proto.family else "中性",
                "pad": [round(v, 2) for v in proto.pad],
            }
            for proto in PROTOTYPES
        ],
        "window_hours": float(window_hours),
        "hits": {"total": 0, "window": 0, "top_words": [], "recent": []},
    }

    if lexicon_store is not None:
        try:
            recent = [hit.to_dict() for hit in lexicon_store.recent(recent_limit)]
            payload["hits"] = {
                "total": int(lexicon_store.count(0.0)),
                "window": int(lexicon_store.count(since)),
                "top_words": lexicon_store.top_words(since, top_limit),
                "recent": recent,
            }
        except Exception as exc:  # noqa: BLE001 - 面板数据出问题不能影响聊天
            payload["hits"]["error"] = str(exc)
    return payload
