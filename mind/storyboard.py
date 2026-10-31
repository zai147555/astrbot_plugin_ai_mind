
"""视频分镜：把一段视频切成可寻址的宫格图 + 文字清单。

移植自 SAOS-3624/dsh-video-storyboard（Apache-2.0）的核心思路：
**纯算术层决定切几格、每格多大，媒体层（ffmpeg）只负责执行。**

为什么要这样做：纯文本模型看不到视频。把视频压成一张「编号宫格图」，
模型就能引用「第 5 格」、能对上时间码，而不是被动接受一段别人写好的摘要。
一张图 9~16 格只花几百 token，逐帧方案要几千。

本实现只依赖标准库 + 本机的 ffmpeg/ffprobe：
  - 没有 ffmpeg 时**什么都不做**，只给一句人话（装哪、怎么装）；
  - 取帧、拼图全部交给 ffmpeg（scale/pad/tile），不需要图像库、不需要字体配置；
  - 台词/ASR、yt-dlp 那条链路没有移植（那是另一套依赖），只做「画面 + 时间码」。

移植时保留的实测常量（来自原项目的盲评数据）：
  - 单图像素预算 640_000
  - 可读性三档：shot 160x90 / scene 320x180 / text 480x270
  - 布局 gutter 按 96% 估，卡门槛再保守 6%
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 常量（与原项目 plan.js 对齐）
# ---------------------------------------------------------------------------

PIXEL_BUDGET = 640_000
IMAGE_TOKEN_CAP = 384
MIN_CELL_W = 240
MIN_CELL_H = 135
DEFAULT_ASPECT = 16 / 9
MAX_SHEETS = 99

#: 可读性档位：模型该说的是"我要看清到什么程度"，格数由它反推
NEED_TIERS: dict[str, dict[str, Any]] = {
    "shot": {"w": 160, "h": 90, "label": "分得清镜头就行"},
    "scene": {"w": 320, "h": 180, "label": "看得清人脸、动作和大标题"},
    "text": {"w": 480, "h": 270, "label": "要能读字幕、招牌和界面文字"},
}
DEFAULT_NEED = "scene"

CELL_GEOMETRY_SLACK = 0.96
CELL_FIT_SLACK = 0.94

#: 一张图覆盖多少秒（原项目实测：5 分钟一张）
SHEET_SECONDS = 300.0
#: 每格时间粒度下限：太短的片子也至少 1 秒一格
MIN_STEP = 0.5

FFMPEG_TIMEOUT = 180


# ---------------------------------------------------------------------------
# 纯算术：格数 / 尺寸 / 时间码
# ---------------------------------------------------------------------------


def cell_pixels(cells: int, aspect: float = DEFAULT_ASPECT,
                budget: int = PIXEL_BUDGET) -> tuple[int, int]:
    """一张图放 n 格时，每格**大致**多少像素（按 96% 估 gutter）。"""
    n = max(1, int(round(float(cells) or 1)))
    area = max(1, int(budget)) / n
    width = int(round(math.sqrt(area * aspect) * CELL_GEOMETRY_SLACK))
    height = int(round(width / aspect))
    return max(16, width), max(9, height)


def image_tokens(cells: int, budget: int = PIXEL_BUDGET) -> int:
    """一张宫格图大概吃多少 token（估算，用于面板提示成本）。"""
    width, height = cell_pixels(cells, DEFAULT_ASPECT, budget)
    area = width * height * max(1, int(cells))
    return int(min(IMAGE_TOKEN_CAP, max(24, round(area / 1666))))


def max_cells_for_need(need: str = DEFAULT_NEED, aspect: float = DEFAULT_ASPECT,
                       budget: int = PIXEL_BUDGET) -> int:
    """按可读性反推：一张图最多放几格还不低于该档位。"""
    tier = NEED_TIERS.get(str(need or "").strip().lower()) or NEED_TIERS[DEFAULT_NEED]
    best = 1
    for count in range(1, 65):
        width, height = cell_pixels(count, aspect, budget)
        if width * CELL_FIT_SLACK < tier["w"] or height * CELL_FIT_SLACK < tier["h"]:
            break
        best = count
    return best


def columns_for(cells: int, aspect: float = DEFAULT_ASPECT) -> int:
    """宫格列数：让整张图的宽高比接近视频本身。"""
    n = max(1, int(cells))
    cols = max(1, min(n, int(round(math.sqrt(n * aspect)))))
    return cols


def format_tc(seconds: float) -> str:
    """时间码 mm:ss.ss（原项目的口径）。"""
    total = max(0.0, float(seconds))
    minutes = int(total // 60)
    rest = total - minutes * 60
    return "%02d:%05.2f" % (minutes, rest)


def plan(duration: float, need: str = DEFAULT_NEED, aspect: float = DEFAULT_ASPECT,
         budget: int = PIXEL_BUDGET, sheet_seconds: float = SHEET_SECONDS) -> dict[str, Any]:
    """切几张、每张几格、每格取第几秒。纯函数，不碰文件。"""
    length = max(0.1, float(duration or 0.1))
    per_sheet = max_cells_for_need(need, aspect, budget)
    sheets_count = max(1, min(MAX_SHEETS, int(math.ceil(length / max(1.0, sheet_seconds)))))
    span = length / sheets_count
    columns = columns_for(per_sheet, aspect)
    rows = int(math.ceil(per_sheet / columns))

    sheets: list[dict[str, Any]] = []
    index = 0
    for sheet_index in range(sheets_count):
        start = sheet_index * span
        end = min(length, start + span)
        window = max(MIN_STEP, end - start)
        step = window / per_sheet
        cells: list[dict[str, Any]] = []
        for cell_index in range(per_sheet):
            index += 1
            cells.append({
                "i": index,
                "sheet": sheet_index + 1,
                "from": round(start + cell_index * step, 2),
                "to": round(min(end, start + (cell_index + 1) * step), 2),
                "t": round(min(end, start + (cell_index + 0.5) * step), 2),
            })
        sheets.append({
            "i": sheet_index + 1,
            "from": round(start, 2),
            "to": round(end, 2),
            "cells": cells,
        })
    return {
        "need": str(need),
        "aspect": round(float(aspect), 4),
        "per_sheet": per_sheet,
        "columns": columns,
        "rows": rows,
        "cell_size": cell_pixels(per_sheet, aspect, budget),
        "tokens_est": image_tokens(per_sheet, budget),
        "cells_total": index,
        "sheets": sheets,
    }


def summarize(plan_result: dict[str, Any], duration: float) -> str:
    """一句话摘要（聊天里用）。"""
    per_sheet = int(plan_result.get("per_sheet") or 1)
    sheets = len(plan_result.get("sheets") or [])
    cells = int(plan_result.get("cells_total") or 0)
    width, height = plan_result.get("cell_size") or (0, 0)
    seconds = max(0.1, float(duration or 0.1))
    return (
        "片长 %s｜%d 张图、共 %d 格｜每格约 %.1f 秒、约 %dx%d"
        "｜一张图约 %d token"
        % (format_tc(seconds), sheets, cells, seconds / max(1, cells),
           width, height, int(plan_result.get("tokens_est") or 0))
    )


def build_manifest(result: dict[str, Any]) -> str:
    """给模型/人读的清单：每格的时间码与覆盖区间。"""
    info = result.get("info") or {}
    plan_result = result.get("plan") or {}
    lines: list[str] = []
    source = info.get("name") or info.get("path") or "视频"
    lines.append("分镜：" + str(source))
    bits = []
    if info.get("duration"):
        bits.append("片长 " + format_tc(float(info["duration"])))
    if info.get("width") and info.get("height"):
        bits.append("%dx%d" % (int(info["width"]), int(info["height"])))
    if info.get("fps"):
        bits.append("fps %.2f" % float(info["fps"]))
    if bits:
        lines.append("｜".join(bits))
    tier = NEED_TIERS.get(str(plan_result.get("need") or ""), {})
    lines.append(
        "档位 %s（%s）：每张最多 %d 格，每格约 %dx%d"
        % (plan_result.get("need"), tier.get("label", ""),
           int(plan_result.get("per_sheet") or 0),
           int((plan_result.get("cell_size") or (0, 0))[0]),
           int((plan_result.get("cell_size") or (0, 0))[1]))
    )
    for sheet in result.get("sheets") or []:
        lines.append("")
        lines.append("第 %d 张（%s–%s）"
                     % (int(sheet.get("i") or 0),
                        format_tc(float(sheet.get("from") or 0)),
                        format_tc(float(sheet.get("to") or 0))))
        for cell in sheet.get("cells") or []:
            lines.append("  第 %d 格 %s（%s–%s）"
                         % (int(cell.get("i") or 0), format_tc(float(cell.get("t") or 0)),
                            format_tc(float(cell.get("from") or 0)),
                            format_tc(float(cell.get("to") or 0))))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 媒体层（ffmpeg / ffprobe）
# ---------------------------------------------------------------------------


def find_tools(ffmpeg_hint: str = "", ffprobe_hint: str = "") -> dict[str, Any]:
    """找 ffmpeg / ffprobe。找不到也返回结构，让上层给一句人话。"""
    def pick(hint: str, name: str) -> str:
        for candidate in (
            str(hint or "").strip(),
            str(os.environ.get(name.upper() + "_PATH") or "").strip(),
        ):
            if candidate and Path(candidate).exists():
                return candidate
        found = shutil.which(name) or shutil.which(name + ".exe") or ""
        return found

    ffmpeg = pick(ffmpeg_hint, "ffmpeg")
    ffprobe = pick(ffprobe_hint, "ffprobe")
    hint = ""
    if not (ffmpeg and ffprobe):
        hint = (
            "没找到 ffmpeg。Windows：去 ffmpeg.org 下 release 包，解压后把 bin 目录加进 PATH，"
            "或在这里直接填 ffmpeg.exe 的完整路径；装完重启 AstrBot。"
        )
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe,
            "ok": bool(ffmpeg and ffprobe), "hint": hint}


def _run(cmd: Sequence[str], timeout: int = FFMPEG_TIMEOUT) -> tuple[int, str]:
    """跑一条命令，返回 (exit code, stderr 尾巴)。测试里会把它换掉。"""
    try:
        done = subprocess.run(
            list(cmd), capture_output=True, timeout=timeout,
            check=False, errors="replace",
        )
        err = (done.stderr or b"").decode("utf-8", "replace")
        return int(done.returncode or 0), err[-800:]
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, "超时（%d 秒）" % timeout
    except OSError as exc:  # noqa: BLE001 - 权限、路径非法之类
        return 126, str(exc)


def probe_video(path: str | Path, tools: dict[str, Any] | None = None) -> dict[str, Any]:
    """真正读元信息：片长、宽高、fps。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        raise ValueError(tools.get("hint") or "没有 ffmpeg")
    target = str(path)
    cmd = [
        str(tools["ffprobe"]), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", target,
    ]
    try:
        done = subprocess.run(
            cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False, errors="replace",
        )
    except FileNotFoundError as exc:
        raise ValueError("ffprobe 不在：" + str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ffprobe 超时（%d 秒）" % FFMPEG_TIMEOUT) from exc
    if int(done.returncode or 0) != 0:
        raise ValueError("ffprobe 读不出来：" +
                         (done.stderr or b"").decode("utf-8", "replace")[-300:].strip())
    try:
        payload = json.loads((done.stdout or b"").decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ValueError("ffprobe 输出不是 JSON") from exc

    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError("读不出片长（可能是纯音频或文件损坏）")
    fps = 0.0
    raw_rate = str(video.get("r_frame_rate") or "")
    if "/" in raw_rate:
        try:
            num, den = raw_rate.split("/", 1)
            fps = float(num) / float(den or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0.0
    return {
        "path": target,
        "name": Path(target).name,
        "duration": round(duration, 3),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
    }


def extract_frame(path: str | Path, at: float, out: str | Path,
                  width: int, height: int, tools: dict[str, Any] | None = None) -> bool:
    """取某一秒的一帧，缩放到格子里（保持比例，补黑边）。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return False
    scale = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        % (width, height, width, height)
    )
    code, _err = _run([
        str(tools["ffmpeg"]), "-v", "error", "-y",
        "-ss", "%.3f" % max(0.0, float(at)),
        "-i", str(path),
        "-frames:v", "1", "-vf", scale, str(out),
    ])
    return code == 0 and Path(out).exists()


def tile(frames: Iterable[str | Path], columns: int, rows: int, out: str | Path,
         cell_w: int, cell_h: int, tools: dict[str, Any] | None = None) -> bool:
    """把一串等大的帧拼成一张宫格图（xstack，按列×行排）。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return False
    files = [str(item) for item in frames]
    if not files:
        return False
    columns = max(1, int(columns))
    total = len(files)
    rows = max(1, int(rows)) if rows else int(math.ceil(total / columns))
    layout = "|".join(
        "%d_%d" % ((index % columns) * int(cell_w), (index // columns) * int(cell_h))
        for index in range(total)
    )
    cmd: list[str] = [str(tools["ffmpeg"]), "-v", "error", "-y"]
    for item in files:
        cmd.extend(["-i", item])
    cmd.extend([
        "-filter_complex", "xstack=inputs=%d:layout=%s:fill=black" % (total, layout),
        "-frames:v", "1", str(out),
    ])
    code, _err = _run(cmd)
    return code == 0 and Path(out).exists()


def build(path: str | Path, need: str = DEFAULT_NEED, workdir: str | Path | None = None,
          ffmpeg_hint: str = "", ffprobe_hint: str = "",
          budget: int = PIXEL_BUDGET) -> dict[str, Any]:
    """完整跑一次：读元信息 → 规划 → 取帧 → 拼图。

    返回 dict：ok / error / hint / info / plan / sheets / manifest / summary。
    永远不抛异常（出错就是 ok=False + 一句人话）。
    """
    target = Path(str(path))
    if not target.exists():
        return {"ok": False, "error": "文件不存在：" + str(target), "hint": ""}
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    if not tools.get("ok"):
        return {"ok": False, "error": "没找到 ffmpeg/ffprobe", "hint": tools.get("hint", "")}
    try:
        info = probe_video(target, tools)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": ""}

    plan_result = plan(info["duration"], need, DEFAULT_ASPECT, budget)
    cell_w, cell_h = plan_result["cell_size"]
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="storyboard_"))
    root.mkdir(parents=True, exist_ok=True)

    sheets: list[dict[str, Any]] = []
    for sheet in plan_result["sheets"]:
        cells_dir = root / ("sheet%d" % int(sheet["i"]))
        cells_dir.mkdir(parents=True, exist_ok=True)
        made: list[str] = []
        for cell in sheet["cells"]:
            frame = cells_dir / ("f%03d.png" % int(cell["i"]))
            if extract_frame(target, cell["t"], frame, cell_w, cell_h, tools):
                made.append(str(frame))
            else:
                made.append("")            # 取不到就留空位，拼图时跳过
        usable = [item for item in made if item]
        if not usable:
            return {"ok": False, "error": "一帧都没取到（时间码落在片尾之后？）", "hint": ""}
        image = root / ("sheet%d.png" % int(sheet["i"]))
        columns = int(plan_result["columns"])
        rows = int(math.ceil(len(usable) / columns))
        # 最后一排不够就补黑，避免 hstack 数量对不上
        while len(usable) < columns * rows and usable:
            blank = cells_dir / ("blank%03d.png" % len(usable))
            blank_cmd = [
                str(tools["ffmpeg"]), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=%dx%d" % (cell_w, cell_h),
                "-frames:v", "1", str(blank),
            ]
            if _run(blank_cmd)[0] == 0 and blank.exists():
                usable.append(str(blank))
            else:
                break
        rows = int(math.ceil(len(usable) / columns))
        if not tile(usable, columns, rows, image, cell_w, cell_h, tools):
            return {"ok": False, "error": "拼图失败（ffmpeg tile 出错）", "hint": ""}
        sheets.append({
            "i": int(sheet["i"]),
            "from": float(sheet["from"]),
            "to": float(sheet["to"]),
            "image": str(image),
            "cells": sheet["cells"],
        })

    result = {
        "ok": True,
        "info": info,
        "plan": plan_result,
        "sheets": sheets,
        "summary": summarize(plan_result, info["duration"]),
        "error": "",
        "hint": "",
    }
    result["manifest"] = build_manifest(result)
    return result


def selfcheck(ffmpeg_hint: str = "", ffprobe_hint: str = "") -> dict[str, Any]:
    """面板自检：ffmpeg 在不在、是什么版本。"""
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    version = ""
    if tools.get("ok"):
        code, _err = 0, ""
        try:
            done = subprocess.run(
                [str(tools["ffmpeg"]), "-version"], capture_output=True,
                timeout=20, check=False, errors="replace",
            )
            code = int(done.returncode or 0)
            version = ((done.stdout or b"").decode("utf-8", "replace") or "").splitlines()[0][:120]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            code = 1
    return {
        "ok": bool(tools.get("ok")) and bool(version),
        "ffmpeg": tools.get("ffmpeg", ""),
        "ffprobe": tools.get("ffprobe", ""),
        "version": version,
        "hint": tools.get("hint", ""),
    }


# ---------------------------------------------------------------------------
# 设置（data/storyboard.json）与取片
# ---------------------------------------------------------------------------


@dataclass
class StoryboardSettings:
    """分镜设置。默认**开**（只是命令/面板触发，不会自动介入聊天）。"""

    enabled: bool = True
    need: str = DEFAULT_NEED
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    max_download_mb: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "need": self.need if self.need in NEED_TIERS else DEFAULT_NEED,
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            "max_download_mb": int(self.max_download_mb),
        }

    @staticmethod
    def from_dict(data: Any) -> "StoryboardSettings":
        raw = data if isinstance(data, dict) else {}
        need = str(raw.get("need") or DEFAULT_NEED).strip().lower()
        try:
            cap = int(raw.get("max_download_mb") or 200)
        except (TypeError, ValueError):
            cap = 200
        return StoryboardSettings(
            enabled=bool(raw.get("enabled", True)),
            need=need if need in NEED_TIERS else DEFAULT_NEED,
            ffmpeg_path=str(raw.get("ffmpeg_path") or "").strip(),
            ffprobe_path=str(raw.get("ffprobe_path") or "").strip(),
            max_download_mb=max(1, min(2048, cap)),
        )


def load_settings(path: Path | str) -> StoryboardSettings:
    try:
        return StoryboardSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return StoryboardSettings()


def save_settings(path: Path | str, settings: StoryboardSettings) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError):
        return False


def is_url(source: str) -> bool:
    text = str(source or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def download(url: str, out: str | Path, max_bytes: int = 200 * 1024 * 1024,
             timeout: int = 120) -> tuple[bool, str]:
    """把视频链接下到本地（边下边计数，超上限就停）。返回 (ok, 错误说明)。"""
    import urllib.error
    import urllib.request

    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    limit = max(1024 * 1024, int(max_bytes))
    request_obj = urllib.request.Request(
        str(url), headers={"User-Agent": "Mozilla/5.0 (astrbot_plugin_ai_mind)"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as response, \
                open(target, "wb") as handle:
            written = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    handle.close()
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    return False, "视频超过 %d MB 上限，先手工下载再给本地路径" % (
                        limit // (1024 * 1024))
                handle.write(chunk)
        return True, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        try:
            target.unlink()
        except OSError:
            pass
        return False, "下载失败：%s: %s" % (type(exc).__name__, exc)


__all__ = [
    "PIXEL_BUDGET", "NEED_TIERS", "DEFAULT_NEED", "SHEET_SECONDS",
    "cell_pixels", "image_tokens", "max_cells_for_need", "columns_for",
    "format_tc", "plan", "summarize", "build_manifest",
    "find_tools", "probe_video", "extract_frame", "tile", "build", "selfcheck",
    "StoryboardSettings", "load_settings", "save_settings", "is_url", "download",
]

