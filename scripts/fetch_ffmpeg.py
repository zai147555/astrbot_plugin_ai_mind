#!/usr/bin/env python3
"""把 ffmpeg 二进制拉进插件 vendor 目录（打包/开发用，运行时不依赖它）。

插件运行时自己的兜底顺序是：设置里填的 → vendor 自带 → data/bin → PATH。
vendor 里的二进制不进 git（体积大），只进交付 zip；想重新生成就跑这个脚本。

用法::

    python3 scripts/fetch_ffmpeg.py                 # 当前平台
    python3 scripts/fetch_ffmpeg.py --platform windows
    python3 scripts/fetch_ffmpeg.py --platform linux --force
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mind import ffmpeg_setup  # noqa: E402


def fetch(platform: str, out: Path, force: bool = False) -> int:
    """下载一个平台的 ffmpeg/ffprobe 到 out；返回进程退出码。"""
    exe = "ffmpeg.exe" if platform == "windows" else "ffmpeg"
    target = out / exe
    if target.is_file() and not force:
        print("已经有 %s（加 --force 覆盖）" % target)
        return 0
    urls = list(ffmpeg_setup.MIRRORS.get(platform) or ())
    if not urls:
        print("不认识的平台：%s（可选 windows / linux）" % platform)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch_ffmpeg_") as tmp:
        for url in urls:
            archive = Path(tmp) / ("ffmpeg" + (".zip" if url.endswith(".zip") else ".pkg"))
            print("下载 " + url)
            err = ffmpeg_setup._download(url, archive)
            if err:
                print("  失败：" + err)
                continue
            try:
                files = ffmpeg_setup._extract(archive, out)
            except Exception as exc:  # noqa: BLE001 - 坏包就换下一个源
                print("  解包失败：%s: %s" % (type(exc).__name__, exc))
                continue
            if not files:
                print("  包里没有 ffmpeg/ffprobe")
                continue
            print("  -> " + ", ".join(item.name for item in files))
            if target.is_file():
                ok, detail = ffmpeg_setup.verify(str(target))
                print("  " + ("能跑：" if ok else "跑不起来：") + detail)
                return 0 if ok else 1
    print("所有下载源都不通")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 ffmpeg 到插件 vendor 目录")
    parser.add_argument("--platform", default=ffmpeg_setup.current_platform(),
                        help="windows / linux（默认当前平台）")
    parser.add_argument("--out", default="",
                        help="输出目录（默认 vendor/ffmpeg/<平台>）")
    parser.add_argument("--force", action="store_true", help="已有也重新下")
    args = parser.parse_args()
    out = Path(args.out) if args.out else         ROOT / "vendor" / "ffmpeg" / args.platform
    return fetch(args.platform, out, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
