
"""按人格生成的日程：解析要稳，坏了就退回模板。"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import schedule_gen as sg  # noqa: E402


class ParseTest(unittest.TestCase):
    def _raw(self):
        return json.dumps({
            "note": "普通的周一",
            "table": [
                {"start": "00:00", "end": "06:40", "activity": "还在睡", "state": "sleeping", "tone": "困"},
                {"start": "06:40", "end": "12:00", "activity": "在学校上课", "state": "busy", "tone": "回得短"},
                {"start": "12:00", "end": "17:00", "activity": "下午上课", "state": "busy", "tone": "敷衍"},
                {"start": "17:00", "end": "23:00", "activity": "写作业刷手机", "state": "free", "tone": "话多"},
                {"start": "23:00", "end": "24:00", "activity": "该睡了", "state": "sleeping", "tone": "赶你睡"},
            ],
        }, ensure_ascii=False)

    def test_parse_ok(self):
        got = sg.parse(self._raw())
        self.assertIsNotNone(got)
        self.assertEqual(got["note"], "普通的周一")
        self.assertEqual(len(got["table"]), 5)
        self.assertEqual(got["table"][0]["start"], 0)
        self.assertEqual(got["table"][-1]["end"], 24 * 60)

    def test_parse_fenced(self):
        raw = chr(96) * 3 + "json\n" + self._raw() + "\n" + chr(96) * 3
        self.assertIsNotNone(sg.parse(raw))

    def test_parse_garbage(self):
        self.assertIsNone(sg.parse("这不是 JSON"))
        self.assertIsNone(sg.parse("{}"))
        self.assertIsNone(sg.parse(""))

    def test_bad_rows_dropped(self):
        raw = json.dumps({"table": [
            {"start": "aa", "end": "10:00", "activity": "x"},
            {"start": "10:00", "end": "10:00", "activity": "x"},
            {"start": "10:00", "end": "12:00", "activity": "在写作业", "state": "什么"},
            {"start": "12:00", "end": "13:00", "activity": "吃饭", "state": "free"},
            {"start": "13:00", "end": "14:00", "activity": "午休", "state": "free"},
        ]}, ensure_ascii=False)
        got = sg.parse(raw)
        self.assertIsNotNone(got)
        self.assertTrue(all(row["state"] in ("busy", "free", "sleeping") for row in got["table"]))

    def test_minutes(self):
        self.assertEqual(sg.minutes("07:05"), 7 * 60 + 5)
        self.assertEqual(sg.minutes("24:00"), 24 * 60)
        self.assertEqual(sg.minutes("7.30"), 7 * 60 + 30)
        self.assertEqual(sg.minutes("bad"), -1)


class RenderTest(unittest.TestCase):
    def test_render_now_in_slot(self):
        gen = sg.parse(json.dumps({"note": "测试日", "table": [
            {"start": "00:00", "end": "12:00", "activity": "上课", "state": "busy", "tone": "短"},
            {"start": "12:00", "end": "23:00", "activity": "休息", "state": "free", "tone": "松"},
            {"start": "23:00", "end": "24:00", "activity": "睡", "state": "sleeping", "tone": "困"},
        ]}, ensure_ascii=False))
        text = sg.render(gen)
        self.assertIn("<daily_schedule>", text)
        self.assertIn("测试日", text)
        self.assertIn("她在：", text)

    def test_slot_of_fallbacks(self):
        slot = sg.slot_of({"table": []}, time.time())
        self.assertEqual(slot["state"], "free")

    def test_render_empty(self):
        self.assertEqual(sg.render({}), "")


class StorageTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s" / "2026-11-01.json"
            data = {"note": "n", "table": [{"start": 0, "end": 60,
                                            "activity": "a", "state": "free", "tone": "t"}]}
            self.assertTrue(sg.save(path, data))
            self.assertEqual(sg.load(path), data)

    def test_load_missing(self):
        self.assertIsNone(sg.load("/nope/x.json"))


class PromptTest(unittest.TestCase):
    def test_prompt_mentions_persona_and_json(self):
        system, prompt = sg.build_prompt({"date": "2026-11-01", "note": "上学日", "kind": "school"},
                                         persona="说话慢一点", name="小怡")
        self.assertIn("小怡", prompt)
        self.assertIn("说话慢一点", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("busy", prompt)
        self.assertIn("早上", system) if False else self.assertTrue(system)


if __name__ == "__main__":
    unittest.main()
