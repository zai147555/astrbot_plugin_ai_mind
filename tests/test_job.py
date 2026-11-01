
"""后台任务表。"""
from __future__ import annotations

import time
import unittest

from mind import job as job_mod


class JobTest(unittest.TestCase):
    def test_lifecycle(self) -> None:
        job = job_mod.Job("刷一轮")
        self.assertEqual(job.status, "running")
        self.assertNotIn("result", job.snapshot())
        job.done({"ok": True})
        snap = job.snapshot()
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["result"], {"ok": True})
        self.assertGreaterEqual(snap["elapsed"], 0.0)

    def test_fail(self) -> None:
        job = job_mod.Job("x")
        job.fail("网络不通")
        snap = job.snapshot()
        self.assertEqual(snap["status"], "error")
        self.assertIn("网络不通", snap["error"])
        self.assertNotIn("result", snap)

    def test_registry_prunes(self) -> None:
        reg = job_mod.JobRegistry(keep=3)
        ids = [reg.create("j").id for _ in range(6)]
        self.assertEqual(reg.stats()["total"], 3)
        self.assertIsNone(reg.get(ids[0]))
        self.assertIsNotNone(reg.get(ids[-1]))

    def test_get_unknown(self) -> None:
        self.assertIsNone(job_mod.JobRegistry().get("nope"))


if __name__ == "__main__":
    unittest.main()

