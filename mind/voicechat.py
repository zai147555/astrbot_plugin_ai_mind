
"""AI 声聊：文字照常发，紧接着补一条语音。

机制来自 QQ 的 AI 声聊（OneBot 扩展）：
  get_ai_characters(group_id, chat_type=1)  → 可选音色列表
  send_group_ai_record(group_id, character, text) → 让 QQ 用指定音色把这段话说出来

参考 zgojin/astrbot_plugin_AIQTalk 的做法（只借鉴接口用法，代码独立实现）。
和上游最大的不同：**文字永远先发、且绝不改动**——
上游为了「只发语音」会过滤颜文字/网址/表情，我们是「先文字后语音」，
所以文字侧的分段、零空格、表情包一条规则都不用让路；语音侧只做「念得出来」的清理。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 语音文本上限（QQ 那边限制 500，留点余量）
MAX_CHARS = 300
#: 群聊之外的平台不发
SUPPORTED_PLATFORMS = ("aiocqhttp",)
#: 模式
MODE_ALWAYS = "always"
MODE_CHANCE = "chance"
MODES = (MODE_ALWAYS, MODE_CHANCE)

_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"[*_\x60~>#|]+")
_BRACKET = re.compile(r"[（(［\[【{｛][^）)］\]】}｝]{0,40}[）)］\]】}｝]")
#: emoji / 符号（留着它们只会被念成乱码）
_JUNK = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)
_SPACES = re.compile(r"\s+")


@dataclass
class VoiceSettings:
    enabled: bool = False
    mode: str = MODE_ALWAYS
    chance: float = 0.35
    character: str = ""
    groups: list[str] = field(default_factory=list)
    max_chars: int = MAX_CHARS
    cooldown: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "mode": self.mode if self.mode in MODES else MODE_ALWAYS,
            "chance": round(float(self.chance), 3),
            "character": self.character,
            "groups": list(self.groups),
            "max_chars": int(self.max_chars),
            "cooldown": round(float(self.cooldown), 1),
        }

    @staticmethod
    def from_dict(data: Any) -> "VoiceSettings":
        raw = data if isinstance(data, dict) else {}

        def num(value: Any, fallback: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        mode = str(raw.get("mode") or MODE_ALWAYS).strip().lower()
        groups = raw.get("groups")
        return VoiceSettings(
            enabled=bool(raw.get("enabled", False)),
            mode=mode if mode in MODES else MODE_ALWAYS,
            chance=max(0.0, min(1.0, num(raw.get("chance", 0.35), 0.35))),
            character=str(raw.get("character") or "").strip(),
            groups=[str(item).strip() for item in (groups if isinstance(groups, list) else [])
                    if str(item).strip()],
            max_chars=max(20, min(500, int(num(raw.get("max_chars", MAX_CHARS), MAX_CHARS)))),
            cooldown=max(0.0, min(3600.0, num(raw.get("cooldown", 8.0), 8.0))),
        )


def load_settings(path: Path | str) -> VoiceSettings:
    try:
        return VoiceSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return VoiceSettings()


def save_settings(path: Path | str, settings: VoiceSettings) -> bool:
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


def clean_for_voice(text: str, max_chars: int = MAX_CHARS) -> str:
    """把一段回复处理成「念得出来」的样子：去网址、去 markdown、去括号动作、去表情。"""
    raw = str(text or "")
    if not raw.strip():
        return ""
    raw = _URL.sub("", raw)
    raw = _BRACKET.sub("", raw)
    raw = _MD.sub("", raw)
    raw = _JUNK.sub("", raw)
    raw = raw.replace(chr(10), " ").replace(chr(13), " ")
    raw = _SPACES.sub(" ", raw).strip()
    if len(raw) > max_chars:
        raw = raw[:max_chars].rstrip("，,。.、；; ") + "…"
    return raw


def should_send(settings: VoiceSettings, *, group: str, platform: str,
                rng: Any = None, last_at: float = 0.0,
                now: float | None = None) -> bool:
    """这一轮要不要补语音（纯函数，好测）。"""
    if not settings.enabled:
        return False
    if str(platform or "").lower() not in SUPPORTED_PLATFORMS:
        return False
    if not str(group or "").strip():
        return False
    if settings.groups and str(group) not in settings.groups:
        return False
    stamp = time.time() if now is None else float(now)
    if settings.cooldown > 0 and (stamp - float(last_at or 0.0)) < settings.cooldown:
        return False
    if settings.mode == MODE_CHANCE:
        import random

        chooser = rng or random.random
        if float(chooser()) > settings.chance:
            return False
    return True


def parse_characters(payload: Any) -> list[dict[str, str]]:
    """从 get_ai_characters 的回应里抠出 [{id,name}]。形状各地不同，尽量都认。"""
    data = payload
    if isinstance(payload, dict):
        data = payload.get("data", payload.get("result", payload))
        if isinstance(data, dict):
            data = data.get("list") or data.get("characters") or data.get("items") or []
    if not isinstance(data, list):
        return []
    rows: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ident = str(
            item.get("character_id") or item.get("characterId")
            or item.get("id") or item.get("key") or ""
        ).strip()
        name = str(
            item.get("character_name") or item.get("characterName")
            or item.get("name") or item.get("nickname") or ident
        ).strip()
        if ident:
            rows.append({"id": ident, "name": name or ident})
    return rows


def pick_character(settings: VoiceSettings, characters: Iterable[dict[str, str]]) -> str:
    """要用哪个音色：设置里指定的优先，否则第一个可用的。"""
    rows = list(characters or ())
    wanted = str(settings.character or "").strip()
    if wanted:
        for item in rows:
            if item.get("id") == wanted:
                return wanted
        return wanted
    return rows[0]["id"] if rows else ""


__all__ = [
    "MAX_CHARS", "SUPPORTED_PLATFORMS", "MODE_ALWAYS", "MODE_CHANCE", "MODES",
    "VoiceSettings", "load_settings", "save_settings",
    "clean_for_voice", "should_send", "parse_characters", "pick_character",
]

