
"""日程按「今天是什么日子」换表：上学 / 周末 / 法定假 / 寒暑假 / 调休上课。"""
from __future__ import annotations

import time
import unittest

from mind import schedule


def at(y: int, m: int, d: int, h: int = 15, mi: int = 0) -> float:
    return time.mktime((y, m, d, h, mi, 0, 0, 0, -1))


class DayKindTest(unittest.TestCase):
    def test_five_kinds_use_five_tables(self) -> None:
        cases = {
            "school": (2026, 11, 3),
            "weekend": (2026, 11, 7),
            "holiday": (2026, 10, 3),
            "vacation": (2026, 7, 20),
            "makeup": (2026, 5, 9),
        }
        tables = {}
        for kind, (y, m, d) in cases.items():
            got = schedule.today(at(y, m, d))
            self.assertEqual(got["kind"], kind, (kind, got["kind"]))
            tables[kind] = id(got["table"])
        self.assertEqual(len(set(tables.values())), 5, "五种日子必须是五张表")

    def test_school_day_says_school(self) -> None:
        got = schedule.today(at(2026, 11, 3))
        self.assertEqual(got["note"], "上学日。")

    def test_spring_festival_has_flavour(self) -> None:
        got = schedule.today(at(2026, 2, 18))
        self.assertEqual(got["kind"], "holiday")
        self.assertIn("春节假期第", got["note"])
        self.assertIn("寒假里", got["note"])
        self.assertGreater(got["day_total"], 0)
        text = schedule.render(at(2026, 2, 18))
        self.assertIn("春节假期第", text)

    def test_summer_vacation_counts_days(self) -> None:
        got = schedule.today(at(2026, 8, 30))
        self.assertEqual(got["vacation"], "暑假")
        self.assertEqual(got["day_total"], 53)
        self.assertIn("作业", got["tone"], "开学前的最后几天该急着赶作业")

    def test_makeup_day_complains(self) -> None:
        got = schedule.today(at(2026, 5, 9, 9))
        self.assertEqual(got["kind"], "makeup")
        self.assertIn("调休", got["note"])
        self.assertEqual(got["state"], "busy")
        self.assertIn("调休", schedule.render(at(2026, 5, 9, 9)))

    def test_holiday_last_day_before_school(self) -> None:
        got = schedule.today(at(2026, 10, 7))
        self.assertIn("又要早起", got["tone"])

    def test_last_summer_day(self) -> None:
        got = schedule.today(at(2026, 8, 31))
        self.assertIn("开学", got["tone"])


class BehaviourTest(unittest.TestCase):
    def test_same_day_is_stable(self) -> None:
        """同一时刻两次调用必须完全一致；同一天不同钟点，日程描述也一样。"""
        a = schedule.today(at(2026, 7, 20, 9))
        b = schedule.today(at(2026, 7, 20, 9))
        self.assertEqual(a, b)
        later = schedule.today(at(2026, 7, 20, 21))
        self.assertEqual(a["note"], later["note"])
        self.assertEqual(a["day_total"], later["day_total"])

    def test_late_night_is_sleeping_on_school_day(self) -> None:
        got = schedule.today(at(2026, 11, 4, 3))
        self.assertEqual(got["state"], "sleeping")
        self.assertIn("本该睡着", schedule.render(at(2026, 11, 4, 3)))

    def test_vacation_morning_is_sleeping(self) -> None:
        self.assertEqual(schedule.today(at(2026, 7, 20, 3))["state"], "sleeping")

    def test_render_survives_odd_times(self) -> None:
        for h in (0, 6, 12, 18, 23):
            text = schedule.render(at(2026, 7, 20, h))
            self.assertIn("<daily_schedule>", text)
            self.assertIn("她在：", text)

    def test_non_student_gets_workday(self) -> None:
        old = dict(schedule.PROFILE)
        schedule.PROFILE["age"] = 30
        try:
            got = schedule.today(at(2026, 11, 3))
            self.assertEqual(got["note"], "工作日。")
            self.assertEqual(got["kind"], "school")   # 工作日仍在"上学日"分支，但用别的表
        finally:
            schedule.PROFILE.clear()
            schedule.PROFILE.update(old)


if __name__ == "__main__":
    unittest.main()

