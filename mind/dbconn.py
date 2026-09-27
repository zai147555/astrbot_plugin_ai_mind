"""数据库连接的取用方式。

两个坑都在这里挡掉
------------------
1. **不能把连接本身存下来。** 采样、词表、配图、风格、去 AI 味日志都用同一个
   sqlite 连接。宿主重载插件会先调 terminate()，我们那时把库关掉（这是对的），
   但老实例有时还在继续收消息 —— 存下来的连接于是变成「已关闭的数据库」，
   之后所有写入静默失败。现在存的是「从哪儿拿连接」，每次用之前现取。

2. **建表不能只在构造时跑一次。** 那一刻连接要是还没准备好（启动时被旧实例
   锁着、首次打开失败），表就永远不建了：之后每次写入都是「no such table」，
   曲线一路空白，而错误还被 _run 吞掉 —— 面板上什么都看不出来。
   现在挂到 conn 属性上：谁取连接，谁顺手把表建好。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable


class ConnectionSource:
    """把「连接本身」换成「连接的来源」，并保证表一定建过。"""

    _source: Any = None
    #: 各仓库把自己的建表语句填在这里
    _schema: str = ""
    _schema_ready: bool = False
    _schema_error: str = ""

    @property
    def conn(self) -> sqlite3.Connection | None:
        conn = self._resolve()
        if conn is not None and self._schema and not self._schema_ready:
            self._build_schema(conn)
        return conn

    def _resolve(self) -> sqlite3.Connection | None:
        source = self._source
        if source is None or isinstance(source, sqlite3.Connection):
            return source
        # 传进来的是仓库（例如 MemoryStore）：它的 connection 是属性，
        # 库被关掉之后会自动重开，这里每次都现取一遍。
        return getattr(source, "connection", None)

    def reset_connection(self) -> bool:
        """要求来源把连接重开一次 —— 谁关的都行。"""
        reset = getattr(self._source, "reset_connection", None)
        if not callable(reset):
            return False
        try:
            reset()
        except Exception:  # noqa: BLE001
            return False
        return True

    def execute(
        self, sql: str, params: Iterable[Any] = ()
    ) -> sqlite3.Cursor | None:
        """跑一条 SQL。连接要是已经被谁关了，重开一次再跑。

        光靠「_conn is None 就重开」不够：连接可能被**别处直接 close()**，
        而缓存里那个对象还在 —— 于是每次都写进一个已死的库，只留一行
        「Cannot operate on a closed database」的 warning。"""
        for attempt in (0, 1):
            conn = self.conn
            if conn is None:
                return None
            try:
                cursor = conn.execute(sql, tuple(params))
                conn.commit()
                return cursor
            except sqlite3.ProgrammingError as exc:
                if attempt or "closed" not in str(exc).lower():
                    raise
                if not self.reset_connection():
                    raise
        return None

    @conn.setter
    def conn(self, value: Any) -> None:
        # 测试里会直接把它设成 None 来模拟「库不可用」，所以留个 setter。
        self._source = value

    def _build_schema(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.executescript(self._schema)
            conn.commit()
        except sqlite3.Error as exc:
            # 建不起来就先记着，下一次取连接再试 —— 启动时的一次失败
            # 不该让这张表永远不存在。
            self._schema_error = f"{type(exc).__name__}: {exc}"
            return False
        self._schema_ready = True
        self._schema_error = ""
        return True

    def ensure_schema(self) -> bool:
        """表建好了吗（取一次连接就会顺手建）。"""
        if self._schema_ready:
            return True
        return self.conn is not None and self._schema_ready


__all__ = ["ConnectionSource"]

