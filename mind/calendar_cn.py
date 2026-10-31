
"""中国日历：今天到底是上学日 / 周末 / 法定节假日 / 寒暑假 / 调休上课日。

为什么需要：日程原本只有「上学日 / 周末」两张表，一遇到国庆、春节、寒暑假就演错——
国庆第七天她还说"在学校上课"，暑假里她还"放学路上"，寒假赶上春节也没有一点年味。

数据分三层，越靠前越准：
1. **官方安排**：逐年的放假 + 调休表（人工核对国办通知，最准）；
2. **农历三节**：春节 / 端午 / 中秋由农历表推，用于还没发布官方安排的年份；
3. **固定位置**：元旦、清明、劳动、国庆的位置每年一样。

假期天数**一律不写死**：起止日期现算，所以春节 8 天、国庆 7 天、暑假四十多天
各不相同；"假期第 N 天 / 共 M 天"也是算出来的。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

#: 官方放假安排（起, 止, 名称）。核对来源：国务院办公厅逐年「部分节假日安排」通知
_OFFICIAL: dict[int, tuple[tuple[str, str, str], ...]] = {
    2025: (
        ("2025-01-01", "2025-01-01", "元旦"),
        ("2025-01-28", "2025-02-04", "春节"),
        ("2025-04-04", "2025-04-06", "清明"),
        ("2025-05-01", "2025-05-05", "劳动节"),
        ("2025-05-31", "2025-06-02", "端午"),
        ("2025-10-01", "2025-10-08", "国庆"),
    ),
    2026: (
        ("2026-01-01", "2026-01-03", "元旦"),
        ("2026-02-15", "2026-02-23", "春节"),
        ("2026-04-04", "2026-04-06", "清明"),
        ("2026-05-01", "2026-05-05", "劳动节"),
        ("2026-06-19", "2026-06-21", "端午"),
        ("2026-09-25", "2026-09-27", "中秋"),
        ("2026-10-01", "2026-10-07", "国庆"),
    ),
}

#: 调休上课日：本来周末，被调去上学（学生视角）
_MAKEUP: dict[int, tuple[str, ...]] = {
    2025: ("2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28", "2025-10-11"),
    2026: ("2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10"),
}

#: 农历三节（春节=正月初一, 端午, 中秋）。没发布官方安排的年份靠它兜底
_LUNAR: dict[int, tuple[str, str, str]] = {
    2024: ("2024-02-10", "2024-06-10", "2024-09-17"),
    2025: ("2025-01-29", "2025-05-31", "2025-10-06"),
    2026: ("2026-02-17", "2026-06-19", "2026-09-25"),
    2027: ("2027-02-06", "2027-06-09", "2027-09-15"),
    2028: ("2028-01-26", "2028-05-28", "2028-10-03"),
    2029: ("2029-02-13", "2029-06-16", "2029-09-22"),
    2030: ("2030-02-03", "2030-06-05", "2030-09-12"),
}

#: 暑假（月, 日）：初中校历一般在 7 月上中旬到 8 月底
SUMMER_START = (7, 10)
SUMMER_END = (8, 31)
#: 寒假：以春节为中心，前后各留几天（各地校历不同，这里取中间值）
WINTER_BEFORE = 18
WINTER_AFTER = 11

#: 日历是否启用（关掉就退回"上学日 / 周末"两套表）
CALENDAR_ENABLED = True


@dataclass(frozen=True)
class DayInfo:
    """某一天是什么日子。"""

    date: str
    weekday: int          # 0 = 周一
    kind: str             # school | makeup | weekend | holiday | vacation
    is_school_day: bool
    holiday: str = ""     # 法定假名，空 = 没有
    holiday_index: int = 0
    holiday_total: int = 0
    vacation: str = ""    # 暑假 / 寒假
    vacation_index: int = 0
    vacation_total: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "date": self.date, "weekday": self.weekday, "kind": self.kind,
            "is_school_day": self.is_school_day, "holiday": self.holiday,
            "holiday_index": self.holiday_index, "holiday_total": self.holiday_total,
            "vacation": self.vacation, "vacation_index": self.vacation_index,
            "vacation_total": self.vacation_total, "note": self.note,
        }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _d(text: str) -> date:
    return date(int(text[:4]), int(text[5:7]), int(text[8:10]))


def _rules(year: int) -> tuple[tuple[date, date, str], ...]:
    """没有官方安排的年份：按固定位置 + 农历表拼。"""
    blocks: list[tuple[date, date, str]] = [
        (date(year, 1, 1), date(year, 1, 3), "元旦"),
        (date(year, 4, 4), date(year, 4, 6), "清明"),
        (date(year, 5, 1), date(year, 5, 5), "劳动节"),
        (date(year, 10, 1), date(year, 10, 7), "国庆"),
    ]
    lunar = _LUNAR.get(year)
    if lunar:
        spring = _d(lunar[0])
        blocks.append((spring - timedelta(days=1), spring + timedelta(days=6), "春节"))  # 除夕~初六
        duanwu = _d(lunar[1])
        blocks.append((duanwu, duanwu + timedelta(days=2), "端午"))
        midautumn = _d(lunar[2])
        blocks.append((midautumn, midautumn + timedelta(days=2), "中秋"))
    return tuple(blocks)


def blocks(year: int) -> tuple[tuple[date, date, str], ...]:
    """某一年的全部法定假期区间。"""
    official = _OFFICIAL.get(year)
    if official:
        return tuple((_d(a), _d(b), n) for a, b, n in official)
    return _rules(year)


def makeup_days(year: int) -> frozenset[date]:
    """某一年的调休上课日。"""
    return frozenset(_d(x) for x in _MAKEUP.get(year, ()))


def _spring_festival(year: int) -> date | None:
    lunar = _LUNAR.get(year)
    return _d(lunar[0]) if lunar else None


def vacations(year: int) -> tuple[tuple[date, date, str], ...]:
    """某一年的寒暑假区间。天数由起止日期现算，不写死。"""
    out: list[tuple[date, date, str]] = []
    spring = _spring_festival(year)
    if spring:
        out.append((spring - timedelta(days=WINTER_BEFORE), spring + timedelta(days=WINTER_AFTER), "寒假"))
    out.append((date(year, *SUMMER_START), date(year, *SUMMER_END), "暑假"))
    return tuple(out)


def _hit(day: date, spans: tuple[tuple[date, date, str], ...]) -> tuple[str, int, int] | None:
    for start, end, name in spans:
        if start <= day <= end:
            return name, (day - start).days + 1, (end - start).days + 1
    return None


def _vacation_of(day: date) -> tuple[str, int, int] | None:
    # 寒假可能跨年（春节在年初，起点落在前一年），所以前后两年都查
    for year in (day.year, day.year + 1):
        got = _hit(day, vacations(year))
        if got:
            return got
    return None


def day_info(now: float | None = None, *, school: bool = True) -> DayInfo:
    """某一天的日子类型。传时间戳，不传就是现在。"""
    stamp = time.time() if now is None else float(now)
    local = time.localtime(stamp)
    day = date(local.tm_year, local.tm_mon, local.tm_mday)
    weekday = local.tm_wday

    if not CALENDAR_ENABLED:
        return DayInfo(
            date=day.isoformat(), weekday=weekday,
            kind="weekend" if weekday >= 5 else "school",
            is_school_day=school and weekday < 5,
        )

    holiday = _hit(day, blocks(day.year))
    vacation = _vacation_of(day) if school else None

    if holiday:
        name, index, total = holiday
        note = "%s假期第 %d 天，共 %d 天" % (name, index, total)
        if vacation:
            note += "（%s里）" % vacation[0]
        return DayInfo(
            date=day.isoformat(), weekday=weekday, kind="holiday", is_school_day=False,
            holiday=name, holiday_index=index, holiday_total=total,
            vacation=vacation[0] if vacation else "",
            vacation_index=vacation[1] if vacation else 0,
            vacation_total=vacation[2] if vacation else 0,
            note=note,
        )
    if vacation:
        name, index, total = vacation
        return DayInfo(
            date=day.isoformat(), weekday=weekday, kind="vacation", is_school_day=False,
            vacation=name, vacation_index=index, vacation_total=total,
            note="%s第 %d 天，共 %d 天" % (name, index, total),
        )
    if school and day in makeup_days(day.year):
        return DayInfo(
            date=day.isoformat(), weekday=weekday, kind="makeup", is_school_day=True,
            note="调休上课日（本来是周末，被调去上学）",
        )
    if weekday >= 5:
        return DayInfo(
            date=day.isoformat(), weekday=weekday, kind="weekend", is_school_day=False,
            note="周末",
        )
    return DayInfo(
        date=day.isoformat(), weekday=weekday, kind="school",
        is_school_day=school, note="上学日" if school else "工作日",
    )


#: 假期还剩几天 → 心情（后段开始赶作业，别一直玩）
def vacation_mood(info: DayInfo) -> str:
    """假期进度对语气的影响，空串 = 没影响。

    只在**长假**上生效：三天小长假说"作业还没写完""马上开学"都是错的。
    """
    total = info.vacation_total or info.holiday_total
    index = info.vacation_index or info.holiday_index
    if not total or not index or total < 5:
        return ""
    left = total - index          # 还剩几天（不含今天）
    in_vacation = bool(info.vacation)
    if left <= 0:
        if in_vacation:
            return "最后一天了，明天就要开学，心里堵"
        return "最后一天了，明天又要早起上学"
    if left <= 2:
        if in_vacation:
            return "就剩 %d 天就开学了，作业还没写完" % (left + 1)
        return "假期快没了，有点不想结束"
    if index <= 2 and in_vacation:
        return "假期刚开始，什么都不想干"
    return ""


__all__ = [
    "DayInfo", "blocks", "makeup_days", "vacations", "day_info", "vacation_mood",
    "CALENDAR_ENABLED", "SUMMER_START", "SUMMER_END", "WINTER_BEFORE", "WINTER_AFTER",
]

