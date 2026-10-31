"""把模型层的情绪原型同步到面板的心情按钮。

为什么必须有：面板里那份 MOOD_PRESETS 是**生成出来的副本**，
以前加心情只改 model.py，面板那份不动，两边就漂了。
现在加完心情跑一下这个脚本，两边永远一致（有测试守着）。

用法：python gen_mood_presets.py
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mind.emotion import model as em  # noqa: E402


def build() -> str:
    items = []
    for proto in em.PROTOTYPES:
        pad = proto.pad
        values = pad if isinstance(pad, (tuple, list)) else (pad.p, pad.a, pad.d)
        label = (proto.emoji + " " + proto.name) if getattr(proto, "emoji", "") else proto.name
        items.append(
            '    { name: "%s", p: %.2f, a: %.2f, d: %.2f }' % (
                label, float(values[0]), float(values[1]), float(values[2])
            )
        )
    return "var MOOD_PRESETS = [" + chr(10) + ("," + chr(10)).join(items) + chr(10) + "  ];"


def main() -> int:
    page = ROOT / "pages" / "mind" / "index.html"
    html = page.read_text(encoding="utf-8")
    start = html.find("var MOOD_PRESETS = [")
    if start < 0:
        print("面板里没有 MOOD_PRESETS，放弃")
        return 1
    end = html.find("];", start)
    html = html[:start] + build() + html[end + 2:]
    page.write_text(html, encoding="utf-8")
    print("面板心情按钮已同步：%d 个" % len(em.PROTOTYPES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
