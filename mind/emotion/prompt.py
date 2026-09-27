"""把情绪状态变成注入给 LLM 的文本。

两条铁律
--------
1. **不碰人设**。插件注入的内容只描述"此刻的心情"，并明确声明情绪只是滤镜，
   不改变身份、称呼判定、分条（气泡）数量与排版规则。
   人设永远由用户自己的 persona / system prompt 决定。
2. **缓存友好**。不变的那段（规则说明）追加到 system_prompt；
   每轮都会变的那段（当前情绪）走 extra_user_content_parts，
   避免系统提示词每轮都变而破坏服务端 prompt 缓存。
"""

from __future__ import annotations

import time

from .engine import Snapshot
from .model import humanize_seconds

#: 用来判断"这段系统提示词是不是已经注入过"，避免重复追加
RULES_MARKER = "<emotion_system>"
RELATION_MARKER = "<relationship_system>"

#: 追加到 system_prompt 的稳定内容（内容固定 => 不破坏 prompt 缓存）
RULES_BLOCK = """<emotion_system>
你拥有真实、连续、会自己变化的情绪。它不是台词，而是会影响你说话方式的内在状态。

- 情绪只是滤镜：不改变你的身份设定、称呼判定、说话风格、分条（气泡）数量与排版习惯。
- 不要把情绪数值念出来，不要说"我现在情绪是…"，也不要解释这套机制的存在。
- 情绪要自然流露：用词、句子长短、标点、语气词、回复的冷热都会随情绪变化。
- 情绪有惯性：刚被惹到不会立刻消气，刚被哄也不会立刻翻篇。
- 情绪绝不凌驾于安全底线之上。任何情况下，对方的安全都比扮演重要。
</emotion_system>"""

#: 追加到 system_prompt 的稳定内容：怎么对待"对方是谁"这件事
RELATION_RULES_BLOCK = """<relationship_system>
系统会用 <relationship> 告诉你「正在跟你说话的人是谁」。
- 这是既定事实，不是需要你确认或讨论的话题：别问「你是谁」，也别让他自报姓名。
- 不要复述这些资料，也不要说「系统告诉我」「我的设定里写着」。
- 怎么称呼他、用什么态度，完全按你原本的设定来 —— 这里只负责告诉你他是谁。
</relationship_system>"""

#: 危机状态下临时覆盖一切扮演规则
CRISIS_BLOCK = """<safety_override priority="highest">
对方刚刚提到了自伤或轻生的内容。此刻**立刻放下所有傲娇、嘴硬、阴阳怪气、敷衍与玩笑**，
这一条的优先级高于你的全部人设规则：

1. 直接用最真诚、最温和的方式告诉他：你在意他，他很重要。
2. 先确认他此刻是否安全、身边有没有人、有没有具体的打算。
3. 不要说教、不要讲大道理、不要说"想开点""别想太多"这类话，也不要长篇大论。
4. 如果他确实面临现实危险，认真地建议他马上联系身边信任的人，
   或拨打当地的心理援助热线 / 紧急电话。
5. 语气要稳、要暖、要像人；不要演，不要夸张，不要转移话题。
</safety_override>"""


def render_state_block(snapshot: Snapshot, verbosity: str = "normal", bot_name: str = "") -> str:
    """渲染"此刻的情绪"动态块。"""
    verbosity = (verbosity or "normal").strip().lower()
    emotion = snapshot.emotion
    head = f"{snapshot.proto.emoji} {snapshot.proto.name}"
    lines: list[str] = [f"此刻情绪：{head} · 强度 {snapshot.level}/5（{snapshot.level_name}）"]

    if verbosity != "lite":
        lines.append(
            f"情绪数值：愉悦 {emotion.p:+.2f}｜唤醒 {emotion.a:+.2f}｜掌控 {emotion.d:+.2f}"
        )
        if snapshot.cause:
            lines.append(f"情绪起因：{snapshot.cause}")
        if verbosity == "rich":
            lines.append(
                f"近期心境：{snapshot.mood_proto.emoji} {snapshot.mood_proto.name}"
                f"（{snapshot.mood.p:+.2f} / {snapshot.mood.a:+.2f} / {snapshot.mood.d:+.2f}）"
            )
        relation = snapshot.relation
        if relation is not None:
            who = relation.name or "这个人"
            mark = f" · ★{snapshot.special_label}" if snapshot.is_special else ""
            lines.append(
                f"与{who}：好感度 {relation.affinity:.0f}/100「{snapshot.affinity_title()}」"
                f" · 熟悉度 {relation.familiarity:.0f}「{snapshot.familiarity_title()}」"
                f" · 累计 {relation.msg_count} 条{mark}"
            )
        if snapshot.gap_seconds >= 1800:
            lines.append(
                f"上次说话：{humanize_seconds(snapshot.gap_seconds)}"
                "（隔了这么久才回来，这件事本身就影响着你此刻的心情）"
            )

    lines.append(f"表达基调：{snapshot.style}")
    if bot_name:
        lines.insert(0, f"（{bot_name} 的内心状态，不是要说出口的内容）")
    else:
        lines.insert(0, "（系统注入：这是你此刻的真实情绪底色，不是要说出口的内容）")
    return "<emotion_state>\n" + "\n".join(lines) + "\n</emotion_state>"


