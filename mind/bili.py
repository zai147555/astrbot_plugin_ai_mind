
"""刷 B 站 + 视频分镜：两件事合到一条链上。

合并的两个来源：
  1. **dsh-bili-scroll**（c1overzz，MIT）：每天刷一轮 B 站 → 读字幕判断 → 写报告 → 值得的单独拎出来；
  2. **dsh-video-storyboard**（SAOS-3624，Apache-2.0）：把视频切成宫格图，让纯文本模型能"看"。

合并后的判定逻辑：**先看字（字幕/简介），再看画（分镜）**。
只有字幕说"有信息量"的视频才值得花 token 去取帧，所以「值得的」这条链上
既有字幕摘要，也有一张能翻的宫格图。

只用标准库（urllib），接口都是**免登录**能用的那几个：
  - @x/web-interface/newlist@   分区最新（popular / ranking 会被 -352 风控拦掉）
  - @x/web-interface/view@      标题、UP、时长、播放量、cid
  - @x/player/v2@               字幕（多数视频匿名取不到，取不到就用简介）
  - @x/player/playurl@          mp4 直链（360P/720P，下载要带 Referer）

没搬的：自动评论 @ 你（要登录 cookie + 风控风险）、Whisper 本地转写。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

API = "https://api.bilibili.com"
REFERER = "https://www.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: 默认逛哪几个分区（新版分区 id）：动画 / 游戏 / 科技 / 生活 / 影视 / 知识
DEFAULT_RIDS = (1, 4, 36, 160, 181, 32)
#: 一轮的默认规模
DEFAULT_CANDIDATES = 6
DEFAULT_STORYBOARD = 2
DEFAULT_MIN_SECONDS = 60
DEFAULT_MAX_SECONDS = 1800

HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# 设置与"已推过"
# ---------------------------------------------------------------------------


@dataclass
class BiliSettings:
    enabled: bool = True
    keywords: str = ""
    candidates: int = DEFAULT_CANDIDATES
    storyboard_n: int = DEFAULT_STORYBOARD
    min_seconds: int = DEFAULT_MIN_SECONDS
    max_seconds: int = DEFAULT_MAX_SECONDS
    max_download_mb: int = 60
    rids: list[int] = field(default_factory=lambda: list(DEFAULT_RIDS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "keywords": self.keywords,
            "candidates": int(self.candidates),
            "storyboard_n": int(self.storyboard_n),
            "min_seconds": int(self.min_seconds),
            "max_seconds": int(self.max_seconds),
            "max_download_mb": int(self.max_download_mb),
            "rids": list(self.rids),
        }

    @staticmethod
    def from_dict(data: Any) -> "BiliSettings":
        raw = data if isinstance(data, dict) else {}

        def num(value: Any, fallback: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return fallback

        rids: list[int] = []
        for item in raw.get("rids") or DEFAULT_RIDS:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in rids:
                rids.append(value)
        return BiliSettings(
            enabled=bool(raw.get("enabled", True)),
            keywords=str(raw.get("keywords") or ""),
            candidates=num(raw.get("candidates"), DEFAULT_CANDIDATES, 1, 20),
            storyboard_n=num(raw.get("storyboard_n"), DEFAULT_STORYBOARD, 0, 5),
            min_seconds=num(raw.get("min_seconds"), DEFAULT_MIN_SECONDS, 5, 7200),
            max_seconds=num(raw.get("max_seconds"), DEFAULT_MAX_SECONDS, 30, 21600),
            max_download_mb=num(raw.get("max_download_mb"), 60, 4, 2048),
            rids=rids or list(DEFAULT_RIDS),
        )


def load_settings(path: Path | str) -> BiliSettings:
    try:
        return BiliSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return BiliSettings()


def save_settings(path: Path | str, settings: BiliSettings) -> bool:
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


class SeenStore:
    """记住推过的 BV 号，别天天推同一条。"""

    def __init__(self, path: Path | str, keep: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(20, int(keep))
        self._data: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): float(v or 0) for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            self._data = {}

    def known(self, bvid: str) -> bool:
        return str(bvid) in self._data

    def mark(self, bvid: str, when: float | None = None) -> None:
        self._data[str(bvid)] = float(when or time.time())
        self.prune()
        self._flush()

    def prune(self) -> None:
        if len(self._data) <= self.keep:
            return
        ordered = sorted(self._data.items(), key=lambda item: item[1], reverse=True)
        self._data = dict(ordered[: self.keep])

    def clear(self) -> int:
        count = len(self._data)
        self._data = {}
        self._flush()
        return count

    def count(self) -> int:
        return len(self._data)

    def _flush(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_json(path: str, params: dict[str, Any] | None = None,
              timeout: int = HTTP_TIMEOUT) -> tuple[dict[str, Any] | None, str]:
    """请求一个 B 站接口，返回 (payload, 错误说明)。"""
    import urllib.error
    import urllib.request

    url = API + path
    if params:
        url += "?" + urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "请求失败：%s: %s" % (type(exc).__name__, exc)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None, "返回的不是 JSON"
    code = payload.get("code")
    if code not in (0, None):
        return None, "接口返回 %s（%s）" % (code, payload.get("message") or "")
    return payload, ""


# ---------------------------------------------------------------------------
# 发现 / 详情 / 字幕 / 直链
# ---------------------------------------------------------------------------


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def normalize(candidate: dict[str, Any]) -> dict[str, Any]:
    """把 newlist / view 的两种结构抹平成一种。"""
    owner = candidate.get("owner")
    if isinstance(owner, dict):
        up = str(owner.get("name") or "")
    else:
        up = str(owner or "")
    stat = candidate.get("stat") if isinstance(candidate.get("stat"), dict) else {}
    pages = candidate.get("pages")
    cid = 0
    if isinstance(pages, list) and pages:
        cid = int(_num(pages[0].get("cid")))
    return {
        "bvid": str(candidate.get("bvid") or ""),
        "title": str(candidate.get("title") or "").strip(),
        "desc": str(candidate.get("desc") or "").strip(),
        "up": up,
        "duration": int(_num(candidate.get("duration"))),
        "view": int(_num(stat.get("view"))),
        "pubdate": int(_num(candidate.get("pubdate"))),
        "cid": cid,
        "url": "https://www.bilibili.com/video/" + str(candidate.get("bvid") or ""),
    }


def fetch_region(rid: int = 1, limit: int = 5) -> tuple[list[dict[str, Any]], str]:
    """分区最新（newlist 免登录可用；popular/ranking 会被 -352 拦）。"""
    payload, err = _get_json("/x/web-interface/newlist",
                             {"rid": int(rid), "ps": max(1, min(20, int(limit))), "pn": 1})
    if err:
        return [], err
    archives = ((payload or {}).get("data") or {}).get("archives") or []
    return [normalize(item) for item in archives if isinstance(item, dict)], ""


def fetch_view(bvid: str) -> tuple[dict[str, Any] | None, str]:
    payload, err = _get_json("/x/web-interface/view", {"bvid": str(bvid)})
    if err:
        return None, err
    data = (payload or {}).get("data") or {}
    if not data:
        return None, "没有这条视频"
    return normalize(data), ""


def fetch_subtitle(bvid: str, cid: int) -> tuple[str, int, str]:
    """取字幕原文。返回 (文本, 行数, 语言)。取不到就是空串。"""
    if not cid:
        return "", 0, ""
    payload, err = _get_json("/x/player/v2", {"bvid": str(bvid), "cid": int(cid)})
    if err:
        return "", 0, ""
    subtitles = (((payload or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not isinstance(subtitles, list) or not subtitles:
        return "", 0, ""
    pick = next((item for item in subtitles if item.get("lan", "").startswith("zh")), subtitles[0])
    url = str(pick.get("subtitle_url") or "")
    if not url:
        return "", 0, ""
    if url.startswith("//"):
        url = "https:" + url
    import urllib.request

    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return "", 0, ""
    body = raw.get("body") if isinstance(raw, dict) else None
    if not isinstance(body, list):
        return "", 0, ""
    lines = [str(item.get("content") or "").strip() for item in body if isinstance(item, dict)]
    lines = [line for line in lines if line]
    return " ".join(lines), len(lines), str(pick.get("lan_doc") or pick.get("lan") or "")


def pick_stream(bvid: str, cid: int, quality: int = 32) -> tuple[str, int, str]:
    """拿 mp4 直链。返回 (url, 字节数, 错误)。"""
    if not cid:
        return "", 0, "没有 cid"
    payload, err = _get_json("/x/player/playurl", {
        "bvid": str(bvid), "cid": int(cid), "qn": int(quality),
        "fnval": 1, "platform": "html5",
    })
    if err:
        return "", 0, err
    data = (payload or {}).get("data") or {}
    durl = data.get("durl") or []
    if not isinstance(durl, list) or not durl:
        return "", 0, "没有可下载的直链（可能是番剧/付费内容）"
    first = durl[0]
    return str(first.get("url") or ""), int(_num(first.get("size"))), ""


def download(url: str, out: Path | str, max_bytes: int = 60 * 1024 * 1024,
             timeout: int = 180) -> tuple[bool, str]:
    """下载视频（带 B 站要求的 Referer）。超上限就停。"""
    import urllib.error
    import urllib.request

    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    limit = max(1024 * 1024, int(max_bytes))
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
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
                    return False, "视频超过 %d MB 上限（面板可改）" % (limit // (1024 * 1024))
                handle.write(chunk)
        return True, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        try:
            target.unlink()
        except OSError:
            pass
        return False, "下载失败：%s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 挑：先看字
# ---------------------------------------------------------------------------


def _keywords(settings: BiliSettings) -> list[str]:
    raw = str(settings.keywords or "").replace("，", " ").replace(",", " ")
    return [word.strip() for word in raw.split() if word.strip()]


def score_item(item: dict[str, Any], settings: BiliSettings) -> tuple[float, list[str]]:
    """给一条视频打分，并写下理由（理由会进日报）。"""
    score = 0.0
    reasons: list[str] = []
    words = _keywords(settings)
    haystack = (item.get("title") or "") + " " + (item.get("desc") or "")
    hits = [word for word in words if word and word.lower() in haystack.lower()]
    if hits:
        score += 2.0 * len(hits)
        reasons.append("标题/简介命中「%s」" % "」「".join(hits[:3]))

    duration = int(item.get("duration") or 0)
    if settings.min_seconds <= duration <= settings.max_seconds:
        score += 1.0
        reasons.append("时长 %s，正好能一口气看完" % format_duration(duration))
    elif duration and duration < settings.min_seconds:
        score -= 2.0
        reasons.append("太短（%s），信息量可能不够" % format_duration(duration))

    views = int(item.get("view") or 0)
    if views > 0:
        score += min(2.0, math.log10(max(1, views)) - 2.0)
        if views >= 100000:
            reasons.append("播放 %s，讨论度不低" % format_count(views))

    subtitle_len = len(str(item.get("subtitle") or ""))
    if subtitle_len >= 200:
        score += 1.5
        reasons.append("有 %d 字字幕，能直接读原文" % subtitle_len)
    elif subtitle_len:
        score += 0.5
        reasons.append("字幕不多（%d 字），主要靠画面" % subtitle_len)
    return round(score, 2), reasons


def pick(items: Iterable[dict[str, Any]], settings: BiliSettings,
         seen: SeenStore | None = None) -> list[dict[str, Any]]:
    """过滤 + 打分 + 排序。返回带 score/reasons 的副本（不含已推过的）。"""
    picked: list[dict[str, Any]] = []
    for item in items:
        bvid = str(item.get("bvid") or "")
        if not bvid:
            continue
        if str(item.get("title") or "").strip() in ("", "-"):
            continue
        if seen is not None and seen.known(bvid):
            continue
        duration = int(item.get("duration") or 0)
        if duration and (duration < 20 or duration > 6 * 3600):
            continue
        score, reasons = score_item(item, settings)
        row = dict(item)
        row["score"] = score
        row["reasons"] = reasons
        picked.append(row)
    picked.sort(key=lambda row: row.get("score", 0), reverse=True)
    return picked[: max(1, int(settings.candidates))]


def format_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    return "%d:%02d" % (total // 60, total % 60)


def format_count(value: int) -> str:
    number = int(value)
    if number >= 100000000:
        return "%.1f 亿" % (number / 100000000)
    if number >= 10000:
        return "%.1f 万" % (number / 10000)
    return str(number)


def build_report(day: str, items: list[dict[str, Any]]) -> str:
    """日报（Markdown）。标题行固定成 ### BV号 —— 数标题行就知道挑了几条。"""
    lines = ["# %s 刷B站" % day, ""]
    if not items:
        lines.append("今天没有值得推的。")
        return "\n".join(lines) + "\n"
    lines.append("今天挑了 %d 条：" % len(items))
    lines.append("")
    for item in items:
        bvid = str(item.get("bvid") or "")
        lines.append("### " + bvid)
        title = str(item.get("title") or "").replace("\n", " ").strip()
        lines.append("**%s**" % title)
        meta = []
        if item.get("up"):
            meta.append("UP：" + str(item["up"]))
        if item.get("duration"):
            meta.append("时长 " + format_duration(int(item["duration"])))
        if item.get("view"):
            meta.append("播放 " + format_count(int(item["view"])))
        meta.append(str(item.get("url") or ("https://www.bilibili.com/video/" + bvid)))
        lines.append("｜".join(meta))
        reasons = item.get("reasons") or []
        if reasons:
            lines.append("为什么值得：" + "；".join(str(reason) for reason in reasons))
        quote = str(item.get("subtitle") or item.get("desc") or "").strip()
        if quote:
            lines.append("它说：" + (quote[:120] + ("…" if len(quote) > 120 else "")))
        if item.get("storyboard"):
            lines.append("分镜：" + str(item["storyboard"]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "API", "DEFAULT_RIDS", "BiliSettings", "SeenStore",
    "load_settings", "save_settings",
    "fetch_region", "fetch_view", "fetch_subtitle", "pick_stream", "download",
    "normalize", "score_item", "pick", "format_duration", "format_count", "build_report",
]

