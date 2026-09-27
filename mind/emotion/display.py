"""把情绪状态渲染成用户能看的文字（指令输出）。"""

from __future__ import annotations

import time as _time

from .engine import Settings, Snapshot
from .model import (
    PAD,
    Relation,
    affinity_tier,
    clamp,
    humanize_seconds,
    intensity_of,
)

FILL = "█"
EMPTY = "░"


def bipolar_bar(value: float, width: int = 10) -> str:
    """双极进度条：负值往左填，正值往右填。"""
    half = max(1, width // 2)
    value = clamp(value, -1.0, 1.0)
    filled = int(round(abs(value) * half))
    if value >= 0:
        return EMPTY * half + FILL * filled + EMPTY * (half - filled)
    return EMPTY * (half - filled) + FILL * filled + EMPTY * half


def unipolar_bar(value: float, maximum: float = 100.0, width: int = 10) -> str:
    """单极进度条：0 到 maximum。"""
    if maximum <= 0:
        return EMPTY * width
    ratio = clamp(value / maximum, 0.0, 1.0)
    filled = int(round(ratio * width))
    return FILL * filled + EMPTY * (width - filled)


def _cause_of(snapshot: Snapshot) -> str:
    return snapshot.cause or "没什么特别的"


def mood_panel(snapshot: Snapshot, settings: Settings) -> str:
    """主面板：/情绪 的输出。"""
    who = settings.bot_name or "她"
    emotion = snapshot.emotion
    lines = [
        f"🎭 {who}此刻的心情",
        "",
        f"  {snapshot.proto.emoji} {snapshot.proto.name}",
        f"  强度  {bipolar_bar(snapshot.intensity * 2 - 1)}  {snapshot.level}/5 {snapshot.level_name}",
        "",
        f"  愉悦  {bipolar_bar(emotion.p)}  {emotion.p:+.2f}",
        f"  唤醒  {bipolar_bar(emotion.a)}  {emotion.a:+.2f}",
        f"  掌控  {bipolar_bar(emotion.d)}  {emotion.d:+.2f}",
        "",
        f"  💭 起因　{_cause_of(snapshot)}",
        f"  🌤 心境　{snapshot.mood_proto.emoji} {snapshot.mood_proto.name}"
        f"（{snapshot.mood.p:+.2f} / {snapshot.mood.a:+.2f} / {snapshot.mood.d:+.2f}）",
    ]

    relation = snapshot.relation
    if relation is not None:
        mark = f" · ★{snapshot.special_label}" if snapshot.is_special else ""
        lines.extend(
            [
                "",
                f"  👤 对你的好感度  {relation.affinity:.0f}/100「{snapshot.affinity_title()}」{mark}",
                f"     {unipolar_bar(relation.affinity, 100.0)}",
                f"     熟悉度 {relation.familiarity:.0f}「{snapshot.familiarity_title()}」"
                f" · 累计聊过 {relation.msg_count} 条",
            ]
        )
        if snapshot.idle_seconds >= 60:
            lines.append(f"     上次说话：{humanize_seconds(snapshot.idle_seconds)}")

    lines.extend(["", f"  ✍️ 表达基调　{snapshot.style}"])
    if snapshot.crisis:
        lines.extend(["", "  ⚠️ 当前处于安全优先状态：所有傲娇与玩笑已临时关闭"])
    return "\n".join(lines)


def relation_panel(snapshot: Snapshot, settings: Settings) -> str:
    """只看关系：/好感度 的输出。"""
    relation = snapshot.relation
    if relation is None:
        return "📭 还没有记录到和你的互动。"
    title, icon = affinity_tier(relation.affinity)
    mark = f" · ★{snapshot.special_label}" if snapshot.is_special else ""
    lines = [
        f"{icon} 她对你的印象",
        "",
        f"  好感度  {relation.affinity:.0f}/100「{title}」{mark}",
        f"          {unipolar_bar(relation.affinity, 100.0)}",
        f"  熟悉度  {relation.familiarity:.0f}「{snapshot.familiarity_title()}」",
        f"  累计    {relation.msg_count} 条消息",
    ]
    if snapshot.idle_seconds >= 60:
        lines.append(f"  上次说话 {humanize_seconds(snapshot.idle_seconds)}")
    lines.extend(
        [
            "",
            f"  🎭 她此刻：{snapshot.proto.emoji} {snapshot.proto.name}"
            f"（{snapshot.level}/5 {snapshot.level_name}）",
        ]
    )
    return "\n".join(lines)


def history_panel(snapshot: Snapshot, limit: int = 8) -> str:
    """情绪史：/情绪 历史 的输出。"""
    events = list(snapshot.history)
    if not events:
        return "📭 还没有记录到任何情绪波动，她的心情一直很平。"

    now = snapshot.taken_at or _time.time()
    lines = ["📜 最近的情绪波动（由近到远）", ""]
    for event in reversed(events[-limit:]):
        delta = (event.delta[0] + event.delta[1] + event.delta[2]) / 3.0
        arrow = "↗" if delta >= 0 else "↘"
        source_tag = {"self": "她自己", "idle": "没人理她", "manual": "手动"}.get(event.source, "")
        suffix = f"（{source_tag}）" if source_tag else ""
        lines.append(
            f"  {humanize_seconds(max(0.0, now - event.t)):<10}"
            f"{event.emoji} {event.label:<4} {arrow}{abs(delta):.2f}  {event.cause}{suffix}"
        )
    return "\n".join(lines)


def rank_panel(relations: list[Relation], settings: Settings) -> str:
    """好感度排行：/情绪 排行 的输出。"""
    if not relations:
        return "📭 还没有记录到任何人的互动。"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 好感度排行", ""]
    for index, relation in enumerate(relations):
        title, icon = affinity_tier(relation.affinity)
        badge = medals[index] if index < len(medals) else f"{index + 1:>2}."
        name = relation.name or relation.uid
        star = " ★" if relation.special else ""
        lines.append(
            f"  {badge} {icon} {name}{star}　{relation.affinity:.0f}/100「{title}」"
            f"　{unipolar_bar(relation.affinity, 100.0)}"
        )
        lines.append(f"      熟悉度 {relation.familiarity:.0f} · 聊过 {relation.msg_count} 条")
    return "\n".join(lines)


def help_panel(prefix: str = "/") -> str:
    return "\n".join(
        [
            "🎭 情绪系统 · 指令一览",
            "",
            f"  {prefix}情绪　　　　　　看看她此刻什么心情",
            f"  {prefix}情绪 历史　　　　最近的情绪波动记录",
            f"  {prefix}情绪 排行　　　　本会话好感度排行",
            f"  {prefix}情绪 详细　　　　调试用：数值 / 会话 / 关系原始信息",
            f"  {prefix}情绪 重置　　　　【管理员】把情绪与心境归零",
            f"  {prefix}情绪 设定 p a d　【管理员】手动设定情绪（-1 ~ 1）",
            f"  {prefix}好感度　　　　　　只看和你的关系（别名：{prefix}好感）",
            "",
            "  数值说明：愉悦(P) / 唤醒(A) / 掌控(D)，取值 -1 ~ 1",
        ]
    )


def detail_panel(snapshot: Snapshot, session_key: str, session_count: int) -> str:
    """调试用：/情绪 详细 的输出。"""
    emotion: PAD = snapshot.emotion
    lines = [
        "🔬 调试详情",
        "",
        f"  会话　　　{session_key}",
        f"  已知会话　{session_count} 个",
        f"  即时情绪　{emotion.pretty()}　强度 {intensity_of(emotion):.3f}",
        f"  心境　　　{snapshot.mood.pretty()}",
        f"  气质基线　{snapshot.baseline.pretty()}",
        f"  情绪标签　{snapshot.proto.key}（{snapshot.proto.name}）",
        f"  几何最近　{snapshot.raw_proto.key}（{snapshot.raw_proto.name}）",
        f"  离线时长　{humanize_seconds(snapshot.idle_seconds)}",
        f"  危机状态　{'是' if snapshot.crisis else '否'}",
        f"  历史条数　{len(snapshot.history)}",
    ]
    if snapshot.relation is not None:
        relation = snapshot.relation
        lines.append(
            f"  关系　　　uid={relation.uid} name={relation.name or '-'} "
            f"aff={relation.affinity:.2f} fam={relation.familiarity:.2f} msg={relation.msg_count}"
        )
    return "\n".join(lines)

