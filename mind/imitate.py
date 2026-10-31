
"""模仿指定 QQ 的说话习惯。

两件事：**学**（只采某几个 QQ 说过的话）和**模仿**（把他的话风算成指纹，注入一句参考）。

为什么单独做：现有的「表达示例」学的是**她自己**的好回复（他说什么 -> 我怎么回），
用来稳住自己的语气；这个是反过来的 —— 学**别人**怎么说话，让她的句长、标点、
口头禅往那个人靠一点。两者互不干扰，可以一起开。

红线：只借**节奏和用词**。内容、观点、称呼、分几条发、中文之间不留空格，
全部还是她自己的 —— 模板里写死这几条，测试也守着。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .dbconn import ConnectionSource

#: 每个人最多留多少条样本（够算指纹就行，不占地方）
MAX_SAMPLES = 600
#: 太短（"嗯""啊"）和太长（复制粘贴的一大段）都不收
MIN_TEXT_LEN = 2
MAX_TEXT_LEN = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imitate_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL,
    text       TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imitate_dup ON imitate_samples(uid, text);
CREATE INDEX IF NOT EXISTS idx_imitate_uid ON imitate_samples(uid, created_at DESC);
"""

#: 常见语气词（按"多少条消息里出现过"算，不是按次数）
PARTICLES = "啊吧呢嘛呀哈哦噢诶哎唉嘿呗啦嘞唔嗯"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CJK_SPACE = re.compile(r"[\u4e00-\u9fff]\s+[\u4e00-\u9fff]")
_URL = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# 设置（存成 data/imitate.json，面板/命令都能改）
# ---------------------------------------------------------------------------


@dataclass
class ImitateSettings:
    """学谁、要不要注入。默认**关** —— 采集别人的话必须显式打开。"""

    enabled: bool = False
    targets: list[str] = field(default_factory=list)
    inject: bool = True
    min_samples: int = 20

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "targets": list(self.targets),
            "inject": bool(self.inject),
            "min_samples": int(self.min_samples),
        }

    @staticmethod
    def from_dict(data: Any) -> "ImitateSettings":
        payload = data if isinstance(data, dict) else {}
        raw_targets = payload.get("targets")
        targets: list[str] = []
        if isinstance(raw_targets, (list, tuple)):
            for item in raw_targets:
                text = str(item).strip()
                if text and text not in targets:
                    targets.append(text)
        elif isinstance(raw_targets, str):
            for item in re.split(r"[\s,，、]+", raw_targets):
                if item.strip() and item.strip() not in targets:
                    targets.append(item.strip())
        try:
            minimum = int(payload.get("min_samples") or 20)
        except (TypeError, ValueError):
            minimum = 20
        return ImitateSettings(
            enabled=bool(payload.get("enabled")),
            targets=targets,
            inject=bool(payload.get("inject", True)),
            min_samples=max(3, min(500, minimum)),
        )

    def add_target(self, uid: str) -> bool:
        text = str(uid or "").strip()
        if not text:
            return False
        if text in self.targets:
            return False
        self.targets.append(text)
        return True

    def remove_target(self, uid: str) -> bool:
        text = str(uid or "").strip()
        if text in self.targets:
            self.targets.remove(text)
            return True
        return False


def load_settings(path: Path | str) -> ImitateSettings:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        return ImitateSettings.from_dict(json.loads(raw))
    except (OSError, ValueError):
        return ImitateSettings()


def save_settings(path: Path | str, settings: ImitateSettings) -> bool:
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
# 指纹
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """一个人怎么说话。"""

    uid: str
    samples: int
    avg_len: float
    short_ratio: float          # 不超过 8 个字的消息占比
    long_ratio: float           # 40 个字以上的占比
    question_ratio: float
    exclaim_ratio: float
    ellipsis_ratio: float
    tilde_ratio: float
    emoji_ratio: float
    space_ratio: float          # 中文之间加空格的习惯（只记录，不学）
    particles: tuple[tuple[str, float], ...] = ()
    endings: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "samples": self.samples,
            "avg_len": round(self.avg_len, 1),
            "short_ratio": round(self.short_ratio, 3),
            "long_ratio": round(self.long_ratio, 3),
            "question_ratio": round(self.question_ratio, 3),
            "exclaim_ratio": round(self.exclaim_ratio, 3),
            "ellipsis_ratio": round(self.ellipsis_ratio, 3),
            "tilde_ratio": round(self.tilde_ratio, 3),
            "emoji_ratio": round(self.emoji_ratio, 3),
            "space_ratio": round(self.space_ratio, 3),
            "particles": [[k, round(v, 3)] for k, v in self.particles],
            "endings": [[k, round(v, 3)] for k, v in self.endings],
        }


