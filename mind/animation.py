
"""动图逐帧：把 GIF / 动态 WebP 摊成一条带时间的帧序列。

移植自 NeoHotaru/dsh-read-animation 的做法（MIT）：**解码每一帧，按时间采样，
给模型一串带编号和时间的真实帧**，而不是像默认那样只丢第一帧过去。

依赖只有 ffmpeg：
  - 解帧：@-vsync 0@ 逐帧导出（有上限，别被长动图撑爆）
  - 拼图：复用分镜的 xstack + drawtext（编号 + 时间）
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from .storyboard import (
    _run,
    cell_pixels,
    find_font,
    find_tools,
    format_tc,
    label_filter,
    tile,
)

#: 默认采几帧
DEFAULT_FRAMES = 8
#: 最多导出多少帧（长动图只取前面这些）
MAX_EXTRACT = 240
#: 一帧的宽度（按 2 行排）
CELL_W = 320
CELL_H = 180


def probe_animation(path: str | Path, tools: dict[str, Any] | None = None) -> dict[str, Any]:
    """动图有几帧、多长、多大。"""
    import json
    import subprocess

    tools = tools or find_tools()
    if not tools.get("ok"):
        raise ValueError(tools.get("hint") or "没有 ffmpeg")
    try:
        done = subprocess.run([
            str(tools["ffprobe"]), "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ], capture_output=True, timeout=60, check=False, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("ffprobe 跑不起来：%s" % exc) from exc
    if int(done.returncode or 0) != 0:
        raise ValueError("读不出来：" + (done.stderr or b"").decode("utf-8", "replace")[-200:])
    try:
        payload = json.loads((done.stdout or b"").decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ValueError("ffprobe 输出不是 JSON") from exc
    streams = payload.get("streams") or []
    stream = next((item for item in streams if item.get("codec_type") == "video"), {}) or {}
    fmt = payload.get("format") or {}

    def num(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    frames = int(num(stream.get("nb_frames")))
    fps = 0.0
    rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
    if "/" in rate:
        try:
            left, right = rate.split("/", 1)
            fps = float(left) / float(right or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0.0
    duration = num(fmt.get("duration")) or num(stream.get("duration"))
    if frames <= 0 and fps > 0 and duration > 0:
        frames = int(round(duration * fps))
    if duration <= 0 and frames > 0 and fps > 0:
        duration = frames / fps
    return {
        "path": str(path),
        "name": Path(str(path)).name,
        "codec": str(stream.get("codec_name") or ""),
        "frames": frames,
        "fps": round(fps, 3),
        "duration": round(duration, 3),
        "width": int(num(stream.get("width"))),
        "height": int(num(stream.get("height"))),
    }


def extract_frames(path: str | Path, out_dir: str | Path,
                   tools: dict[str, Any] | None = None,
                   limit: int = MAX_EXTRACT) -> list[str]:
    """把动图逐帧导成 png（有上限）。"""
    tools = tools or find_tools()
    if not tools.get("ok"):
        return []
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    pattern = str(folder / "f%04d.png")
    code, _err = _run([
        str(tools["ffmpeg"]), "-v", "error", "-y",
        "-i", str(path), "-vsync", "0",
        "-frames:v", str(max(1, int(limit))), pattern,
    ])
    if code != 0:
        return []
    return sorted(str(item) for item in folder.glob("f*.png"))


def pick_evenly(frames: list[str], count: int) -> list[str]:
    """从所有帧里均匀挑 count 个（首尾都带上）。"""
    rows = [item for item in frames if item]
    if not rows:
        return []
    count = max(1, min(len(rows), int(count)))
    if count == 1:
        return [rows[0]]
    step = (len(rows) - 1) / (count - 1)
    picked: list[str] = []
    for index in range(count):
        position = int(round(index * step))
        picked.append(rows[min(len(rows) - 1, position)])
    return picked


def frame_time(index: int, fps: float, frames: int, duration: float) -> float:
    """第 index 帧（0 起）在时间轴上的位置。"""
    if fps > 0:
        return index / fps
    if frames > 0 and duration > 0:
        return index * duration / frames
    return 0.0


def build_manifest(info: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "动图：%s" % (info.get("name") or ""),
        "帧数 %d｜%dx%d｜%s｜总时长 %s"
        % (int(info.get("frames") or 0), int(info.get("width") or 0),
           int(info.get("height") or 0), info.get("codec") or "?",
           format_tc(float(info.get("duration") or 0))),
        "下面这张图是按时间均匀挑的 %d 帧（不是全部）：" % len(rows),
    ]
    for row in rows:
        lines.append("  第 %d 帧 %s（原片第 %d 帧）"
                     % (int(row["i"]), format_tc(float(row["t"])), int(row["source_index"])))
    return "\n".join(lines)


def build(path: str | Path, count: int = DEFAULT_FRAMES,
          workdir: str | Path | None = None, annotate: bool = True,
          font_hint: str = "", ffmpeg_hint: str = "",
          ffprobe_hint: str = "") -> dict[str, Any]:
    """解帧 → 均匀采样 → 拼一条带编号时间的图。永不抛异常。"""
    target = Path(str(path))
    if not target.exists():
        return {"ok": False, "error": "文件不存在：" + str(target)}
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    if not tools.get("ok"):
        return {"ok": False, "error": "没找到 ffmpeg/ffprobe",
                "hint": str(tools.get("hint") or "")}
    try:
        info = probe_animation(target, tools)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    frames_total = int(info.get("frames") or 0)
    if frames_total <= 1:
        return {"ok": False, "error": "这不是动图（只有 %d 帧）" % max(0, frames_total)}

    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="anim_"))
    root.mkdir(parents=True, exist_ok=True)
    frames = extract_frames(target, root / "frames", tools)
    if not frames:
        return {"ok": False, "error": "一帧都没解出来（可能不是 GIF/WebP）"}
    picked = pick_evenly(frames, count)
    font = find_font(font_hint) if annotate else ""
    width, height = CELL_W, CELL_H

    prepared: list[str] = []
    rows: list[dict[str, Any]] = []
    for index, frame in enumerate(picked, 1):
        at = frame_time(index - 1, float(info.get("fps") or 0),
                        frames_total, float(info.get("duration") or 0))
        out = root / ("p%03d.png" % index)
        scale = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                 "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
                 % (width, height, width, height))
        vf = scale
        if font:
            vf += "," + label_filter(0, at, width, height, font).replace(
                "text=" + _escape("0  00:00.00"),
                "text=" + _escape("%d  %s" % (index, format_tc(at))), 1,
            )
        code, _err = _run([
            str(tools["ffmpeg"]), "-v", "error", "-y", "-i", str(frame),
            "-frames:v", "1", "-vf", vf, str(out),
        ])
        if code == 0 and out.exists():
            prepared.append(str(out))
            rows.append({"i": index, "t": round(at, 3),
                         "source_index": index - 1, "file": str(out)})
    if not prepared:
        return {"ok": False, "error": "帧处理失败（ffmpeg 出错）"}

    columns = len(prepared) if len(prepared) <= 4 else int(math.ceil(len(prepared) / 2))
    rows_count = int(math.ceil(len(prepared) / columns))
    image = root / "strip.png"
    if not tile(prepared, columns, rows_count, image, width, height, tools):
        return {"ok": False, "error": "拼图失败"}

    result = {
        "ok": True,
        "info": info,
        "frames": rows,
        "image": str(image),
        "annotated": bool(font),
        "summary": "动图 %d 帧，挑了 %d 帧（%dx%d）"
                   % (frames_total, len(rows), info.get("width") or 0, info.get("height") or 0),
        "error": "",
    }
    result["manifest"] = build_manifest(info, rows)
    return result


def _escape(text: str) -> str:
    from .storyboard import filter_escape_text

    return filter_escape_text(text)


__all__ = [
    "DEFAULT_FRAMES", "MAX_EXTRACT", "CELL_W", "CELL_H",
    "probe_animation", "extract_frames", "pick_evenly", "frame_time",
    "build_manifest", "build",
]

