"""关键词配图：说到某些话，直接甩一张图。

设计取舍
--------
- **图片文件放 data 目录，元数据放 SQLite**。面板预览走 base64，
  这样不用往插件目录里写东西（插件更新/重装时不丢数据）。
- **匹配分三档**：精确、包含、模糊。
  "包含"能覆盖大部分场景（关键词在消息里出现即可）；
  "模糊"针对错别字和语序变化，用**字符集合的 Jaccard**而不是二元组 ——
  中文短词用二元组太苛刻，"安晚"和"晚安"会一个二元组都对不上，
  而字符集合能拿到满分。
- **冷却时间**：同一个会话里同一个关键词短时间内只发一次，
  否则一句"晚安"刷十遍就变成刷屏机器了。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .dbconn import ConnectionSource
from .memory.model import normalize

#: 匹配模式
MODE_EXACT = "exact"
MODE_CONTAINS = "contains"
MODE_FUZZY = "fuzzy"
MODES: tuple[str, ...] = (MODE_EXACT, MODE_CONTAINS, MODE_FUZZY)

MODE_LABELS: dict[str, str] = {
    MODE_EXACT: "完全等于",
    MODE_CONTAINS: "包含",
    MODE_FUZZY: "模糊相似",
}

#: 作用域
SCOPE_GLOBAL = "global"
SCOPE_USER = "user"

ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stored_name   TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    mime          TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS image_triggers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword    TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'contains',
    image_id   INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    scope      TEXT NOT NULL DEFAULT 'global',
    owner_id   TEXT NOT NULL DEFAULT '',
    priority   INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trigger_enabled ON image_triggers(enabled);
"""


#: 模型用它来"点图"：<pic>晚安</pic> 或 [pic:晚安]
PIC_TAG_RE = re.compile(
    r"<\s*pic\s*>(?P<a>.*?)<\s*/\s*pic\s*>"
    r"|\[\s*pic\s*[:：]\s*(?P<b>.*?)\s*\]",
    re.IGNORECASE | re.DOTALL,
)

#: 注入给模型的图片清单标记
IMAGES_MARKER = "<available_images>"

IMAGE_PROMPT_TEMPLATE = """{marker}
你手上有一批图，可以在回复里用标记把它们发出去：

<pic>关键词</pic>

规则：
- 标记会被系统替换成真正的图片，用户看不到标记本身，所以别解释它。
- 用在你想表达情绪、但打字不够的时候：被逗笑、想撒娇、生气、道晚安、想他了。
- 一条回复里最多发 {max_per_message} 张，发多了很吵。
- 只能从下面这些关键词里选，不要自己编：
  {keywords}
</available_images>"""


def has_pic_tag(text: str) -> bool:
    return bool(text) and PIC_TAG_RE.search(text) is not None


def extract_pic_tags(text: str) -> list[str]:
    """取出文本里所有想发的图（关键词列表）。"""
    if not text:
        return []
    found: list[str] = []
    for match in PIC_TAG_RE.finditer(text):
        token = (match.group("a") or match.group("b") or "").strip()
        if token:
            found.append(token)
    return found


def strip_pic_tags(text: str) -> str:
    """把标记摘干净，只留文字。"""
    if not text:
        return ""
    cleaned = PIC_TAG_RE.sub("", text)
    return cleaned.strip()


def render_available_images(
    triggers: "list[ImageTrigger]",
    *,
    max_per_message: int = 2,
    limit: int = 40,
) -> str:
    """告诉模型它有哪些图可以发。

    不注入这段，模型根本不知道"点图"这件事存在，也不会知道有哪些关键词可用 ——
    这是整个功能的开关。
    """
    labels: list[str] = []
    for trigger in triggers:
        if not trigger.enabled or not trigger.keyword:
            continue
        label = trigger.keyword
        if trigger.note and trigger.note not in label:
            label = label + "（" + trigger.note + "）"
        if label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    if not labels:
        return ""
    return IMAGE_PROMPT_TEMPLATE.format(
        marker=IMAGES_MARKER,
        max_per_message=max(1, int(max_per_message)),
        keywords=" / ".join(labels),
    )


