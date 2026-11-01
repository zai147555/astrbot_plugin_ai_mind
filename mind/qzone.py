"""QQ 空间：她写日记、配图、发说说，面板里按空间的样式看。

为什么要单独一层
----------------
QQ 空间发说说是 NapCat 的扩展接口（send_qzone_msg），参数看着简单：
content + images + ugc_right。坑在别的地方：

1. **图片要 base64**：NapCat 支持 base64://，但如果传本地路径，它只认
   file:// 或 http(s)://，AstrBot 这边本地文件路径直接传会失败。
2. **返回结构不固定**：有的版本回 {tid, time}，有的包一层 {status, retcode, data}。
3. **默认权限**：ugc_right=1 所有人可见，64 仅自己可见。写日记这种私人的东西
   默认给 1 是用户的选择，但代码里必须把 5 个值都写清楚，面板才能让人选。
4. **失败要留痕**：发失败了不能说"发过了"，草稿状态要能回到 draft 让用户重试。

所以这里只做纯逻辑（设置、拼内容、解析返回、日记库、提示词），
真正调接口在 main.py（它才拿得到 bot api）。
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dbconn import ConnectionSource

#: 说说正文上限（空间本身能更长，但日记不该刷屏）
MAX_CONTENT = 2000
#: 一条说说最多几张图
MAX_IMAGES = 9
#: 查看权限（NapCat send_qzone_msg 的 ugc_right）
UGC_RIGHTS: dict[int, str] = {
    1: "所有人可见",
    4: "好友可见",
    16: "部分好友可见",
    64: "仅自己可见",
    128: "部分好友不可见",
}
#: 写在日记里的心情（尽量用她自己的词，不用"悲伤/愤怒"这种标签）
MOODS = ("平静", "开心", "有点累", "想你", "闷闷的", "困", "有点慌")
#: 单张配图上限定死，免得把 QQ 撑爆
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS diaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL DEFAULT 0,
    day          TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    mood         TEXT NOT NULL DEFAULT '',
    image        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft',
    tid          TEXT NOT NULL DEFAULT '',
    published_at REAL NOT NULL DEFAULT 0,
    likes        INTEGER NOT NULL DEFAULT 0,
    comments     INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_diary_created ON diaries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diary_status ON diaries(status);
"""


# ---------------------------------------------------------------------------
# 提示词与文本处理
# ---------------------------------------------------------------------------

DIARY_SYSTEM = (
    "你在替一个 14 岁的女孩写她自己的日记。她叫杜可怡，嘴硬、心软、有点别扭，"
    "说话不看场合但很在意别人。\n"
    "写日记的规矩：\n"
    "1. 第一人称，写今天真实发生的一件小事和当时的情绪，不写大道理、不总结升华；\n"
    "2. 不要排比句、不要「作为…」、不要用「仿佛/似乎/不禁」这类词；不用 emoji 连发；\n"
    "3. 中文和中文之间不要空格；不用 markdown 标记；\n"
    "4. 不写真名、学校、住址、生日、电话；提到人就用昵称；\n"
    "5. 长度 150~350 字，像随手记的，不像作文。"
)


def day_text(ts: float | None = None) -> str:
    """2026年11月1日 这样的日期文本。"""
    import datetime

    moment = datetime.datetime.fromtimestamp(float(ts if ts is not None else time.time()))
    return "%d年%d月%d日" % (moment.year, moment.month, moment.day)


def feed_time_text(ts: float | None = None) -> str:
    """空间列表用：11月1日 21:30。"""
    import datetime

    try:
        moment = datetime.datetime.fromtimestamp(float(ts or 0))
    except (OverflowError, OSError, ValueError):
        return ""
    return "%d月%d日 %02d:%02d" % (moment.month, moment.day, moment.hour, moment.minute)


