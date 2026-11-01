
"""AI 声聊：念稿清洗、发送判断、音色解析。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mind import voicechat as vc


class CleanTest(unittest.TestCase):
    def test_strips_urls_and_markdown(self) -> None:
        text = vc.clean_for_voice("看这个 https://example.com/x 超好笑**真的**")
        self.assertNotIn("http", text)
        self.assertNotIn("**", text)
        self.assertIn("超好笑真的", text)

    def test_strips_brackets_and_emoji(self) -> None:
        text = vc.clean_for_voice("（笑）你来了😊✨")
        self.assertNotIn("笑", text)
        self.assertNotIn("😊", text)
        self.assertIn("你来了", text)

    def test_collapses_whitespace_and_newlines(self) -> None:
        self.assertEqual(vc.clean_for_voice("你  好\n\n在吗"), "你 好 在吗")

    def test_caps_length(self) -> None:
        text = vc.clean_for_voice("啊" * 400, max_chars=50)
        self.assertLessEqual(len(text), 51)
        self.assertTrue(text.endswith("…"))

    def test_empty(self) -> None:
        self.assertEqual(vc.clean_for_voice("   "), "")
        self.assertEqual(vc.clean_for_voice(None), "")


class ShouldSendTest(unittest.TestCase):
    def _conf(self, **over) -> vc.VoiceSettings:
        base = dict(enabled=True, mode=vc.MODE_ALWAYS, chance=0.0, cooldown=0.0)
        base.update(over)
        return vc.VoiceSettings(**base)

    def test_basics(self) -> None:
        self.assertTrue(vc.should_send(self._conf(), group="1", platform="aiocqhttp"))

    def test_disabled(self) -> None:
        self.assertFalse(vc.should_send(self._conf(enabled=False), group="1", platform="aiocqhttp"))

    def test_platform_only_qq(self) -> None:
        self.assertFalse(vc.should_send(self._conf(), group="1", platform="telegram"))

    def test_group_only(self) -> None:
        self.assertFalse(vc.should_send(self._conf(), group="", platform="aiocqhttp"))

    def test_allowlist(self) -> None:
        conf = self._conf(groups=["100"])
        self.assertTrue(vc.should_send(conf, group="100", platform="aiocqhttp"))
        self.assertFalse(vc.should_send(conf, group="200", platform="aiocqhttp"))

    def test_cooldown(self) -> None:
        conf = self._conf(cooldown=10.0)
        self.assertFalse(vc.should_send(conf, group="1", platform="aiocqhttp",
                                       last_at=100.0, now=105.0))
        self.assertTrue(vc.should_send(conf, group="1", platform="aiocqhttp",
                                      last_at=100.0, now=112.0))

    def test_chance_mode(self) -> None:
        conf = self._conf(mode=vc.MODE_CHANCE, chance=0.5)
        self.assertTrue(vc.should_send(conf, group="1", platform="aiocqhttp", rng=lambda: 0.1))
        self.assertFalse(vc.should_send(conf, group="1", platform="aiocqhttp", rng=lambda: 0.9))


class CharacterTest(unittest.TestCase):
    def test_list_shape(self) -> None:
        payload = {"status": "ok", "data": [
            {"character_id": "c1", "character_name": "小怡"},
            {"id": "c2", "name": "麦麦"},
        ]}
        rows = vc.parse_characters(payload)
        self.assertEqual([row["id"] for row in rows], ["c1", "c2"])
        self.assertEqual(rows[0]["name"], "小怡")

    def test_dict_shape(self) -> None:
        payload = {"data": {"list": [{"character_id": "x", "nickname": "X"}]}}
        self.assertEqual(vc.parse_characters(payload)[0]["id"], "x")

    def test_garbage(self) -> None:
        self.assertEqual(vc.parse_characters(None), [])
        self.assertEqual(vc.parse_characters({"data": "nope"}), [])
        self.assertEqual(vc.parse_characters([{}]), [])

    def test_pick(self) -> None:
        rows = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        self.assertEqual(vc.pick_character(vc.VoiceSettings(), rows), "a")
        self.assertEqual(vc.pick_character(vc.VoiceSettings(character="b"), rows), "b")
        self.assertEqual(vc.pick_character(vc.VoiceSettings(character="zz"), rows), "zz")
        self.assertEqual(vc.pick_character(vc.VoiceSettings(), []), "")


class SettingsTest(unittest.TestCase):
    def test_defaults_off(self) -> None:
        conf = vc.VoiceSettings()
        self.assertFalse(conf.enabled, "默认关：先确认群支持 AI 声聊再开")
        self.assertEqual(conf.mode, vc.MODE_ALWAYS)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.json"
            conf = vc.VoiceSettings(enabled=True, mode=vc.MODE_CHANCE, chance=0.2)
            self.assertTrue(vc.save_settings(path, conf))
            self.assertEqual(vc.load_settings(path).to_dict(), conf.to_dict())

    def test_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertFalse(vc.load_settings(path).enabled)

    def test_clamps(self) -> None:
        conf = vc.VoiceSettings.from_dict({"mode": "weird", "chance": 5,
                                           "max_chars": 1, "cooldown": -3})
        self.assertEqual(conf.mode, vc.MODE_ALWAYS)
        self.assertEqual(conf.chance, 1.0)
        self.assertEqual(conf.max_chars, 20)
        self.assertEqual(conf.cooldown, 0.0)


if __name__ == "__main__":
    unittest.main()

