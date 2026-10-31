
"""表情包库：收/去重/打标/按情绪挑。"""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from mind.memory.store import MemoryStore
from mind import sticker as st

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
GIF = b"GIF89a" + b"y" * 32


class SettingsTest(unittest.TestCase):
    def test_defaults_off(self) -> None:
        conf = st.StickerSettings()
        self.assertFalse(conf.enabled, "默认关：先收一会儿再开")
        self.assertTrue(conf.collect)

    def test_clamps(self) -> None:
        conf = st.StickerSettings.from_dict({"chance": 9, "per_hour": 99, "max_bytes": 1})
        self.assertEqual(conf.chance, 1.0)
        self.assertEqual(conf.per_hour, 20)
        self.assertEqual(conf.max_bytes, 64 * 1024)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sticker.json"
            conf = st.StickerSettings(enabled=True, chance=0.5)
            self.assertTrue(st.save_settings(path, conf))
            self.assertEqual(st.load_settings(path).to_dict(), conf.to_dict())


class TagTest(unittest.TestCase):
    def test_guess_from_name(self) -> None:
        self.assertIn("joy", st.guess_emotions("哈哈大笑.png"))
        self.assertIn("sad", st.guess_emotions("t_T_cry.gif"))
        self.assertEqual(st.guess_emotions("图1.png"), [])

    def test_emotion_join_parse(self) -> None:
        joined = st.join_emotions(["joy", "joy", " shy "])
        self.assertEqual(joined, "joy,shy")
        self.assertEqual(st.parse_emotions(joined), ["joy", "shy"])


class StoreTest(unittest.TestCase):
    def _store(self, tmp: str) -> st.StickerStore:
        return st.StickerStore(MemoryStore(Path(tmp) / "mind.db", None), None)

    def test_add_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            folder = Path(tmp) / "stickers"
            first = store.add(PNG, folder, name="笑.png", emotions=["joy"])
            self.assertIsNotNone(first)
            self.assertTrue(Path(first.path).is_file())
            again = store.add(PNG, folder, name="笑2.png")
            self.assertEqual(again.id, first.id, "同内容只存一份")
            self.assertEqual(store.stats()["total"], 1)

    def test_suffix_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            item = store.add(GIF, Path(tmp) / "s", name="a.gif")
            self.assertTrue(item.path.endswith(".gif"))

    def test_too_big_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertIsNone(store.add(b"x" * (200 * 1024), Path(tmp) / "s", max_bytes=64 * 1024))

    def test_tag_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            item = store.add(PNG, Path(tmp) / "s")
            self.assertTrue(store.tag(item.id, desc="狗头", emotions=["bored"]))
            got = store.get(item.id)
            self.assertEqual(got.desc, "狗头")
            self.assertEqual(got.emotions, ("bored",))
            self.assertEqual(store.stats()["tagged"], 1)

    def test_delete_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            item = store.add(PNG, Path(tmp) / "s")
            path = Path(item.path)
            self.assertTrue(store.delete(item.id))
            self.assertFalse(path.exists())
            self.assertEqual(store.stats()["total"], 0)

    def test_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add(PNG, Path(tmp) / "s")
            store.add(GIF, Path(tmp) / "s", name="a.gif")
            self.assertEqual(store.clear(), 2)
            self.assertEqual(store.stats()["total"], 0)


class PickTest(unittest.TestCase):
    def _seed(self, tmp: str):
        store = st.StickerStore(MemoryStore(Path(tmp) / "mind.db", None), None)
        folder = Path(tmp) / "s"
        joy = store.add(PNG, folder, emotions=["joy"])
        sad = store.add(GIF, folder, name="a.gif", emotions=["sad"])
        return store, joy, sad

    def test_prefers_matching_emotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, joy, sad = self._seed(tmp)
            picked = store.pick("joy", rng=random.Random(1))
            self.assertEqual(picked.id, joy.id)

    def test_falls_back_when_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, joy, sad = self._seed(tmp)
            self.assertIsNotNone(store.pick("angry"))

    def test_avoid_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, joy, sad = self._seed(tmp)
            picked = store.pick("joy", avoid=[joy.id], rng=random.Random(1))
            self.assertNotEqual(picked.id, joy.id)

    def test_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = st.StickerStore(MemoryStore(Path(tmp) / "mind.db", None), None)
            self.assertIsNone(store.pick("joy"))

    def test_mark_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, joy, _sad = self._seed(tmp)
            store.mark_used(joy.id)
            self.assertEqual(store.get(joy.id).hits, 1)


if __name__ == "__main__":
    unittest.main()