@dataclass
class ImageAsset:
    id: int = 0
    stored_name: str = ""
    original_name: str = ""
    mime: str = ""
    size: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stored_name": self.stored_name,
            "original_name": self.original_name,
            "mime": self.mime,
            "size": self.size,
            "size_text": human_size(self.size),
            "created_at": self.created_at,
        }


@dataclass
class ImageTrigger:
    id: int = 0
    keyword: str = ""
    mode: str = MODE_CONTAINS
    image_id: int = 0
    enabled: bool = True
    scope: str = SCOPE_GLOBAL
    owner_id: str = ""
    priority: int = 0
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "keyword": self.keyword,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "image_id": self.image_id,
            "enabled": self.enabled,
            "scope": self.scope,
            "owner_id": self.owner_id,
            "priority": self.priority,
            "note": self.note,
        }


def _coerce_ids(values: Any) -> list[int]:
    """把任意输入转成干净的 id 列表：非数字丢掉，0 丢掉，顺序去重。"""
    if values is None:
        return []
    if isinstance(values, (str, int)):
        values = [values]
    ids: list[int] = []
    for item in values:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


def human_size(size: int) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def guess_extension(mime: str, filename: str = "") -> str:
    if mime in ALLOWED_MIME:
        return ALLOWED_MIME[mime]
    lowered = (filename or "").lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        if lowered.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""


def char_jaccard(left: str, right: str) -> float:
    """按**字符集合**算 Jaccard，而不是二元组。

    中文短词用二元组太苛刻："安晚" 和 "晚安" 一个二元组都不共享，
    但字符集合完全一致。这里要的正是"差不多是这句话"。
    """
    a = {ch for ch in normalize(left) if not ch.isspace()}
    b = {ch for ch in normalize(right) if not ch.isspace()}
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def match_score(text: str, keyword: str, mode: str) -> float:
    """一条触发器对一条消息的匹配分，0 表示不匹配。"""
    if not text or not keyword:
        return 0.0
    normalized_text = normalize(text)
    normalized_keyword = normalize(keyword)
    if not normalized_text or not normalized_keyword:
        return 0.0
    if mode == MODE_EXACT:
        return 1.0 if normalized_text == normalized_keyword else 0.0
    if mode == MODE_CONTAINS:
        return 1.0 if normalized_keyword in normalized_text else 0.0
    if mode == MODE_FUZZY:
        return char_jaccard(normalized_text, normalized_keyword)
    return 0.0


def row_to_asset(row: sqlite3.Row) -> ImageAsset:
    return ImageAsset(
        id=int(row["id"]),
        stored_name=str(row["stored_name"] or ""),
        original_name=str(row["original_name"] or ""),
        mime=str(row["mime"] or ""),
        size=int(row["size"] or 0),
        created_at=float(row["created_at"] or 0.0),
    )


def row_to_trigger(row: sqlite3.Row) -> ImageTrigger:
    return ImageTrigger(
        id=int(row["id"]),
        keyword=str(row["keyword"] or ""),
        mode=str(row["mode"] or MODE_CONTAINS),
        image_id=int(row["image_id"] or 0),
        enabled=bool(row["enabled"]),
        scope=str(row["scope"] or SCOPE_GLOBAL),
        owner_id=str(row["owner_id"] or ""),
        priority=int(row["priority"] or 0),
        note=str(row["note"] or ""),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
    )


