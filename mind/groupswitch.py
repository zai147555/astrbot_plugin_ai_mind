"""群聊对话开关：一句话让她在所有群 / 单个群里闭嘴或回魂。

从「群聊对话开关 v1.2.1」（astrbot_plugin_group_switch）并进来的，
但做了两处调整：
  - **主人判定复用 ai_mind 已有的那套**（面板上填的专属用户），
    不再单独配一遍「管理QQ」—— 两个地方各配一次，迟早会配岔；
  - 状态存自己的文件，跟调试模式一个路子。

判定顺序（跟原插件一致）：全局关 → 哪个群都不回；全局开 → 再看这个群是否被单关。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OPEN_ALL = "开启对话"
CLOSE_ALL = "关闭对话"
OPEN_ONE = "开启本群"
CLOSE_ONE = "关闭本群"

COMMANDS = (OPEN_ALL, CLOSE_ALL, OPEN_ONE, CLOSE_ONE)


class GroupSwitch:
    """哪些群现在是闭嘴的。"""

    def __init__(self, path: Path, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self.all_open = True
        self.closed: set[str] = set()
        self.load()

    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.all_open = bool(data.get("all_open", True))
                self.closed = {str(x) for x in (data.get("closed") or []) if str(x)}
        except Exception as exc:  # noqa: BLE001 - 读不回来就按「都开着」
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 群聊开关状态读不回来：{exc}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"all_open": self.all_open, "closed": sorted(self.closed)},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 群聊开关状态存不下来：{exc}")

    def is_open(self, group_id: str) -> bool:
        if not self.all_open:
            return False
        return str(group_id) not in self.closed

    def set_all(self, opened: bool) -> None:
        self.all_open = bool(opened)
        if opened:
            self.closed.clear()      # 全局开启 = 顺带把单群关的全放出来
        self.save()

    def set_one(self, group_id: str, opened: bool) -> None:
        gid = str(group_id)
        if opened:
            self.closed.discard(gid)
        else:
            self.closed.add(gid)
        self.save()


__all__ = ["GroupSwitch", "COMMANDS", "OPEN_ALL", "CLOSE_ALL", "OPEN_ONE", "CLOSE_ONE"]

