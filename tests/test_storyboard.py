
"""分镜引擎：规划算术 + ffmpeg 命令形状 + 失败也要说人话。"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import storyboard as sb


class PlanMathTest(unittest.TestCase):
    def test_cells_get_smaller_as_count_grows(self) -> None:
        big = sb.cell_pixels(4)
        small = sb.cell_pixels(16)
        self.assertGreater(big[0], small[0])
        self.assertGreater(big[1], small[1])
        self.assertAlmostEqual(big[0] / big[1], 16 / 9, places=1)

    def test_need_tiers_are_ordered(self) -> None:
        shot = sb.max_cells_for_need("shot")
        scene = sb.max_cells_for_need("scene")
        text = sb.max_cells_for_need("text")
        self.assertGreater(shot, scene)
        self.assertGreater(scene, text)
        self.assertGreaterEqual(text, 1)

    def test_unknown_need_falls_back_to_default(self) -> None:
        self.assertEqual(sb.max_cells_for_need("nonsense"), sb.max_cells_for_need(sb.DEFAULT_NEED))

    def test_scene_fits_a_dozen_cells(self) -> None:
        cells = sb.max_cells_for_need("scene")
        width, height = sb.cell_pixels(cells)
        self.assertGreaterEqual(width, 320)
        self.assertGreaterEqual(height, 180)
        # 再多一格就低于门槛（门槛按 6% 容差卡，见 CELL_FIT_SLACK）
        worse_w, worse_h = sb.cell_pixels(cells + 1)
        self.assertTrue(worse_w * sb.CELL_FIT_SLACK < 320
                        or worse_h * sb.CELL_FIT_SLACK < 180)

    def test_columns_cover_all_cells(self) -> None:
        for cells in (1, 2, 3, 4, 9, 16):
            cols = sb.columns_for(cells)
            rows = int(-(-cells // cols))
            self.assertGreaterEqual(cols * rows, cells)

    def test_timecode_format(self) -> None:
        self.assertEqual(sb.format_tc(0), "00:00.00")
        self.assertEqual(sb.format_tc(90), "01:30.00")
        self.assertEqual(sb.format_tc(65.5), "01:05.50")
        self.assertEqual(sb.format_tc(-5), "00:00.00")


class PlanTest(unittest.TestCase):
    def test_short_video_is_one_sheet(self) -> None:
        result = sb.plan(90)
        self.assertEqual(len(result["sheets"]), 1)
        self.assertEqual(result["cells_total"], result["per_sheet"])
        sheet = result["sheets"][0]
        times = [cell["t"] for cell in sheet["cells"]]
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(min(times), 0)
        self.assertLessEqual(max(times), 90)

    def test_cells_tile_the_whole_video(self) -> None:
        result = sb.plan(90)
        cells = result["sheets"][0]["cells"]
        self.assertAlmostEqual(cells[0]["from"], 0.0, places=2)
        self.assertAlmostEqual(cells[-1]["to"], 90.0, places=1)
        for left, right in zip(cells, cells[1:]):
            self.assertAlmostEqual(left["to"], right["from"], places=1)

    def test_long_video_gets_several_sheets(self) -> None:
        result = sb.plan(660)          # 11 分钟
        self.assertGreaterEqual(len(result["sheets"]), 3)
        self.assertEqual(result["cells_total"],
                         result["per_sheet"] * len(result["sheets"]))
        self.assertAlmostEqual(result["sheets"][-1]["to"], 660, places=1)

    def test_absurd_duration_is_fine(self) -> None:
        result = sb.plan(36000)
        self.assertLessEqual(len(result["sheets"]), sb.MAX_SHEETS)
        self.assertGreater(result["cells_total"], 0)

    def test_tokens_are_capped(self) -> None:
        self.assertLessEqual(sb.image_tokens(1), sb.IMAGE_TOKEN_CAP)
        self.assertLessEqual(sb.image_tokens(100), sb.IMAGE_TOKEN_CAP)

    def test_summary_is_readable(self) -> None:
        text = sb.summarize(sb.plan(90), 90)
        self.assertIn("01:30.00", text)
        self.assertIn("张图", text)


class ManifestTest(unittest.TestCase):
    def _result(self) -> dict:
        plan = sb.plan(660)
        return {
            "info": {"name": "a.mp4", "duration": 660, "width": 1920, "height": 1080, "fps": 30},
            "plan": plan,
            "sheets": [{"i": sheet["i"], "from": sheet["from"],
                        "to": sheet["to"], "cells": sheet["cells"]}
                       for sheet in plan["sheets"]],
        }

    def test_manifest_lists_every_cell(self) -> None:
        result = self._result()
        text = sb.build_manifest(result)
        self.assertIn("分镜：a.mp4", text)
        self.assertIn("1920x1080", text)
        self.assertIn("第 1 格", text)
        self.assertEqual(text.count("第 1 张"), 1)
        self.assertGreaterEqual(text.count("格 0"), 1)

    def test_manifest_has_one_header_per_sheet(self) -> None:
        result = self._result()
        text = sb.build_manifest(result)
        for sheet in result["sheets"]:
            self.assertIn("第 %d 张" % sheet["i"], text)


class ToolsTest(unittest.TestCase):
    def test_hints_are_used_when_they_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg.exe"
            fake.write_text("x", encoding="utf-8")
            tools = sb.find_tools(str(fake), str(fake))
            self.assertTrue(tools["ok"])
            self.assertEqual(tools["ffmpeg"], str(fake))

    def test_missing_tools_give_an_install_hint(self) -> None:
        with mock.patch.dict("os.environ", {"PATH": "", "FFMPEG_PATH": "", "FFPROBE_PATH": ""}):
            tools = sb.find_tools("", "")
        self.assertFalse(tools["ok"])
        self.assertIn("ffmpeg", tools["hint"])

    def test_selfcheck_shape(self) -> None:
        report = sb.selfcheck()
        self.assertIn("ok", report)
        self.assertIn("hint", report)
        self.assertIn("version", report)


class BuildFailureTest(unittest.TestCase):
    def test_missing_file(self) -> None:
        result = sb.build("/definitely/not/here.mp4")
        self.assertFalse(result["ok"])
        self.assertIn("文件不存在", result["error"])

    def test_no_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.mp4"
            video.write_bytes(b"not a real video")
            with mock.patch.object(sb, "find_tools", return_value={
                "ffmpeg": "", "ffprobe": "", "ok": False, "hint": "装 ffmpeg",
            }):
                result = sb.build(video)
        self.assertFalse(result["ok"])
        self.assertIn("ffmpeg", result["error"])
        self.assertIn("装 ffmpeg", result["hint"])

    def test_probe_failure_becomes_a_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.mp4"
            video.write_bytes(b"x")
            with mock.patch.object(sb, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(sb, "probe_video", side_effect=ValueError("坏文件")):
                result = sb.build(video)
        self.assertFalse(result["ok"])
        self.assertIn("坏文件", result["error"])


class CommandShapeTest(unittest.TestCase):
    def test_tile_builds_a_grid(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, timeout=0):
            seen.append(list(cmd))
            out = cmd[-1]
            Path(out).write_bytes(b"png")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            frames = []
            for index in range(9):
                frame = Path(tmp) / ("f%d.png" % index)
                frame.write_bytes(b"x")
                frames.append(str(frame))
            out = Path(tmp) / "sheet.png"
            with mock.patch.object(sb, "_run", fake_run):
                ok = sb.tile(frames, 3, 3, out, 320, 180, {"ffmpeg": "ffmpeg", "ok": True})
        self.assertTrue(ok)
        cmd = seen[-1]
        self.assertIn("xstack=inputs=9", " ".join(cmd))
        joined = " ".join(cmd)
        layout = joined.split("layout=", 1)[1].split(":fill=", 1)[0]
        self.assertEqual(len(layout.split("|")), 9)
        self.assertIn("0_0", layout)
        self.assertIn("640_360", layout)   # 第 3 列第 3 行

    def test_extract_frame_reports_failure(self) -> None:
        with mock.patch.object(sb, "_run", lambda cmd, timeout=0: (1, "boom")):
            ok = sb.extract_frame("a.mp4", 1.0, "/tmp/x.png", 320, 180, {"ffmpeg": "ffmpeg", "ok": True})
        self.assertFalse(ok)


class BuildOkTest(unittest.TestCase):
    def test_build_orchestrates_sheets(self) -> None:
        info = {"path": "a.mp4", "name": "a.mp4", "duration": 660,
                "width": 1280, "height": 720, "fps": 30.0}

        def fake_extract(path, at, out, w, h, tools=None, **kwargs):
            Path(out).write_bytes(b"png")
            return True

        def fake_tile(frames, cols, rows, out, w, h, tools=None):
            Path(out).write_bytes(b"png")
            return True

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.mp4"
            video.write_bytes(b"x")
            with mock.patch.object(sb, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(sb, "probe_video", return_value=info), \
                    mock.patch.object(sb, "extract_frame", fake_extract), \
                    mock.patch.object(sb, "tile", fake_tile):
                result = sb.build(video, "scene", workdir=Path(tmp) / "work")
            self.assertTrue(result["ok"], result.get("error"))
            self.assertGreaterEqual(len(result["sheets"]), 3)
            for sheet in result["sheets"]:
                self.assertTrue(Path(sheet["image"]).exists())
                self.assertTrue(sheet["cells"])
            self.assertIn("第 1 格", result["manifest"])
            self.assertIn("张图", result["summary"])




class AnnotateTest(unittest.TestCase):
    def test_font_hint_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            font = Path(tmp) / "f.ttf"
            font.write_bytes(b"x")
            self.assertEqual(sb.find_font(str(font)), str(font))

    def test_escapes_filter_specials(self) -> None:
        self.assertIn(":", sb.filter_escape_path("C:/a/b.ttc"))
        self.assertNotEqual(sb.filter_escape_path("C:/a/b.ttc"), "C:/a/b.ttc")
        self.assertIn("%", sb.filter_escape_text("100%"))
        self.assertNotEqual(sb.filter_escape_text("100%"), "100%")

    def test_label_has_number_and_timecode(self) -> None:
        spec = sb.label_filter(3, 65.5, 341, 192, "C:/f.ttc")
        self.assertIn("drawtext=", spec)
        self.assertIn("fontsize=", spec)
        self.assertIn("box=1", spec)
        self.assertIn("3", spec)
        self.assertIn("01", spec)


class CacheTest(unittest.TestCase):
    def test_key_is_stable_and_need_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.mp4"
            video.write_bytes(b"x")
            first = sb.cache_key(video, "scene", sb.PIXEL_BUDGET)
            self.assertEqual(first, sb.cache_key(video, "scene", sb.PIXEL_BUDGET))
            self.assertNotEqual(first, sb.cache_key(video, "text", sb.PIXEL_BUDGET))

    def test_cache_roundtrip_and_missing_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            image = work / "s.png"
            image.write_bytes(b"x")
            result = {"info": {"path": "a"}, "plan": {}, "sheets": [{"image": str(image)}],
                      "summary": "s", "cached": False}
            self.assertTrue(sb.save_cache(work, "k1", result))
            hit = sb.load_cache(work, "k1")
            self.assertIsNotNone(hit)
            self.assertTrue(hit["cached"])
            image.unlink()
            self.assertIsNone(sb.load_cache(work, "k1"))

    def test_build_uses_cache_and_skips_probe(self) -> None:
        info = {"path": "a.mp4", "name": "a.mp4", "duration": 90,
                "width": 640, "height": 360, "fps": 30.0}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.mp4"
            video.write_bytes(b"x")
            work = Path(tmp) / "work"
            cache = Path(tmp) / "cache"

            def fake_extract(path, at, out, w, h, tools=None, **kwargs):
                Path(out).write_bytes(b"png")
                return True

            def fake_tile(frames, cols, rows, out, w, h, tools=None):
                Path(out).write_bytes(b"png")
                return True

            with mock.patch.object(sb, "find_tools", return_value={
                "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "ok": True, "hint": "",
            }), mock.patch.object(sb, "probe_video", return_value=info), \
                    mock.patch.object(sb, "extract_frame", fake_extract), \
                    mock.patch.object(sb, "tile", fake_tile), \
                    mock.patch.object(sb, "audio_envelope", return_value=False):
                first = sb.build(video, "scene", workdir=work, cache_dir=cache)
                self.assertTrue(first["ok"], first.get("error"))
                self.assertFalse(first["cached"])
                with mock.patch.object(sb, "probe_video",
                                       side_effect=AssertionError("不该再读元信息")):
                    second = sb.build(video, "scene", workdir=work, cache_dir=cache)
            self.assertTrue(second["ok"])
            self.assertTrue(second["cached"])
            self.assertIn("第 1 格", second["manifest"])


class TranscriptTest(unittest.TestCase):
    def test_parse_srt(self) -> None:
        rows = sb.parse_srt("1" + chr(10) + "00:00:01,000 --> 00:00:03,500" + chr(10)
                            + "第一句" + chr(10) + chr(10) + "2" + chr(10)
                            + "00:01:04,000 --> 00:01:06,000" + chr(10) + "第二句" + chr(10))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "第一句")
        self.assertAlmostEqual(rows[1]["from"], 64.0)

    def test_parse_srt_garbage(self) -> None:
        self.assertEqual(sb.parse_srt(""), [])
        self.assertEqual(sb.parse_srt("没有时间轴的一段话"), [])

    def test_attach_maps_lines_to_cells(self) -> None:
        plan = sb.plan(90)
        attached = sb.attach_transcript(plan, [
            {"from": 0, "to": 9.5, "text": "开头"},        # 落在第 1 格（0–10）
            {"from": 10.5, "to": 19.5, "text": "第二段"},  # 落在第 2 格（10–20）
        ])
        self.assertEqual(attached, 2)
        texts = [cell.get("text") for cell in plan["sheets"][0]["cells"]]
        self.assertEqual(texts[0], "开头")
        self.assertEqual(texts[1], "第二段")
        self.assertIsNone(texts[2])

    def test_manifest_shows_transcript(self) -> None:
        plan = sb.plan(90)
        sb.attach_transcript(plan, [{"from": 0, "to": 11, "text": "他说了这句"}])
        result = {"info": {"name": "a.mp4", "duration": 90},
                  "plan": plan,
                  "sheets": [{"i": 1, "from": 0, "to": 90, "cells": plan["sheets"][0]["cells"]}]}
        self.assertIn("他说：他说了这句", sb.build_manifest(result))


class EvidenceTest(unittest.TestCase):
    def test_evidence_shape(self) -> None:
        plan = sb.plan(90)
        sheet = {
            "i": 1, "from": 0, "to": 90,
            "cells": [dict(cell, text="台词") for cell in plan["sheets"][0]["cells"]],
        }
        evidence = sb.build_evidence({
            "info": {"path": "a.mp4", "duration": 90, "width": 640, "height": 360},
            "sheets": [sheet],
        })
        self.assertEqual(evidence["version"], 1)
        self.assertEqual(evidence["cells_total"], len(sheet["cells"]))
        first = evidence["cells"][0]
        self.assertEqual(first["cell"], 1)
        self.assertEqual(first["chars"], 2)
        self.assertIn("frame_at", first)


class ZoomTest(unittest.TestCase):
    def test_unknown_cell(self) -> None:
        with mock.patch.object(sb, "find_tools", return_value={"ffmpeg": "f", "ok": True}):
            result = sb.zoom("a.mp4", sb.plan(90), 999)
        self.assertFalse(result["ok"])
        self.assertIn("没有第", result["error"])

    def test_zoom_builds_a_strip(self) -> None:
        def fake_extract(path, at, out, w, h, tools=None, **kwargs):
            Path(out).write_bytes(b"png")
            return True

        def fake_tile(frames, cols, rows, out, w, h, tools=None):
            Path(out).write_bytes(b"png")
            return True

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sb, "find_tools", return_value={"ffmpeg": "f", "ok": True}), \
                    mock.patch.object(sb, "extract_frame", fake_extract), \
                    mock.patch.object(sb, "tile", fake_tile):
                result = sb.zoom("a.mp4", sb.plan(90), 1, workdir=Path(tmp))
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(result["cell"], 1)
            self.assertTrue(Path(result["image"]).exists())
            self.assertIn("第 1 格放大", result["lines"][0])

    def test_no_frames_is_an_error(self) -> None:
        with mock.patch.object(sb, "find_tools", return_value={"ffmpeg": "f", "ok": True}), \
                mock.patch.object(sb, "extract_frame", lambda *a, **k: False):
            result = sb.zoom("a.mp4", sb.plan(90), 1)
        self.assertFalse(result["ok"])


class AudioBarTest(unittest.TestCase):
    def test_envelope_and_compose_run_ffmpeg(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, timeout=0):
            seen.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"png")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "w.png"
            with mock.patch.object(sb, "_run", fake_run):
                self.assertTrue(sb.audio_envelope("a.mp4", out, 320, 48,
                                                 {"ffmpeg": "ffmpeg", "ok": True}))
                self.assertIn("showwavespic", " ".join(seen[-1]))
                top = Path(tmp) / "t.png"
                top.write_bytes(b"x")
                self.assertTrue(sb.compose_vertical(top, out, Path(tmp) / "m.png",
                                                   {"ffmpeg": "ffmpeg", "ok": True}))
                self.assertIn("vstack=inputs=2", " ".join(seen[-1]))




class AsrTest(unittest.TestCase):
    def test_no_command(self) -> None:
        self.assertEqual(sb.transcribe("a.mp4", ""), [])

    def test_runs_command_and_parses_srt(self) -> None:
        captured: list[str] = []

        def fake_run(command, timeout=0):
            captured.append(command)
            out = command.split()[-1].strip(chr(34))
            path = pathlib.Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix != ".srt":
                path = path.with_suffix(".srt")
            path.write_text(
                "1" + chr(10) + "00:00:01,000 --> 00:00:03,000" + chr(10) + "第一句" + chr(10),
                encoding="utf-8",
            )
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sb, "_run_command", fake_run):
                rows = sb.transcribe(
                    "a.mp4", "echo {input} > {output}", workdir=Path(tmp)
                )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "第一句")
        self.assertIn("a.mp4", captured[0])

    def test_command_failure_is_empty(self) -> None:
        with mock.patch.object(sb, "_run_command", lambda command, timeout=0: (1, "boom")):
            self.assertEqual(sb.transcribe("a.mp4", "x {input} {output}"), [])

if __name__ == "__main__":
    unittest.main()
