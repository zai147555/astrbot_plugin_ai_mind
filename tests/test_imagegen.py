
"""生图：适配器、尺寸、请求体、回应解析、自愈与降级。"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import imagegen as ig

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


def settings(**over) -> ig.ImageGenSettings:
    base = dict(enabled=True, base_url="https://api.openai.com/v1", api_key="k",
                model="gpt-image-1")
    base.update(over)
    return ig.ImageGenSettings(**base)


class AdapterTest(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(ig.pick_adapter("https://x", "y", "dall-e-3"), "dall-e-3")

    def test_by_domain(self) -> None:
        self.assertEqual(ig.pick_adapter("https://api.siliconflow.cn/v1", ""), "siliconflow")
        self.assertEqual(ig.pick_adapter("https://dashscope.aliyuncs.com/api/v1", ""),
                         "dashscope-wanx")

    def test_by_model(self) -> None:
        self.assertEqual(ig.pick_adapter("", "gpt-image-1"), "gpt-image")
        self.assertEqual(ig.pick_adapter("", "dall-e-3"), "dall-e-3")
        self.assertEqual(ig.pick_adapter("", "flux-1.1-pro"), "siliconflow")

    def test_fallback(self) -> None:
        self.assertEqual(ig.pick_adapter("", "mystery-model"), "openai-images")

    def test_unknown_adapter_name_falls_back(self) -> None:
        self.assertEqual(ig.adapter_of("nope")["label"], "OpenAI Images 兼容")


class SizeTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(ig.parse_size("512x512"), (512, 512))
        self.assertEqual(ig.parse_size("512*512"), (512, 512))
        self.assertEqual(ig.parse_size("乱写", "768x768"), (768, 768))

    def test_snap_to_allowed(self) -> None:
        size, note = ig.adapt_size((512, 512), ig.adapter_of("gpt-image"))
        self.assertEqual(size, "1024x1024")
        self.assertIn("模型不支持", note)

    def test_exact_allowed_has_no_note(self) -> None:
        size, note = ig.adapt_size((1536, 1024), ig.adapter_of("gpt-image"))
        self.assertEqual((size, note), ("1536x1024", ""))

    def test_multiple_and_limits(self) -> None:
        adapter = ig.adapter_of("siliconflow")
        size, _note = ig.adapt_size((300, 300), adapter)
        width, height = [int(part) for part in size.split("x")]
        self.assertEqual(width % 32, 0)
        self.assertGreaterEqual(width * height, 256 * 256)

    def test_max_pixels(self) -> None:
        adapter = ig.adapter_of("siliconflow")
        size, note = ig.adapt_size((4096, 4096), adapter)
        width, height = [int(part) for part in size.split("x")]
        self.assertLessEqual(width * height, 2048 * 2048)
        self.assertIn("最大", note)

    def test_joiner_for_wanx(self) -> None:
        size, _note = ig.adapt_size((1024, 1024), ig.adapter_of("dashscope-wanx"))
        self.assertIn("*", size)


class BodyTest(unittest.TestCase):
    def test_whitelist_only(self) -> None:
        body = ig.build_body("画猫", settings(quality="high"), "1024x1024",
                             ig.adapter_of("gpt-image"))
        self.assertEqual(set(body), {"model", "prompt", "size", "n", "quality"})
        self.assertNotIn("response_format", body)

    def test_dalle_quality_mapping(self) -> None:
        body = ig.build_body("画猫", settings(model="dall-e-3", quality="high"),
                             "1024x1024", ig.adapter_of("dall-e-3"))
        self.assertEqual(body["quality"], "hd")

    def test_siliconflow_size_field(self) -> None:
        body = ig.build_body("画猫", settings(model="flux", adapter="siliconflow"),
                             "1024x1024", ig.adapter_of("siliconflow"))
        self.assertIn("image_size", body)
        self.assertNotIn("size", body)

    def test_compose_for_wanx(self) -> None:
        body = ig.build_body("画猫", settings(model="wanx-v1", adapter="dashscope-wanx"),
                             "1024*1024", ig.adapter_of("dashscope-wanx"))
        self.assertIn("input", body)
        self.assertEqual(body["input"]["prompt"], "画猫")
        self.assertIn("parameters", body)


class ExtractTest(unittest.TestCase):
    def test_b64_json(self) -> None:
        blob = base64.b64encode(PNG).decode()
        self.assertEqual(ig.extract_images({"data": [{"b64_json": blob}]}), [PNG])

    def test_data_url(self) -> None:
        blob = base64.b64encode(PNG).decode()
        self.assertEqual(ig.extract_images({"data": [{"url": "data:image/png;base64," + blob}]}),
                         [PNG])

    def test_external_url_is_kept_as_marker(self) -> None:
        got = ig.extract_images({"data": [{"url": "https://x/y.png"}]})
        self.assertEqual(got, [b"https://x/y.png"])

    def test_flat_and_nested_shapes(self) -> None:
        blob = base64.b64encode(PNG).decode()
        self.assertEqual(len(ig.extract_images({"images": [blob]})), 0, "裸字符串不认")
        self.assertEqual(ig.extract_images({"output": {"results": [{"url": "https://a"}]}}),
                         [b"https://a"])

    def test_garbage(self) -> None:
        self.assertEqual(ig.extract_images(None), [])
        self.assertEqual(ig.extract_images({"data": []}), [])


class GenerateTest(unittest.TestCase):
    def _fake(self, status=200, reply=b""):
        calls = []

        def run(method, url, *, headers=None, body=b"", timeout=0):
            calls.append({"method": method, "url": url, "body": body, "headers": headers or {}})
            return status, reply, {}

        return run, calls

    def test_missing_prompt(self) -> None:
        result = ig.generate("  ", settings(), "/tmp")
        self.assertFalse(result["ok"])
        self.assertIn("画什么", result["error"])

    def test_not_configured(self) -> None:
        result = ig.generate("猫", ig.ImageGenSettings(), "/tmp")
        self.assertFalse(result["ok"])
        self.assertIn("还没配好", result["error"])

    def test_success_saves_png(self) -> None:
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        run, calls = self._fake(200, payload)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run):
                result = ig.generate("画猫", settings(), Path(tmp))
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(len(result["images"]), 1)
            self.assertTrue(Path(result["images"][0]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("/images/generations", calls[0]["url"])
        self.assertEqual(json.loads(calls[0]["body"])["prompt"], "画猫")
        self.assertIn("Bearer k", calls[0]["headers"]["Authorization"])

    def test_http_error_is_a_sentence(self) -> None:
        run, _calls = self._fake(401, b'{"error":{"message":"bad key"}}')
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run):
                result = ig.generate("猫", settings(), Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 401", result["error"])
        self.assertIn("bad key", result["error"])

    def test_unknown_param_retries_without_it(self) -> None:
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        seen = []

        def run(method, url, *, headers=dict, body=b"", timeout=0):
            seen.append(json.loads(body))
            if len(seen) == 1:
                return 400, b'{"error":{"message":"unknown parameter: quality"}}', {}
            return 200, payload, {}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run):
                result = ig.generate("猫", settings(quality="high"), Path(tmp))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("quality", seen[0])
        self.assertNotIn("quality", seen[1])

    def test_429_retries(self) -> None:
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        calls = []

        def run(method, url, *, headers=dict, body=b"", timeout=0):
            calls.append(1)
            return (429, b"slow down", {}) if len(calls) == 1 else (200, payload, {})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run), \
                    mock.patch.object(ig.time, "sleep", lambda _s: None):
                result = ig.generate("猫", settings(), Path(tmp))
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)

    def test_no_image_in_response(self) -> None:
        run, _ = self._fake(200, b'{"data":[]}')
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run):
                result = ig.generate("猫", settings(), Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("没有图片", result["error"])

    def test_unreachable(self) -> None:
        run, _ = self._fake(0, b"connection refused")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ig, "_request", run):
                result = ig.generate("猫", settings(), Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("连不上", result["error"])


class EditTest(unittest.TestCase):
    def test_needs_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("参考图", ig.edit("改背景", settings(), Path(tmp))["error"])

    def test_missing_reference_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = ig.edit("改背景", settings(), Path(tmp), ["/nope.png"])
        self.assertIn("不存在", result["error"])

    def test_multipart_edit(self) -> None:
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        seen = {}

        def run(method, url, *, headers=dict, body=b"", timeout=0):
            seen["url"] = url
            seen["ctype"] = headers.get("Content-Type", "")
            seen["body"] = body
            return 200, payload, {}

        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "ref.png"
            ref.write_bytes(PNG)
            with mock.patch.object(ig, "_request", run):
                result = ig.edit("把背景换成灰色", settings(), Path(tmp), [ref])
            self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("/images/edits", seen["url"])
        self.assertTrue(seen["ctype"].startswith("multipart/form-data; boundary="))
        self.assertIn("把背景换成灰色".encode(), seen["body"])
        self.assertIn(PNG, seen["body"])


class MultipartTest(unittest.TestCase):
    def test_body_has_fields_and_file(self) -> None:
        body, ctype = ig.multipart_body({"model": "m", "prompt": "p"},
                                        [("image", "a.png", PNG)])
        self.assertIn(b'name="model"', body)
        self.assertIn(b"m", body)
        self.assertIn(b'filename="a.png"', body)
        self.assertIn(PNG, body)
        self.assertIn("boundary=", ctype)


class SettingsTest(unittest.TestCase):
    def test_masked_hides_key(self) -> None:
        shown = settings().masked()
        self.assertNotIn("api_key", shown)
        self.assertTrue(shown["has_key"])

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imagegen.json"
            conf = settings(size="512x512", count=2)
            self.assertTrue(ig.save_settings(path, conf))
            self.assertEqual(ig.load_settings(path).to_dict(), conf.to_dict())

    def test_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imagegen.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertFalse(ig.load_settings(path).enabled)

    def test_clamps(self) -> None:
        conf = ig.ImageGenSettings.from_dict({"count": 99, "timeout": 1})
        self.assertEqual(conf.count, 4)
        self.assertEqual(conf.timeout, 10)


class ReportTest(unittest.TestCase):
    def test_failure(self) -> None:
        self.assertIn("画不出来", ig.report({"ok": False, "error": "401"}))

    def test_success_mentions_note(self) -> None:
        text = ig.report({"ok": True, "images": ["a", "b"], "size": "1024x1024",
                          "adapter": "gpt-image", "note": "改用了 1024x1024"})
        self.assertIn("2 张", text)
        self.assertIn("gpt-image", text)
        self.assertIn("改用了", text)


if __name__ == "__main__":
    unittest.main()

