
"""仿人脑：结构 → 功能的映射表 + 三个新增的脑功能。

本插件不是"给情绪贴标签"，而是按人脑的分工搭了几层结构。
目前的对应关系（新加的功能全部在本文件）：

    下丘脑  hypothalamus   physiology.py      昼夜节律 / 能量 / 饥饿 / 压力 / 疾病 / 周期
    杏仁核  amygdala       appraisal.py       对一句话做情绪评估，吐出一个 PAD 冲击
    前额叶  prefrontal     brain.habituation  同一刺激反复出现 → 反应被抑制（习惯化）
    镜像神经元 mirror      brain.empathy      把对方情绪按亲密度吸进心境（共情）
    伏隔核  accumbens      brain.reward_*     预期 vs 实际，落差 → 唤起（奖励预测误差）
    海马体  hippocampus    memory.activation  记忆唤醒强度（遗忘曲线 + 间隔效应）
    皮层    cortex         engine.SessionMood 即时情绪 / 慢心境 / 关系好感

三个功能的共同点：都作用在**已有链路上**，不新增一条并行通路——
习惯化缩放冲击量，共情往心境里渗，奖励预测误差补一个唤起脉冲。
"""
from __future__ import annotations

from typing import Any, Iterable

from .model import PAD, clamp

# -- 习惯化（前额叶抑制）----------------------------------------------------
#: 记住最近多少条刺激指纹
HABIT_SEEN = 20
#: 每重复一次，反应衰减多少（1/(1+gain*n)）
HABIT_GAIN = 0.35
#: 衰减地板——再熟的动作也不会完全无感，否则会说不出话
HABIT_FLOOR = 0.25

# -- 共情（镜像神经元）------------------------------------------------------
#: 完全陌生的人，情绪往心境里渗多少
EMPATHY_BASE = 0.10
#: 最熟悉的人，情绪往心境里渗多少
EMPATHY_MAX = 0.38

# -- 奖励预测误差（伏隔核）--------------------------------------------------
#: 落差小于这个数就当"意料之中"，不给唤起
SURPRISE_THRESHOLD = 0.55
#: 唤起脉冲的幅度
SURPRISE_AROUSAL = 0.18


# -- 习惯化 ---------------------------------------------------------------
def fingerprint(stimulus: Any) -> str:
    """给一次刺激取指纹：同一个触发规则 = 同一种刺激。

    用 (来源, 规则 key) 而不是原始消息文本——"你又凶我"和"你怎么这么凶"
    在情绪上是同一件事，反复挨凶应该越来越习惯。
    """
    source = str(getattr(stimulus, "source", "") or "user")
    key = str(getattr(stimulus, "key", "") or "")
    if key:
        return source + ":" + key
    cause = str(getattr(stimulus, "cause", "") or "")
    if cause:
        return source + ":" + cause
    return ""


def habituation(
    recent: Iterable[str],
    fp: str,
    gain: float = HABIT_GAIN,
    floor: float = HABIT_FLOOR,
) -> tuple[float, int]:
    """返回 (衰减系数, 之前已经重复过几次)。

    系数 1.0 = 头一回，完全按原样反应；重复越多次越小，但不低于 floor。
    """
    if not fp:
        return 1.0, 0
    window = [f for f in list(recent)[-HABIT_SEEN:] if f]
    same = sum(1 for f in window if f == fp)
    factor = max(floor, 1.0 / (1.0 + max(0.0, gain) * same))
    return factor, same


def remember(recent: list[str], fp: str, cap: int = HABIT_SEEN) -> list[str]:
    """把本次指纹记进工作记忆（就地改 list，返回同一个对象）。"""
    if not fp:
        return recent
    recent.append(fp)
    if len(recent) > cap:
        del recent[: len(recent) - cap]
    return recent


# -- 共情 ----------------------------------------------------------------
def empathy_share(familiarity: float) -> float:
    """共情强度随熟悉度上升：越是熟人，她的情绪越能动到我。"""
    f = clamp((familiarity or 0.0) / 100.0, 0.0, 1.0)
    return EMPATHY_BASE + (EMPATHY_MAX - EMPATHY_BASE) * f


def empathy_delta(delta: PAD, familiarity: float) -> PAD:
    """共情只渗进**慢心境**，不进即时情绪——所以我不会跟着对方立刻炸。"""
    return delta * empathy_share(familiarity)


# -- 奖励预测误差 ---------------------------------------------------------
def expected_valence(
    affinity: float,
    familiarity: float,
    affinity_max: float = 100.0,
) -> float:
    """对某人，我预期这一轮会是好的还是坏的（-1..1）。

    好感高 → 预期好事；不熟 → 预期接近中性（不熟的人说啥都不意外）。
    """
    top = affinity_max if affinity_max and affinity_max > 0 else 100.0
    a = clamp((affinity or 0.0) / top, -1.0, 1.0)
    f = clamp((familiarity or 0.0) / 100.0, 0.0, 1.0)
    return clamp(a * (0.45 + 0.55 * f), -1.0, 1.0)


def reward_prediction_error(stimulus_p: float, expected: float) -> float:
    """带符号的落差：实际愉悦 - 预期愉悦。负 = 比预想的更糟。"""
    return clamp(stimulus_p) - clamp(expected)


def surprise_boost(
    rpe: float,
    threshold: float = SURPRISE_THRESHOLD,
    gain: float = SURPRISE_AROUSAL,
) -> PAD:
    """落差够大才给一个唤起脉冲：没想到会更好就惊喜，没想到会这么糟就担心。"""
    mag = abs(rpe)
    if mag <= max(0.0, threshold):
        return PAD()
    over = min(1.0, (mag - threshold) / max(0.01, 1.0 - threshold))
    if rpe < 0:
        return PAD.of(-0.10 * over, gain * over, -0.08 * over)
    return PAD.of(0.10 * over, gain * over, 0.05 * over)


def is_zero(pad: PAD) -> bool:
    """脉冲是否为"没发生"（避免无谓地改心境）。"""
    return pad.p == 0.0 and pad.a == 0.0 and pad.d == 0.0


def describe(factor: float, repeat: int, rpe: float = 0.0) -> str:
    """给面板/日志用的一句话（空串 = 本轮到没特别的）。"""
    parts: list[str] = []
    if repeat > 0 and factor < 0.999:
        parts.append("习惯了(第%d次,系数%.2f)" % (repeat + 1, factor))
    if abs(rpe) > SURPRISE_THRESHOLD:
        parts.append("没想到(%.2f)" % rpe)
    return " · ".join(parts)

