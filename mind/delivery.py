
"""结果投递到哪：原地回 / 发到「发指令那个 QQ」的私聊。

为什么单独一层：群里刷 9 张分镜图、贴一份日报，会把别人烦死；
但私聊里执行时用户就想当场看到。所以规则很简单 ——
**群里发指令，大块结果走私聊；私聊发指令，原地给。**
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DeliverySettings:
    #: 群里执行命令时，把大块结果发到指令 QQ 的私聊（默认开）
    bulk_to_private: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"bulk_to_private": bool(self.bulk_to_private)}

    @staticmethod
    def from_dict(data: Any) -> "DeliverySettings":
        raw = data if isinstance(data, dict) else {}
        return DeliverySettings(bulk_to_private=bool(raw.get("bulk_to_private", True)))


def load_settings(path: Path | str) -> DeliverySettings:
    try:
        return DeliverySettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return DeliverySettings()


def save_settings(path: Path | str, settings: DeliverySettings) -> bool:
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


def private_umo(umo: str, uid: str, platform: str = "") -> str:
    """把「某个会话 + 某个 QQ」拼成私聊地址。

    AstrBot 的 unified_msg_origin 形如 aiocqhttp:GroupMessage:123456，
    私聊就是 aiocqhttp:FriendMessage:<QQ>。
    """
    left = str(platform or "").strip()
    if not left:
        left = str(umo or "").split(":", 1)[0].strip()
    who = str(uid or "").strip()
    if not left or not who:
        return ""
    return "%s:FriendMessage:%s" % (left, who)


def is_group(umo: str) -> bool:
    return "GroupMessage" in str(umo or "")


__all__ = ["DeliverySettings", "load_settings", "save_settings", "private_umo", "is_group"]