def _ratio(items: Iterable[bool]) -> float:
    rows = list(items)
    if not rows:
        return 0.0
    return sum(1 for row in rows if row) / len(rows)


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2B00 <= code <= 0x2BFF
    )


def build_profile(uid: str, texts: Iterable[str]) -> Profile:
    """把一堆话算成指纹。样本为空时返回全 0（调用方据此不注入）。"""
    clean: list[str] = []
    for raw in texts:
        text = str(raw or "").strip()
        if text:
            clean.append(text)
    n = len(clean)
    if not n:
        return Profile(uid=str(uid), samples=0, avg_len=0.0, short_ratio=0.0,
                       long_ratio=0.0, question_ratio=0.0, exclaim_ratio=0.0,
                       ellipsis_ratio=0.0, tilde_ratio=0.0, emoji_ratio=0.0,
                       space_ratio=0.0)

    lengths = [len(text) for text in clean]
    particles: list[tuple[str, float]] = []
    for word in PARTICLES:
        hit = _ratio(word in text for text in clean)
        if hit >= 0.15:
            particles.append((word, hit))
    particles.sort(key=lambda item: item[1], reverse=True)

    tail: dict[str, int] = {}
    for text in clean:
        last = text[-1]
        if _CJK.match(last):
            tail[last] = tail.get(last, 0) + 1
    endings = [
        (char, count / n) for char, count in sorted(tail.items(), key=lambda x: x[1], reverse=True)
        if count / n >= 0.15
    ][:2]

    return Profile(
        uid=str(uid),
        samples=n,
        avg_len=sum(lengths) / n,
        short_ratio=_ratio(length <= 8 for length in lengths),
        long_ratio=_ratio(length >= 40 for length in lengths),
        question_ratio=_ratio(("?" in text or "？" in text) for text in clean),
        exclaim_ratio=_ratio(("!" in text or "！" in text) for text in clean),
        ellipsis_ratio=_ratio(("…" in text or "..." in text) for text in clean),
        tilde_ratio=_ratio(("~" in text or "～" in text) for text in clean),
        emoji_ratio=_ratio(any(_is_emoji(ch) for ch in text) for text in clean),
        space_ratio=_ratio(bool(_CJK_SPACE.search(text)) for text in clean),
        particles=tuple(particles[:3]),
        endings=tuple(endings),
    )


def describe(profile: Profile) -> list[str]:
    """指纹 -> 几句人话（面板和命令共用）。"""
    if profile.samples <= 0:
        return []
    lines: list[str] = []
    if profile.avg_len < 12:
        lines.append("说话很短，平均 %.0f 个字，%d%% 的消息不超过 8 个字"
                     % (profile.avg_len, round(profile.short_ratio * 100)))
    elif profile.avg_len > 30:
        lines.append("爱说长句，平均 %.0f 个字" % profile.avg_len)
    else:
        lines.append("长短都有，平均 %.0f 个字" % profile.avg_len)

    marks: list[tuple[str, float]] = []
    if profile.question_ratio >= 0.2:
        marks.append(("爱用问号", profile.question_ratio))
    if profile.exclaim_ratio >= 0.25:
        marks.append(("爱用感叹号", profile.exclaim_ratio))
    elif profile.exclaim_ratio <= 0.03:
        marks.append(("几乎不用感叹号", profile.exclaim_ratio))
    if profile.ellipsis_ratio >= 0.15:
        marks.append(("爱用「…」", profile.ellipsis_ratio))
    if profile.tilde_ratio >= 0.15:
        marks.append(("爱用「~」", profile.tilde_ratio))
    for label, value in marks[:2]:
        lines.append("%s（%d%% 的消息里有）" % (label, round(value * 100)))

    if profile.particles:
        words = "」「".join(item[0] for item in profile.particles)
        lines.append("常带「" + words + "」")
    if profile.endings:
        chars = "」「".join(item[0] for item in profile.endings)
        lines.append("句末爱用「" + chars + "」")
    if profile.emoji_ratio >= 0.3:
        lines.append("爱发表情符号")
    if profile.space_ratio >= 0.1:
        lines.append("有在中文之间打空格的习惯（这一条不学）")
    return lines


