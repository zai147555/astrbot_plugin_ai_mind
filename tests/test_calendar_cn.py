
"""中国日历：法定假 / 调休 / 寒暑假，天数由日期现算（不写死）。"""
from __future__ import annotations

import time
import unittest
from datetime import date

from mind import calendar_cn as cal


def at(y: int, m: int, d: int, h: int = 12, mi: int = 0) -> float:
    return time.mktime((y, m, d, h, mi, 0, 0, 0, -1))


def info(y: int, m: int, d: int, *, school: bool = True):
    return cal.day_info(at(y, m, d), school=school)


class OfficialHolidayTest(unittest.TestCase):
    def test_2026_spring_festival_is_nine_days(self) -> None:
        first = info(2026, 2, 15)
        last = info(2026, 2, 23)
        self.assertEqual(first.kind, "holiday")
        self.assertEqual(first.holiday, "春节")
        self.assertEqual((first.holiday_index, first.holiday_total), (1, 9))
        self.assertEqual((last.holiday_index, last.holiday_total), (9, 9))
        self.assertEqual(info(2026, 2, 24).kind, "vacation")

    def test_2026_national_day_is_seven_days(self) -> None:
        start = info(2026, 10, 1)
        self.assertEqual((start.holiday, start.holiday_total), ("国庆", 7))
        self.assertEqual(info(2026, 10, 7).holiday_index, 7)
        self.assertEqual(info(2026, 10, 8).kind, "school")

    def test_short_holidays(self) -> None:
        self.assertEqual(info(2026, 1, 1).holiday_total, 3)     # 元旦
        self.assertEqual(info(2026, 4, 4).holiday, "清明")
        self.assertEqual(info(2026, 5, 1).holiday_total, 5)     # 劳动节
        self.assertEqual(info(2026, 6, 19).holiday, "端午")
        self.assertEqual(info(2026, 9, 25).holiday, "中秋")

    def test_holiday_lengths_differ(self) -> None:
        """不固定天数：春节 9 天、国庆 7 天、劳动 5 天、中秋 3 天。"""
        lengths = {
            info(2026, 2, 17).holiday_total,
            info(2026, 10, 3).holiday_total,
            info(2026, 5, 2).holiday_total,
            info(2026, 9, 26).holiday_total,
        }
        self.assertEqual(len(lengths), 4)
        self.assertIn(9, lengths)
        self.assertIn(3, lengths)


class MakeupDayTest(unittest.TestCase):
    def test_weekend_turned_into_school(self) -> None:
        got = info(2026, 5, 9)          # 周六，劳动节调休上班/上课
        self.assertEqual(got.kind, "makeup")
        self.assertTrue(got.is_school_day)
        self.assertIn("调休", got.note)

    def test_makeup_inside_winter_vacation_stays_a_vacation(self) -> None:
        """寒假里被调的周六不用去上课 —— 学生在家。"""
        got = info(2026, 2, 14)
        self.assertEqual(got.kind, "vacation")
        self.assertFalse(got.is_school_day)

    def test_ordinary_saturday_is_weekend(self) -> None:
        self.assertEqual(info(2026, 11, 7).kind, "weekend")


class VacationTest(unittest.TestCase):
    def test_summer_length_is_computed(self) -> None:
        got = info(2026, 7, 10)
        self.assertEqual(got.vacation, "暑假")
        self.assertEqual((got.vacation_index, got.vacation_total), (1, 53))
        self.assertEqual(info(2026, 8, 31).vacation_index, 53)
        self.assertEqual(info(2026, 9, 2).kind, "school")

    def test_winter_revolves_around_spring_festival(self) -> None:
        got = info(2026, 1, 30)
        self.assertEqual(got.vacation, "寒假")
        self.assertEqual(got.vacation_total, cal.WINTER_BEFORE + cal.WINTER_AFTER + 1)
        self.assertEqual(info(2026, 3, 2).kind, "school")   # 3/1 是周日

    def test_vacations_differ_by_year(self) -> None:
        self.assertNotEqual(info(2026, 7, 10).vacation_total, info(2027, 7, 10).vacation_total + 100)

    def test_not_a_student_has_no_vacation(self) -> None:
        got = info(2026, 7, 20, school=False)
        self.assertEqual(got.vacation, "")
        self.assertEqual(got.kind, "school")


class FallbackYearTest(unittest.TestCase):
    def test_2027_uses_rules_with_lunar_table(self) -> None:
        spring = info(2027, 2, 6)
        self.assertEqual((spring.holiday, spring.holiday_total), ("春节", 8))
        self.assertEqual(info(2027, 2, 5).holiday_index, 1)     # 除夕
        self.assertEqual(info(2027, 6, 9).holiday, "端午")
        self.assertEqual(info(2027, 9, 15).holiday, "中秋")
        self.assertEqual(info(2027, 10, 4).holiday_total, 7)

    def test_unknown_year_degrades_without_crashing(self) -> None:
        got = info(2033, 2, 17)
        self.assertIn(got.kind, ("school", "weekend"))
        self.assertEqual(got.holiday, "")
        self.assertEqual(info(2033, 10, 2).holiday, "国庆")

    def test_disable_switch_falls_back_to_two_tables(self) -> None:
        old = cal.CALENDAR_ENABLED
        cal.CALENDAR_ENABLED = False
        try:
            self.assertEqual(info(2026, 2, 21).kind, "weekend")  # 周六
            self.assertEqual(info(2026, 11, 3).kind, "school")
        finally:
            cal.CALENDAR_ENABLED = old


class MoodTest(unittest.TestCase):
    def test_short_holiday_says_nothing_about_school(self) -> None:
        self.assertEqual(cal.vacation_mood(info(2026, 9, 26)), "")

    def test_national_day_last_day(self) -> None:
        self.assertIn("又要早起", cal.vacation_mood(info(2026, 10, 7)))

    def test_summer_last_days_push_homework(self) -> None:
        self.assertIn("作业", cal.vacation_mood(info(2026, 8, 30)))
        self.assertIn("开学", cal.vacation_mood(info(2026, 8, 31)))

    def test_summer_start_is_idle(self) -> None:
        self.assertIn("什么都不想干", cal.vacation_mood(info(2026, 7, 11)))

    def test_school_day_has_no_mood(self) -> None:
        self.assertEqual(cal.vacation_mood(info(2026, 11, 3)), "")


class DeterminismTest(unittest.TestCase):
    def test_same_instant_same_answer(self) -> None:
        stamp = at(2026, 2, 18, 15, 30)
        self.assertEqual(cal.day_info(stamp), cal.day_info(stamp))

    def test_accepts_none(self) -> None:
        self.assertTrue(cal.day_info().date)

    def test_blocks_are_sorted_and_within_year(self) -> None:
        for y in (2025, 2026, 2027):
            spans = cal.blocks(y)
            self.assertTrue(spans)
            for start, end, name in spans:
                self.assertLessEqual(start, end, name)
                self.assertIn(start.year, (y - 1, y))

    def test_makeup_days_are_frozen(self) -> None:
        self.assertIsInstance(cal.makeup_days(2026), frozenset)
        self.assertIn(date(2026, 9, 20), cal.makeup_days(2026))


if __name__ == "__main__":
    unittest.main()

