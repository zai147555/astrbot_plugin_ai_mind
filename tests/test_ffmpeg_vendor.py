"""插件自带 ffmpeg 的查找与兜底（无 ffprobe 也能干活）。"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind import animation as animation_engine  # noqa: E402
from mind import ffmpeg_setup, storyboard  # noqa: E402


def _fake_exe(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class VendoredLookupTest(unittest.TestCase):
    def test_vendored_dir_used_by_storyboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "vendor" / "ffmpeg" / ("windows" if os.name == "nt" else "linux")
            _fake_exe(bundle, "ffmpeg" if os.name != "nt" else "ffmpeg.exe", "#!/bin/sh\n")
            with mock.patch.object(storyboard, "__file__", str(root / "mind" / "storyboard.py")):
                tools = storyboard.find_tools()
            self.assertTrue(tools.get("ok"))
            self.assertIn("vendor", str(tools.get("ffmpeg") or ""))
            self.assertEqual(tools.get("ffprobe"), "")

    def test_locate_prefers_vendored_over_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "plugin"
            bundle = ffmpeg_setup.vendor_dir(plugin)
            name = "ffmpeg.exe" if ffmpeg_setup.current_platform() == "windows" else "ffmpeg"
            _fake_exe(bundle, name, "#!/bin/sh\necho ffmpeg version 9.9\n")
            found = ffmpeg_setup.locate(Path(tmp) / "data", "", plugin)
            self.assertTrue(found.get("ok"))
            self.assertEqual(found.get("source"), "vendored")
            self.assertEqual(Path(str(found.get("ffmpeg"))).name, name)

    def test_status_ok_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "plugin"
            name = "ffmpeg.exe" if ffmpeg_setup.current_platform() == "windows" else "ffmpeg"
            _fake_exe(ffmpeg_setup.vendor_dir(plugin), name,
                      "#!/bin/sh\necho 'ffmpeg version 9.9-test'\n")
            report = ffmpeg_setup.status(Path(tmp) / "data", "", plugin)
            self.assertTrue(report.get("ok"))
            self.assertTrue(report.get("bundled"))
            self.assertEqual(report.get("source"), "vendored")
            self.assertEqual(report.get("ffprobe"), "")

    def test_status_without_plugin_dir_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = ffmpeg_setup.status(Path(tmp) / "data")
            self.assertIn("ok", report)
            self.assertIn("source", report)


class ProbeFallbackTest(unittest.TestCase):
    def test_parse_ffmpeg_info(self):
        text = ("Input #0, mov, from 'a.mp4':\n"
                "  Duration: 00:01:30.12, start: 0.000000, bitrate: 1000 kb/s\n"
                "  Stream #0:0: Video: h264, yuv420p, 1280x720, 30 fps, 30 tbr\n")
        info = storyboard.parse_ffmpeg_info(text)
        self.assertAlmostEqual(info["duration"], 90.12, places=2)
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        self.assertAlmostEqual(info["fps"], 30.0)

    def test_parse_ffmpeg_info_empty(self):
        info = storyboard.parse_ffmpeg_info("")
        self.assertEqual(info["duration"], 0.0)
        self.assertEqual((info["width"], info["height"]), (0, 0))
        self.assertEqual(info["fps"], 0.0)

    def test_probe_video_falls_back_without_ffprobe(self):
        tools = {"ffmpeg": "/x/ffmpeg", "ffprobe": "", "ok": True}
        stderr = "Duration: 00:00:10.00, start: 0.0\nStream #0:0: Video: vp9, 640x360, 25 fps\n"
        with mock.patch.object(storyboard, "_run", return_value=(0, stderr)):
            info = storyboard.probe_video("/x/a.webm", tools)
        self.assertAlmostEqual(info["duration"], 10.0, places=2)
        self.assertEqual(info["name"], "a.webm")
        self.assertAlmostEqual(info["fps"], 25.0)

    def test_probe_video_fallback_raises_when_unreadable(self):
        tools = {"ffmpeg": "/x/ffmpeg", "ffprobe": "", "ok": True}
        with mock.patch.object(storyboard, "_run", return_value=(1, "nope")):
            with self.assertRaises(ValueError):
                storyboard.probe_video("/x/a.bin", tools)

    def test_animation_probe_falls_back_without_ffprobe(self):
        tools = {"ffmpeg": "/x/ffmpeg", "ffprobe": "", "ok": True}
        stderr = "Duration: 00:00:02.50\nStream #0:0: Video: gif, 320x240, 20 fps\n"
        with mock.patch.object(storyboard, "_run", return_value=(0, stderr)):
            info = animation_engine.probe_animation("/x/a.gif", tools)
        self.assertEqual(info["frames"], 50)
        self.assertAlmostEqual(info["fps"], 20.0)
        self.assertAlmostEqual(info["duration"], 2.5, places=2)
        self.assertEqual((info["width"], info["height"]), (320, 240))

    def test_find_tools_ok_with_ffmpeg_only(self):
        tools = storyboard.find_tools("", "")
        self.assertEqual(bool(tools.get("ok")), bool(tools.get("ffmpeg")))
        if not tools.get("ok"):
            self.assertTrue(tools.get("hint"))


if __name__ == "__main__":
    unittest.main()
