
"""动图逐帧：采样、时间码、拼图、降级。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import animation


class PickTest(unittest.TestCase):
    def test_even_picks_include_ends(self) -> None:
        frames = [str(index) for index in range(100)]
        picked = animation.pick_evenly(frames, 8)
        self.assertEqual(len(picked), 8)
        self.assertEqual(picked[0], "0")
        self.assertEqual(picked[-1], "99")

    def test_fewer_frames_than_requested(self) -> None:
        self.assertEqual(animation.pick_evenly(["a", "b"], 8), ["a", "b"])

    def test_empty(self) -> None:
        self.assertEqual(animation.pick_evenly([], 8), [])

    def test_single(self) -> None:
        self.assertEqual(animation.pick_evenly(["a", "b", "c"], 1), ["a"])


class TimeTest(unittest.TestCase):
    def test_from_fps(self) -> None:
        self.assertAlmostEqual(animation.frame_time(10, 20.0, 100, 5.0), 0.5)

    def test_from_duration_when_no_fps(self) -> None:
        self.assertAlmostEqual(animation.frame_time(50, 0.0, 100, 5.0), 2.5)

    def test_zero(self) -> None:
        self.assertEqual(animation.frame_time(3, 0.0, 0, 0.0), 0.0)


class ManifestTest(unittest.TestCase):
    def test_shape(self) -> None:
        info = {"name": "a.gif", "frames": 24, "width": 320, "height": 180,
                "codec": "gif", "duration": 1.2}
        rows = [{"i": 1, "t": 0.0, "source_index": 0},
                {"i": 2, "t": 0.6, "source_index": 12}]
        text = animation.build_manifest(info, rows)
        self.assertIn("a.gif", text)
        self.assertIn("帧数 24", text)
        self.assertIn("第 1 帧 00:00.00", text)
        self.assertIn("原片第 12 帧", text)


class ProbeTest(unittest.TestCase):
    def test_not_ffmpeg(self) -> None:
        with mock.patch.object(animation, "find_tools", return_value={"ok": False, "hint": "装"}):
            with self.assertRaises(ValueError):
                animation.probe_animation("a.gif")


class BuildTest(unittest.TestCase):
    def _tools(self):
        return {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": ""}

    def test_missing_file(self) -> None:
        result = animation.build("/nope.gif")
        self.assertFalse(result["ok"])
        self.assertIn("文件不存在", result["error"])

    def test_no_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gif = Path(tmp) / "a.gif"
            gif.write_bytes(b"x")
            with mock.patch.object(animation, "find_tools", return_value={"ok": False, "hint": "装"}):
                result = animation.build(gif)
        self.assertFalse(result["ok"])
        self.assertIn("ffmpeg", result["error"])

    def test_static_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gif = Path(tmp) / "a.png"
            gif.write_bytes(b"x")
            with mock.patch.object(animation, "find_tools", return_value=self._tools()), \
                    mock.patch.object(animation, "probe_animation",
                                      return_value={"frames": 1, "fps": 0, "duration": 0}):
                result = animation.build(gif)
        self.assertFalse(result["ok"])
        self.assertIn("不是动图", result["error"])

    def test_build_makes_a_strip(self) -> None:
        info = {"name": "a.gif", "frames": 24, "fps": 20.0, "duration": 1.2,
                "width": 320, "height": 180, "codec": "gif"}

        def fake_extract(path, out_dir, tools=None, limit=0):
            folder = Path(out_dir)
            folder.mkdir(parents=True, exist_ok=True)
            made = []
            for index in range(24):
                item = folder / ("f%04d.png" % index)
                item.write_bytes(b"x")
                made.append(str(item))
            return made

        def fake_run(cmd, timeout=0):
            Path(cmd[-1]).write_bytes(b"png")
            return 0, ""

        def fake_tile(frames, cols, rows, out, w, h, tools=None):
            Path(out).write_bytes(b"png")
            return True

        with tempfile.TemporaryDirectory() as tmp:
            gif = Path(tmp) / "a.gif"
            gif.write_bytes(b"x")
            with mock.patch.object(animation, "find_tools", return_value=self._tools()), \
                    mock.patch.object(animation, "probe_animation", return_value=info), \
                    mock.patch.object(animation, "extract_frames", fake_extract), \
                    mock.patch.object(animation, "_run", fake_run), \
                    mock.patch.object(animation, "tile", fake_tile):
                result = animation.build(gif, count=6, workdir=Path(tmp) / "w")
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(len(result["frames"]), 6)
            self.assertTrue(Path(result["image"]).exists())
            self.assertIn("挑了 6 帧", result["summary"])
            self.assertIn("第 1 帧", result["manifest"])
            self.assertGreater(result["frames"][1]["t"], 0)

    def test_no_frames_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gif = Path(tmp) / "a.gif"
            gif.write_bytes(b"x")
            with mock.patch.object(animation, "find_tools", return_value=self._tools()), \
                    mock.patch.object(animation, "probe_animation",
                                      return_value={"frames": 10, "fps": 10, "duration": 1}), \
                    mock.patch.object(animation, "extract_frames", return_value=[]):
                result = animation.build(gif)
        self.assertFalse(result["ok"])
        self.assertIn("一帧都没解出来", result["error"])


class ExtractCommandTest(unittest.TestCase):
    def test_command_shape(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, timeout=0):
            seen.append(list(cmd))
            folder = Path(cmd[-1]).parent
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "f0001.png").write_bytes(b"x")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(animation, "_run", fake_run):
                frames = animation.extract_frames(
                    "a.gif", Path(tmp), {"ffmpeg": "f", "ok": True}, limit=5
                )
        self.assertEqual(len(frames), 1)
        self.assertIn("-vsync", seen[-1])
        self.assertIn("5", seen[-1])


if __name__ == "__main__":
    unittest.main()

