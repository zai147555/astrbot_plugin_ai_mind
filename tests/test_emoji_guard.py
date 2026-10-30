"""Emoji 红线守卫：不许用 Emoji 14.0 及以上的新字。

为什么：老系统（旧版 Android / Windows）画不出新 emoji，会显示成方框 □。
用户已经因此报过一次「心事重重和有点黏人的 emoji 显示成方框」。
新字每次都是「顺手挑个好看的」，没有守卫就会反复犯。

规则：本文件里的 NEW_EMOJI 是 Emoji 14.0+ 新增字，扫到就算红。
要加新 emoji 前先在这张表里查一下。
"""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Emoji 14.0 及以上新增（老系统画不出来）
NEW_EMOJI = {
    0x1F979: "🥹", 0x1FAE0: "🫠", 0x1FAE1: "🫡", 0x1FAE2: "🫢",
    0x1FAE3: "🫣", 0x1FAE4: "🫤", 0x1FAE5: "🫥", 0x1FAE8: "🫨",
    0x1FAB7: "🪷", 0x1FAB8: "🪸", 0x1FAB9: "🪹", 0x1FABA: "🪺",
    0x1FABB: "🪻", 0x1FABC: "🪼", 0x1FABD: "🪽", 0x1FABF: "🪿",
    0x1FA77: "🩷", 0x1FA78: "🩸", 0x1FAAD: "🪭", 0x1FAAE: "🪮",
    0x1FAC5: "🫅", 0x1FAC6: "🫆", 0x1FAC8: "🫈", 0x1FADA: "🫚",
    0x1FADB: "🫛", 0x1FADC: "🫜", 0x1FADF: "🫟", 0x1FA89: "🪉",
    0x1FA8F: "🪏",
}


def _scan() -> list[str]:
    problems: list[str] = []
    targets = list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.html"))
    own = pathlib.Path(__file__).resolve()
    for path in targets:
        if path.resolve() == own:
            continue          # 别把自己的字典算进去
        if "__pycache__" in str(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(ROOT)
        for lineno, line in enumerate(text.splitlines(), 1):
            for ch in line:
                if ord(ch) in NEW_EMOJI:
                    problems.append(str(rel) + ":" + str(lineno) + " 用了 " + ch)
    return problems


class EmojiRedLineTest(unittest.TestCase):
    def test_no_emoji_newer_than_13(self) -> None:
        problems = _scan()
        self.assertEqual(
            problems, [],
            "这些地方用了 Emoji 14.0+ 的新字，老系统会显示成方框：\n  "
            + "\n  ".join(problems),
        )


if __name__ == "__main__":
    print("\n".join(_scan()) or "没有新 emoji")

