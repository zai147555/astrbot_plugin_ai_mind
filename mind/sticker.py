
"""表情包库：收 → 打标 → 按情绪挑一张发出去。

设计参考 MaiBot 的 src/emoji_system（GPL-3.0）——**只借鉴概念，代码独立实现**：
它是「LLM 描述 + 情绪标签 + 按情绪/上下文采样发送 + 缓存清理」，
我们这里做到同样的事，但不引 rapidfuzz / sqlmodel / rich，标签也可以手工补。

和「关键词配图」的分工：
  - 关键词配图 = 你放好的图，命中触发词才发（可控，像图库）；
  - 表情包 = **从聊天里自己收**的图/表情，按她此刻的情绪挑一张（像人的表情）。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .dbconn import ConnectionSource

#: 收图上限（单张），超过就丢
MAX_BYTES = 4 * 1024 * 1024
#: 一个会话每小时最多发几张
DEFAULT_PER_HOUR = 3
#: 允许的后缀
SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    hash      TEXT NOT NULL UNIQUE,
    path      TEXT NOT NULL DEFAULT '',
    desc      TEXT NOT NULL DEFAULT '',
    emotions  TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT 'auto',
    hits      INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL DEFAULT 0,
    last_used REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sticker_created ON stickers(created DESC);
"""


@dataclass
class StickerSettings:
    enabled: bool = False        # 默认关：先收一会儿，确认无害再开
    collect: bool = True
    #: 发一张的概率（0~1）
    chance: float = 0.25
    per_hour: int = DEFAULT_PER_HOUR
    max_bytes: int = MAX_BYTES
    #: 只在这些会话里收（空 = 全部）
    sessions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "collect": bool(self.collect),
            "chance": round(float(self.chance), 3),
            "per_hour": int(self.per_hour),
            "max_bytes": int(self.max_bytes),
            "sessions": list(self.sessions),
        }

    @staticmethod
    def from_dict(data: Any) -> "StickerSettings":
        raw = data if isinstance(data, dict) else {}
        try:
            chance = float(raw.get("chance", 0.25))
        except (TypeError, ValueError):
            chance = 0.25
        try:
            per_hour = int(raw.get("per_hour", DEFAULT_PER_HOUR))
        except (TypeError, ValueError):
            per_hour = DEFAULT_PER_HOUR
        try:
            max_bytes = int(raw.get("max_bytes", MAX_BYTES))
        except (TypeError, ValueError):
            max_bytes = MAX_BYTES
        sessions = raw.get("sessions")
        return StickerSettings(
            enabled=bool(raw.get("enabled", False)),
            collect=bool(raw.get("collect", True)),
            chance=max(0.0, min(1.0, chance)),
            per_hour=max(0, min(20, per_hour)),
            max_bytes=max(64 * 1024, min(16 * 1024 * 1024, max_bytes)),
            sessions=[str(item) for item in (sessions if isinstance(sessions, list) else [])],
        )


def load_settings(path: Path | str) -> StickerSettings:
    try:
        return StickerSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return StickerSettings()


def save_settings(path: Path | str, settings: StickerSettings) -> bool:
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
# 打标
# ---------------------------------------------------------------------------

#: 从文件名/描述里认情绪（够用就行，识别不到就留空，靠手工补）
EMOTION_WORDS: dict[str, tuple[str, ...]] = {
    "joy": ("笑", "开心", "哈哈", "laugh", "smile", "happy", "dd"),
    "angry": ("怒", "凶", "气", "angry", "mad", "拳头"),
    "sad": ("哭", "难过", "泪", "cry", "sad"),
    "shy": ("害羞", "脸红", "shy", "blush"),
    "surprised": ("惊", "震惊", "wow", "surprise"),
    "bored": ("无语", "汗", "...", "boring", "sweat"),
    "tired": ("困", "睡", "累", "sleepy", "tired"),
    "affection": ("贴贴", "抱", "心心", "hug", "love", "heart"),
    "confused": ("问号", "疑惑", "?", "confused", "what"),
    "proud": ("得意", "哼", "smug", "proud"),
}


def guess_emotions(*texts: str) -> list[str]:
    """从文件名/描述里猜情绪标签（可能为空）。"""
    blob = " ".join(str(item or "") for item in texts).lower()
    hits: list[str] = []
    for key, words in EMOTION_WORDS.items():
        if any(word.lower() in blob for word in words):
            hits.append(key)
    return hits


def parse_emotions(raw: str) -> list[str]:
    return [item for item in re.split(r"[,，\s]+", str(raw or "")) if item]


def join_emotions(items: Iterable[str]) -> str:
    return ",".join(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


@dataclass
class Sticker:
    id: int = 0
    hash: str = ""
    path: str = ""
    desc: str = ""
    emotions: tuple[str, ...] = ()
    source: str = "auto"
    hits: int = 0
    created: float = 0.0
    last_used: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "hash": self.hash, "path": self.path, "desc": self.desc,
            "emotions": list(self.emotions), "source": self.source, "hits": self.hits,
            "created": self.created, "last_used": self.last_used,
        }


