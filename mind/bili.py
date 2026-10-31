
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
PASSPORT = "https://passport.bilibili.com"
REFERER = "https://www.bilibili.com"

#: 扫码登录的状态码 → 给面板看的说法
QR_STATES = {
    0: "success",
    86038: "expired",
    86090: "scanned",
    86101: "waiting",
}
QR_MESSAGES = {
    "success": "登录成功，凭据已保存",
    "expired": "二维码过期了，重新生成一张",
    "scanned": "扫到了，等手机上点确认",
    "waiting": "等着扫码…",
    "error": "请求失败",
}
#: 扫码成功后要留下哪几个 Cookie
COOKIE_KEEP = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "sid")
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
    #: 有就用关键词搜索（WBI 签名），没有就逛分区最新
    use_search: bool = True
    #: 每天几点自己刷一轮（-1 = 不定时）
    daily_hour: int = -1
    #: 让模型给每条写「一句话」（上游的「它自己的一句话」）
    one_liner: bool = True
    #: 登录凭据（浏览器里复制的 Cookie 原文）。填了才有高清直链 / 字幕 / 排行榜
    cookie: str = ""
    #: 有登录态时优先要高清
    prefer_hq: bool = True

    def masked(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "keywords": self.keywords,
            "has_cookie": bool(self.cookie),
            "prefer_hq": bool(self.prefer_hq),
            "candidates": int(self.candidates),
            "storyboard_n": int(self.storyboard_n),
            "min_seconds": int(self.min_seconds),
            "max_seconds": int(self.max_seconds),
            "max_download_mb": int(self.max_download_mb),
            "use_search": bool(self.use_search),
            "daily_hour": int(self.daily_hour),
            "one_liner": bool(self.one_liner),
        }

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
            "use_search": bool(self.use_search),
            "daily_hour": int(self.daily_hour),
            "one_liner": bool(self.one_liner),
            "cookie": self.cookie,
            "prefer_hq": bool(self.prefer_hq),
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
            use_search=bool(raw.get("use_search", True)),
            daily_hour=num(raw.get("daily_hour"), -1, -1, 23),
            one_liner=bool(raw.get("one_liner", True)),
            cookie=str(raw.get("cookie") or "").strip(),
            prefer_hq=bool(raw.get("prefer_hq", True)),
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


