"""面板按钮体检：每个 button 都要有处理函数，点完都要有反馈。

为什么必须有这个脚本：面板是零构建的原生 HTML，按钮漏绑一个不会报错，
只会「点了没反应」—— 用户看到的就是「这个按钮是坏的」。

判定口径（宽进严出）：
  - 有 id 的按钮：脚本里必须出现按钮 id 的引用（直接绑 / 事件委托 / 动态插入都算）；
  - 没有 id 的按钮：无法审计，单独列出来提醒；
  - 显式声明 data-nobind 的跳过。

用法：python audit_buttons.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "pages" / "mind" / "index.html"

BUTTON_RE = re.compile(r"<button\b([^>]*)>", re.I)
ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.I)


def main() -> int:
    html = PAGE.read_text(encoding="utf-8")
    scripts = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))

    buttons = []
    for match in BUTTON_RE.finditer(html):
        attrs = match.group(1)
        if "data-nobind" in attrs:
            continue
        found = ID_RE.search(attrs)
        buttons.append(found.group(1) if found else None)

    with_id = [b for b in buttons if b]
    no_id = [b for b in buttons if not b]

    unbound = []
    for bid in with_id:
        patterns = (
            '$("' + bid + '")',
            "$('" + bid + "')",
            'getElementById("' + bid + '")',
            "getElementById('" + bid + "')",
            "#" + bid + '"',
            "#" + bid + "'",
        )
        if not any(pat in scripts for pat in patterns):
            unbound.append(bid)

    for bid in sorted(unbound):
        print("  X 按钮 #" + bid + " 在脚本里找不到任何引用（点了不会有反应）")

    print()
    print(str(len(buttons)) + " 个按钮，" + str(len(unbound)) + " 个没绑定")
    if no_id:
        print("（另有 " + str(len(no_id)) + " 个按钮没有 id，无法审计 —— 建议补上 id）")
    return 1 if unbound else 0


if __name__ == "__main__":
    sys.exit(main())

