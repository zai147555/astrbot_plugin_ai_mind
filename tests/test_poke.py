
"""戳一戳：识别、冷却、提示词。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mind import poke


def raw(**over):
    base = {"post_type": "notice", "notice_type": "poke", "sub_type": "poke",
            "target_id": 10001, "user_id": 20002, "group_id": 30003}
    base.update(over)
    return base


class DetectTest(unittest.TestCase):
    def test_is_poke(self) -> None:
        self.assertTrue(poke.is_poke(raw()))
        self.assertFalse(poke.is_poke({"post_type": "message"}))
        self.assertFalse(poke.is_poke(None))
        self.assertFalse(poke.is_poke({}))

    def test_fields(self) -> None:
        self.assertEqual(poke.poke_target(raw()), "10001")
        self.assertEqual(poke.poke_sender(raw()), "20002")
        self.assertEqual(poke.poke_group(raw()), "30003")
        self.assertEqual(poke.poke_group(raw(group_id=None)), "")

    def test_hits_me(self) -> None:
        self.assertTrue(poke.hits_me(raw(), "10001"))
        self.assertFalse(poke.hits_me(raw(), "99999"))
        self.assertFalse(poke.hits_me(raw(), ""))


class GateTest(unittest.TestCase):
    def _conf(self, **over) -> poke.PokeSettings:
        base = dict(enabled=True, user_cooldown=8.0, group_cooldown=20.0)
        base.update(over)
        return poke.PokeSettings(**base)

    def test_first_poke_allowed(self) -> None:
        gate = poke.PokeGate(self._conf())
        self.assertTrue(gate.allow(1000.0, sender="u1", group="g1")[0])

    def test_user_cooldown(self) -> None:
        gate = poke.PokeGate(self._conf(group_cooldown=0.0))
        ok, _ = gate.allow(1000.0, sender="u1", group="g1")
        self.assertTrue(ok)
        gate.mark(1000.0, sender="u1", group="g1")
        self.assertFalse(gate.allow(1003.0, sender="u1", group="g1")[0])
        self.assertTrue(gate.allow(1010.0, sender="u1", group="g1")[0])

    def test_group_cooldown_blocks_others(self) -> None:
        gate = poke.PokeGate(self._conf(user_cooldown=0.0))
        gate.mark(1000.0, sender="u1", group="g1")
        self.assertFalse(gate.allow(1005.0, sender="u2", group="g1")[0])

    def test_private_has_no_group_cooldown(self) -> None:
        gate = poke.PokeGate(self._conf(user_cooldown=0.0))
        gate.mark(1000.0, sender="u1", group="g1")
        self.assertTrue(gate.allow(1001.0, sender="u2", group="")[0])

    def test_blacklist(self) -> None:
        gate = poke.PokeGate(self._conf(blacklist=["bad"]))
        self.assertFalse(gate.allow(1000.0, sender="bad", group="g1")[0])

    def test_scope_switches(self) -> None:
        gate = poke.PokeGate(self._conf(in_groups=False))
        self.assertFalse(gate.allow(1000.0, sender="u1", group="g1")[0])
        gate = poke.PokeGate(self._conf(in_private=False))
        self.assertFalse(gate.allow(1000.0, sender="u1", group="")[0])

    def test_admin_bypasses(self) -> None:
        gate = poke.PokeGate(self._conf(blacklist=["boss"]))
        self.assertTrue(gate.allow(1000.0, sender="boss", group="g1", is_admin=True)[0])

    def test_prune(self) -> None:
        gate = poke.PokeGate(self._conf())
        gate.mark(1000.0, sender="u1", group="g1")
        self.assertGreaterEqual(gate.prune(10000.0), 1)


class PromptTest(unittest.TestCase):
    def test_substitutes(self) -> None:
        text = poke.build_prompt(poke.PokeSettings(), username="宝宝",
                                 user_id="42", scene="群聊")
        self.assertIn("宝宝", text)
        self.assertIn("42", text)
        self.assertIn("群聊", text)
        self.assertNotIn("$username", text)

    def test_no_context_note(self) -> None:
        text = poke.build_prompt(poke.PokeSettings(), username="a", user_id="1",
                                 scene="私聊", has_context=False)
        self.assertIn("没有可用的上下文", text)

    def test_custom_prompt(self) -> None:
        conf = poke.PokeSettings(prompt="戳我干嘛，$username")
        self.assertEqual(poke.build_prompt(conf, username="他", user_id="1", scene="群聊"),
                         "戳我干嘛，他")

    def test_broken_template_does_not_raise(self) -> None:
        conf = poke.PokeSettings(prompt="坏模板 $")
        self.assertIn("坏模板", poke.build_prompt(conf, username="x", user_id="1", scene="群聊"))


class SettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        conf = poke.PokeSettings()
        self.assertTrue(conf.enabled)
        self.assertEqual(conf.user_cooldown, 8.0)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poke.json"
            conf = poke.PokeSettings(user_cooldown=3.0, blacklist=["x"])
            self.assertTrue(poke.save_settings(path, conf))
            self.assertEqual(poke.load_settings(path).to_dict(), conf.to_dict())

    def test_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poke.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertTrue(poke.load_settings(path).enabled)

    def test_clamps(self) -> None:
        conf = poke.PokeSettings.from_dict({"user_cooldown": -5, "group_cooldown": "x"})
        self.assertEqual(conf.user_cooldown, 8.0)
        self.assertEqual(conf.group_cooldown, 20.0)


if __name__ == "__main__":
    unittest.main()

