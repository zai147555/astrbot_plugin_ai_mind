"""QQ 空间：日记库、拼内容、解析返回。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import qzone  # noqa: E402


def _store() -> qzone.DiaryStore:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return qzone.DiaryStore(conn)


class TextTest(unittest.TestCase):
    def test_day_text(self):
        import datetime
        ts = datetime.datetime(2026, 11, 1, 9, 30).timestamp()
        self.assertEqual(qzone.day_text(ts), "2026年11月1日")

    def test_feed_time_text(self):
        import datetime
        ts = datetime.datetime(2026, 11, 1, 21, 5).timestamp()
        self.assertEqual(qzone.feed_time_text(ts), "11月1日 21:05")

    def test_feed_time_text_bad_value(self):
        self.assertEqual(qzone.feed_time_text(0), "1月1日 08:00")

    def test_strip_fence(self):
        body = qzone.strip_fence("```json\n{\"a\": 1}\n```")
        self.assertEqual(body, '{"a": 1}')

    def test_tidy_body_joins_cjk(self):
        self.assertEqual(qzone.tidy_body("下 午 很 热 ，  外面 吵"), "下午很热，外面吵")

    def test_tidy_body_keeps_english_words(self):
        self.assertEqual(qzone.tidy_body("我在听 QQ 音乐"), "我在听 QQ 音乐")

    def test_tidy_body_collapses_blank_lines(self):
        self.assertEqual(qzone.tidy_body("a\n\n\n\nb"), "a\n\nb")


class ParseDiaryTest(unittest.TestCase):
    def test_json(self):
        got = qzone.parse_diary('{"title": "放学", "body": "路上买了个包子。", "mood": "开心"}')
        self.assertEqual(got["title"], "放学")
        self.assertEqual(got["body"], "路上买了个包子。")
        self.assertEqual(got["mood"], "开心")

    def test_json_in_fence(self):
        raw = '```json\n{"title": "夜里", "body": "睡不着。"}\n```'
        self.assertEqual(qzone.parse_diary(raw)["body"], "睡不着。")

    def test_content_field(self):
        got = qzone.parse_diary('{"content": "今天去了河边。", "mood": "平静"}')
        self.assertEqual(got["body"], "今天去了河边。")

    def test_plain_text(self):
        got = qzone.parse_diary("今天什么都没干，就躺着。")
        self.assertEqual(got["body"], "今天什么都没干，就躺着。")
        self.assertTrue(got["title"].startswith("今天"))

    def test_empty(self):
        self.assertEqual(qzone.parse_diary(""), {"title": "", "body": "", "mood": ""})

    def test_title_cut_short(self):
        got = qzone.parse_diary('{"title": "' + "标题" * 30 + '", "body": "x"}')
        self.assertLessEqual(len(got["title"]), 24)

    def test_broken_json_falls_back(self):
        got = qzone.parse_diary('{"title": "没闭合"')
        self.assertIn("没闭合", got["body"])


class PromptTest(unittest.TestCase):
    def test_diary_prompt_has_json_hint(self):
        system, prompt = qzone.diary_prompt("2026年11月1日", "想念")
        self.assertIn("14 岁", system)
        self.assertIn("2026年11月1日", prompt)
        self.assertIn("想念", prompt)
        self.assertIn("JSON", prompt)

    def test_diary_prompt_extra_and_custom_system(self):
        system, prompt = qzone.diary_prompt("d", "m", persona="你自己的写法", extra="不要写人")
        self.assertEqual(system, "你自己的写法")
        self.assertIn("不要写人", prompt)

    def test_image_prompt_mentions_scene(self):
        text = qzone.image_prompt("傍晚", "我在阳台上看云，风很大。", "水彩")
        self.assertIn("阳台", text)
        self.assertIn("水彩", text)
        self.assertIn("不要文字", text)


class SettingsTest(unittest.TestCase):
    def test_defaults_are_safe(self):
        conf = qzone.QzoneSettings()
        self.assertFalse(conf.auto_post)
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.ugc_right, 1)

    def test_roundtrip(self):
        conf = qzone.QzoneSettings(auto_post=True, auto_hour=8, ugc_right=64,
                                   signature="——可怡", max_chars=200)
        again = qzone.QzoneSettings.from_dict(conf.to_dict())
        self.assertTrue(again.auto_post)
        self.assertEqual(again.auto_hour, 8)
        self.assertEqual(again.ugc_right, 64)
        self.assertEqual(again.signature, "——可怡")
        self.assertEqual(again.max_chars, 200)

    def test_clamp(self):
        conf = qzone.QzoneSettings.from_dict({
            "auto_hour": 99, "auto_minute": -1, "ugc_right": 7, "max_chars": 1,
        })
        self.assertEqual(conf.auto_hour, 23)
        self.assertEqual(conf.auto_minute, 0)
        self.assertEqual(conf.ugc_right, 1)
        self.assertEqual(conf.max_chars, 80)

    def test_from_dict_junk(self):
        conf = qzone.QzoneSettings.from_dict("nope")
        self.assertTrue(conf.enabled)

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qzone.json"
            self.assertTrue(qzone.save_settings(path, qzone.QzoneSettings(auto_post=True)))
            loaded = qzone.load_settings(path)
            self.assertTrue(loaded.auto_post)
            self.assertTrue(qzone.save_settings(Path(tmp) / "x" / "qzone.json", loaded))

    def test_load_missing(self):
        self.assertTrue(qzone.load_settings("/nope/qzone.json").enabled)


class StoreTest(unittest.TestCase):
    def test_add_get(self):
        store = _store()
        ident = store.add("今天", "下 午 很 热", "闷闷的")
        self.assertGreater(ident, 0)
        entry = store.get(ident)
        self.assertEqual(entry.title, "今天")
        self.assertEqual(entry.status, "draft")

    def test_update_and_publish(self):
        store = _store()
        ident = store.add("a", "b")
        self.assertTrue(store.mark_published(ident, "tid-1"))
        entry = store.get(ident)
        self.assertEqual(entry.status, "published")
        self.assertEqual(entry.tid, "tid-1")
        self.assertGreater(entry.published_at, 0)

    def test_mark_failed(self):
        store = _store()
        ident = store.add("a", "b")
        store.mark_failed(ident, "没权限")
        entry = store.get(ident)
        self.assertEqual(entry.status, "failed")
        self.assertEqual(entry.error, "没权限")

    def test_latest_draft_falls_back_to_failed(self):
        store = _store()
        ident = store.add("a", "b")
        store.mark_failed(ident, "x")
        self.assertEqual(store.latest_draft().id, ident)

    def test_recent_and_stats(self):
        store = _store()
        one = store.add("a", "b")
        store.mark_published(one, "t")
        store.add("c", "d")
        self.assertEqual(store.count(), 2)
        self.assertEqual(store.count("published"), 1)
        stats = store.stats()
        self.assertEqual((stats["total"], stats["drafts"], stats["published"]), (2, 1, 1))
        self.assertEqual(len(store.recent(10)), 2)
        self.assertEqual(len(store.recent(10, status="draft")), 1)

    def test_remove(self):
        store = _store()
        ident = store.add("a", "b")
        self.assertTrue(store.remove(ident))
        self.assertIsNone(store.get(ident))

    def test_update_ignores_unknown_fields(self):
        store = _store()
        ident = store.add("a", "b")
        self.assertTrue(store.update(ident, title="c", hacked=1))
        entry = store.get(ident)
        self.assertEqual(entry.title, "c")
        self.assertEqual(entry.body, "b")
        self.assertFalse(store.update(ident, hacked=1))

    def test_no_connection_is_safe(self):
        store = qzone.DiaryStore(None)
        self.assertEqual(store.add("a", "b"), 0)
        self.assertEqual(store.recent(), [])
        self.assertIsNone(store.get(1))
        self.assertEqual(store.stats()["total"], 0)

    def test_diary_to_dict(self):
        entry = qzone.Diary(id=1, title="t", body="b", status="published",
                            tid="x", created_at=0)
        data = entry.to_dict()
        self.assertEqual(data["status_text"], "已发")
        self.assertIn("time_text", data)


class ComposeTest(unittest.TestCase):
    def test_body_only(self):
        self.assertEqual(qzone.compose_content({"body": "今天 很 好"}), "今天很好")

    def test_title_fallback(self):
        self.assertEqual(qzone.compose_content({"title": "只是标题"}), "只是标题")

    def test_signature(self):
        self.assertTrue(qzone.compose_content({"body": "嗯"}, "——可怡").endswith("——可怡"))

    def test_limit(self):
        text = qzone.compose_content({"body": "字" * 300}, "", limit=200)
        self.assertLessEqual(len(text), 200)
        self.assertTrue(text.endswith("…"))


class ImageTest(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(qzone.image_data_uri("/nope.png"), "")

    def test_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(b"\x89PNG\r\n")
            uri = qzone.image_data_uri(path)
            self.assertTrue(uri.startswith("base64://"))
            self.assertEqual(qzone.image_data_uri(Path(tmp)), "")


class SendParamsTest(unittest.TestCase):
    def test_basic(self):
        params = qzone.build_send_params("hi", [], 1)
        self.assertEqual(params["content"], "hi")
        self.assertNotIn("images", params)
        self.assertEqual(params["ugc_right"], 1)

    def test_images_capped(self):
        params = qzone.build_send_params("hi", ["a"] * 20, 4)
        self.assertEqual(len(params["images"]), qzone.MAX_IMAGES)

    def test_invalid_right_falls_back(self):
        self.assertEqual(qzone.build_send_params("x", [], 999)["ugc_right"], 1)

    def test_target_uins_only_when_partial(self):
        self.assertNotIn("target_uins", qzone.build_send_params("x", [], 1, ["1"]))
        self.assertEqual(qzone.build_send_params("x", [], 16, ["1"])["target_uins"], ["1"])


class ParseSendTest(unittest.TestCase):
    def test_plain(self):
        got = qzone.parse_send({"tid": "t1", "time": 100})
        self.assertTrue(got["ok"])
        self.assertEqual(got["tid"], "t1")
        self.assertEqual(got["time"], 100)

    def test_wrapped(self):
        got = qzone.parse_send({"status": "ok", "retcode": 0, "data": {"tid": "t2"}})
        self.assertTrue(got["ok"])
        self.assertEqual(got["tid"], "t2")

    def test_retcode_failure(self):
        got = qzone.parse_send({"status": "failed", "retcode": 120, "message": "权限不足"})
        self.assertFalse(got["ok"])
        self.assertIn("权限不足", got["error"])

    def test_no_tid(self):
        got = qzone.parse_send({"status": "ok"})
        self.assertFalse(got["ok"])
        self.assertIn("tid", got["error"])

    def test_none(self):
        self.assertFalse(qzone.parse_send(None)["ok"])


class ParseFeedsTest(unittest.TestCase):
    def test_msglist(self):
        rows = qzone.parse_feeds({"msglist": [{
            "tid": "t1", "content": "今天", "created_time": 10,
            "pic": [{"url1": "http://x/1.jpg"}], "likes": [1, 2], "commentlist": [{}],
        }]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tid"], "t1")
        self.assertEqual(rows[0]["likes"], 2)
        self.assertEqual(rows[0]["comments"], 1)
        self.assertEqual(rows[0]["images"], ["http://x/1.jpg"])

    def test_wrapped(self):
        rows = qzone.parse_feeds({"data": {"list": [{"tid": "t2", "content": "x"}]}})
        self.assertEqual(rows[0]["tid"], "t2")

    def test_skips_rows_without_tid(self):
        self.assertEqual(qzone.parse_feeds({"msglist": [{"content": "x"}, 1, None]}), [])

    def test_junk(self):
        self.assertEqual(qzone.parse_feeds(None), [])
        self.assertEqual(qzone.parse_feeds({"msglist": "nope"}), [])


class ProfileTest(unittest.TestCase):
    def test_plain(self):
        got = qzone.parse_profile({"uin": 123, "nickname": "杜可怡"})
        self.assertEqual(got["uin"], "123")
        self.assertEqual(got["nickname"], "杜可怡")

    def test_wrapped(self):
        got = qzone.parse_profile({"data": {"nickname": "n"}})
        self.assertEqual(got["nickname"], "n")


class DueTest(unittest.TestCase):
    def test_auto_off(self):
        self.assertFalse(qzone.due_daily(qzone.QzoneSettings(), 1e9, 0))

    def test_not_yet(self):
        import datetime
        conf = qzone.QzoneSettings(auto_post=True, auto_hour=21, auto_minute=0)
        now = datetime.datetime(2026, 11, 1, 20, 0).timestamp()
        self.assertFalse(qzone.due_daily(conf, now))

    def test_due_once(self):
        import datetime
        conf = qzone.QzoneSettings(auto_post=True, auto_hour=21, auto_minute=0)
        morning = datetime.datetime(2026, 11, 1, 8, 0).timestamp()
        evening = datetime.datetime(2026, 11, 1, 21, 30).timestamp()
        self.assertTrue(qzone.due_daily(conf, evening, morning))
        self.assertFalse(qzone.due_daily(conf, evening, evening))
        next_evening = datetime.datetime(2026, 11, 2, 21, 30).timestamp()
        self.assertTrue(qzone.due_daily(conf, next_evening, evening))
        self.assertFalse(qzone.due_daily(conf, datetime.datetime(2026, 11, 2, 8, 0).timestamp(), evening))


if __name__ == "__main__":
    unittest.main()
