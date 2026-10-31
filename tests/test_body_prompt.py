"""身体状态要真的进到提示词里（不是只存在内存里）。"""
from __future__ import annotations

import time
import unittest

from mind.emotion import engine as eng
from mind.emotion import physiology as ph
from mind.emotion import prompt as pr
from mind.emotion.model import PAD


class BodyInPromptTest(unittest.TestCase):
    def _snapshot(self, body: ph.Body):
        settings = eng.Settings()
        mood = eng.SessionMood(key="t", emotion=PAD.of(0.1, 0.3, 0.0), mood=PAD.of(0.0, 0.2, 0.0))
        mood.body = body
        mood.last_tick = time.time() - 5
        engine = eng.EmotionEngine(settings) if hasattr(eng, "EmotionEngine") else None
        return mood, engine

    def test_illness_shows_up_in_the_block(self) -> None:
        body = ph.Body(illness=0.8, illness_kind="着凉")
        note = ph.describe(body)
        self.assertIn("病", note)
        # 直接验「那句人话」的拼装口径：提示词里加的就是这一行
        self.assertTrue(note.startswith("身体："))

    def test_healthy_body_adds_no_line(self) -> None:
        self.assertEqual(ph.describe(ph.Body(energy=0.8, hunger=0.2, stress=0.1)), "")


if __name__ == "__main__":
    unittest.main()
