
"""听音频：解析、判词、失败降级。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import soundcheck

SAMPLE = """
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.7 LUFS

  Loudness range:
    LRA:         8.3 LU
    Threshold: -34.7 LUFS

  True peak:
    Peak:       -0.8 dBFS

[Parsed_volumedetect_0 @ 0x2] mean_volume: -18.3 dB
[Parsed_volumedetect_0 @ 0x2] max_volume: -0.6 dB
"""


class ParseTest(unittest.TestCase):
    def test_numbers(self) -> None:
        features = soundcheck.parse_loudness(SAMPLE)
        self.assertAlmostEqual(features["lufs"], -14.2)
        self.assertAlmostEqual(features["lra"], 8.3)
        self.assertAlmostEqual(features["true_peak"], -0.8)
        self.assertAlmostEqual(features["mean_db"], -18.3)
        self.assertAlmostEqual(features["max_db"], -0.6)

    def test_empty(self) -> None:
        self.assertEqual(soundcheck.parse_loudness(""), {})
        self.assertEqual(soundcheck.parse_loudness("随便一段话"), {})

    def test_takes_the_last_summary(self) -> None:
        text = "I: -20.0 LUFS\n" + SAMPLE
        self.assertAlmostEqual(soundcheck.parse_loudness(text)["lufs"], -14.2)


class VerdictTest(unittest.TestCase):
    def test_normal(self) -> None:
        text = soundcheck.verdict({"lufs": -14.0, "lra": 8.0, "true_peak": -1.5})
        self.assertIn("响度正常", text)
        self.assertIn("正常", text)

    def test_too_loud(self) -> None:
        self.assertIn("太吵", soundcheck.verdict({"lufs": -5.0}))

    def test_too_quiet(self) -> None:
        self.assertIn("很轻", soundcheck.verdict({"lufs": -26.0}))

    def test_clipping_risk(self) -> None:
        self.assertIn("削波", soundcheck.verdict({"true_peak": 0.0}))

    def test_flat_dynamics(self) -> None:
        self.assertIn("压得比较狠", soundcheck.verdict({"lra": 2.0}))

    def test_empty(self) -> None:
        self.assertEqual(soundcheck.verdict({}), "")


class AnalyzeTest(unittest.TestCase):
    def test_missing_file(self) -> None:
        result = soundcheck.analyze("/nope.mp3")
        self.assertFalse(result["ok"])
        self.assertIn("文件不存在", result["error"])

    def test_no_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            with mock.patch.object(soundcheck, "find_tools", return_value={
                "ok": False, "hint": "去装 ffmpeg",
            }):
                result = soundcheck.analyze(audio)
        self.assertFalse(result["ok"])
        self.assertIn("ffmpeg", result["error"])
        self.assertIn("装", result["hint"])

    def test_ok_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.flac"
            audio.write_bytes(b"x")
            with mock.patch.object(soundcheck, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(soundcheck, "probe_video",
                                 side_effect=ValueError("没有视频流")), \
                    mock.patch.object(soundcheck, "_run", return_value=(0, SAMPLE)):
                result = soundcheck.analyze(audio)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertAlmostEqual(result["lufs"], -14.2)
        self.assertIn("响度正常", result["verdict"])
        self.assertEqual(result["name"], "a.flac")

    def test_ffmpeg_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            with mock.patch.object(soundcheck, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(soundcheck, "probe_video",
                                 side_effect=ValueError("x")), \
                    mock.patch.object(soundcheck, "_run", return_value=(1, "不是音频")):
                result = soundcheck.analyze(audio)
        self.assertFalse(result["ok"])
        self.assertIn("不是音频", result["error"])

    def test_no_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            with mock.patch.object(soundcheck, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(soundcheck, "probe_video",
                                 side_effect=ValueError("x")), \
                    mock.patch.object(soundcheck, "_run", return_value=(0, "没数字")):
                result = soundcheck.analyze(audio)
        self.assertFalse(result["ok"])
        self.assertIn("响度", result["error"])


class ReportTest(unittest.TestCase):
    def test_report_shape(self) -> None:
        text = soundcheck.report({
            "ok": True, "name": "a.mp3", "duration": 213.0, "channels": 2,
            "sample_rate": 44100, "lufs": -14.2, "lra": 8.3, "true_peak": -0.8,
            "verdict": "响度正常。",
        })
        self.assertIn("a.mp3", text)
        self.assertIn("-14.2 LUFS", text)
        self.assertIn("44100 Hz", text)
        self.assertIn("响度正常", text)

    def test_report_failure(self) -> None:
        text = soundcheck.report({"ok": False, "error": "没找到 ffmpeg", "hint": "去装"})
        self.assertIn("听不了", text)
        self.assertIn("去装", text)


class ToolsTest(unittest.TestCase):
    def test_spectrum_command(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, timeout=0):
            seen.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"png")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s.png"
            with mock.patch.object(soundcheck, "find_tools", return_value={
                "ffmpeg": "f", "ok": True,
            }), mock.patch.object(soundcheck, "_run", fake_run):
                self.assertTrue(soundcheck.spectrum("a.mp3", out))
        self.assertIn("showspectrumpic", " ".join(seen[-1]))

    def test_no_tools(self) -> None:
        with mock.patch.object(soundcheck, "find_tools", return_value={"ok": False}):
            self.assertFalse(soundcheck.spectrum("a.mp3", "/tmp/x.png"))


if __name__ == "__main__":
    unittest.main()

