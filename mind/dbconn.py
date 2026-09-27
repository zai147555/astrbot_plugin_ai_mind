"""数据库连接的取用方式。

为什么不能把连接本身存下来
--------------------------
采样、词表、配图、风格、去 AI 味日志都用同一个 sqlite 连接。以前它们在构造时
就把**裸连接对象**存了下来。宿主重载插件会先调 terminate()，我们那时把库关掉
（这是对的），但老实例有时还在继续收消息 —— 存下来的连接于是变成「已关闭的
数据库」，之后所有写入都静默失败：曲线永远是空的、记忆写不进去，日志里只剩
一行 warning，面板上还看不出原因。

现在存的是「从哪儿拿连接」，每次用之前现取：库被重新打开也能跟上。
"""

from __future__ import annotations

import sqlite3
from typing import Any


class ConnectionSource:
    """把「连接本身」换成「连接的来源」。"""

    _source: Any = None

    @property
    def conn(self) -> sqlite3.Connection | None:
        source = self._source
        if source is None or isinstance(source, sqlite3.Connection):
            return source
        # 传进来的是仓库（例如 MemoryStore）：它的 connection 是属性，
        # 库被关掉之后会自动重开，这里每次都现取一遍。
        return getattr(source, "connection", None)

    @conn.setter
    def conn(self, value: Any) -> None:
        # 测试里会直接把它设成 None 来模拟「库不可用」，所以留个 setter。
        self._source = value


__all__ = ["ConnectionSource"]