def render_block(profile: Profile, name: str = "") -> str:
    """注入给模型的一段。没有可学的就返回空串。"""
    lines = describe(profile)
    if not lines:
        return ""
    who = (name or "对方").strip()
    body = "\n".join("- " + line for line in lines)
    return (
        '<imitate_style>\n'
        "你在跟" + who + "说话，可以借一点他的说话习惯（只是节奏和用词，不是照抄内容）：\n"
        + body + "\n"
        "内容、观点、怎么称呼、分几条发，还是按你自己的来；\n"
        "也不要因为学他把中文拆开加空格 —— 那一条永远不学。\n"
        "</imitate_style>"
    )


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


class ImitateStore(ConnectionSource):
    """样本仓库。异常一律降级，不让插件崩。"""

    _schema = _SCHEMA

    def __init__(self, connection: Any = None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        self.ensure_schema()

    # -- 写 ---------------------------------------------------------------
    def add_sample(self, uid: str, text: str, session_id: str = "") -> int:
        """记一条样本。重复、太短、太长、带链接的一律不收。"""
        conn = self.conn
        if conn is None:
            return 0
        owner = str(uid or "").strip()
        body = _URL.sub("", str(text or "")).strip()
        if not owner or not (MIN_TEXT_LEN <= len(body) <= MAX_TEXT_LEN):
            return 0
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO imitate_samples (uid, text, session_id, created_at)"
                " VALUES (?,?,?,?)",
                (owner, body, str(session_id or ""), time.time()),
            )
            if not cursor.rowcount:
                return 0
            conn.execute(
                "DELETE FROM imitate_samples WHERE uid = ? AND id NOT IN"
                " (SELECT id FROM imitate_samples WHERE uid = ? ORDER BY id DESC LIMIT ?)",
                (owner, owner, MAX_SAMPLES),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
        except sqlite3.Error as exc:
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 记录模仿样本失败：{exc}")
            return 0

    def clear(self, uid: str = "") -> int:
        conn = self.conn
        if conn is None:
            return 0
        try:
            if str(uid or "").strip():
                cursor = conn.execute("DELETE FROM imitate_samples WHERE uid = ?", (str(uid).strip(),))
            else:
                cursor = conn.execute("DELETE FROM imitate_samples")
            conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    # -- 读 ---------------------------------------------------------------
    def samples(self, uid: str, limit: int = MAX_SAMPLES) -> list[str]:
        conn = self.conn
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT text FROM imitate_samples WHERE uid = ? ORDER BY id DESC LIMIT ?",
                (str(uid or "").strip(), int(limit)),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(row[0]) for row in rows]

    def count(self, uid: str) -> int:
        conn = self.conn
        if conn is None:
            return 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM imitate_samples WHERE uid = ?", (str(uid or "").strip(),)
            ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0) if row else 0

    def users(self) -> list[tuple[str, int]]:
        conn = self.conn
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT uid, COUNT(*) FROM imitate_samples GROUP BY uid ORDER BY COUNT(*) DESC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [(str(row[0]), int(row[1] or 0)) for row in rows]

    def profile(self, uid: str) -> Profile:
        return build_profile(uid, self.samples(uid))

    def stats(self) -> dict[str, Any]:
        users = self.users()
        return {
            "users": len(users),
            "samples": sum(count for _, count in users),
            "detail": [{"uid": uid, "samples": count} for uid, count in users],
        }


__all__ = [
    "MAX_SAMPLES", "ImitateSettings", "Profile", "ImitateStore",
    "load_settings", "save_settings", "build_profile", "describe", "render_block",
]

