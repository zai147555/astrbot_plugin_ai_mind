
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

#: 常见字体（烧编号和时间码要用；找不到就不烧，图照样出）
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


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
            line = ("  第 %d 格 %s（%s–%s）"
                    % (int(cell.get("i") or 0), format_tc(float(cell.get("t") or 0)),
                       format_tc(float(cell.get("from") or 0)),
                       format_tc(float(cell.get("to") or 0))))
            text = str(cell.get("text") or "").strip()
            if text:
                line += "｜他说：" + (text[:80] + ("…" if len(text) > 80 else ""))
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 媒体层（ffmpeg / ffprobe）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 烧字 / 音频包络 / 缓存 / 台词 / 放大
# ---------------------------------------------------------------------------


def find_font(hint: str = "") -> str:
    """找一个能用的字体文件。找不到返回空串（那就不烧编号）。"""
    candidates = []
    if hint:
        candidates.append(str(hint))
    candidates.extend(FONT_CANDIDATES)
    try:
        import glob

        candidates.extend(sorted(glob.glob("/usr/share/fonts/**/*.tt[fc]", recursive=True))[:5])
    except Exception:  # noqa: BLE001
        pass
    for item in candidates:
        try:
            if item and Path(item).exists():
                return item
        except OSError:
            continue
    return ""


def filter_escape_path(path: str) -> str:
    """filtergraph 里的路径转义：Windows 盘符里的冒号要躲开。"""
    text = str(path).replace(chr(92), "/")
    return text.replace(":", chr(92) + ":")


def filter_escape_text(text: str) -> str:
    """drawtext 的文字转义：冒号、单引号、逗号、百分号都要躲开。"""
    out = str(text)
    for char in (chr(92), ":", "'", ",", "%"):
        out = out.replace(char, chr(92) + char)
    return out


def label_filter(index: int, at: float, cell_w: int, cell_h: int, font: str = "") -> str:
    """在格子左上角烧「序号 + 时间码」—— 这样模型说"第 5 格"就真的对得上。"""
    size = max(12, int(cell_h * 0.16))
    label = filter_escape_text("%d  %s" % (index, format_tc(at)))
    parts = []
    if font:
        parts.append("fontfile=" + filter_escape_path(font))
    parts.extend([
        "text=" + label,
        "x=6", "y=6",
        "fontsize=%d" % size,
        "fontcolor=white",
        "box=1", "boxcolor=black@0.55", "boxborderw=4",
    ])
    return "drawtext=" + ":".join(parts)


def audio_envelope(path: str | Path, out: str | Path, width: int, height: int = 48,
                   tools: dict[str, Any] | None = None) -> bool:
    """把整段音频压成一条波形带（与画面同时间轴）。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return False
    code, _err = _run([
        str(tools["ffmpeg"]), "-v", "error", "-y", "-i", str(path),
        "-filter_complex",
        "showwavespic=s=%dx%d:colors=white|white:split_channels=0" % (max(64, int(width)),
                                                                     max(16, int(height))),
        "-frames:v", "1", str(out),
    ])
    return code == 0 and Path(out).exists()


def compose_vertical(top: str | Path, bottom: str | Path, out: str | Path,
                     tools: dict[str, Any] | None = None) -> bool:
    """把宫格图和波形带竖着拼起来。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return False
    code, _err = _run([
        str(tools["ffmpeg"]), "-v", "error", "-y",
        "-i", str(top), "-i", str(bottom),
        "-filter_complex", "vstack=inputs=2",
        "-frames:v", "1", str(out),
    ])
    return code == 0 and Path(out).exists()


def cache_key(path: str | Path, need: str, budget: int) -> str:
    """同一文件 + 同一档位 = 同一个缓存键（原项目：第二次调用 3.6 秒）。"""
    import hashlib

    try:
        stat = Path(path).stat()
        stamp = "%s|%s|%s" % (Path(path).resolve(), int(stat.st_size), int(stat.st_mtime))
    except OSError:
        stamp = str(path)
    raw = "%s|%s|%s" % (stamp, need, int(budget))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def load_cache(cache_dir: str | Path, key: str) -> dict[str, Any] | None:
    """命中缓存就整份返回（图还在才算命中）。"""
    if not cache_dir:
        return None
    index = Path(cache_dir) / (key + ".json")
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for sheet in payload.get("sheets") or []:
        if not Path(str(sheet.get("image") or "")).is_file():
            return None
    payload["cached"] = True
    return payload


