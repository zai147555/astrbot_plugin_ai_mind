"""全部 .py 的语法体检。

为什么单独一个脚本：单元测试只会 import 到被覆盖到的模块，
一个没被任何测试碰过的文件语法坏了，测试照样全绿 —— 等插件加载时才炸。
这个脚本挨个 compile 一遍，一个都不放过。

用法：python syntax_check.py
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {"__pycache__", ".git", "data", "tests"}


def main() -> int:
    files = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        files.append(path)

    errors = []
    for path in files:
        try:
            py_compile.compile(str(path), doraise=True, cfile=None)
        except py_compile.PyCompileError as exc:
            last = str(exc).strip().splitlines()[-1]
            errors.append((path.relative_to(ROOT), last))
        except SyntaxError as exc:
            errors.append((path.relative_to(ROOT), exc.msg + " (line " + str(exc.lineno) + ")"))

    for rel, msg in errors:
        print("  X " + str(rel) + ": " + msg)
    print()
    print(str(len(files)) + " 文件，" + str(len(errors)) + " 错误")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