def normalize_cookie(raw: str) -> str:
    """把「浏览器整段 Cookie」规整成可以直接放进请求头的一串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    text = text.replace("\n", " ").replace("\r", " ")
    parts = [piece.strip() for piece in text.split(";") if piece.strip()]
    return "; ".join(parts)


def cookie_value(cookie: str, name: str) -> str:
    target = str(name or "").strip().lower()
    for piece in normalize_cookie(cookie).split(";"):
        key, _, value = piece.partition("=")
        if key.strip().lower() == target:
            return value.strip()
    return ""


def _get_json(path: str, params: dict[str, Any] | None = None,
              timeout: int = HTTP_TIMEOUT, cookie: str = "") -> tuple[dict[str, Any] | None, str]:
    """请求一个 B 站接口，返回 (payload, 错误说明)。"""
    import urllib.error
    import urllib.request

    url = API + path
    if params:
        url += "?" + urlencode(params)
    headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "application/json"}
    jar = normalize_cookie(cookie)
    if jar:
        headers["Cookie"] = jar
    request = urllib.request.Request(url, headers=headers)
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


def fetch_region(rid: int = 1, limit: int = 5,
                 cookie: str = "") -> tuple[list[dict[str, Any]], str]:
    """分区最新（newlist 免登录可用；popular/ranking 会被 -352 拦）。"""
    payload, err = _get_json("/x/web-interface/newlist",
                             {"rid": int(rid), "ps": max(1, min(20, int(limit))), "pn": 1},
                             cookie=cookie)
    if err:
        return [], err
    archives = ((payload or {}).get("data") or {}).get("archives") or []
    return [normalize(item) for item in archives if isinstance(item, dict)], ""


def _get_json_headers(path: str, params: dict[str, Any] | None = None,
                      timeout: int = HTTP_TIMEOUT, cookie: str = "",
                      host: str = API) -> tuple[dict[str, Any] | None, list[str], str]:
    """请求并**留下响应头**（扫码登录要从 Set-Cookie 里拿凭据）。"""
    import urllib.error
    import urllib.request

    url = host + path
    if params:
        url += "?" + urlencode(params)
    headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "application/json"}
    jar = normalize_cookie(cookie)
    if jar:
        headers["Cookie"] = jar
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            set_cookie = list(response.headers.get_all("Set-Cookie") or [])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, [], "%s: %s" % (type(exc).__name__, exc)
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return None, [], "返回的不是 JSON"
    code = payload.get("code")
    if code not in (0, None):
        return None, [], "接口返回 %s（%s）" % (code, payload.get("message") or "")
    return payload, set_cookie, ""


def cookie_from_set_cookie(rows: list[str]) -> str:
    """从 Set-Cookie 列表里挑出登录凭据，拼成请求头那种一串。"""
    found: dict[str, str] = {}
    for row in rows or ():
        head = str(row or "").split(";", 1)[0].strip()
        name, _, value = head.partition("=")
        name = name.strip()
        if name in COOKIE_KEEP and value.strip():
            found[name] = value.strip()
    order = [name for name in COOKIE_KEEP if name in found]
    return "; ".join("%s=%s" % (name, found[name]) for name in order)


def qr_generate(timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    """要一张扫码登录的二维码（url 给前端画，key 给轮询）。"""
    payload, _headers, err = _get_json_headers(
        "/x/passport-login/web/qrcode/generate", timeout=timeout, host=PASSPORT
    )
    if err:
        return {"ok": False, "error": err}
    data = (payload or {}).get("data") or {}
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        return {"ok": False, "error": "没拿到二维码"}
    return {"ok": True, "url": url, "key": key, "error": ""}


def qr_poll(key: str, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    """问一次扫码结果。成功时把 Set-Cookie 里的凭据带回去。"""
    token = str(key or "").strip()
    if not token:
        return {"status": "error", "message": "缺 qrcode_key", "cookie": ""}
    payload, headers, err = _get_json_headers(
        "/x/passport-login/web/qrcode/poll", {"qrcode_key": token},
        timeout=timeout, host=PASSPORT,
    )
    if err:
        return {"status": "error", "message": err, "cookie": ""}
    data = (payload or {}).get("data") or {}
    try:
        code = int(data.get("code") or 0)
    except (TypeError, ValueError):
        code = -1
    state = QR_STATES.get(code, "error")
    cookie = cookie_from_set_cookie(headers) if state == "success" else ""
    message = str(data.get("message") or QR_MESSAGES.get(state, ""))
    return {
        "status": state,
        "message": message or QR_MESSAGES.get(state, ""),
        "cookie": cookie,
        "code": code,
    }


def login_check(cookie: str, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    """查这个 Cookie 是不是登录态（顺便拿到昵称）。"""
    jar = normalize_cookie(cookie)
    if not jar:
        return {"ok": False, "is_login": False, "uname": "", "message": "还没填 Cookie"}
    payload, err = _get_json("/x/web-interface/nav", None, timeout=timeout, cookie=jar)
    if err:
        return {"ok": False, "is_login": False, "uname": "", "message": "接口返回：" + err}
    data = (payload or {}).get("data") or {}
    is_login = bool(data.get("isLogin"))
    return {
        "ok": True,
        "is_login": is_login,
        "uname": str(data.get("uname") or ""),
        "mid": int(_num(data.get("mid"))),
        "level": int(_num(((data.get("level_info") or {}).get("current_level")))),
        "message": ("已登录：" + str(data.get("uname") or "")) if is_login
                   else "Cookie 无效或已过期（没登录态）",
    }


def fetch_view(bvid: str, cookie: str = "") -> tuple[dict[str, Any] | None, str]:
    payload, err = _get_json("/x/web-interface/view", {"bvid": str(bvid)}, cookie=cookie)
    if err:
        return None, err
    data = (payload or {}).get("data") or {}
    if not data:
        return None, "没有这条视频"
    return normalize(data), ""


def fetch_subtitle(bvid: str, cid: int, cookie: str = "") -> tuple[str, int, str]:
    """取字幕原文。返回 (文本, 行数, 语言)。取不到就是空串（登录后命中率高很多）。"""
    if not cid:
        return "", 0, ""
    payload, err = _get_json("/x/player/v2", {"bvid": str(bvid), "cid": int(cid)},
                             cookie=cookie)
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


def pick_stream(bvid: str, cid: int, quality: int = 32,
                cookie: str = "", prefer_hq: bool = True) -> tuple[str, int, str]:
    """拿 mp4 直链。有登录态时先要高清（80 = 1080P），拿不到就退回低清。"""
    if not cid:
        return "", 0, "没有 cid"
    jar = normalize_cookie(cookie)
    attempts: list[int] = []
    if jar and prefer_hq:
        attempts.extend([80, 64, 32])
    else:
        attempts.append(int(quality))
    last_err = ""
    for qn in attempts:
        payload, err = _get_json("/x/player/playurl", {
            "bvid": str(bvid), "cid": int(cid), "qn": int(qn),
            "fnval": 1, "platform": "html5",
        }, cookie=jar)
        if err:
            last_err = err
            continue
        data = (payload or {}).get("data") or {}
        durl = data.get("durl") or []
        if isinstance(durl, list) and durl:
            first = durl[0]
            return str(first.get("url") or ""), int(_num(first.get("size"))), ""
        last_err = "没有可下载的直链（可能是番剧/付费内容）"
    return "", 0, last_err or "没有可下载的直链"
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


#: WBI 混淆表（B 站前端公开算法，anonymous 也能用）
_WBI_MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
)


def _clean_wbi_value(value: Any) -> str:
    text = str(value)
    for char in "!'()*":
        text = text.replace(char, "")
    return text


def sign_wbi(params: dict[str, Any], mixin_key: str, wts: int | None = None) -> str:
    """给一组参数算出带 w_rid 的查询串（纯函数，能单测）。"""
    import hashlib

    payload = dict(params)
    payload["wts"] = int(wts if wts is not None else time.time())
    clean = {key: _clean_wbi_value(value) for key, value in payload.items()}
    query = urlencode(sorted(clean.items()))
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return query + "&w_rid=" + w_rid


def _fetch_buvid() -> str:
    import urllib.request

    try:
        request = urllib.request.Request(
            API + "/x/frontend/finger/spi",
            headers={"User-Agent": UA, "Referer": REFERER},
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return str(((payload.get("data") or {}).get("b_3")) or "")
    except (OSError, ValueError):
        return ""


def _fetch_mixin_key() -> str:
    import urllib.request

    try:
        request = urllib.request.Request(
            API + "/x/web-interface/nav",
            headers={"User-Agent": UA, "Referer": REFERER},
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        wbi = ((payload.get("data") or {}).get("wbi_img")) or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            return ""
        return "".join(raw[index] for index in _WBI_MIXIN)[:32]
    except (OSError, ValueError, IndexError):
        return ""


def _clean_title(raw: str) -> str:
    """搜索结果标题里带 <em class="keyword">，要剥掉。"""
    import re

    return re.sub(r"<[^>]+>", "", str(raw or "")).replace("&quot;", '"').strip()


def _duration_to_seconds(raw: Any) -> int:
    """搜索结果给的是 '6:11' / '96:19'，转成秒。"""
    text = str(raw or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def search_videos(keyword: str, limit: int = 6,
                  cookie: str = "") -> tuple[list[dict[str, Any]], str]:
    """关键词搜索（WBI 签名 + buvid3）。有登录 Cookie 就一起带上，命中率更高。"""
    import urllib.request

    word = str(keyword or "").strip()
    if not word:
        return [], "没有关键词"
    mixin = _fetch_mixin_key()
    if not mixin:
        return [], "拿不到 WBI 签名用的密钥"
    query = sign_wbi({
        "keyword": word, "search_type": "video",
        "page": 1, "page_size": max(1, min(20, int(limit))), "order": "pubdate",
    }, mixin)
    headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "application/json"}
    jar = normalize_cookie(cookie)
    if jar:
        headers["Cookie"] = jar
    else:
        buvid = _fetch_buvid()
        if buvid:
            headers["Cookie"] = "buvid3=" + buvid
    try:
        request = urllib.request.Request(
            API + "/x/web-interface/wbi/search/type?" + query, headers=headers
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        return [], "搜索请求失败：%s: %s" % (type(exc).__name__, exc)
    code = payload.get("code")
    if code not in (0, None):
        return [], "搜索接口返回 %s（%s）" % (code, payload.get("message") or "")
    rows = ((payload.get("data") or {}).get("result")) or []
    if not isinstance(rows, list):
        return [], "搜索结果格式变了"
    out: list[dict[str, Any]] = []
    for item in rows:
        bvid = str(item.get("bvid") or "")
        if not bvid:
            continue
        out.append({
            "bvid": bvid,
            "title": _clean_title(item.get("title")),
            "desc": _clean_title(item.get("description")),
            "up": str(item.get("author") or ""),
            "duration": _duration_to_seconds(item.get("duration")),
            "view": int(_num(item.get("play"))),
            "pubdate": int(_num(item.get("pubdate"))),
            "cid": 0,
            "url": "https://www.bilibili.com/video/" + bvid,
        })
    return out, ""


def fetch_subtitle_lines(bvid: str, cid: int,
                         cookie: str = "") -> list[dict[str, Any]]:
    """带时间码的字幕行（挂到分镜每一格用）。取不到就是空列表。"""
    if not cid:
        return []
    payload, err = _get_json("/x/player/v2", {"bvid": str(bvid), "cid": int(cid)},
                             cookie=cookie)
    if err:
        return []
    subtitles = (((payload or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not isinstance(subtitles, list) or not subtitles:
        return []
    pick = next((item for item in subtitles if str(item.get("lan", "")).startswith("zh")),
                subtitles[0])
    url = str(pick.get("subtitle_url") or "")
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return []
    import urllib.request

    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return []
    body = raw.get("body") if isinstance(raw, dict) else None
    if not isinstance(body, list):
        return []
    lines: list[dict[str, Any]] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        lines.append({"from": _num(item.get("from")), "to": _num(item.get("to")), "text": text})
    return lines


def ytdlp_path() -> str:
    """有 yt-dlp 就用它取流（它认得 B 站/YouTube 的绝大多数页面）。"""
    import shutil

    for name in ("yt-dlp", "yt-dlp.exe", "youtube-dl", "youtube-dl.exe"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def download_with_ytdlp(url: str, out: Path | str, max_bytes: int = 60 * 1024 * 1024,
                        bin_path: str = "", timeout: int = 180) -> tuple[bool, str]:
    """用 yt-dlp 取流（限 480P、单文件），失败就把话说清楚。"""
    import subprocess

    tool = bin_path or ytdlp_path()
    if not tool:
        return False, "没装 yt-dlp"
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        tool, "--no-warnings", "--no-playlist",
        "-f", "bv*[height<=480]+ba/b[height<=480]/b",
        "--merge-output-format", "mp4",
        "-o", str(target), str(url),
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              check=False, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "yt-dlp 跑不起来：%s" % exc
    if int(done.returncode or 0) != 0:
        tail = (done.stderr or b"").decode("utf-8", "replace")[-300:].strip()
        return False, "yt-dlp 失败：" + (tail or "未知原因")
    if not target.exists():
        return False, "yt-dlp 说成功了但文件不在"
    try:
        if target.stat().st_size > max(1024 * 1024, int(max_bytes)):
            target.unlink()
            return False, "视频超过上限"
    except OSError:
        pass
    return True, ""


def due_today(daily_hour: int, now: float | None = None,
              last_run: float = 0.0) -> bool:
    """今天这个点该不该自己刷一轮（同一天只跑一次）。"""
    if int(daily_hour) < 0:
        return False
    stamp = time.time() if now is None else float(now)
    local = time.localtime(stamp)
    if local.tm_hour != int(daily_hour):
        return False
    today = time.strftime("%Y-%m-%d", local)
    if last_run > 0 and time.strftime("%Y-%m-%d", time.localtime(last_run)) == today:
        return False
    return True


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
        ones = str(item.get("oneself") or "").strip()
        if ones:
            lines.append("它说：" + ones[:120])
        quote = str(item.get("subtitle") or item.get("desc") or "").strip()
        if quote:
            label = "字幕摘录" if ones else "它说"
            lines.append(label + "：" + (quote[:120] + ("…" if len(quote) > 120 else "")))
        if item.get("storyboard"):
            lines.append("分镜：" + str(item["storyboard"]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "API", "DEFAULT_RIDS", "BiliSettings", "SeenStore",
    "load_settings", "save_settings",
    "fetch_region", "fetch_view", "fetch_subtitle", "pick_stream", "download",
    "normalize", "score_item", "pick", "format_duration", "format_count", "build_report",
    "sign_wbi", "search_videos", "fetch_subtitle_lines", "ytdlp_path",
    "normalize_cookie", "cookie_value", "login_check",
    "PASSPORT", "QR_STATES", "QR_MESSAGES", "COOKIE_KEEP",
    "cookie_from_set_cookie", "qr_generate", "qr_poll",
    "download_with_ytdlp", "due_today",
]

