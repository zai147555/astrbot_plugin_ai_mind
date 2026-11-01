
"""帮用户把 ffmpeg 弄进插件自己的目录里。

为什么要有这一层：分镜/动图/听音频都靠 ffmpeg，而 Windows 用户大多数没有它，
让每个人自己下 zip、解压、加 PATH、重启，是这类功能最常见的卡点。
这里做三件事：

1. **先找**：设置里的路径 → 插件自带（vendor/ffmpeg）→ data/bin → PATH → 常见安装位置；
2. **找不到就下**：从镜像列表拉静态包，解出 ffmpeg / ffprobe 放进 data/bin（免管理员、不改 PATH）；
3. **验一下**：跑 @ffmpeg -version@，能跑才算成功；失败就把原因原样带回面板。

下载源可换、可关（settings.auto_install=False 就只找不下）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 单个包最大允许多大（静态包通常 30~120 MB）
MAX_BYTES = 220 * 1024 * 1024
#: 下载超时
TIMEOUT = 600

#: 按平台给几个源，按顺序试
MIRRORS: dict[str, tuple[str, ...]] = {
    "windows": (
        # GitHub 加速代理放前面：国内直连 gyan.dev 常常只有几十 KB/s
        "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ),
    "linux": (
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-linux64-gpl.tar.xz",
    ),
    "darwin": (
        "https://evermeet.cx/ffmpeg/getrelease/zip",
    ),
}


def current_platform() -> str:
    name = platform.system().lower()
    if name.startswith("win"):
        return "windows"
    if name.startswith("darwin"):
        return "darwin"
    return "linux"


def bin_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "bin"


def _extra_lookup_paths() -> list[str]:
    """常见安装位置（Windows 居多），只是尽量帮忙，命中不了也不报错。"""
    home = Path.home()
    rows = [
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/usr/bin/ffmpeg"),
        Path("/opt/homebrew/bin/ffmpeg"),
        home / "scoop" / "shims" / "ffmpeg.exe",
        home / "AppData/Local/Microsoft/WinGet/Links/ffmpeg.exe",
        Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
    ]
    globbed: list[str] = []
    winget_root = home / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_root.is_dir():
        for candidate in winget_root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            globbed.append(str(candidate))
            if len(globbed) >= 4:
                break
    return [str(item) for item in rows] + globbed


def vendor_dir(plugin_dir: Path | str | None = None) -> Path | None:
    """插件自带 ffmpeg 的目录：<插件>/vendor/ffmpeg/<平台>。"""
    if not plugin_dir:
        return None
    return Path(plugin_dir) / "vendor" / "ffmpeg" / current_platform()


def locate(data_dir: Path | str | None = None,
           hint: str = "", plugin_dir: Path | str | None = None) -> dict[str, Any]:
    """按优先级找 ffmpeg/ffprobe，返回 {ok, ffmpeg, ffprobe, source}。

    顺序：设置里填的 → 插件自带 → data/bin → PATH → 常见安装位置。
    """
    candidates: list[tuple[str, str]] = []
    if hint:
        candidates.append((hint, "settings"))
    bundled = vendor_dir(plugin_dir)
    if bundled is not None:
        exe = "ffmpeg.exe" if current_platform() == "windows" else "ffmpeg"
        candidates.append((str(bundled / exe), "vendored"))
    if data_dir:
        folder = bin_dir(data_dir)
        exe = "ffmpeg.exe" if current_platform() == "windows" else "ffmpeg"
        candidates.append((str(folder / exe), "bundled"))
    found_path = shutil.which("ffmpeg") or ""
    if found_path:
        candidates.append((found_path, "PATH"))
    for item in _extra_lookup_paths():
        candidates.append((item, "system"))

    for path, source in candidates:
        try:
            if path and Path(path).is_file():
                folder = Path(path).parent
                probe = folder / ("ffprobe.exe" if current_platform() == "windows" else "ffprobe")
                return {
                    "ok": True,
                    "ffmpeg": str(path),
                    "ffprobe": str(probe) if probe.is_file() else "",
                    "source": source,
                }
        except OSError:
            continue
    return {"ok": False, "ffmpeg": "", "ffprobe": "", "source": ""}


def _text(blob: Any) -> str:
    """subprocess 的 stdout 可能是 bytes（传了 errors= 就是 str），统一成文本。"""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return str(blob or "")


def verify(ffmpeg: str) -> tuple[bool, str]:
    """真跑一下 -version，能跑才算装好了。"""
    if not ffmpeg:
        return False, "没有可执行文件"
    try:
        done = subprocess.run([ffmpeg, "-version"], capture_output=True,
                              timeout=30, check=False, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if int(done.returncode or 0) != 0:
        return False, _text(done.stderr)[-200:] or "非零退出"
    first = (_text(done.stdout).splitlines() or [""])[0]
    return True, first[:120]


def _download(url: str, target: Path, timeout: int = TIMEOUT) -> str:
    """下载到 target，返回错误说明（空 = 成功）。测试里会替换掉。"""
    import urllib.request

    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (astrbot_plugin_ai_mind)",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(target, "wb") as handle:
            written = 0
            while True:
                chunk = response.read(512 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES:
                    return "包太大（超过 %d MB），已中止" % (MAX_BYTES // (1024 * 1024))
                handle.write(chunk)
    except (OSError, ValueError) as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    return ""


def _extract(archive: Path, target_dir: Path) -> list[Path]:
    """从 zip / tar.xz 里把 ffmpeg、ffprobe 解到 target_dir。"""
    wanted = {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}
    found: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name = Path(info.filename).name.lower()
                if name not in wanted or info.is_dir():
                    continue
                out = target_dir / Path(info.filename).name
                with bundle.open(info) as source, open(out, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                found.append(out)
    elif tarfile.is_tarfile(str(archive)):
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                name = Path(member.name).name.lower()
                if name not in wanted or not member.isfile():
                    continue
                source = bundle.extractfile(member)
                if source is None:
                    continue
                out = target_dir / Path(member.name).name
                with source, open(out, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                found.append(out)
    for item in found:
        try:
            os.chmod(item, 0o755)
        except OSError:
            pass
    return found


def install(data_dir: Path | str, url: str = "", timeout: int = TIMEOUT) -> dict[str, Any]:
    """下载 + 解包 + 校验，装进 data/bin。返回 {ok, message, ffmpeg, ffprobe}。"""
    folder = bin_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    platforms = [current_platform()]
    urls: list[str] = []
    if url:
        urls.append(url)
    for key in platforms:
        urls.extend(MIRRORS.get(key, ()))

    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        for candidate in urls:
            archive = Path(tmp) / ("ffmpeg" + (".zip" if candidate.endswith(".zip") else ".pkg"))
            err = _download(candidate, archive, timeout)
            if err:
                continue
            try:
                files = _extract(archive, folder)
            except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
                continue
            if not files:
                continue
            exe_name = "ffmpeg.exe" if current_platform() == "windows" else "ffmpeg"
            ffmpeg = folder / exe_name
            probe = folder / exe_name.replace("ffmpeg", "ffprobe")
            ok, detail = verify(str(ffmpeg))
            if ok:
                return {
                    "ok": True,
                    "message": "装好了：" + detail,
                    "ffmpeg": str(ffmpeg),
                    "ffprobe": str(probe) if probe.is_file() else "",
                    "source": candidate,
                }
            return {"ok": False, "message": "下载成功但跑不起来：" + detail,
                    "ffmpeg": "", "ffprobe": ""}
    return {
        "ok": False,
        "message": "所有下载源都不通。可以手动把 ffmpeg.exe 放进 %s，或在面板里填路径。" % folder,
        "ffmpeg": "",
        "ffprobe": "",
    }


def status(data_dir: Path | str, hint: str = "",
           plugin_dir: Path | str | None = None) -> dict[str, Any]:
    """面板自检：现在能不能用、用的是哪一个、是不是插件自带的。"""
    found = locate(data_dir, hint, plugin_dir)
    if not found.get("ok"):
        return {"ok": False, "message": "没找到 ffmpeg", "ffmpeg": "", "ffprobe": "",
                "source": "", "version": "", "bundled": False}
    ok, version = verify(found["ffmpeg"])
    return {
        "ok": ok,
        "message": version if ok else ("找到了但跑不起来：" + version),
        "ffmpeg": found["ffmpeg"],
        "ffprobe": found.get("ffprobe", ""),
        "source": found.get("source", ""),
        "version": version,
        "bundled": found.get("source") in ("bundled", "vendored"),
    }


__all__ = [
    "MAX_BYTES", "MIRRORS", "current_platform", "bin_dir", "vendor_dir",
    "locate", "verify", "_text",
    "install", "status",
]

