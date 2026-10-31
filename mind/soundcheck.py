
"""听一段音频：响度、动态、峰值 → 一句人话结论。

移植自 ALittleLov/dsh-soundcheck 的思路（MIT）：**只做本地分析，不联网、不上传**，
把一长串数字收成一句能听懂的评价。

这里用 ffmpeg 自带的两个滤镜算：
  - @ebur128@：流媒体口径的响度（LUFS）、动态范围（LRA）、真峰值（dBFS）
  - @volumedetect@：平均 / 最大电平（dB）
判词按流媒体发布的标准分档（-14 LUFS 附近是主流平台的目标响度）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .storyboard import _run, audio_envelope, find_tools, probe_video

#: 主流平台的目标响度（-14 LUFS 上下）
TARGET_LUFS = -14.0
#: 峰值到这条线就准备削波了
PEAK_CEILING = -1.0
#: 动态范围低于这个数说明压得有点狠
NARROW_LRA = 4.0

_RE_LUFS = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_RE_LRA = re.compile(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU")
_RE_PEAK = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")
_RE_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_RE_MAX = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def parse_loudness(text: str) -> dict[str, float]:
    """从 ffmpeg 的 stderr 里抠出响度/动态/峰值（纯函数，能单测）。"""
    raw = str(text or "")
    out: dict[str, float] = {}

    def grab(pattern: re.Pattern[str], key: str) -> None:
        match = None
        for found in pattern.finditer(raw):
            match = found
        if match is not None:
            out[key] = float(match.group(1))

    grab(_RE_LUFS, "lufs")
    grab(_RE_LRA, "lra")
    grab(_RE_PEAK, "true_peak")
    grab(_RE_MEAN, "mean_db")
    grab(_RE_MAX, "max_db")
    return out


def verdict(features: dict[str, Any]) -> str:
    """把数字翻成人话结论。"""
    lufs = features.get("lufs")
    peak = features.get("true_peak")
    lra = features.get("lra")
    lines: list[str] = []
    if lufs is not None:
        if lufs < -20:
            lines.append("整体很轻（%.1f LUFS），手机外放基本听不清" % lufs)
        elif lufs < -16:
            lines.append("偏轻（%.1f LUFS），安静环境才行" % lufs)
        elif lufs <= -11:
            lines.append("响度正常（%.1f LUFS），跟主流平台差不多" % lufs)
        elif lufs <= -8:
            lines.append("偏响（%.1f LUFS），发流媒体会被压一点" % lufs)
        else:
            lines.append("太吵了（%.1f LUFS），动态快被榨干了" % lufs)
    if peak is not None:
        if peak >= -0.1:
            lines.append("峰值 %.1f dBFS，已经贴到顶，转码容易削波" % peak)
        elif peak >= PEAK_CEILING:
            lines.append("峰值 %.1f dBFS，贴着上限，算正常" % peak)
        else:
            lines.append("峰值 %.1f dBFS，留了余量" % peak)
    if lra is not None:
        if lra < NARROW_LRA:
            lines.append("动态只有 %.1f LU，压得比较狠，听起来会平" % lra)
        elif lra <= 12:
            lines.append("动态 %.1f LU，正常" % lra)
        else:
            lines.append("动态 %.1f LU，起伏很大，安静地方听才舒服" % lra)
    return "；".join(lines) + "。" if lines else ""


def analyze(path: str | Path, ffmpeg_hint: str = "",
            ffprobe_hint: str = "", timeout: int = 300) -> dict[str, Any]:
    """跑一次分析（读整段音频，长歌要几十秒）。永不抛异常。"""
    target = Path(str(path))
    if not target.exists():
        return {"ok": False, "error": "文件不存在：" + str(target)}
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    if not tools.get("ok"):
        return {"ok": False, "error": "没找到 ffmpeg/ffprobe",
                "hint": str(tools.get("hint") or "")}
    info: dict[str, Any] = {}
    try:
        info = probe_video(target, tools)
    except ValueError:
        info = {}          # 纯音频没有视频流，读不到就算了
    code, err = _run([
        str(tools["ffmpeg"]), "-hide_banner", "-nostats",
        "-i", str(target),
        "-af", "ebur128=peak=true:framelog=quiet,volumedetect",
        "-f", "null", "-",
    ], timeout=timeout)
    if code != 0:
        return {"ok": False, "error": "分析失败：" + (err.strip() or "ffmpeg 出错")}
    features = parse_loudness(err)
    if not features:
        return {"ok": False, "error": "没读到响度数据（可能不是音频文件）"}
    result = {
        "ok": True,
        "error": "",
        "path": str(target),
        "name": target.name,
        "duration": info.get("duration") or 0.0,
        "channels": info.get("channels") or 0,
        "sample_rate": info.get("sample_rate") or 0,
        **features,
    }
    result["verdict"] = verdict(result)
    return result


def spectrum(path: str | Path, out: str | Path, width: int = 1200, height: int = 400,
             ffmpeg_hint: str = "", ffprobe_hint: str = "") -> bool:
    """画一张频谱图（给模型/人看整体能量分布）。"""
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    if not tools.get("ok"):
        return False
    code, _err = _run([
        str(tools["ffmpeg"]), "-v", "error", "-y", "-i", str(path),
        "-lavfi", "showspectrumpic=s=%dx%d:legend=1:color=intensity"
                  % (max(200, int(width)), max(120, int(height))),
        "-frames:v", "1", str(out),
    ])
    return code == 0 and Path(out).exists()


def waveform(path: str | Path, out: str | Path, width: int = 1200, height: int = 160,
             ffmpeg_hint: str = "", ffprobe_hint: str = "") -> bool:
    """波形条（复用分镜那条）。"""
    tools = find_tools(ffmpeg_hint, ffprobe_hint)
    return bool(tools.get("ok")) and audio_envelope(path, out, width, height, tools)


def report(result: dict[str, Any]) -> str:
    """聊天里那段字。"""
    if not result.get("ok"):
        text = "听不了：" + str(result.get("error") or "未知原因")
        if result.get("hint"):
            text += "\n" + str(result["hint"])
        return text
    bits = []
    if result.get("duration"):
        bits.append("%.1f 秒" % float(result["duration"]))
    if result.get("channels"):
        bits.append("%d 声道" % int(result["channels"]))
    if result.get("sample_rate"):
        bits.append("%d Hz" % int(result["sample_rate"]))
    lines = ["音频：" + str(result.get("name") or "") + ("（" + "，".join(bits) + "）" if bits else "")]
    numbers = []
    for key, label, unit in (
        ("lufs", "响度", "LUFS"), ("lra", "动态", "LU"),
        ("true_peak", "真峰值", "dBFS"), ("max_db", "最大电平", "dB"),
        ("mean_db", "平均电平", "dB"),
    ):
        if result.get(key) is not None:
            numbers.append("%s %.1f %s" % (label, float(result[key]), unit))
    if numbers:
        lines.append("｜".join(numbers))
    if result.get("verdict"):
        lines.append(str(result["verdict"]))
    return "\n".join(lines)


__all__ = [
    "TARGET_LUFS", "PEAK_CEILING", "NARROW_LRA",
    "parse_loudness", "verdict", "analyze", "spectrum", "waveform", "report",
]

