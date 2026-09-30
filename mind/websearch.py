"""联网搜索（纯标准库）。

为什么自己做而不是装库：本插件的前提是零第三方依赖。
所以用 urllib 抓 DuckDuckGo 网页版，再用 re 解析。

它一定会坏：对方改版、限流、换反爬。所以解析失败不抛异常，只返回空列表
（调用方按「这次没搜到」处理）；结果统一成 title / url / snippet，
将来换来源上层不用动。
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Any

ENDPOINT = "https://html.duckduckgo.com/html/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_Q = chr(34)
_RESULT_RE = re.compile(
    "<a[^>]+class=" + _Q + "[^" + _Q + "]*result__a[^" + _Q + "]*" + _Q
    + "[^>]*href=" + _Q + "([^" + _Q + "]+)" + _Q + "[^>]*>(.*?)</a>",
    re.S | re.I,
)
_SNIPPET_RE = re.compile(
    "class=" + _Q + "[^" + _Q + "]*result__snippet[^" + _Q + "]*" + _Q + "[^>]*>(.*?)</a>",
    re.S | re.I,
)
_TAG_RE = re.compile("<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", str(text or ""))).strip()


def _real_url(raw: str) -> str:
    """DDG 的链接常常是跳转壳，把真正的地址抠出来。"""
    link = html.unescape(str(raw or ""))
    if link.startswith("//"):
        link = "https:" + link
    try:
        parts = urllib.parse.urlparse(link)
        target = (urllib.parse.parse_qs(parts.query).get("uddg") or [""])[0]
        if target:
            return urllib.parse.unquote(target)
    except Exception:  # noqa: BLE001
        pass
    return link


def search(query: str, limit: int = 5, timeout: float = 8.0) -> list[dict[str, Any]]:
    """搜一次。失败一律返回空列表 —— 搜不到不是错误，不该把聊天打断。"""
    text = str(query or "").strip()
    if not text:
        return []
    body = urllib.parse.urlencode({"q": text, "kl": "cn-zh"}).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            page = response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 断网/超时/被限流都算「没搜到」
        return []
    hits = _RESULT_RE.findall(page)
    snippets = [_clean(one) for one in _SNIPPET_RE.findall(page)]
    out: list[dict[str, Any]] = []
    for index, (href, title) in enumerate(hits):
        name = _clean(title)
        if not name:
            continue
        out.append({
            "title": name,
            "url": _real_url(href),
            "snippet": snippets[index] if index < len(snippets) else "",
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def render(results: list[dict[str, Any]], limit: int = 5) -> str:
    """把结果压成几行，喂给模型。"""
    if not results:
        return ""
    lines = []
    for index, item in enumerate(results[: max(1, int(limit))], 1):
        line = str(index) + ". " + str(item.get("title") or "")
        snippet = str(item.get("snippet") or "").strip()
        if snippet:
            line += " —— " + snippet[:120]
        lines.append(line)
    return "\n".join(lines)


__all__ = ["search", "render", "ENDPOINT"]

