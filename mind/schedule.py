"""每日日程：让她有「今天在干什么」的常识。

为什么需要：没有日程时，她凌晨三点也秒回、上课时间也长篇大论 ——
再像人的语气也架不住「她好像 24 小时都在等我」。

生成方式：**规则 + 按日期固定的随机**。
  - 规则来自「身份」：初中生（年龄）、女生（性别）、性格（影响措辞松紧）；
  - 随机用日期当种子 —— 同一天内每次都一样（不会一会儿在上课一会儿在睡觉），
    换一天就换一套（今天补课、明天社团）。

对外只暴露 render(now) -> 一段话，直接贴进系统提示词。
"""

from __future__ import annotations

import random
import time
from typing import Any

#: 身份默认值（跟人设一致；以后要改从面板来，只动这里）
PROFILE: dict[str, Any] = {
    "age": 14,
    "gender": "female",
    "personality": "tsundere",
}

#: 上学日：起床 / 上学 / 午休 / 下午课 / 放学 / 晚饭 / 作业 / 睡前
_SCHOOL_DAY = [
    (6, 40, 7, 20, "刚起床，在洗漱收拾书包", "busy", "回得很短，带点起床气"),
    (7, 20, 12, 0, "在学校上课", "busy", "只能偷偷回一两句，别写长"),
    (12, 0, 13, 30, "午休，在食堂或者教室", "free", "能多聊两句，但一会儿要上课"),
    (13, 30, 17, 0, "下午的课", "busy", "回得短，偶尔抱怨困"),
    (17, 0, 18, 30, "放学路上 / 到家先躺一会儿", "free", "松下来了，话变多"),
    (18, 30, 19, 30, "吃晚饭", "busy", "回得慢，可能只回一个字"),
    (19, 30, 22, 0, "写作业、被家里催", "free", "一边写一边聊，会抱怨作业多"),
    (22, 0, 23, 30, "洗漱完躺着刷手机", "free", "最放松的时候，话最多"),
    (23, 30, 24, 0, "该睡了", "sleeping", "困，句子变短，会赶你去睡"),
]

#: 周末：起得晚，白天自由，晚上熬夜
_WEEKEND = [
    (0, 0, 9, 30, "还在睡", "sleeping", "基本不回；被吵醒会很凶"),
    (9, 30, 12, 0, "赖床、刷手机、吃早午饭", "free", "懒洋洋的，愿意聊"),
    (12, 0, 17, 0, "出门 / 在家看剧 / 跟朋友玩", "free", "看情况回，可能隔一会儿"),
    (17, 0, 19, 0, "回家吃饭", "busy", "回得慢"),
    (19, 0, 23, 0, "追剧、打游戏、写点作业", "free", "话最多的时候"),
    (23, 0, 24, 0, "熬夜不想睡", "free", "嘴上说睡了其实还在"),
]

#: 周末白天的几种变体 —— 用日期当种子选一个，同一天内固定
_WEEKEND_VARIANTS = [
    "去上补习班，不太情愿",
    "在家窝着看剧，一步没出门",
    "跟同学约了出去逛，刚回来",
    "被家里拉着做家务 / 走亲戚",
    "在赶作业，拖到最后一天",
]


def _minutes(hour: int, minute: int) -> int:
    return int(hour) * 60 + int(minute)


def _slot_of(table: list[Any], now_minutes: int) -> tuple[str, str, str]:
    for start_h, start_m, end_h, end_m, what, state, tone in table:
        begin = _minutes(start_h, start_m)
        end = _minutes(end_h, end_m)
        if begin <= now_minutes < end:
            return what, state, tone
    return table[-1][4], table[-1][5], table[-1][6]


def today(now: float | None = None) -> dict[str, Any]:
    """今天的日程。同一天内结果稳定，换一天换一套。"""
    stamp = time.time() if now is None else float(now)
    local = time.localtime(stamp)
    weekday = local.tm_wday            # 0 = 周一
    minutes = local.tm_hour * 60 + local.tm_min
    age = int(PROFILE.get("age") or 14)
    is_student = 6 <= age <= 22
    lines: list[str] = []
    if weekday >= 5:
        table = _WEEKEND
        rng = random.Random(int(time.strftime("%Y%m%d", local)))
        lines.append("周末。" + rng.choice(_WEEKEND_VARIANTS) + "。")
    else:
        table = _SCHOOL_DAY if is_student else _WEEKEND
        lines.append("上学日。")
    what, state, tone = _slot_of(table, minutes)
    return {
        "date": time.strftime("%Y-%m-%d", local),
        "weekday": weekday,
        "now": time.strftime("%H:%M", local),
        "activity": what,
        "state": state,
        "tone": tone,
        "note": "".join(lines),
        "table": table,
    }


def render(now: float | None = None) -> str:
    """给模型看的一段话。拿不到任何信息就返回空串。"""
    try:
        info = today(now)
    except Exception:  # noqa: BLE001 - 日程算不出来不该影响聊天
        return ""
    head = (
        "（她今天的日程，不是要说出口的内容）\n"
        + "今天 " + info["date"] + "，" + info["note"] + "\n"
        + "现在 " + info["now"] + "，她在：" + info["activity"] + "。"
    )
    state = info["state"]
    if state == "sleeping":
        head += "\n这个点她本该睡着：回得很勉强、很短，会赶对方去睡。"
    elif state == "busy":
        head += "\n她正忙：只回一两句、别写长，态度可以敷衍一点。"
    else:
        head += "\n她有闲：可以正常聊。"
    head += "\n语气参考：" + info["tone"] + "。"
    return "<daily_schedule>\n" + head + "\n</daily_schedule>"


__all__ = ["render", "today", "PROFILE"]

