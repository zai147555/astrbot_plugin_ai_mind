
"""被戳一下：像真人那样接一句。

设计参考 muyouzhi6/astrbot_plugin_poke_to_llm（MIT）——思路照搬、代码独立实现：
监听 OneBot 的 poke 通知（post_type=notice, notice_type=poke, target_id=机器人），
做作用域/黑名单/双重冷却检查，然后**走标准 LLM 链路**（event.request_llm）生成一句反应，
这样人设、情绪、记忆、分段、表情包全都会自动生效。

和上游不一样的地方：提示词只写「怎么反应」，不写"判断流程"；冷却做成纯函数好测。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

#: 默认提示词（$username / $user_id / $scene 会被替换）
DEFAULT_PROMPT = (
    "$username（ID：$user_id）刚刚在$scene里戳了你一下。这不是一句话，是一次肢体互动。"
    "别问「怎么了」，也别每次都用同一种反应。"
    "先想：他刚才有没有说过话是我没接上的？有就直接接那件事；"
    "如果只是随手戳，就按我此刻的心情给一句很短的反应（可以嫌烦、可以回戳、可以笑一下）。"
    "一句就够，不要解释，不要客服腔。"
)

#: 冷却记录至少留这么久，免得一直占内存
COOLDOWN_KEEP = 600.0


@dataclass
class PokeSettings:
    enabled: bool = True
    in_groups: bool = True
    in_private: bool = True
    #: 同一个人两次戳之间的间隔（秒）
    user_cooldown: float = 8.0
    #: 同一个群里任意两人之间（防止轮流戳）
    group_cooldown: float = 20.0
    blacklist: list[str] = field(default_factory=list)
    prompt: str = DEFAULT_PROMPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "in_groups": bool(self.in_groups),
            "in_private": bool(self.in_private),
            "user_cooldown": round(float(self.user_cooldown), 1),
            "group_cooldown": round(float(self.group_cooldown), 1),
            "blacklist": list(self.blacklist),
            "prompt": self.prompt,
        }

    @staticmethod
    def from_dict(data: Any) -> "PokeSettings":
        raw = data if isinstance(data, dict) else {}

        def num(value: Any, fallback: float) -> float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return fallback
            if value != value or value < 0:      # NaN / 负数
                return fallback
            return min(86400.0, value)

        blacklist = raw.get("blacklist")
        return PokeSettings(
            enabled=bool(raw.get("enabled", True)),
            in_groups=bool(raw.get("in_groups", True)),
            in_private=bool(raw.get("in_private", True)),
            user_cooldown=num(raw.get("user_cooldown"), 8.0),
            group_cooldown=num(raw.get("group_cooldown"), 20.0),
            blacklist=[str(item).strip() for item in (blacklist if isinstance(blacklist, list) else [])
                       if str(item).strip()],
            prompt=str(raw.get("prompt") or DEFAULT_PROMPT),
        )


def load_settings(path: Path | str) -> PokeSettings:
    try:
        return PokeSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return PokeSettings()


def save_settings(path: Path | str, settings: PokeSettings) -> bool:
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
# 事件识别
# ---------------------------------------------------------------------------


def is_poke(raw_message: Any) -> bool:
    """这条原始事件是不是「戳一戳」。"""
    if not isinstance(raw_message, dict):
        return False
    post = str(raw_message.get("post_type") or "").lower()
    notice = str(raw_message.get("notice_type") or "").lower()
    sub = str(raw_message.get("sub_type") or "").lower()
    if post != "notice":
        return False
    return notice in ("poke", "notify") and ("poke" in notice or "poke" in sub or not sub) \
        or notice == "poke"


def poke_target(raw_message: Any) -> str:
    if not isinstance(raw_message, dict):
        return ""
    return str(raw_message.get("target_id") or "")


def poke_sender(raw_message: Any) -> str:
    if not isinstance(raw_message, dict):
        return ""
    return str(raw_message.get("user_id") or "")


def poke_group(raw_message: Any) -> str:
    if not isinstance(raw_message, dict):
        return ""
    return str(raw_message.get("group_id") or "")


def hits_me(raw_message: Any, self_id: str) -> bool:
    """戳的是不是机器人自己。"""
    target = poke_target(raw_message)
    me = str(self_id or "")
    return bool(target and me and target == me)


# ---------------------------------------------------------------------------
# 冷却
# ---------------------------------------------------------------------------


class PokeGate:
    """双重冷却：同一个人 + 同一个群（防止轮流戳）。"""

    def __init__(self, settings: PokeSettings) -> None:
        self.settings = settings
        self._user: dict[str, float] = {}
        self._group: dict[str, float] = {}
        self._hits = 0

    def allow(self, now: float, *, sender: str, group: str = "",
              is_admin: bool = False) -> tuple[bool, str]:
        conf = self.settings
        if is_admin:
            return True, "admin"
        key = str(sender or "")
        if not key:
            return False, "no_sender"
        if key in conf.blacklist:
            return False, "blacklisted"
        last = self._user.get(key, 0.0)
        if conf.user_cooldown > 0 and (now - last) < conf.user_cooldown:
            return False, "user_cooldown"
        if group:
            if not conf.in_groups:
                return False, "groups_off"
            last_group = self._group.get(group, 0.0)
            if conf.group_cooldown > 0 and (now - last_group) < conf.group_cooldown:
                return False, "group_cooldown"
        elif not conf.in_private:
            return False, "private_off"
        return True, ""

    def mark(self, now: float, *, sender: str, group: str = "") -> None:
        self._user[str(sender)] = now
        if group:
            self._group[str(group)] = now
        self._hits += 1
        if self._hits % 50 == 0:
            self.prune(now)

    def prune(self, now: float) -> int:
        keep = max(COOLDOWN_KEEP, self.settings.user_cooldown * 2, self.settings.group_cooldown * 2)
        removed = 0
        for table in (self._user, self._group):
            for key in [item for item, stamp in table.items() if now - stamp > keep]:
                table.pop(key, None)
                removed += 1
        return removed


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def build_prompt(settings: PokeSettings, *, username: str, user_id: str,
                 scene: str, has_context: bool = True) -> str:
    template = Template(str(settings.prompt or DEFAULT_PROMPT))
    text = template.safe_substitute(
        username=str(username or "对方"),
        user_id=str(user_id or ""),
        scene=str(scene or "群聊"),
    )
    if not has_context:
        text += "\n（这次没有可用的上下文：就按被戳这一下本身反应，别编没发生的事。）"
    return re.sub(r"[ \t]+\n", "\n", text).strip()


__all__ = [
    "DEFAULT_PROMPT", "COOLDOWN_KEEP", "PokeSettings", "PokeGate",
    "load_settings", "save_settings", "is_poke", "poke_target", "poke_sender",
    "poke_group", "hits_me", "build_prompt",
]

