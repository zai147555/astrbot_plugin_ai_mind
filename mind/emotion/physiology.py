"""生理状态：让她有身体。

为什么单独一层：情绪（PAD）回答「她现在什么心情」，但人不是只有心情 ——
凌晨三点的疲惫、饿过头的暴躁、低烧时的迟钝，都不是「情绪」而是「身体」。
把它们混进 PAD 里，就会被情绪衰减冲掉、也没法在面板上单独看。

所以这里维护一份独立的身体状态：
  - 能量：跟着昼夜节律走（凌晨最低、上午和傍晚最高），不是恒定值；
  - 饥饿：随时间上升，到了饭点回落；
  - 压力：随时间衰减，被负面刺激抬高（由引擎调用）；
  - 疾病：能量低 + 压力高的时候有概率着凉，持续一段时间后自己好。

身体只是「另一层滤镜」：它给 PAD 一个偏移，不改人设、不改说话风格。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .model import PAD

#: 饭点（小时）：到点饥饿回落
MEAL_HOURS = (7.5, 12.0, 18.5)
#: 饥饿每小时涨多少（约 7 小时从 0 到 1）
HUNGER_PER_HOUR = 0.145
#: 压力半衰期（小时）
STRESS_HALF_LIFE = 2.0
#: 病了多久才自己好（小时）
ILLNESS_HOURS = (6.0, 20.0)

# ---- 生理周期 ----
#: 是否启用（关掉就完全不影响任何东西）
CYCLE_ENABLED = True
#: 一轮多少天
CYCLE_LENGTH = 28
#: 经期几天
PERIOD_DAYS = 5
#: 起算日（固定值）：从这一天开始算，所以同一天算出来永远一样，
#: 重启、换会话都不会跳。真要改起算日就改这一个数。
CYCLE_ANCHOR = 1767225600.0      # 2026-01-01 00:00 UTC


@dataclass
class Body:
    """一个人的身体状态。全部归一化到 0~1（pressure 也是）。"""

    energy: float = 0.75
    hunger: float = 0.18
    stress: float = 0.12
    #: 0 = 健康；>0 = 病中（越大越重）
    illness: float = 0.0
    illness_kind: str = ""
    illness_until: float = 0.0
    last_update: float = 0.0
    #: 周期第几天 / 相位（都是按时间算出来的派生值，不参与手写配置）
    cycle_day: int = 0
    cycle_phase: str = ""

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "energy": round(self.energy, 4),
            "hunger": round(self.hunger, 4),
            "stress": round(self.stress, 4),
            "illness": round(self.illness, 4),
            "illness_kind": self.illness_kind,
            "illness_until": self.illness_until,
            "last_update": self.last_update,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Body":
        body = cls()
        if not isinstance(data, dict):
            return body
        for key in ("energy", "hunger", "stress", "illness"):
            try:
                setattr(body, key, float(data.get(key, getattr(body, key))))
            except (TypeError, ValueError):
                pass
        body.illness_kind = str(data.get("illness_kind") or "")
        for key in ("illness_until", "last_update"):
            try:
                setattr(body, key, float(data.get(key, 0.0) or 0.0))
            except (TypeError, ValueError):
                pass
        return body.limit()


    # -- 取值范围 -----------------------------------------------------------
    def limit(self) -> "Body":
        self.energy = clamp(self.energy)
        self.hunger = clamp(self.hunger)
        self.stress = clamp(self.stress)
        self.illness = clamp(self.illness)
        return self


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(number):
        return low
    return max(low, min(high, number))


def hour_of(now: float) -> float:
    local = time.localtime(now)
    return local.tm_hour + local.tm_min / 60.0


#: 一天的精力关键帧：(小时, 该有多少精力)。中间用平滑插值，不查表。
#: 凌晨 4 点最低（深睡），下午 14:30 一个凹陷（犯困），晚 20 点最高。
_ENERGY_CURVE = [
    (0.0, 0.22), (4.0, 0.14), (6.5, 0.38), (8.0, 0.72),
    (10.0, 0.92), (12.5, 0.78), (14.5, 0.56), (16.5, 0.74),
    (20.0, 0.90), (22.0, 0.66), (24.0, 0.22),
]


def _smooth(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)      # 两头平、中间陡，不会出现折角


def circadian_energy(now: float) -> float:
    """这个点「该有多少精力」。

    用关键帧 + 平滑插值，而不是余弦：余弦只能有**一个**低谷，
    可人一天有两个低谷（凌晨深睡、下午犯困）和两个高峰（上午、傍晚）。"""
    hour = hour_of(now)
    points = _ENERGY_CURVE
    for index in range(len(points) - 1):
        left_h, left_v = points[index]
        right_h, right_v = points[index + 1]
        if left_h <= hour <= right_h:
            span = right_h - left_h
            if span <= 0:
                return clamp(left_v, 0.05, 0.98)
            ratio = _smooth((hour - left_h) / span)
            return clamp(left_v + (right_v - left_v) * ratio, 0.05, 0.98)
    return clamp(points[-1][1], 0.05, 0.98)

def meal_bonus(now: float) -> float:
    """刚吃完饭的那一小段时间，饥饿回落得特别快。"""
    hour = hour_of(now)
    best = 0.0
    for meal in MEAL_HOURS:
        diff = abs(hour - meal)
        diff = min(diff, 24.0 - diff)
        if diff < 1.0:
            best = max(best, 1.0 - diff)
    return best


def evolve(
    body: Body,
    now: float,
    *,
    allow_illness: bool = True,
    rng: random.Random | None = None,
) -> list[str]:
    """按流逝的时间推进身体。返回这一段时间里发生的「身体事件」描述。"""
    events: list[str] = []
    if body.last_update <= 0:
        body.last_update = now
        body.energy = circadian_energy(now)
        return events

    dt = now - body.last_update
    if dt <= 0:
        return events
    hours = dt / 3600.0
    body.last_update = now

    # 0) 生理周期：纯按时间算，同一天永远一样
    if CYCLE_ENABLED:
        body.cycle_day = cycle_day(now)
        body.cycle_phase = cycle_phase(body.cycle_day)
    else:
        body.cycle_day = 0
        body.cycle_phase = ""

    # 1) 能量：朝「这个点该有的精力」靠拢；病中上限压低
    target = circadian_energy(now)
    if body.illness > 0:
        target *= max(0.35, 1.0 - body.illness * 0.6)
    # 吃得少也会没力气
    target *= max(0.55, 1.0 - body.hunger * 0.45)
    # 经期/经前：身体能调动的上限确实低一些（不是矫情，是真实的）
    if body.cycle_phase == "经期":
        target *= 0.84
    elif body.cycle_phase == "经前":
        target *= 0.93
    rate = 0.55 if target > body.energy else 0.28
    body.energy += (target - body.energy) * clamp(hours * rate, 0.0, 1.0)

    # 2) 饥饿
    body.hunger += HUNGER_PER_HOUR * hours * (1.0 - meal_bonus(now) * 0.85)

    # 3) 压力自然消退
    decay = 0.5 ** (hours / STRESS_HALF_LIFE)
    body.stress *= decay

    # 4) 疾病：到点就好；没好之前按阶段起伏
    if body.illness > 0:
        if now >= body.illness_until:
            events.append("病好了，人轻松下来")
            body.illness = 0.0
            body.illness_kind = ""
            body.illness_until = 0.0
        else:
            # 病到后面慢慢减轻
            left = max(1.0, (body.illness_until - now) / 3600.0)
            body.illness = clamp(body.illness * (0.92 ** hours) + 0.004 * hours)
            if left < 2.0:
                body.illness = clamp(body.illness * (0.5 ** (hours / 1.5)))

    # 5) 着凉：累狠了 + 压力大，才有一点概率
    if allow_illness and body.illness <= 0 and body.energy < 0.35 and body.stress > 0.5:
        chance = (0.35 - body.energy) * (body.stress - 0.5) * 0.9 * hours
        generator = rng or random
        if generator.random() < chance:
            body.illness = 0.55 + generator.random() * 0.25
            body.illness_kind = "着凉"
            span = ILLNESS_HOURS[0] + generator.random() * (ILLNESS_HOURS[1] - ILLNESS_HOURS[0])
            body.illness_until = now + span * 3600.0
            events.append("着凉了，头有点沉")

    body.limit()
    return events


def cycle_day(now: float, anchor: float = CYCLE_ANCHOR,
              length: int = CYCLE_LENGTH) -> int:
    """今天是周期第几天（1 起算）。纯按时间算，不存状态。"""
    span = max(1, int(length))
    days = int((now - anchor) // 86400.0)
    return days % span + 1


def cycle_phase(day: int, length: int = CYCLE_LENGTH) -> str:
    """周期相位：经期 / 经期后 / 排卵期 / 经前 / 平稳。"""
    if not CYCLE_ENABLED:
        return ""
    if 1 <= day <= PERIOD_DAYS:
        return "经期"
    span = max(1, int(length))
    if PERIOD_DAYS < day <= span - 5:
        middle = span // 2
        if abs(day - middle) <= 1:
            return "排卵期"
        if day <= middle:
            return "经期后"
        return "平稳"
    if day > span - 5:
        return "经前"
    return "平稳"


def pad_offset(body: Body) -> PAD:
    """身体给情绪的那一层偏移。

    这不是「她现在开心」——而是「身体把她往哪儿推」：
      - 没力气 → 愉悦降、唤醒降；
      - 饿了 → 愉悦降、唤醒升（烦躁）；
      - 压力大 → 愉悦降、唤醒升、掌控降；
      - 病了 → 三样全往下拖。
    """
    energy_gap = max(0.0, 0.55 - body.energy)
    hunger = max(0.0, body.hunger - 0.45)
    stress = max(0.0, body.stress - 0.25)
    illness = body.illness
    # 周期：经期是「不舒服 + 没力气」，经前是「烦躁」
    period = 1.0 if body.cycle_phase == "经期" else 0.0
    pms = 1.0 if body.cycle_phase == "经前" else 0.0
    p = -0.30 * energy_gap - 0.22 * hunger - 0.26 * stress - 0.45 * illness
    p += -0.12 * period - 0.10 * pms
    a = -0.18 * energy_gap + 0.16 * hunger + 0.20 * stress - 0.10 * illness
    a += 0.06 * period + 0.12 * pms
    d = -0.10 * energy_gap - 0.06 * hunger - 0.22 * stress - 0.34 * illness
    d += -0.14 * period - 0.08 * pms
    return PAD.of(clamp(p, -1.0, 1.0), clamp(a, -1.0, 1.0), clamp(d, -1.0, 1.0))


    p = -0.30 * energy_gap - 0.22 * hunger - 0.26 * stress - 0.45 * illness
    a = -0.18 * energy_gap + 0.16 * hunger + 0.20 * stress - 0.10 * illness
    d = -0.10 * energy_gap - 0.06 * hunger - 0.22 * stress - 0.34 * illness
    return PAD.of(clamp(p, -1.0, 1.0), clamp(a, -1.0, 1.0), clamp(d, -1.0, 1.0))


def describe(body: Body) -> str:
    """一句人话，给提示词用。没特殊情况就返回空串。"""
    parts: list[str] = []
    if body.illness > 0:
        if body.illness > 0.6:
            parts.append("病着呢（" + (body.illness_kind or "不舒服") + "）：头发沉、懒得动，回得比平时慢也短")
        else:
            parts.append("还有点没好利索：" + (body.illness_kind or "不太舒服") + "，容易累")
    if body.energy < 0.25:
        parts.append("困得不行，脑子转不动")
    elif body.energy < 0.45:
        parts.append("有点乏，注意力不太集中")
    if body.hunger > 0.75:
        parts.append("饿得有点烦，容易一句话带刺")
    elif body.hunger > 0.55:
        parts.append("有点饿了")
    if body.stress > 0.7:
        parts.append("绷得太紧了，一点就炸")
    elif body.stress > 0.5:
        parts.append("心里有点紧")
    if body.cycle_phase == "经期":
        parts.append("生理期第 " + str(body.cycle_day) + " 天：肚子坠着难受、腰酸，人容易累")
    elif body.cycle_phase == "经前":
        parts.append("快到日子了：这几天容易烦躁，也容易馋")
    if not parts:
        return ""
    return "身体：" + "；".join(parts)


__all__ = [
    "Body",
    "circadian_energy",
    "clamp",
    "describe",
    "evolve",
    "hour_of",
    "pad_offset",
]