class ImageStore(ConnectionSource):
    """图片 + 触发词的仓库。所有异常降级为空结果，不让插件崩掉。"""

    def __init__(self, connection: sqlite3.Connection | None, root: Path, logger: Any = None) -> None:
        self.conn = connection
        self.root = Path(root)
        self.logger = logger
        if self.conn is not None:
            try:
                self.conn.executescript(_SCHEMA)
                self.conn.commit()
            except sqlite3.Error as exc:
                if self.logger is not None:
                    self.logger.warning(f"[ai_mind] 配图表初始化失败：{exc}")
                self.conn = None

    # -- 基础 ---------------------------------------------------------------
    def _run(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor | None:
        if self.conn is None:
            return None
        try:
            cursor = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cursor
        except (sqlite3.Error, OSError) as exc:
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 配图 SQL 失败：{exc}")
            return None

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cursor = self._run(sql, params)
        if cursor is None:
            return []
        try:
            return list(cursor.fetchall())
        except sqlite3.Error:
            return []

    # -- 图片 ---------------------------------------------------------------
    def image_dir(self) -> Path:
        path = self.root / "images"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return path

    def path_of(self, stored_name: str) -> Path:
        return self.image_dir() / stored_name

    def add_image(self, *, stored_name: str, original_name: str, mime: str, size: int) -> int:
        cursor = self._run(
            "INSERT INTO images (stored_name, original_name, mime, size, created_at) VALUES (?,?,?,?,?)",
            (stored_name, original_name, mime, int(size), time.time()),
        )
        return int(cursor.lastrowid) if cursor is not None and cursor.lastrowid else 0

    def get_image(self, image_id: int) -> ImageAsset | None:
        rows = self._all("SELECT * FROM images WHERE id = ?", (int(image_id),))
        return row_to_asset(rows[0]) if rows else None

    def list_images(self) -> list[ImageAsset]:
        return [row_to_asset(r) for r in self._all("SELECT * FROM images ORDER BY id DESC")]

    def delete_image(self, image_id: int) -> bool:
        asset = self.get_image(image_id)
        if asset is None:
            return False
        self._run("DELETE FROM image_triggers WHERE image_id = ?", (int(image_id),))
        cursor = self._run("DELETE FROM images WHERE id = ?", (int(image_id),))
        try:
            self.path_of(asset.stored_name).unlink(missing_ok=True)
        except OSError:
            pass
        return cursor is not None and cursor.rowcount > 0

    # -- 触发词 -------------------------------------------------------------
    def add_trigger(
        self,
        *,
        keyword: str,
        image_id: int,
        mode: str = MODE_CONTAINS,
        scope: str = SCOPE_GLOBAL,
        owner_id: str = "",
        priority: int = 0,
        note: str = "",
    ) -> int:
        now = time.time()
        cursor = self._run(
            """
            INSERT INTO image_triggers
                (keyword, mode, image_id, enabled, scope, owner_id, priority, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (keyword, mode, int(image_id), 1, scope, owner_id, int(priority), note, now, now),
        )
        return int(cursor.lastrowid) if cursor is not None and cursor.lastrowid else 0

    def enabled_triggers(self, sender_id: str = "") -> list[ImageTrigger]:
        """当前这个人能触发的图（面板注入给模型看的那份清单）。"""
        result: list[ImageTrigger] = []
        for trigger in self.list_triggers():
            if not trigger.enabled or not trigger.keyword:
                continue
            if trigger.scope == SCOPE_USER:
                if not trigger.owner_id or str(trigger.owner_id) != str(sender_id or ""):
                    continue
            result.append(trigger)
        return result

    def resolve(
        self,
        keyword: str,
        *,
        sender_id: str = "",
        fuzzy_threshold: float = 0.6,
    ) -> ImageTrigger | None:
        """模型说"我要发 XX 那张图"时，把 XX 解析成具体的一条触发器。

        顺序：先精确对关键词，再模糊对关键词，最后模糊对备注 ——
        模型经常会把关键词记成近义的说法。
        """
        token = normalize(keyword or "")
        if not token:
            return None
        triggers = self.enabled_triggers(sender_id)
        if not triggers:
            return None
        for trigger in triggers:
            if normalize(trigger.keyword) == token:
                return trigger

        best: ImageTrigger | None = None
        best_score = 0.0
        for trigger in triggers:
            score = char_jaccard(token, trigger.keyword)
            if score > best_score:
                best, best_score = trigger, score
        if best is not None and best_score >= fuzzy_threshold:
            return best

        best, best_score = None, 0.0
        for trigger in triggers:
            if not trigger.note:
                continue
            score = char_jaccard(token, trigger.note)
            if score > best_score:
                best, best_score = trigger, score
        if best is not None and best_score >= max(fuzzy_threshold, 0.5):
            return best
        return None

    def list_triggers(self) -> list[ImageTrigger]:
        return [
            row_to_trigger(r)
            for r in self._all(
                "SELECT * FROM image_triggers ORDER BY priority DESC, LENGTH(keyword) DESC, id DESC"
            )
        ]

    def update_trigger(self, trigger_id: int, **fields: Any) -> bool:
        allowed = {"keyword", "mode", "enabled", "scope", "owner_id", "priority", "note"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "enabled":
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(time.time())
        params.append(int(trigger_id))
        cursor = self._run(f"UPDATE image_triggers SET {', '.join(sets)} WHERE id = ?", params)
        return cursor is not None and cursor.rowcount > 0

    def delete_trigger(self, trigger_id: int) -> bool:
        cursor = self._run("DELETE FROM image_triggers WHERE id = ?", (int(trigger_id),))
        return cursor is not None and cursor.rowcount > 0

    def delete_many(self, image_ids: Iterable[int]) -> int:
        """批量删图（连带触发词和磁盘上的文件），返回删掉几张。"""
        removed = 0
        for image_id in _coerce_ids(image_ids):
            if self.delete_image(image_id):
                removed += 1
        return removed

    def update_triggers_of_images(self, image_ids: Iterable[int], **fields: Any) -> int:
        """批量改这些图下面所有触发词的字段，返回改动的触发词条数。"""
        allowed = {"enabled", "scope", "owner_id", "mode", "priority"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "enabled":
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
        ids = _coerce_ids(image_ids)
        if not sets or not ids:
            return 0
        sets.append("updated_at = ?")
        params.append(time.time())
        placeholders = ",".join("?" for _ in ids)
        cursor = self._run(
            f"UPDATE image_triggers SET {', '.join(sets)} WHERE image_id IN ({placeholders})",
            [*params, *ids],
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def stats(self) -> dict[str, int]:
        rows = self._all(
            """
            SELECT (SELECT COUNT(*) FROM images) AS images,
                   (SELECT COUNT(*) FROM image_triggers) AS triggers,
                   (SELECT COUNT(*) FROM image_triggers WHERE enabled = 1) AS active
            """
        )
        if not rows:
            return {"images": 0, "triggers": 0, "active": 0}
        row = rows[0]
        return {
            "images": int(row["images"] or 0),
            "triggers": int(row["triggers"] or 0),
            "active": int(row["active"] or 0),
        }

    # -- 匹配 ---------------------------------------------------------------
    def match(
        self,
        text: str,
        *,
        sender_id: str = "",
        fuzzy_threshold: float = 0.6,
    ) -> ImageTrigger | None:
        """找出最该触发的一条。分数相同则更长的关键词优先（更具体）。"""
        if not text:
            return None
        best: ImageTrigger | None = None
        best_score = 0.0
        for trigger in self.list_triggers():
            if not trigger.enabled or not trigger.keyword:
                continue
            if trigger.scope == SCOPE_USER:
                if not trigger.owner_id or str(trigger.owner_id) != str(sender_id or ""):
                    continue
            score = match_score(text, trigger.keyword, trigger.mode)
            if trigger.mode == MODE_FUZZY and score < fuzzy_threshold:
                continue
            if score <= 0:
                continue
            if score > best_score or (
                score == best_score and best is not None and len(trigger.keyword) > len(best.keyword)
            ):
                best, best_score = trigger, score
        return best


class Cooldown:
    """同一会话 + 同一触发器，短时间内只触发一次。"""

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = max(0.0, float(seconds))
        self._last: dict[tuple[str, int], float] = {}

    def allow(self, session: str, trigger_id: int, now: float | None = None) -> bool:
        if self.seconds <= 0:
            return True
        now = time.time() if now is None else now
        key = (str(session), int(trigger_id))
        last = self._last.get(key, 0.0)
        if now - last < self.seconds:
            return False
        self._last[key] = now
        if len(self._last) > 2000:
            cutoff = now - self.seconds
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        return True

    def reset(self) -> None:
        self._last.clear()


__all__ = [
    "ALLOWED_MIME",
    "IMAGES_MARKER",
    "IMAGE_PROMPT_TEMPLATE",
    "PIC_TAG_RE",
    "MODE_CONTAINS",
    "MODE_EXACT",
    "MODE_FUZZY",
    "MODE_LABELS",
    "MODES",
    "SCOPE_GLOBAL",
    "SCOPE_USER",
    "Cooldown",
    "ImageAsset",
    "ImageStore",
    "ImageTrigger",
    "char_jaccard",
    "extract_pic_tags",
    "guess_extension",
    "has_pic_tag",
    "human_size",
    "match_score",
    "render_available_images",
    "strip_pic_tags",
]