class StickerStore(ConnectionSource):
    _schema = _SCHEMA

    def __init__(self, connection: Any = None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        self.ensure_schema()

    # -- 写 ---------------------------------------------------------------
    def add(self, blob: bytes, folder: Path | str, name: str = "",
            emotions: Iterable[str] = (), desc: str = "",
            source: str = "auto", max_bytes: int = MAX_BYTES) -> Sticker | None:
        """存一张表情，按内容哈希去重。"""
        conn = self.conn
        if conn is None or not blob:
            return None
        if len(blob) > max(64 * 1024, int(max_bytes)):
            return None
        digest = hashlib.sha1(blob).hexdigest()
        try:
            row = conn.execute("SELECT id FROM stickers WHERE hash = ?", (digest,)).fetchone()
        except sqlite3.Error:
            return None
        if row:
            return self.get(int(row[0]))

        suffix = Path(str(name or "")).suffix.lower()
        if suffix not in SUFFIXES:
            suffix = ".png"
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        path = target / (digest[:16] + suffix)
        try:
            path.write_bytes(blob)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO stickers (hash, path, desc, emotions, source,"
                " created) VALUES (?,?,?,?,?,?)",
                (digest, str(path), str(desc or "")[:200],
                 join_emotions(emotions), str(source or "auto"), time.time()),
            )
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 存表情失败：{exc}")
            return None
        return self.get(int(cursor.lastrowid or 0))

    def tag(self, sticker_id: int, *, desc: str | None = None,
            emotions: Iterable[str] | None = None) -> bool:
        conn = self.conn
        if conn is None:
            return False
        fields: list[str] = []
        values: list[Any] = []
        if desc is not None:
            fields.append("desc = ?")
            values.append(str(desc)[:200])
        if emotions is not None:
            fields.append("emotions = ?")
            values.append(join_emotions(emotions))
        if not fields:
            return False
        values.append(int(sticker_id))
        try:
            conn.execute("UPDATE stickers SET " + ", ".join(fields) + " WHERE id = ?", values)
            conn.commit()
            return True
        except sqlite3.Error:
            return False

    def delete(self, sticker_id: int) -> bool:
        conn = self.conn
        if conn is None:
            return False
        item = self.get(int(sticker_id))
        try:
            conn.execute("DELETE FROM stickers WHERE id = ?", (int(sticker_id),))
            conn.commit()
        except sqlite3.Error:
            return False
        if item is not None and item.path:
            try:
                Path(item.path).unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def mark_used(self, sticker_id: int) -> None:
        conn = self.conn
        if conn is None:
            return
        try:
            conn.execute(
                "UPDATE stickers SET hits = hits + 1, last_used = ? WHERE id = ?",
                (time.time(), int(sticker_id)),
            )
            conn.commit()
        except sqlite3.Error:
            pass

    def clear(self) -> int:
        conn = self.conn
        if conn is None:
            return 0
        rows = self.list(limit=10000)
        try:
            cursor = conn.execute("DELETE FROM stickers")
            conn.commit()
        except sqlite3.Error:
            return 0
        for item in rows:
            try:
                Path(item.path).unlink(missing_ok=True)
            except OSError:
                pass
        return int(cursor.rowcount or 0)

    # -- 读 ---------------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row) -> Sticker:
        return Sticker(
            id=int(row["id"]), hash=str(row["hash"]), path=str(row["path"]),
            desc=str(row["desc"]), emotions=tuple(parse_emotions(row["emotions"])),
            source=str(row["source"]), hits=int(row["hits"]),
            created=float(row["created"] or 0), last_used=float(row["last_used"] or 0),
        )

    def get(self, sticker_id: int) -> Sticker | None:
        conn = self.conn
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM stickers WHERE id = ?", (int(sticker_id),)).fetchone()
        except sqlite3.Error:
            return None
        return self._row(row) if row else None

    def list(self, limit: int = 100) -> list[Sticker]:
        conn = self.conn
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM stickers ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        except sqlite3.Error:
            return []
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.conn
        if conn is None:
            return {"total": 0, "tagged": 0, "used": 0}
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN emotions <> '' THEN 1 ELSE 0 END) AS tagged,"
                " SUM(CASE WHEN hits > 0 THEN 1 ELSE 0 END) AS used FROM stickers"
            ).fetchone()
        except sqlite3.Error:
            return {"total": 0, "tagged": 0, "used": 0}
        return {
            "total": int(row["total"] or 0) if row else 0,
            "tagged": int(row["tagged"] or 0) if row else 0,
            "used": int(row["used"] or 0) if row else 0,
        }

    # -- 选 ---------------------------------------------------------------
    def pick(self, emotion: str = "", avoid: Iterable[int] = (),
             rng: random.Random | None = None) -> Sticker | None:
        """按情绪挑一张：优先情绪命中，其次最近没用过的。"""
        pool = self.list(limit=400)
        if not pool:
            return None
        banned = {int(item) for item in avoid or ()}
        pool = [item for item in pool if item.id not in banned] or pool
        wanted = str(emotion or "").strip().lower()
        if wanted:
            matched = [
                item for item in pool
                if wanted in {word.lower() for word in item.emotions}
            ]
            if matched:
                pool = matched
        pool.sort(key=lambda item: (item.hits, item.last_used))
        head = pool[: max(1, min(5, len(pool)))]
        chooser = rng or random
        return chooser.choice(head)


__all__ = [
    "MAX_BYTES", "DEFAULT_PER_HOUR", "SUFFIXES", "EMOTION_WORDS",
    "StickerSettings", "Sticker", "StickerStore",
    "load_settings", "save_settings", "guess_emotions", "parse_emotions", "join_emotions",
]