def save_cache(cache_dir: str | Path, key: str, result: dict[str, Any]) -> bool:
    if not cache_dir:
        return False
    try:
        target = Path(cache_dir)
        target.mkdir(parents=True, exist_ok=True)
        payload = {key_: value for key_, value in result.items() if key_ != "manifest"}
        (target / (key + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except (OSError, ValueError):
        return False


def parse_srt(content: str) -> list[dict[str, Any]]:
    """解析 srt（给外部 ASR 用）。失败就返回空列表。"""
    lines: list[dict[str, Any]] = []
    current: list[str] = []
    start = end = 0.0
    timed = False

    def stamp(text: str) -> float:
        piece = text.strip().replace(",", ".")
        try:
            hh, mm, ss = piece.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
        except (ValueError, TypeError):
            return 0.0

    for raw in str(content or "").replace("\r", "").split("\n"):
        row = raw.strip()
        if "-->" in row:
            left, right = row.split("-->", 1)
            start = stamp(left)
            end = stamp(right.split()[0] if right.split() else "0")
            current = []
            timed = True
            continue
        if not row:
            if current and timed:
                lines.append({"from": start, "to": end, "text": " ".join(current)})
            current = []
            continue
        if row.isdigit() and not current:
            continue
        current.append(row)
    if current and timed:
        lines.append({"from": start, "to": end, "text": " ".join(current)})
    return lines


def transcribe(path: str | Path, command: str,
               workdir: str | Path | None = None,
               timeout: int = 1800) -> list[dict[str, Any]]:
    """跑用户自己装的转写器（Whisper 之类），把 srt 变成台词行。

    命令里用 {input} 和 {output} 两个占位符，例如：
        whisper "{input}" --model small --output_format srt --output_dir "{output}"
    或者：
        faster-whisper "{input}" --output-srt --output "{output}"
    我们不打包模型（那是 GB 级依赖），只负责把结果挂到分格上。
    """
    template = str(command or "").strip()
    if not template:
        return []
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="asr_"))
    root.mkdir(parents=True, exist_ok=True)
    out = root / "subtitle"
    cmd = (template.replace("{input}", str(path))
                   .replace("{output}", str(out)))
    code, err = _run_command(cmd, timeout)
    if code != 0:
        return []
    # 转写器可能把文件叫 subtitle.srt，也可能放在目录里
    candidates = [out, out.with_suffix(".srt")]
    candidates.extend(sorted(root.glob("*.srt")))
    for item in candidates:
        try:
            if item.is_file():
                return parse_srt(item.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return []


def _text(blob: Any) -> str:
    """subprocess 输出统一成文本（传了 errors= 就是 str，没传是 bytes）。"""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return str(blob or "")


def _run_command(command: str, timeout: int) -> tuple[int, str]:
    """跑一条带空格的命令行（转写器的参数经常带引号，所以走 shell）。"""
    try:
        done = subprocess.run(
            command, shell=True, capture_output=True, timeout=timeout,
            check=False, errors="replace",
        )
        return int(done.returncode or 0), _text(done.stderr)[-400:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def attach_transcript(plan_result: dict[str, Any],
                      lines: Iterable[dict[str, Any]]) -> int:
    """把台词按时间码挂到每一格上（就地改 plan_result），返回挂上的格数。"""
    rows = [row for row in lines if str(row.get("text") or "").strip()]
    if not rows:
        return 0
    attached = 0
    for sheet in plan_result.get("sheets") or []:
        for cell in sheet.get("cells") or []:
            start = float(cell.get("from") or 0)
            end = float(cell.get("to") or 0)
            hit = [
                str(row.get("text")).strip()
                for row in rows
                if float(row.get("to") or 0) > start and float(row.get("from") or 0) < end
            ]
            text = " ".join(dict.fromkeys(hit))
            if text:
                cell["text"] = text[:400]
                attached += 1
    return attached


def zoom(path: str | Path, plan_result: dict[str, Any], cell_index: int,
         frames: int = 3, workdir: str | Path | None = None,
         tools: dict[str, Any] | None = None, font: str = "") -> dict[str, Any]:
    """把某一格再放大：在它那段时间里多取几帧，按 text 档位拼一张。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return {"ok": False, "error": "没找到 ffmpeg"}
    cell = None
    for sheet in plan_result.get("sheets") or []:
        for item in sheet.get("cells") or []:
            if int(item.get("i") or 0) == int(cell_index):
                cell = item
                break
    if cell is None:
        return {"ok": False, "error": "没有第 %d 格" % int(cell_index)}
    start = float(cell.get("from") or 0)
    end = max(start + MIN_STEP, float(cell.get("to") or 0))
    count = max(1, min(6, int(frames)))
    step = (end - start) / count
    per_sheet = max_cells_for_need("text")
    width, height = cell_pixels(per_sheet)
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="zoom_"))
    root.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    for index in range(count):
        at = start + (index + 0.5) * step
        out = root / ("z%02d.png" % index)
        if extract_frame(path, at, out, width, height, tools, font=font,
                         label="%d-%d  %s" % (cell_index, index + 1, format_tc(at))):
            made.append(str(out))
    if not made:
        return {"ok": False, "error": "这一格取不到帧"}
    image = root / ("zoom%d.png" % int(cell_index))
    if not tile(made, len(made), 1, image, width, height, tools):
        return {"ok": False, "error": "拼图失败"}
    return {
        "ok": True,
        "cell": int(cell_index),
        "from": start,
        "to": end,
        "image": str(image),
        "frames": len(made),
        "text": str(cell.get("text") or ""),
        "lines": ["第 %d 格放大：%s–%s（%d 帧）"
                  % (int(cell_index), format_tc(start), format_tc(end), len(made))],
    }


def build_evidence(result: dict[str, Any]) -> dict[str, Any]:
    """机器可读的证据清单（复核「第 5 格真的是 3:29 吗」用）。"""
    cells: list[dict[str, Any]] = []
    for sheet in result.get("sheets") or []:
        for cell in sheet.get("cells") or []:
            cells.append({
                "cell": int(cell.get("i") or 0),
                "sheet": int(sheet.get("i") or 0),
                "from": float(cell.get("from") or 0),
                "to": float(cell.get("to") or 0),
                "frame_at": float(cell.get("t") or 0),
                "chars": len(str(cell.get("text") or "")),
                "text": str(cell.get("text") or ""),
            })
    info = result.get("info") or {}
    return {
        "version": 1,
        "input": info.get("path"),
        "duration": info.get("duration"),
        "size": "%sx%s" % (info.get("width"), info.get("height")),
        "cells_total": len(cells),
        "cells": cells,
    }


def _vendored_dir() -> Path | None:
    """插件自带的 ffmpeg 放在 <插件>/vendor/ffmpeg/<平台>/。"""
    plat = "windows" if os.name == "nt" else "linux"
    folder = Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / plat
    return folder if folder.is_dir() else None


def find_tools(ffmpeg_hint: str = "", ffprobe_hint: str = "") -> dict[str, Any]:
    """找 ffmpeg / ffprobe。找不到也返回结构，让上层给一句人话。"""
    def pick(hint: str, name: str) -> str:
        bundle = _vendored_dir()
        for candidate in (
            str(hint or "").strip(),
            str(os.environ.get(name.upper() + "_PATH") or "").strip(),
            str(bundle / (name + ".exe")) if bundle else "",
            str(bundle / name) if bundle else "",
        ):
            if candidate and Path(candidate).exists():
                return candidate
        found = shutil.which(name) or shutil.which(name + ".exe") or ""
        return found

    ffmpeg = pick(ffmpeg_hint, "ffmpeg")
    ffprobe = pick(ffprobe_hint, "ffprobe")
    hint = ""
    if not ffmpeg:
        hint = (
            "没找到 ffmpeg。面板「分镜」页点「一键内置」会自动装到插件目录（免管理员）；"
            "也可以自己填 ffmpeg.exe 的路径，或把它放进 PATH；装完重启 AstrBot。"
        )
    # 只要 ffmpeg 在就能干活：ffprobe 缺失时用 -i 的 stderr 兜底读元信息
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe,
            "ok": bool(ffmpeg), "hint": hint}


def _run(cmd: Sequence[str], timeout: int = FFMPEG_TIMEOUT) -> tuple[int, str]:
    """跑一条命令，返回 (exit code, stderr 尾巴)。测试里会把它换掉。"""
    try:
        done = subprocess.run(
            list(cmd), capture_output=True, timeout=timeout,
            check=False, errors="replace",
        )
        err = _text(done.stderr)
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
    if not tools.get("ffprobe"):
        return _probe_with_ffmpeg(target, tools)
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
        raise ValueError("ffprobe 读不出来：" + _text(done.stderr)[-300:].strip())
    try:
        payload = json.loads(_text(done.stdout) or "{}")
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


def parse_ffmpeg_info(text: str) -> dict[str, Any]:
    """从 @ffmpeg -i@ 的 stderr 里读时长/分辨率/fps（没有 ffprobe 时的兜底）。"""
    import re as _re

    blob = str(text or "")
    duration = 0.0
    match = _re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", blob)
    if match:
        duration = (int(match.group(1)) * 3600 + int(match.group(2)) * 60
                    + float(match.group(3)))
    width = height = 0
    size = _re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", blob)
    if size:
        width, height = int(size.group(1)), int(size.group(2))
    fps = 0.0
    rate = _re.search(r"Video:.*?,\s*([\d.]+)\s*fps", blob)
    if rate:
        try:
            fps = float(rate.group(1))
        except ValueError:
            fps = 0.0
    return {"duration": round(duration, 3), "width": width, "height": height,
            "fps": round(fps, 3)}


def _probe_with_ffmpeg(target: str, tools: dict[str, Any]) -> dict[str, Any]:
    """ffprobe 不在时，用 ffmpeg -i 的 stderr 读元信息。"""
    code, err = _run([str(tools["ffmpeg"]), "-hide_banner", "-i", target], timeout=60)
    info = parse_ffmpeg_info(err)
    if not info.get("duration"):
        raise ValueError("读不出片长（ffmpeg 也不认识这个文件？）")
    info.update({"path": target, "name": Path(target).name})
    return info


def extract_frame(path: str | Path, at: float, out: str | Path,
                  width: int, height: int, tools: dict[str, Any] | None = None,
                  font: str = "", label: str = "") -> bool:
    """取某一秒的一帧，缩放到格子里（保持比例，补黑边），可顺便烧上编号。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return False
    scale = (
        "scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        % (width, height, width, height)
    )
    base = [
        str(tools["ffmpeg"]), "-v", "error", "-y",
        "-ss", "%.3f" % max(0.0, float(at)),
        "-i", str(path),
        "-frames:v", "1",
    ]
    attempts: list[str] = []
    if font and label:
        attempts.append(scale + "," + label_filter(0, 0, width, height, font).replace(
            "text=" + filter_escape_text("0  " + format_tc(0)),
            "text=" + filter_escape_text(label),
            1,
        ))
    attempts.append(scale)
    for vf in attempts:
        code, _err = _run(base + ["-vf", vf, str(out)])
        if code == 0 and Path(out).exists():
            return True
    return False


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
          budget: int = PIXEL_BUDGET, cache_dir: str | Path | None = None,
          annotate: bool = True, audio_bar: bool = True,
          transcript: Iterable[dict[str, Any]] | None = None,
          font_hint: str = "", asr_command: str = "") -> dict[str, Any]:
    """完整跑一次：读元信息 → 规划 → 取帧（烧编号）→ 拼图 → 波形带 → 台词 → 证据。

    返回 dict：ok / error / hint / info / plan / sheets / manifest / summary / cached。
    永远不抛异常（出错就是 ok=False + 一句人话）。
    """
    target = Path(str(path))
    if not target.exists():
        return {"ok": False, "error": "文件不存在：" + str(target), "hint": ""}
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    if not tools.get("ok"):
        return {"ok": False, "error": "没找到 ffmpeg/ffprobe", "hint": tools.get("hint", "")}

    key = cache_key(target, need, budget)
    if cache_dir:
        cached = load_cache(cache_dir, key)
        if cached:
            cached["ok"] = True
            cached["manifest"] = build_manifest(cached)
            if transcript:
                attach_transcript(cached, transcript)
                cached["manifest"] = build_manifest(cached)
            return cached

    try:
        info = probe_video(target, tools)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": ""}

    plan_result = plan(info["duration"], need, DEFAULT_ASPECT, budget)
    if transcript is None and asr_command:
        transcript = transcribe(
            target, asr_command, str(workdir) if workdir else None
        )
    if transcript:
        plan_result["transcript_cells"] = attach_transcript(plan_result, transcript)
    cell_w, cell_h = plan_result["cell_size"]
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="storyboard_"))
    root.mkdir(parents=True, exist_ok=True)
    font = find_font(font_hint) if annotate else ""

    sheets: list[dict[str, Any]] = []
    for sheet in plan_result["sheets"]:
        cells_dir = root / ("sheet%d" % int(sheet["i"]))
        cells_dir.mkdir(parents=True, exist_ok=True)
        usable: list[str] = []
        for cell in sheet["cells"]:
            frame = cells_dir / ("f%03d.png" % int(cell["i"]))
            label = "%d  %s" % (int(cell["i"]), format_tc(float(cell["t"])))
            if extract_frame(target, cell["t"], frame, cell_w, cell_h, tools,
                             font=font, label=label):
                usable.append(str(frame))
        if not usable:
            return {"ok": False, "error": "一帧都没取到（时间码落在片尾之后？）", "hint": ""}
        columns = int(plan_result["columns"])
        rows = int(math.ceil(len(usable) / columns))
        # 最后一排不够就补黑，避免 xstack 数量对不上
        while len(usable) < columns * rows:
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
        grid = root / ("sheet%d-grid.png" % int(sheet["i"]))
        rows = int(math.ceil(len(usable) / columns))
        if not tile(usable, columns, rows, grid, cell_w, cell_h, tools):
            return {"ok": False, "error": "拼图失败（ffmpeg xstack 出错）", "hint": ""}
        image = grid
        if audio_bar:
            strip = root / ("sheet%d-wave.png" % int(sheet["i"]))
            if audio_envelope(target, strip, columns * cell_w, 48, tools):
                merged = root / ("sheet%d.png" % int(sheet["i"]))
                if compose_vertical(grid, strip, merged, tools):
                    image = merged
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
        "annotated": bool(font),
        "cache_key": key,
        "cached": False,
    }
    result["manifest"] = build_manifest(result)
    result["evidence"] = build_evidence(result)
    try:
        (root / "evidence.json").write_text(
            json.dumps(result["evidence"], ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass
    save_cache(cache_dir, key, result) if cache_dir else None
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
            lines = (_text(done.stdout) or "").splitlines()
            version = lines[0][:120] if lines else ""
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
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
    font_path: str = ""
    max_download_mb: int = 200
    #: 把编号+时间码烧进格子（原项目靠这个让模型"引用第 5 格"）
    annotate: bool = True
    #: 宫格图下面贴一条音频波形（与画面同时间轴）
    audio_bar: bool = True
    #: 同一文件第二次直接命中缓存
    cache: bool = True
    #: 外挂转写命令：{input} {output} 两个占位符，输出 srt
    asr_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "need": self.need if self.need in NEED_TIERS else DEFAULT_NEED,
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            "font_path": self.font_path,
            "max_download_mb": int(self.max_download_mb),
            "annotate": bool(self.annotate),
            "audio_bar": bool(self.audio_bar),
            "cache": bool(self.cache),
            "asr_command": self.asr_command,
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
            font_path=str(raw.get("font_path") or "").strip(),
            max_download_mb=max(1, min(2048, cap)),
            annotate=bool(raw.get("annotate", True)),
            audio_bar=bool(raw.get("audio_bar", True)),
            cache=bool(raw.get("cache", True)),
            asr_command=str(raw.get("asr_command") or "").strip(),
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
    "find_font", "filter_escape_path", "filter_escape_text", "label_filter",
    "audio_envelope", "compose_vertical", "cache_key", "load_cache", "save_cache",
    "parse_srt", "attach_transcript", "zoom", "build_evidence",
    "transcribe",
]

