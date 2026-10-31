
"""生图：接 OpenAI Images 兼容接口，文生图 + 参考图编辑。

移植自 CM-Silence/dsh-mo-image（MIT）。上游最值钱的是「模型适配层」：
不同平台的字段名、尺寸规则、回应形状都不一样，把这些差异收进一张描述符表里，
调用方只认一套规范参数。这里照着做了同一件事。

只用标准库（urllib + 手写 multipart）：
  - @json@ 传输：文生图（/images/generations）
  - @multipart@ 传输：参考图编辑（/images/edits）
  - 回应认 b64_json / data url / 外链 / 直接二进制四种
  - 尺寸自动适配到模型允许的最近合法值（缩不回去就不缩，如实说明）
  - 报错自愈：上游说"不认识某参数"就摘掉重试一次；429/5xx 退避重试一次
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

#: 一个请求最多等多久
TIMEOUT = 120

#: 规范参数字段（适配器用它做白名单）
FIELDS = ("model", "prompt", "size", "n", "quality", "response_format")


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


def _sizes(*pairs: str) -> dict[str, Any]:
    return {"allowed": list(pairs)}


ADAPTERS: dict[str, dict[str, Any]] = {
    # 通用兜底：绝大多数 OpenAI 兼容平台都能吃这一套
    "openai-images": {
        "label": "OpenAI Images 兼容",
        "send": ("model", "prompt", "size", "n", "quality", "response_format"),
        "size": {"allowed": ["256x256", "512x512", "1024x1024", "1536x1024", "1024x1536"]},
        "response_format": "b64_json",
        "transport": "json",
        "path": "/images/generations",
    },
    "gpt-image": {
        "label": "gpt-image 系列",
        "send": ("model", "prompt", "size", "n", "quality"),
        "size": {"allowed": ["1024x1024", "1536x1024", "1024x1536"]},
        "transport": "json",
        "path": "/images/generations",
    },
    "dall-e-3": {
        "label": "DALL·E 3",
        "send": ("model", "prompt", "size", "n", "quality", "response_format"),
        "size": {"allowed": ["1024x1024", "1792x1024", "1024x1792"]},
        "quality": {"high": "hd", "standard": "standard", "low": "standard"},
        "response_format": "b64_json",
        "transport": "json",
        "path": "/images/generations",
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "send": ("model", "prompt", "image_size", "batch_size", "num_inference_steps", "guidance_scale"),
        "size": {"min_pixels": 256 * 256, "max_pixels": 2048 * 2048, "multiple": 32, "field": "image_size"},
        "response": "images",
        "transport": "json",
        "path": "/images/generations",
    },
    "dashscope-wanx": {
        "label": "通义万相（异步）",
        "send": ("model", "input", "parameters"),
        "size": {"allowed": ["1024*1024", "1280*720", "720*1280"], "field": "parameters.size", "joiner": "*"},
        "compose": {"input": ("prompt",), "parameters": ("size", "n")},
        "transport": "json",
        "async": True,
        "path": "/services/aigc/text2image/image-synthesis",
        "poll": {"path": "/tasks/{taskId}", "interval": 2.0, "tries": 60,
                 "status_field": "output.task_status",
                 "success": "SUCCEEDED", "result_field": "output.results"},
    },
}

#: 域名 → 适配器（设置里没显式指定时按这个猜）
DOMAIN_ADAPTERS = (
    ("api.openai.com", "openai-images"),
    ("api.siliconflow.cn", "siliconflow"),
    ("dashscope.aliyuncs.com", "dashscope-wanx"),
    ("ark.cn-beijing.volces.com", "openai-images"),
)

#: 模型 ID 关键字 → 适配器
MODEL_ADAPTERS = (
    ("gpt-image", "gpt-image"),
    ("dall-e-3", "dall-e-3"),
    ("dall-e", "dall-e-3"),
    ("wanx", "dashscope-wanx"),
    ("flux", "siliconflow"),
    ("stable-diffusion", "siliconflow"),
    ("sd3", "siliconflow"),
)


def pick_adapter(base_url: str, model: str, explicit: str = "") -> str:
    """显式指定 → 域名 → 模型 ID → 通用兜底。"""
    wanted = str(explicit or "").strip()
    if wanted in ADAPTERS:
        return wanted
    host = str(base_url or "").lower()
    for needle, name in DOMAIN_ADAPTERS:
        if needle in host:
            return name
    lowered = str(model or "").lower()
    for needle, name in MODEL_ADAPTERS:
        if needle in lowered:
            return name
    return "openai-images"


def adapter_of(name: str) -> dict[str, Any]:
    return ADAPTERS.get(str(name or ""), ADAPTERS["openai-images"])


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------


def parse_size(text: str, default: str = "1024x1024") -> tuple[int, int]:
    match = re.match(r"^\s*(\d{2,5})\s*[x*×]\s*(\d{2,5})\s*$", str(text or ""))
    if not match:
        first, _, second = str(default or "1024x1024").partition("x")
        try:
            return int(first), int(second or first)
        except (TypeError, ValueError):
            return 1024, 1024
    return int(match.group(1)), int(match.group(2))


def adapt_size(requested: tuple[int, int], adapter: dict[str, Any]) -> tuple[str, str]:
    """把用户要的尺寸换成模型允许的最近合法值。返回 (尺寸串, 说明)。"""
    width, height = max(16, int(requested[0])), max(16, int(requested[1]))
    joiner = str((adapter.get("size") or {}).get("joiner") or "x")
    allowed = (adapter.get("size") or {}).get("allowed") or []
    if allowed:
        want = width * height
        best = min(
            (item for item in allowed),
            key=lambda item: abs(parse_size(item.replace("*", "x"))[0]
                                 * parse_size(item.replace("*", "x"))[1] - want),
        )
        note = ""
        if best.replace("*", "x") != ("%dx%d" % (width, height)):
            note = "模型不支持 %dx%d，改用了最近的 %s" % (width, height, best.replace("*", "x"))
        return best, note
    rule = adapter.get("size") or {}
    multiple = int(rule.get("multiple") or 1) or 1
    if multiple > 1:
        width = max(multiple, int(round(width / multiple)) * multiple)
        height = max(multiple, int(round(height / multiple)) * multiple)
    min_pixels = int(rule.get("min_pixels") or 0)
    max_pixels = int(rule.get("max_pixels") or 0)
    note = ""
    pixels = width * height
    if min_pixels and pixels < min_pixels:
        scale = math.sqrt(min_pixels / max(1, pixels))
        width = int(math.ceil(width * scale / multiple)) * multiple
        height = int(math.ceil(height * scale / multiple)) * multiple
        note = "模型最小 %d 像素，放大到 %dx%d" % (min_pixels, width, height)
    elif max_pixels and pixels > max_pixels:
        scale = math.sqrt(max_pixels / max(1, pixels))
        width = max(multiple, int(width * scale // multiple) * multiple)
        height = max(multiple, int(height * scale // multiple) * multiple)
        note = "模型最大 %d 像素，缩到 %dx%d" % (max_pixels, width, height)
    return "%d%s%d" % (width, joiner, height), note


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


@dataclass
class ImageGenSettings:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-image-1"
    adapter: str = ""
    size: str = "1024x1024"
    quality: str = ""
    count: int = 1
    timeout: int = TIMEOUT

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled), "base_url": self.base_url,
            "api_key": self.api_key, "model": self.model, "adapter": self.adapter,
            "size": self.size, "quality": self.quality,
            "count": int(self.count), "timeout": int(self.timeout),
        }

    def masked(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "base_url": self.base_url,
            "model": self.model, "adapter": self.adapter,
            "resolved_adapter": pick_adapter(self.base_url, self.model, self.adapter),
            "size": self.size, "quality": self.quality,
            "count": int(self.count), "has_key": bool(self.api_key),
        }

    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    @staticmethod
    def from_dict(data: Any) -> "ImageGenSettings":
        raw = data if isinstance(data, dict) else {}

        def num(value: Any, fallback: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return fallback

        return ImageGenSettings(
            enabled=bool(raw.get("enabled")),
            base_url=str(raw.get("base_url") or "https://api.openai.com/v1").strip(),
            api_key=str(raw.get("api_key") or "").strip(),
            model=str(raw.get("model") or "gpt-image-1").strip(),
            adapter=str(raw.get("adapter") or "").strip(),
            size=str(raw.get("size") or "1024x1024").strip(),
            quality=str(raw.get("quality") or "").strip(),
            count=num(raw.get("count"), 1, 1, 4),
            timeout=num(raw.get("timeout"), TIMEOUT, 10, 600),
        )


def load_settings(path: Path | str) -> ImageGenSettings:
    try:
        return ImageGenSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ImageGenSettings()


def save_settings(path: Path | str, settings: ImageGenSettings) -> bool:
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


# ---------------------------------------------------------------------------
# 请求体 / 回应
# ---------------------------------------------------------------------------


def build_body(prompt: str, settings: ImageGenSettings,
               size: str, adapter: dict[str, Any]) -> dict[str, Any]:
    """按白名单拼请求体（没列的字段一个都不发）。"""
    size_field = str((adapter.get("size") or {}).get("field") or "size")
    values: dict[str, Any] = {
        "model": settings.model,
        "prompt": prompt,
        "size": size,
        "n": max(1, int(settings.count)),
        "quality": settings.quality,
        "response_format": adapter.get("response_format") or "",
    }
    quality_map = adapter.get("quality") or {}
    canonical: dict[str, Any] = {}
    for key in FIELDS:
        value = values.get(key)
        if value in ("", None):
            continue
        if key == "size":
            canonical[size_field] = size
        elif key == "quality" and quality_map:
            canonical[key] = quality_map.get(str(value), value)
        else:
            canonical[key] = value

    compose = adapter.get("compose")
    if isinstance(compose, dict):
        # 嵌套型（如通义万相 {input:{prompt}, parameters:{size}}）：
        # 规范值直接折叠进去，不走白名单
        body: dict[str, Any] = {"model": canonical.get("model", settings.model)}
        for group, keys in compose.items():
            chunk = {key: canonical[key] for key in keys if key in canonical}
            if chunk:
                body[group] = chunk
        return body

    allowed = adapter.get("send") or FIELDS
    body = {}
    for key in allowed:
        if key == size_field:
            body[key] = size
            continue
        if key in canonical:
            body[key] = canonical[key]
    return body


def extract_images(payload: Any) -> list[bytes]:
    """从任意形状的回应里挖图片：b64 / data-url / 外链 / 二进制。"""
    out: list[bytes] = []
    if isinstance(payload, (bytes, bytearray)):
        return [bytes(payload)]
    if not isinstance(payload, (dict, list)):
        return out
    items = payload if isinstance(payload, list) else (
        payload.get("data") or payload.get("images")
        or (payload.get("output") or {}).get("results") or []
    )
    if not isinstance(items, list):
        items = [items]
    for item in items:
        if isinstance(item, (bytes, bytearray)):
            out.append(bytes(item))
            continue
        if isinstance(item, str):
            if item.startswith("data:"):
                _, _, blob = item.partition(",")
                try:
                    out.append(base64.b64decode(blob))
                except ValueError:
                    pass
            continue
        if not isinstance(item, dict):
            continue
        for key in ("b64_json", "b64", "base64", "image_base64"):
            blob = item.get(key)
            if isinstance(blob, str) and blob:
                try:
                    out.append(base64.b64decode(blob))
                except ValueError:
                    pass
                break
        else:
            for key in ("url", "image_url"):
                link = str(item.get(key) or "").strip()
                if link.startswith("data:"):
                    _, _, blob = link.partition(",")
                    try:
                        out.append(base64.b64decode(blob))
                    except ValueError:
                        pass
                    break
                if link.startswith("http"):
                    out.append(link.encode("utf-8"))
                    break
    return out


def _dig(payload: Any, path: str) -> Any:
    node = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
    return node


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _endpoint(base_url: str, path: str) -> str:
    root = str(base_url or "").rstrip("/")
    if root.endswith("/v1"):
        return root + path
    return root + "/v1" + path


def _request(method: str, url: str, *, headers: dict[str, str],
             body: bytes, timeout: int) -> tuple[int, bytes, dict[str, str]]:
    """发一个请求，返回 (状态码, 响应体, 头)。测试里会把它换掉。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b"", dict(exc.headers or {})
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc).encode("utf-8", "replace"), {}


