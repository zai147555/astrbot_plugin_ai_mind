
"""ffmpeg 内置：找、验、装（全部打桩，不真下载）。"""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mind import ffmpeg_setup as fs


class LocateTest(unittest.TestCase):
    def test_settings_hint_wins(self) -> None:
        suffix = ".exe" if fs.current_platform() == "windows" else ""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / ("ffmpeg" + suffix)
            exe.write_bytes(b"x")
            (Path(tmp) / ("ffprobe" + suffix)).write_bytes(b"x")
            found = fs.locate(Path(tmp) / "data", str(exe))
        self.assertTrue(found["ok"])
        self.assertEqual(found["source"], "settings")
        self.assertTrue(found["ffprobe"].endswith("ffprobe" + suffix))

    def test_bundled_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = fs.bin_dir(tmp)
            folder.mkdir(parents=True)
            exe = folder / ("ffmpeg.exe" if fs.current_platform() == "windows" else "ffmpeg")
            exe.write_bytes(b"x")
            found = fs.locate(tmp)
        self.assertTrue(found["ok"])
        self.assertEqual(found["source"], "bundled")

    def test_nothing_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fs.shutil, "which", return_value=""), \
                    mock.patch.object(fs, "_extra_lookup_paths", return_value=[]):
                found = fs.locate(tmp)
        self.assertFalse(found["ok"])

    def test_verify_runs_binary(self) -> None:
        class Done:
            returncode = 0
            stdout = b"ffmpeg version 7.1\nbuilt with gcc\n"
            stderr = b""

        with mock.patch.object(fs.subprocess, "run", return_value=Done()):
            ok, detail = fs.verify("ffmpeg")
        self.assertTrue(ok)
        self.assertIn("ffmpeg version", detail)

    def test_verify_failure(self) -> None:
        class Done:
            returncode = 1
            stdout = b""
            stderr = b"boom"

        with mock.patch.object(fs.subprocess, "run", return_value=Done()):
            ok, detail = fs.verify("ffmpeg")
        self.assertFalse(ok)
        self.assertIn("boom", detail)


class InstallTest(unittest.TestCase):
    def _make_zip(self, target: Path, names=("ffmpeg.exe", "ffprobe.exe")) -> None:
        with zipfile.ZipFile(target, "w") as bundle:
            for name in names:
                bundle.writestr("ffmpeg-7.1/bin/" + name, b"binary")

    def test_install_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"

            def fake_download(url, target, timeout=0):
                self._make_zip(Path(target))
                return ""

            with mock.patch.object(fs, "_download", fake_download), \
                    mock.patch.object(fs, "verify", return_value=(True, "ffmpeg version 7.1")):
                result = fs.install(data)
            self.assertTrue(result["ok"], result.get("message"))
            self.assertTrue((fs.bin_dir(data) / "ffmpeg.exe").is_file() or
                            (fs.bin_dir(data) / "ffmpeg").is_file())
            self.assertIn("装好了", result["message"])

    def test_all_mirrors_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fs, "_download", lambda url, target, timeout=0: "连不上"):
                result = fs.install(Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("手动", result["message"])

    def test_archive_without_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def fake_download(url, target, timeout=0):
                with zipfile.ZipFile(target, "w") as bundle:
                    bundle.writestr("readme.txt", b"hi")
                return ""

            with mock.patch.object(fs, "_download", fake_download):
                result = fs.install(Path(tmp))
        self.assertFalse(result["ok"])

    def test_status_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = fs.bin_dir(tmp)
            folder.mkdir(parents=True)
            exe = folder / ("ffmpeg.exe" if fs.current_platform() == "windows" else "ffmpeg")
            exe.write_bytes(b"x")
            with mock.patch.object(fs, "verify", return_value=(True, "ffmpeg version 7.1")):
                report = fs.status(tmp)
        self.assertTrue(report["ok"])
        self.assertTrue(report["bundled"])
        self.assertEqual(len(report["version"]), len("ffmpeg version 7.1"))


if __name__ == "__main__":
    unittest.main()

