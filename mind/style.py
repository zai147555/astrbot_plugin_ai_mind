"""表达示例：学她以前是怎么说话的，再拿出来当参考。

参考 astrbot_plugin_self_learning 的思路（从真实 user -> bot 对话对里提取表达模式，
批准后作为 few-shot 注入），但做了两处针对本插件的调整：

1. **质量门槛直接复用去 AI 味的打分**。一段回复像不像人话，本来就已经算过了
   （mind/antiai.py），拿来当"这条值不值得学"的门槛，不用再写一套判断。
2. **学来的东西默认先待审**。自动学习最容易出的问题是"越学越跑偏"，
   所以默认进待审队列，你在面板上点批准才会被注入。

注入时**只给语气参考**：示例块里明确写「参考语气和句长，不要照抄内容」，
免得她变成一个复读机。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .dbconn import ConnectionSource

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)
STATUS_LABELS = {
    STATUS_PENDING: "待审",
    STATUS_APPROVED: "已批准",
    STATUS_REJECTED: "已拒绝",
}

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"

SCENE_GROUP = "group"
SCENE_PRIVATE = "private"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS style_examples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    scene      TEXT NOT NULL DEFAULT 'private',
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    keywords   TEXT NOT NULL DEFAULT '',
    grams      TEXT NOT NULL DEFAULT '',
    score      REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'pending',
    source     TEXT NOT NULL DEFAULT 'auto',
    note       TEXT NOT NULL DEFAULT '',
    hit_count  INTEGER NOT NULL DEFAULT 0,
    last_hit   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_style_status ON style_examples(status, score DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_style_fingerprint ON style_examples(grams, reply_text);
"""

#: 抽词时丢掉的常见虚词（两字组合里出现这些就不算关键词）
STOPWORDS = frozenset((
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那", "什么",
    "怎么", "为什么", "一个", "一下", "可以", "没有", "不是", "就是", "还是",
    "但是", "因为", "所以", "如果", "已经", "自己", "时候", "现在", "真的",
))

#: 检索时看几个字的滑窗
NGRAM = 2
MAX_GRAMS = 60


def ngrams(text: str, size: int = NGRAM, limit: int = MAX_GRAMS) -> list[str]:
    """中文没有空格，用二元组当检索单位最省事也够用。"""
    body = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(text or ""))
    if len(body) < size:
        return []
    grams: list[str] = []
    for index in range(len(body) - size + 1):
        gram = body[index : index + size]
        # 「我的」「你的」这种整串都是虚词的组合也要丢掉，
        # 只比单字的话二元组永远匹配不上，等于没过滤
        if gram in STOPWORDS or all(ch in STOPWORDS for ch in gram):
            continue
        grams.append(gram)
        if len(grams) >= limit:
            break
    return grams


def keywords_of(text: str, limit: int = 6) -> list[str]:
    """粗提关键词：按出现频率取前几个二元组。"""
    grams = ngrams(text, limit=400)
    if not grams:
        return []
    counts: dict[str, int] = {}
    for gram in grams:
        counts[gram] = counts.get(gram, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    picked: list[str] = []
    for gram, _count in ordered:
        if any(gram in other or other in gram for other in picked):
            continue
        picked.append(gram)
        if len(picked) >= limit:
            break
    return picked


def similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """两串二元组的重合度（Jaccard）。"""
    a, b = set(left or ()), set(right or ())
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def is_followup(user_text: str) -> bool:
    """太短的附和、纯表情、纯指令不值得当示例。"""
    body = str(user_text or "").strip()
    if len(body) < 4:
        return True
    if body[:1] in {"/", "!", "！", "／", "#"}:
        return True
    return not ngrams(body, limit=1)


@dataclass
class StyleExample:
    """一条表达示例。"""

    user_text: str = ""
    reply_text: str = ""
    created_at: float = 0.0
    session_id: str = ""
    scene: str = SCENE_PRIVATE
    keywords: tuple[str, ...] = ()
    grams: tuple[str, ...] = ()
    score: float = 0.0
    status: str = STATUS_PENDING
    source: str = SOURCE_AUTO
    note: str = ""
    hit_count: int = 0
    last_hit: float = 0.0
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user_text,
            "reply": self.reply_text,
            "created_at": round(self.created_at, 2),
            "session": self.session_id,
            "scene": self.scene,
            "keywords": list(self.keywords),
            "score": round(self.score, 1),
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "source": self.source,
            "note": self.note,
            "hit_count": int(self.hit_count),
        }