def _json_headers(settings: ImageGenSettings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + settings.api_key,
    }


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, bytes]]
                   ) -> tuple[bytes, str]:
    """手写 multipart（参考图编辑要用），返回 (body, content-type)。"""
    boundary = "----astrbot-mind-%d" % int(time.time() * 1000)
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, key, value)).encode("utf-8")
        )
    for field, filename, payload in files:
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n"
             % (boundary, field, filename)).encode("utf-8")
        )
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _error_text(raw: bytes) -> str:
    payload = _parse_json(raw)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "")[:200]
        if error:
            return str(error)[:200]
        for key in ("message", "msg", "error_msg"):
            if payload.get(key):
                return str(payload[key])[:200]
    return raw.decode("utf-8", "replace")[:200].strip()


# ---------------------------------------------------------------------------
# 生成 / 编辑
# ---------------------------------------------------------------------------


def _save(blob: bytes, out_dir: Path, index: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = ".png" if blob[:4] in (b"\x89PNG",) else (
        ".jpg" if blob[:2] == b"\xff\xd8" else ".png"
    )
    path = out_dir / ("%s-%02d%s" % (stamp, index, suffix))
    path.write_bytes(blob)
    return path


def generate(prompt: str, settings: ImageGenSettings, out_dir: Path | str,
             size: str = "") -> dict[str, Any]:
    """文生图。返回 ok / images(路径) / note / error。"""
    text = str(prompt or "").strip()
    if not text:
        return {"ok": False, "error": "先说画什么", "images": []}
    if not settings.ready():
        return {"ok": False, "error": "生图还没配好（base_url / api_key / model）", "images": []}
    adapter = adapter_of(pick_adapter(settings.base_url, settings.model, settings.adapter))
    requested = parse_size(size or settings.size)
    adapted, note = adapt_size(requested, adapter)
    body = build_body(text, settings, adapted, adapter)
    url = _endpoint(settings.base_url, str(adapter.get("path") or "/images/generations"))
    timeout = max(10, int(settings.timeout or TIMEOUT))

    for attempt in range(2):
        status, raw, _headers = _request(
            "POST", url, headers=_json_headers(settings),
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=timeout,
        )
        if status in (200, 201):
            break
        if status == 0:
            return {"ok": False, "error": "连不上：" + raw.decode("utf-8", "replace")[:200],
                    "images": []}
        message = _error_text(raw)
        if attempt == 0 and status in (429, 500, 502, 503, 504):
            time.sleep(1.0)
            continue
        if attempt == 0 and status in (400, 422):
            # 上游说"不认识某参数"：摘掉再试一次
            dropped = _drop_unknown_param(body, message)
            if dropped is not None:
                body = dropped
                continue
        return {"ok": False, "error": "生图失败（HTTP %d）：%s" % (status, message),
                "images": []}

    blobs = extract_images(_parse_json(raw))
    if not blobs:
        return {"ok": False, "error": "接口回来了但没有图片", "images": []}
    folder = Path(out_dir)
    paths: list[str] = []
    for index, blob in enumerate(blobs, 1):
        if blob[:4] == b"http":
            continue          # 外链稍后统一下载
        paths.append(str(_save(blob, folder, index)))
    for index, blob in enumerate([b for b in blobs if b[:4] == b"http"], 1):
        link = blob.decode("utf-8", "replace")
        status, raw, _headers = _request("GET", link, headers={}, body=b"", timeout=timeout)
        if status == 200 and raw:
            paths.append(str(_save(raw, folder, 100 + index)))
    if not paths:
        return {"ok": False, "error": "图片存不下来（外链下载失败）", "images": []}
    return {"ok": True, "images": paths, "note": note, "error": "",
            "size": adapted, "adapter": pick_adapter(settings.base_url, settings.model,
                                                     settings.adapter)}


def _drop_unknown_param(body: dict[str, Any], message: str) -> dict[str, Any] | None:
    """错误信息里提到哪个参数，就摘掉哪个（只摘规范字段）。

    先按完整字段名找（长的优先），避免 "n" 命中 "unknown" 里的那个 n。
    """
    lowered = str(message or "").lower()
    candidates = [key for key in body if key in FIELDS or key in ("image_size", "batch_size")]
    for key in sorted(candidates, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9_])" + re.escape(key.lower()) + r"(?![a-z0-9_])", lowered):
            updated = dict(body)
            updated.pop(key, None)
            return updated
    aliases = {
        "size": ("尺寸", "image_size"),
        "quality": ("质量",),
        "response_format": ("response format",),
        "n": ("count", "数量"),
        "model": ("模型",),
    }
    for key in sorted(candidates, key=len, reverse=True):
        if any(alias in lowered for alias in aliases.get(key, ())):
            updated = dict(body)
            updated.pop(key, None)
            return updated
    return None


