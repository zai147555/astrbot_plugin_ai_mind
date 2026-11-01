
"""后台任务：给「一轮要跑很久」的操作一个可轮询的句柄。

为什么需要：面板的请求是同步 HTTP，刷一轮（拉列表 → 读字幕 → 下载 → 分镜）动辄 30~90 秒，
宿主的桥很容易先超时，前端就只看到一个 Network Error —— 实际后端还在跑。
这里把这类操作丢到后台，前端拿 job id 轮询进度/结果。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

#: 同时最多留几个任务（老的自动清掉）
KEEP = 12


class Job:
    __slots__ = ("id", "name", "status", "started", "finished", "result", "error")

    def __init__(self, name: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.name = str(name or "")
        self.status = "running"        # running | done | error
        self.started = time.time()
        self.finished = 0.0
        self.result: Any = None
        self.error = ""

    def done(self, result: Any) -> None:
        self.status = "done"
        self.result = result
        self.finished = time.time()

    def fail(self, error: str) -> None:
        self.status = "error"
        self.error = str(error or "未知错误")
        self.finished = time.time()

    @property
    def elapsed(self) -> float:
        end = self.finished or time.time()
        return round(end - self.started, 1)

    def snapshot(self, *, with_result: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id, "name": self.name, "status": self.status,
            "elapsed": self.elapsed, "error": self.error,
        }
        if with_result and self.status == "done":
            data["result"] = self.result
        return data


class JobRegistry:
    """进程内的小任务表（不落盘：重启后旧任务本来也接不上了）。"""

    def __init__(self, keep: int = KEEP) -> None:
        self._jobs: dict[str, Job] = {}
        self.keep = max(1, int(keep))

    def create(self, name: str = "") -> Job:
        job = Job(name)
        self._jobs[job.id] = job
        self._prune()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(str(job_id or ""))

    def _prune(self) -> None:
        if len(self._jobs) <= self.keep:
            return
        ordered = sorted(self._jobs.values(), key=lambda item: item.started)
        for item in ordered[: max(0, len(self._jobs) - self.keep)]:
            self._jobs.pop(item.id, None)

    def stats(self) -> dict[str, int]:
        running = sum(1 for item in self._jobs.values() if item.status == "running")
        return {"total": len(self._jobs), "running": running}


__all__ = ["KEEP", "Job", "JobRegistry"]