def strip_fence(text: str) -> str:
    """去掉模型爱加的代码块围栏。"""
    body = str(text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^`{3}[a-zA-Z]*\s*", "", body)
        body = re.sub(r"`{3}\s*$", "", body)
    return body.strip()


def tidy_body(text: str) -> str:
    """收拾正文：去 CJK 之间的空格、压空行、去首尾空白。"""
    body = strip_fence(text)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    # 中文、中文标点之间的空格一律去掉：模型经常「每 个 字 都 空 一 格」
    cjk = r"[\u4e00-\u9fff\u3000-\u303f\uff01\uff0c\uff1a\uff1b\uff1f]"
    body = re.sub("(?<=%s)[ \\t]+(?=%s)" % (cjk, cjk), "", body)
    return body.strip()


def parse_diary(raw: str) -> dict[str, str]:
    """把模型输出解析成 {title, body, mood}；JSON 解析不了就当纯文本。"""
    text = strip_fence(raw)
    data: Any = None
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            data = None
    title = body = mood = ""
    if isinstance(data, dict):
        title = str(data.get("title") or "").strip()
        body = str(data.get("body") or data.get("content") or "").strip()
        mood = str(data.get("mood") or "").strip()
    if not body:
        body = text
    body = tidy_body(body)
    if not title:
        first = body.splitlines()[0] if body else ""
        title = first.strip("，。！？,!? ")[:14]
    return {"title": title[:24], "body": body, "mood": mood[:8]}


def diary_prompt(day: str = "", mood: str = "", persona: str = "",
                 extra: str = "", max_chars: int = 350) -> tuple[str, str]:
    """返回 (system, prompt)。persona/extra 为空就用内置文案。"""
    system = (persona or "").strip() or DIARY_SYSTEM
    prompt = (
        "日期：%s\n"
        "此刻的心情：%s\n"
        "请写今天的日记，只输出 JSON："
        "{\"title\": \"8 字以内的小标题\", \"body\": \"日记正文\", \"mood\": \"一个词\"}\n"
        "正文控制在 %d 字以内。"
        % (day or day_text(), mood or "平静", max(80, int(max_chars)))
    )
    if extra.strip():
        prompt += "\n额外要求：" + extra.strip()
    return system, prompt


def image_prompt(title: str, body: str, style: str = "") -> str:
    """把日记变成一张插画的提示词。"""
    scene = tidy_body(body)[:220].replace("\n", " ")
    text = (
        "一张安静的插画，用来配一篇日记。画面内容：%s。"
        "标题是「%s」。不要文字、不要水印、不要边框、不要拼图，"
        "人物不要正脸特写，构图留白，像随手拍下的一个瞬间。"
        % (scene or title or "傍晚的房间", title or "")
    )
    if style.strip():
        text += "风格：" + style.strip() + "。"
    return text


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

@dataclass
class QzoneSettings:
    """空间设置。默认**不自动发**：先攒草稿，人看过再发。"""

    enabled: bool = True
    #: 每天定时写 + 发（默认关；开了就是替主人往空间发东西，风险自担）
    auto_post: bool = False
    auto_hour: int = 21
    auto_minute: int = 0
    #: 1 所有人可见 / 4 好友 / 16 部分好友 / 64 仅自己 / 128 部分好友不可见
    ugc_right: int = 1
    with_image: bool = True
    image_style: str = "手绘水彩，暖光，安静"
    signature: str = ""
    write_system: str = ""
    max_chars: int = 350
    #: 只有这些 QQ 号能用 #日记 命令（空 = 主人自己）
    owners: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "auto_post": bool(self.auto_post),
            "auto_hour": int(self.auto_hour),
            "auto_minute": int(self.auto_minute),
            "ugc_right": int(self.ugc_right),
            "with_image": bool(self.with_image),
            "image_style": self.image_style,
            "signature": self.signature,
            "write_system": self.write_system,
            "max_chars": int(self.max_chars),
            "owners": list(self.owners),
            "ugc_rights": [{"value": key, "label": label} for key, label in UGC_RIGHTS.items()],
        }

    @staticmethod
    def from_dict(raw: Any) -> "QzoneSettings":
        data = raw if isinstance(raw, dict) else {}
        conf = QzoneSettings()
        conf.enabled = bool(data.get("enabled", True))
        conf.auto_post = bool(data.get("auto_post", False))
        try:
            conf.auto_hour = max(0, min(23, int(data.get("auto_hour", 21))))
        except (TypeError, ValueError):
            conf.auto_hour = 21
        try:
            conf.auto_minute = max(0, min(59, int(data.get("auto_minute", 0))))
        except (TypeError, ValueError):
            conf.auto_minute = 0
        try:
            right = int(data.get("ugc_right", 1))
            conf.ugc_right = right if right in UGC_RIGHTS else 1
        except (TypeError, ValueError):
            conf.ugc_right = 1
        conf.with_image = bool(data.get("with_image", True))
        conf.image_style = str(data.get("image_style") or "").strip()[:80]
        conf.signature = str(data.get("signature") or "").strip()[:80]
        conf.write_system = str(data.get("write_system") or "")[:2000]
        try:
            conf.max_chars = max(80, min(1200, int(data.get("max_chars", 350))))
        except (TypeError, ValueError):
            conf.max_chars = 350
        owners = data.get("owners")
        if isinstance(owners, list):
            conf.owners = [str(item).strip() for item in owners if str(item).strip()]
        return conf