def edit(prompt: str, settings: ImageGenSettings, out_dir: Path | str,
         refs: Iterable[str | Path] = (), size: str = "") -> dict[str, Any]:
    """参考图编辑（multipart）。只保留原图没说的部分。"""
    text = str(prompt or "").strip()
    if not text:
        return {"ok": False, "error": "先说怎么改", "images": []}
    if not settings.ready():
        return {"ok": False, "error": "生图还没配好", "images": []}
    files: list[tuple[str, str, bytes]] = []
    for path in refs or ():
        item = Path(str(path))
        if not item.is_file():
            return {"ok": False, "error": "参考图不存在：" + str(item), "images": []}
        try:
            files.append(("image", item.name, item.read_bytes()))
        except OSError as exc:
            return {"ok": False, "error": "参考图读不出来：" + str(exc), "images": []}
    if not files:
        return {"ok": False, "error": "要改图至少给一张参考图", "images": []}

    adapter = adapter_of(pick_adapter(settings.base_url, settings.model, settings.adapter))
    requested = parse_size(size or settings.size)
    adapted, note = adapt_size(requested, adapter)
    fields = {"model": settings.model, "prompt": text, "n": str(max(1, settings.count))}
    if adapter.get("size", {}).get("field", "size") == "size":
        fields["size"] = adapted
    body, content_type = multipart_body(fields, files)
    url = _endpoint(settings.base_url, "/images/edits")
    headers = {"Content-Type": content_type, "Accept": "application/json",
               "Authorization": "Bearer " + settings.api_key}
    status, raw, _hdrs = _request("POST", url, headers=headers, body=body,
                                  timeout=max(30, int(settings.timeout or TIMEOUT)))
    if status not in (200, 201):
        return {"ok": False, "error": "改图失败（HTTP %d）：%s" % (status, _error_text(raw)),
                "images": []}
    blobs = extract_images(_parse_json(raw))
    if not blobs:
        return {"ok": False, "error": "接口回来了但没有图片", "images": []}
    folder = Path(out_dir)
    paths = [str(_save(blob, folder, index))
             for index, blob in enumerate([b for b in blobs if b[:4] != b"http"], 1)]
    if not paths:
        return {"ok": False, "error": "改完的图存不下来", "images": []}
    return {"ok": True, "images": paths, "note": note, "error": ""}


def report(result: dict[str, Any]) -> str:
    """聊天里那句字。"""
    if not result.get("ok"):
        text = "画不出来：" + str(result.get("error") or "未知原因")
        return text
    bits = ["画好了（%d 张）" % len(result.get("images") or [])]
    if result.get("size"):
        bits.append("尺寸 " + str(result["size"]))
    if result.get("adapter"):
        bits.append("适配器 " + str(result["adapter"]))
    note = str(result.get("note") or "").strip()
    if note:
        bits.append(note)
    return "｜".join(bits)


__all__ = [
    "TIMEOUT", "FIELDS", "ADAPTERS", "DOMAIN_ADAPTERS", "MODEL_ADAPTERS",
    "ImageGenSettings", "load_settings", "save_settings",
    "pick_adapter", "adapter_of", "parse_size", "adapt_size", "build_body",
    "extract_images", "multipart_body", "generate", "edit", "report",
]

