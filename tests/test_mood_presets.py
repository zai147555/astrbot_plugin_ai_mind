"""面板的心情按钮必须和模型层同步（防漂移）。

为什么要有这条：面板里那份 MOOD_PRESETS 是**生成出来的副本**。
以前加心情只改 model.py，面板那份不动 —— 两边漂了没人发现。
现在谁只改一边，这条就红；跑 python gen_mood_presets.py 即可修好。
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mind.emotion import model as em  # noqa: E402


class MoodPresetSyncTest(unittest.TestCase):
    def test_panel_matches_prototypes(self) -> None:
        html = (ROOT / "pages" / "mind" / "index.html").read_text(encoding="utf-8")
        match = re.search(r"var MOOD_PRESETS = \[(.*?)\];", html, re.S)
        self.assertIsNotNone(match, "面板里找不到 MOOD_PRESETS")
        names = re.findall(r'name: "([^"]+)"', match.group(1))

        expected = [
            (proto.emoji + " " + proto.name) if getattr(proto, "emoji", "") else proto.name
            for proto in em.PROTOTYPES
        ]
        self.assertEqual(
            names, expected,
            "面板心情按钮和模型层不同步 —— 跑一下 python gen_mood_presets.py",
        )

    def test_no_duplicate_prototypes(self) -> None:
        names = [proto.name for proto in em.PROTOTYPES]
        self.assertEqual(
            len(names), len(set(names)),
            "情绪原型里有重复注册：" + str([n for n in names if names.count(n) > 1]),
        )


if __name__ == "__main__":
    unittest.main()

