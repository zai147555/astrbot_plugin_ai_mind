
"""按人格生成的每日日程。

规则版日程（mind/schedule.py）是兜底：它保证「上课时间她不会长篇大论」，
但每个人的她是不一样的 —— 同一个模板套所有人，读起来就是「像」而不是「是」。

这里做的是：让模型根据人格味道，生成当天的作息（几点干什么、忙还是闲、什么语气），
按日期缓存在 data/schedule/<日期>.json。生成失败、JSON 不合法、字段越界，
一律退回规则版 —— 日程算不出来绝不能影响聊天。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_STATES = ("busy", "free", "sleeping")
_CLOCK = re.compile(r"^\s*(\d{1,2})\s*[:：.]\s*(\d{1,2})\s*$")


def minutes(hhmm: str) -> int:
    match = _CLOCK.match(str(hhmm or ""))
    if not match:
        return -1
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return -1
    return max(0, min(24 * 60, hour * 60 + minute))


def build_prompt(day: dict[str, Any], persona: str = "", name: str = "") -> tuple[str, str]:
    """返回 (system, prompt)，让模型输出当天作息 JSON。"""
    system = (
        "你在给一个初中生安排今天的作息，只输出 JSON，不要解释。"
        "她是真人不是机器人：上课要忙、睡觉要睡、放假会赖床。"
        "不要写真实学校名、地址、真名。"
    )
    who = ("她是「%s」。" % name) if name else ""
    prompt = (
        "%s今天是 %s（%s），%s。\n"
        "她的人设味道：%s\n"
        "请按这个人设安排今天的作息，输出 JSON："
        "{\"note\": \"一句话概括今天\", "
        "\"table\": [{\"start\": \"06:40\", \"end\": \"07:20\", "
        "\"activity\": \"在做什么\", \"state\": \"busy/free/sleeping\", "
        "\"tone\": \"这时候说话的语气\"}]}\n"
        "要求：6~9 段；时间从 00:00 排到 24:00 连续不断；state 只能是 busy / free / sleeping；"
        "activity 和 tone 各 30 字以内；符合她这个年纪。"
        % (who, day.get("date", ""), day.get("note", ""),
           "上学日" if day.get("kind") == "school" else "不用上学",
           (persona or "嘴硬心软、有点别扭"))
    )
    return system, prompt


def parse(raw: str) -> dict[str, Any] | None:
    """把模型输出解析成 {note, table}；不合格返回 None。"""
    text = str(raw or "").strip()
    if text.startswith(chr(96) * 3):
        text = text.strip(chr(96)).strip()
        text = re.sub(r"^json\s*", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    rows: list[dict[str, Any]] = []
    for item in data.get("table") or []:
        if not isinstance(item, dict):
            continue
        begin, finish = minutes(item.get("start")), minutes(item.get("end"))
        if begin < 0 or finish < 0 or finish <= begin:
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in _STATES:
            state = "free"
        rows.append({
            "start": begin, "end": finish,
            "activity": str(item.get("activity") or "").strip()[:40] or "在忙自己的事",
            "state": state,
            "tone": str(item.get("tone") or "").strip()[:40],
        })
    if len(rows) < 3:
        return None
    rows.sort(key=lambda row: row["start"])
    return {"note": str(data.get("note") or "").strip()[:80], "table": rows}


def slot_of(gen: dict[str, Any], now: float | None = None) -> dict[str, str]:
    stamp = time.time() if now is None else float(now)
    local = time.localtime(stamp)
    minute = local.tm_hour * 60 + local.tm_min
    for row in gen.get("table") or []:
        if int(row["start"]) <= minute < int(row["end"]):
            return {"activity": str(row.get("activity") or ""),
                    "state": str(row.get("state") or "free"),
                    "tone": str(row.get("tone") or "")}
    return {"activity": "在忙自己的事", "state": "free", "tone": ""}


def render(gen: dict[str, Any], now: float | None = None) -> str:
    """和 schedule.render 一样的结构，模型不用学第二套格式。"""
    if not gen or not gen.get("table"):
        return ""
    stamp = time.time() if now is None else float(now)
    local = time.localtime(stamp)
    slot = slot_of(gen, stamp)
    head = (
        "（她今天的日程，不是要说出口的内容）\n"
        + "今天 " + time.strftime("%Y-%m-%d", local) + "，" + (gen.get("note") or "") + "\n"
        + "现在 " + time.strftime("%H:%M", local) + "，她在：" + slot["activity"] + "。"
    )
    state = slot["state"]
    if state == "sleeping":
        head += "\n这个点她本该睡着：回得很勉强、很短，会赶对方去睡。"
    elif state == "busy":
        head += "\n她正忙：只回一两句、别写长，态度可以敷衍一点。"
    else:
        head += "\n她有闲：可以正常聊。"
    if slot["tone"]:
        head += "\n语气参考：" + slot["tone"] + "。"
    return "<daily_schedule>\n" + head + "\n</daily_schedule>"


def load(path: Path | str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("table"):
        return None
    return data


def save(path: Path | str, data: dict[str, Any]) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


__all__ = ["build_prompt", "parse", "slot_of", "render", "load", "save", "minutes"]