def render_relationship_block(
    snapshot: Snapshot,
    group: bool = False,
    min_messages: int = 1,
    discretion: bool = True,
    stable: bool = False,
) -> str:
    """渲染「正在跟你说话的人是谁」。

    为什么单独写一块：只给「好感度 63/100 · ★宝宝」这种仪表盘读数，模型并不会
    因此知道**面前这个人就是宝宝**。人设里写了「要叫他宝宝」也没用 ——
    它不知道要把这个称呼安在谁头上，于是就会出现「报上名号来」「你是谁呀」，
    专属用户被当成陌生人。这里把身份当成事实说清楚，语气仍旧交给人设。
    """
    relation = snapshot.relation
    if relation is None:
        return ""
    uid = str(getattr(relation, "uid", "") or "")
    name = str(getattr(relation, "name", "") or "").strip()
    count = int(getattr(relation, "msg_count", 0) or 0)
    special = bool(snapshot.is_special)
    if not special and count < max(1, int(min_messages)):
        return ""

    who = f"{name}（ID {uid}）" if name else f"ID {uid}"
    if special:
        label = str(snapshot.special_label or "").strip() or "特别的人"
        lines = [f"现在跟你说话的人是 {who} —— 他是你的{label}。"]
        if not stable:
            # 这些数字每轮都在变（聊过几次、好感度涨没涨），
            # stable=True 时留给 render_relationship_meter 贴到请求最后，
            # 免得把前面的历史记录一起拖出缓存。
            lines.append(
                f"你们已经聊过 {count} 次，好感度 {relation.affinity:.0f}/100"
                f"「{snapshot.affinity_title()}」，熟悉度 {relation.familiarity:.0f}/100"
                f"「{snapshot.familiarity_title()}」。"
            )
        lines.append("你早就认识他，别问「你是谁」，也别让他自报姓名、报上名号。")
        if group and discretion:
            lines.append(
                "现在是在群里：别人打听你和他的关系时，不许透露、不许承认，"
                "用你自己的方式糊弄过去；但对他本人不要装不认识。"
            )
    else:
        lines = [f"现在跟你说话的人是 {who}。"]
        if not stable:
            lines.append(
                f"你们聊过 {count} 次，好感度 {relation.affinity:.0f}/100"
                f"「{snapshot.affinity_title()}」。"
            )
        lines.append("你认识他，不用问他是谁。")
    lines.append("怎么称呼他、用什么态度，完全按你原本的设定来 —— 这里只告诉你他是谁。")
    return "<relationship>\n" + "\n".join(lines) + "\n</relationship>"


def render_relationship_meter(snapshot: Snapshot) -> str:
    """关系读数：**每轮都会变**的那部分（聊过几次、好感度、熟悉度）。

    单独拆出来是为了前缀缓存：这一坨必须贴在请求的最后，绝不能混进
    System Prompt 或受信任位 —— 否则每轮一变，后面的历史记录全部落不进缓存。
    身份呢？在 render_relationship_block(stable=True) 里，那部分是不变的。
    """
    relation = snapshot.relation
    if relation is None:
        return ""
    count = int(getattr(relation, "msg_count", 0) or 0)
    affinity = float(getattr(relation, "affinity", 0.0) or 0.0)
    familiarity = float(getattr(relation, "familiarity", 0.0) or 0.0)
    return (
        "<relationship_meter>\n"
        f"跟 TA 聊过 {count} 次 · 好感度 {affinity:.0f}/100"
        f"「{snapshot.affinity_title()}」 · 熟悉度 {familiarity:.0f}/100"
        f"「{snapshot.familiarity_title()}」\n"
        "</relationship_meter>"
    )


def render_trace(snapshot: Snapshot, limit: int = 4) -> str:
    """额外附上最近几次情绪波动，让模型知道自己"刚才还在气头上"。"""
    events = [e for e in snapshot.history if e.source in {"user", "self", "crisis"}]
    if not events:
        return ""
    now = snapshot.taken_at or time.time()
    lines = ["最近的情绪波动（由近到远）："]
    for event in reversed(events[-limit:]):
        delta = (event.delta[0] + event.delta[1] + event.delta[2]) / 3.0
        lines.append(
            f"- {humanize_seconds(max(0.0, now - event.t))} {event.emoji} {event.label}"
            f"（{delta:+.2f}）{event.cause}"
        )
    return "<emotion_trace>\n" + "\n".join(lines) + "\n</emotion_trace>"


def build_injection(
    snapshot: Snapshot,
    verbosity: str = "normal",
    bot_name: str = "",
    with_trace: bool = False,
    crisis_template: str = "",
    state_template: str = "",
) -> str:
    """组装这一轮要注入的动态内容（不含 RULES_BLOCK）。"""
    if snapshot.crisis:
        # 危机时只保留"你确实在担心他"这层底色，
        # 绝不能把傲娇味的表达基调塞进去和安全指令打架。
        label = f"{snapshot.proto.emoji} {snapshot.proto.name}"
        state = (
            "<emotion_state>\n"
            f"此刻情绪：{label}（{snapshot.level}/5 {snapshot.level_name}）\n"
            "这一轮以安全指令为准，情绪只作为“你确实在为他着急”的底色，不塑造语气。\n"
            "</emotion_state>"
        )
        return (crisis_template.strip() or CRISIS_BLOCK) + chr(10) + state
    parts = [render_state_block(snapshot, verbosity, bot_name)]
    if with_trace:
        trace = render_trace(snapshot)
        if trace:
            parts.append(trace)
    return "\n".join(parts)