def load_settings(path: Path | str) -> QzoneSettings:
    try:
        return QzoneSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return QzoneSettings()


def save_settings(path: Path | str, settings: QzoneSettings) -> bool:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.to_dict()
        payload.pop("ugc_rights", None)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 日记库
# ---------------------------------------------------------------------------

@dataclass
class Diary:
    id: int = 0
    created_at: float = 0.0
    day: str = ""
    title: str = ""
    body: str = ""
    mood: str = ""
    image: str = ""
    status: str = "draft"
    tid: str = ""
    published_at: float = 0.0
    likes: int = 0
    comments: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "created_at": self.created_at, "day": self.day,
            "title": self.title, "body": self.body, "mood": self.mood,
            "image": self.image, "status": self.status, "tid": self.tid,
            "published_at": self.published_at, "likes": self.likes,
            "comments": self.comments, "error": self.error,
            "time_text": feed_time_text(self.published_at or self.created_at),
            "status_text": self.status_text,
        }

    @property
    def status_text(self) -> str:
        return {"draft": "草稿", "published": "已发",
                "failed": "没发出去"}.get(self.status, self.status)


class DiaryStore(ConnectionSource):
    _schema = _SCHEMA

    def __init__(self, connection: Any = None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        self.ensure_schema()

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning("[ai_mind] " + message)

    def add(self, title: str, body: str, mood: str = "", image: str = "",
            day: str = "", status: str = "draft", created_at: float | None = None) -> int:
        conn = self.conn
        if conn is None:
            return 0
        at = time.time() if created_at is None else float(created_at)
        try:
            cursor = conn.execute(
                "INSERT INTO diaries (created_at, day, title, body, mood, image, status)"
                " VALUES (?,?,?,?,?,?,?)",
                (at, day or day_text(at), str(title or "")[:60],
                 str(body or "")[:4000], str(mood or "")[:8], str(image or ""),
                 status if status in ("draft", "published", "failed") else "draft"),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
        except sqlite3.Error as exc:
            self._log(f"存日记失败：{exc}")
            return 0

    def update(self, diary_id: int, **fields: Any) -> bool:
        conn = self.conn
        allowed = {"title", "body", "mood", "image", "status", "tid",
                   "published_at", "likes", "comments", "error"}
        pairs = [(key, value) for key, value in fields.items() if key in allowed]
        if conn is None or not pairs:
            return False
        try:
            conn.execute(
                "UPDATE diaries SET " + ", ".join(key + " = ?" for key, _ in pairs)
                + " WHERE id = ?",
                [value for _, value in pairs] + [int(diary_id)],
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            self._log(f"改日记失败：{exc}")
            return False

    def get(self, diary_id: int) -> Diary | None:
        conn = self.conn
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM diaries WHERE id = ?", (int(diary_id),)).fetchone()
        except sqlite3.Error:
            return None
        return _row_to_diary(row) if row else None

    def recent(self, limit: int = 20, status: str = "") -> list[Diary]:
        conn = self.conn
        if conn is None:
            return []
        sql = "SELECT * FROM diaries"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        return [_row_to_diary(row) for row in rows]

    def latest_draft(self) -> Diary | None:
        rows = self.recent(1, status="draft")
        if rows:
            return rows[0]
        rows = self.recent(1, status="failed")
        return rows[0] if rows else None

    def mark_published(self, diary_id: int, tid: str, at: float | None = None) -> bool:
        return self.update(diary_id, status="published", tid=str(tid or ""),
                           published_at=time.time() if at is None else float(at),
                           error="")

    def mark_failed(self, diary_id: int, error: str) -> bool:
        return self.update(diary_id, status="failed", error=str(error or "")[:300])

    def update_stats(self, tid: str, likes: int = 0, comments: int = 0) -> bool:
        """空间列表回来时顺手更新赞/评论数。"""
        conn = self.conn
        if conn is None or not str(tid or "").strip():
            return False
        try:
            conn.execute(
                "UPDATE diaries SET likes = ?, comments = ? WHERE tid = ?",
                (max(0, int(likes or 0)), max(0, int(comments or 0)), str(tid)),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            return False

    def remove(self, diary_id: int) -> bool:
        conn = self.conn
        if conn is None:
            return False
        try:
            conn.execute("DELETE FROM diaries WHERE id = ?", (int(diary_id),))
            conn.commit()
            return True
        except sqlite3.Error:
            return False

    def count(self, status: str = "") -> int:
        conn = self.conn
        if conn is None:
            return 0
        try:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) FROM diaries WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM diaries").fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0) if row else 0

    def stats(self) -> dict[str, int]:
        return {
            "total": self.count(),
            "drafts": self.count("draft") + self.count("failed"),
            "published": self.count("published"),
        }


def _row_to_diary(row: sqlite3.Row | tuple) -> Diary:
    keys = ("id", "created_at", "day", "title", "body", "mood", "image",
            "status", "tid", "published_at", "likes", "comments", "error")
    try:
        data = {key: row[key] for key in keys}
    except (TypeError, IndexError, KeyError):
        data = dict(zip(keys, tuple(row)))
    return Diary(
        id=int(data.get("id") or 0),
        created_at=float(data.get("created_at") or 0),
        day=str(data.get("day") or ""),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        mood=str(data.get("mood") or ""),
        image=str(data.get("image") or ""),
        status=str(data.get("status") or "draft"),
        tid=str(data.get("tid") or ""),
        published_at=float(data.get("published_at") or 0),
        likes=int(data.get("likes") or 0),
        comments=int(data.get("comments") or 0),
        error=str(data.get("error") or ""),
    )


# ---------------------------------------------------------------------------
# 发送前拼参数、发送后解析
# ---------------------------------------------------------------------------

def compose_content(entry: Diary | dict[str, Any], signature: str = "",
                    limit: int = MAX_CONTENT) -> str:
    """正文 + 可选署名，压到上限内。"""
    if isinstance(entry, Diary):
        body = entry.body
        title = entry.title
    else:
        body = str(entry.get("body") or "")
        title = str(entry.get("title") or "")
    text = tidy_body(body) or tidy_body(title)
    note = str(signature or "").strip()
    if note:
        text = (text + "\n\n" + note).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max(80, int(limit)):
        text = text[: max(80, int(limit)) - 1].rstrip() + "…"
    return text


def image_data_uri(path: str | Path) -> str:
    """本地图片 → NapCat 认识的 base64://。读不了就返回空串。"""
    target = Path(str(path or ""))
    try:
        if not target.is_file() or target.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        blob = target.read_bytes()
    except OSError:
        return ""
    if not blob:
        return ""
    return "base64://" + base64.b64encode(blob).decode("ascii")


def build_send_params(content: str, images: list[str],
                      ugc_right: int = 1,
                      target_uins: list[str] | None = None) -> dict[str, Any]:
    """拼 send_qzone_msg 的参数。"""
    params: dict[str, Any] = {"content": str(content or "")}
    keep = [item for item in (images or []) if item][:MAX_IMAGES]
    if keep:
        params["images"] = keep
    right = int(ugc_right or 1)
    if right not in UGC_RIGHTS:
        right = 1
    params["ugc_right"] = right
    if right in (16, 128) and target_uins:
        params["target_uins"] = [str(item) for item in target_uins if str(item)]
    return params


def _unwrap(payload: Any) -> dict[str, Any]:
    """把 {status, retcode, data:{...}} / {result:{...}} 拆成里面那层。"""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if not isinstance(inner, dict):
        inner = payload.get("result")
    if isinstance(inner, dict):
        merged = dict(payload)
        merged.update({key: value for key, value in inner.items() if key not in merged})
        return merged
    return payload


def parse_send(payload: Any) -> dict[str, Any]:
    """解析 send_qzone_msg 的返回。"""
    data = _unwrap(payload)
    tid = str(data.get("tid") or data.get("id") or "").strip()
    try:
        at = float(data.get("time") or data.get("created_time") or 0)
    except (TypeError, ValueError):
        at = 0.0
    retcode = data.get("retcode")
    failed = retcode not in (None, 0, "0", "ok")
    if failed and not tid:
        return {"ok": False, "tid": "", "time": 0.0,
                "error": str(data.get("message") or data.get("msg") or "空间接口返回失败")[:200]}
    if not tid:
        return {"ok": False, "tid": "", "time": 0.0,
                "error": "空间接口没回 tid（可能没发出去）"}
    return {"ok": True, "tid": tid, "time": at, "error": ""}


def parse_feeds(payload: Any) -> list[dict[str, Any]]:
    """解析 get_qzone_msg_list 的返回，尽量宽容。"""
    data = _unwrap(payload)
    rows: Any = None
    for key in ("msglist", "list", "feeds", "msgs"):
        if isinstance(data.get(key), list):
            rows = data[key]
            break
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tid") or row.get("id") or "").strip()
        if not tid:
            continue
        images: list[str] = []
        for pic in row.get("pic") or row.get("pics") or []:
            if isinstance(pic, dict):
                link = pic.get("url1") or pic.get("url") or pic.get("pic")
                if link:
                    images.append(str(link))
            elif isinstance(pic, str):
                images.append(pic)
        likes = row.get("likes") or row.get("like") or []
        comments = row.get("commentlist") or row.get("comments") or []
        out.append({
            "tid": tid,
            "content": str(row.get("content") or ""),
            "created": float(row.get("created_time") or row.get("ctime") or 0),
            "likes": len(likes) if isinstance(likes, list) else int(likes or 0),
            "comments": len(comments) if isinstance(comments, list) else int(comments or 0),
            "images": images[:MAX_IMAGES],
        })
    return out


def parse_profile(payload: Any) -> dict[str, str]:
    """解析 get_qzone_profile。"""
    data = _unwrap(payload)
    return {
        "uin": str(data.get("uin") or data.get("qq") or ""),
        "nickname": str(data.get("nickname") or data.get("name") or ""),
        "avatar": str(data.get("avatar") or data.get("portrait") or ""),
    }


def due_daily(settings: QzoneSettings, now: float, last_at: float = 0.0) -> bool:
    """今天的定时点到了吗（同一天只跑一次）。"""
    if not settings.auto_post:
        return False
    import datetime

    moment = datetime.datetime.fromtimestamp(float(now))
    hour = max(0, min(23, int(settings.auto_hour)))
    minute = max(0, min(59, int(settings.auto_minute)))
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < target:
        return False
    last = float(last_at or 0)
    if last <= 0:
        return True
    return last < target.timestamp()