def _row(row: sqlite3.Row) -> StyleExample:
    return StyleExample(
        id=int(row["id"]),
        created_at=float(row["created_at"] or 0.0),
        session_id=str(row["session_id"] or ""),
        scene=str(row["scene"] or SCENE_PRIVATE),
        user_text=str(row["user_text"] or ""),
        reply_text=str(row["reply_text"] or ""),
        keywords=tuple(k for k in str(row["keywords"] or "").split(chr(31)) if k),
        grams=tuple(g for g in str(row["grams"] or "").split(chr(31)) if g),
        score=float(row["score"] or 0.0),
        status=str(row["status"] or STATUS_PENDING),
        source=str(row["source"] or SOURCE_AUTO),
        note=str(row["note"] or ""),
        hit_count=int(row["hit_count"] or 0),
        last_hit=float(row["last_hit"] or 0.0),
    )


class StyleStore(ConnectionSource):
    """表达示例仓库。异常一律降级，不让插件崩。"""

    _schema = _SCHEMA

    def __init__(self, connection: sqlite3.Connection | None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        self.ensure_schema()

    # -- 写 ---------------------------------------------------------------
    def add(self, example: StyleExample) -> int:
        """加一条；已经有一模一样的（同样的问法 + 同样的回答）就返回 0。"""
        if self.conn is None:
            return 0
        stamp = float(example.created_at or time.time())
        row = (
            stamp,
            str(example.session_id or ""),
            str(example.scene or SCENE_PRIVATE),
            str(example.user_text or "")[:500],
            str(example.reply_text or "")[:1000],
            chr(31).join(list(example.keywords)[:12]),
            chr(31).join(list(example.grams)[:80]),
            float(example.score or 0.0),
            str(example.status or STATUS_PENDING),
            str(example.source or SOURCE_AUTO),
            str(example.note or "")[:200],
        )
        try:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO style_examples (created_at, session_id, scene,"
                " user_text, reply_text, keywords, grams, score, status, source, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0) if cursor.rowcount else 0
        except sqlite3.Error as exc:
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 记录表达示例失败：{exc}")
            return 0

    def set_status(self, ids: Iterable[int], status: str) -> int:
        if self.conn is None or status not in STATUSES:
            return 0
        target = [int(item) for item in ids if str(item).strip().lstrip("-").isdigit()]
        if not target:
            return 0
        marks = ",".join("?" for _ in target)
        try:
            cursor = self.conn.execute(
                f"UPDATE style_examples SET status = ? WHERE id IN ({marks})",
                [status, *target],
            )
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    def delete(self, ids: Iterable[int]) -> int:
        if self.conn is None:
            return 0
        target = [int(item) for item in ids if str(item).strip().lstrip("-").isdigit()]
        if not target:
            return 0
        marks = ",".join("?" for _ in target)
        try:
            cursor = self.conn.execute(
                f"DELETE FROM style_examples WHERE id IN ({marks})", target
            )
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    def clear_by_owner(self, uid: str) -> int:
        """删掉跟某个人有关的示例（会话标识里带他的 ID 就算）。"""
        if self.conn is None or not str(uid or "").strip():
            return 0
        try:
            cursor = self.conn.execute(
                "DELETE FROM style_examples WHERE session_id LIKE ?",
                ("%" + str(uid) + "%",),
            )
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    def clear(self, *, only_auto: bool = False, status: str = "") -> int:
        if self.conn is None:
            return 0
        where: list[str] = []
        params: list[Any] = []
        if only_auto:
            where.append("source = ?")
            params.append(SOURCE_AUTO)
        if status in STATUSES:
            where.append("status = ?")
            params.append(status)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        try:
            cursor = self.conn.execute(f"DELETE FROM style_examples {clause}", params)
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    def touch(self, ids: Iterable[int], now: float | None = None) -> None:
        if self.conn is None:
            return
        target = [int(item) for item in ids]
        if not target:
            return
        marks = ",".join("?" for _ in target)
        try:
            self.conn.execute(
                f"UPDATE style_examples SET hit_count = hit_count + 1, last_hit = ?"
                f" WHERE id IN ({marks})",
                [float(now if now is not None else time.time()), *target],
            )
            self.conn.commit()
        except sqlite3.Error:
            pass

    # -- 读 ---------------------------------------------------------------
    def list(
        self,
        *,
        status: str = "",
        scene: str = "",
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[StyleExample], int]:
        if self.conn is None:
            return [], 0
        where: list[str] = []
        params: list[Any] = []
        if status in STATUSES:
            where.append("status = ?")
            params.append(status)
        if scene in (SCENE_GROUP, SCENE_PRIVATE):
            where.append("scene = ?")
            params.append(scene)
        if query:
            where.append("(user_text LIKE ? OR reply_text LIKE ? OR keywords LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        try:
            total_row = self.conn.execute(
                f"SELECT COUNT(*) AS c FROM style_examples {clause}", params
            ).fetchone()
            total = int(total_row["c"] or 0) if total_row else 0
            rows = self.conn.execute(
                f"SELECT * FROM style_examples {clause}"
                f" ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, max(1, int(limit)), max(0, int(offset))],
            ).fetchall()
        except sqlite3.Error:
            return [], 0
        return [_row(row) for row in rows], total

    def approved(self, scene: str = "") -> list[StyleExample]:
        rows, _total = self.list(status=STATUS_APPROVED, scene=scene, limit=500)
        return rows

    def counts(self) -> dict[str, int]:
        if self.conn is None:
            return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "auto": 0}
        try:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM style_examples GROUP BY status"
            ).fetchall()
            auto_row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM style_examples WHERE source = ?", (SOURCE_AUTO,)
            ).fetchone()
        except sqlite3.Error:
            return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "auto": 0}
        counts = {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
        for row in rows:
            key = str(row["status"] or "")
            value = int(row["c"] or 0)
            counts["total"] += value
            if key in counts:
                counts[key] = value
        counts["auto"] = int(auto_row["c"] or 0) if auto_row else 0
        return counts

    def matched(self, text: str, scene: str = "", limit: int = 3) -> list[StyleExample]:
        """挑几条最像"这次该怎么说话"的示例。"""
        candidates = self.approved(scene)
        if not candidates:
            return []
        grams = ngrams(text)
        scored: list[tuple[float, StyleExample]] = []
        for item in candidates:
            overlap = similarity(grams, item.grams)
            if overlap <= 0:
                continue
            # 像 + 质量高 + 用过的少一点，三个一起排
            score = overlap * 2.0 + (item.score / 100.0) * 0.5 - min(item.hit_count, 8) * 0.05
            scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        return [item for _score, item in scored[: max(1, limit)]]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_examples(examples: Sequence[StyleExample]) -> str:
    """把示例排成 few-shot 片段。"""
    lines: list[str] = []
    for item in examples:
        lines.append(f"他：{item.user_text.strip()}")
        lines.append(f"她：{item.reply_text.strip()}")
        lines.append("")
    return "\n".join(lines).strip()


__all__ = [
    "MAX_GRAMS",
    "NGRAM",
    "SCENE_GROUP",
    "SCENE_PRIVATE",
    "SOURCE_AUTO",
    "SOURCE_MANUAL",
    "STATUSES",
    "STATUS_APPROVED",
    "STATUS_LABELS",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STOPWORDS",
    "StyleExample",
    "StyleStore",
    "is_followup",
    "keywords_of",
    "ngrams",
    "render_examples",
    "similarity",
]
