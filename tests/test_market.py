
"""需求先搜市场：意图识别、检索词、渲染、目录客户端。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mind import market


class IntentTest(unittest.TestCase):
    def test_requirement_phrases(self) -> None:
        for text in (
            "能不能做个记忆插件",
            "帮我加一个语音功能",
            "有没有支持发邮件的插件",
            "接入一下图片识别功能",
        ):
            self.assertTrue(market.looks_like_requirement(text), text)

    def test_casual_chat_is_not_a_requirement(self) -> None:
        for text in (
            "今天好累",
            "有没有吃饭",
            "你在干嘛",
            "这个功能是什么",   # 有「功能」但没有动词
        ):
            self.assertFalse(market.looks_like_requirement(text), text)

    def test_too_short(self) -> None:
        self.assertFalse(market.looks_like_requirement("加"))

    def test_keywords_drop_politeness(self) -> None:
        words = market.keywords_of("能不能帮我加一个图片识别的插件")
        self.assertIn("图片识别", words)
        self.assertNotIn("能不能", words)
        self.assertNotIn("插件", words)


class SettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        conf = market.MarketSettings()
        self.assertTrue(conf.enabled)
        self.assertTrue(conf.auto)
        self.assertTrue(conf.owner_only)
        self.assertEqual(conf.base_url, market.DEFAULT_BASE)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.json"
            conf = market.MarketSettings(limit=3, owner_only=False)
            self.assertTrue(market.save_settings(path, conf))
            self.assertEqual(market.load_settings(path).to_dict(), conf.to_dict())

    def test_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertTrue(market.load_settings(path).enabled)

    def test_clamps(self) -> None:
        conf = market.MarketSettings.from_dict({"limit": 999})
        self.assertEqual(conf.limit, 20)


class HistoryTest(unittest.TestCase):
    def test_keeps_recent_and_roundtrips(self) -> None:
        history = market.MarketHistory(keep=3)
        for index in range(5):
            history.add("词%d" % index, [{"fullName": "a/b", "summary": "s"}])
        self.assertEqual(len(history.rows), 3)
        self.assertEqual(history.rows[0]["query"], "词4")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_history.json"
            self.assertTrue(market.save_history(path, history))
            again = market.load_history(path)
        self.assertEqual(len(again.rows), 3)
        self.assertEqual(again.rows[0]["hits"][0]["fullName"], "a/b")

    def test_broken_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(market.load_history(path).rows, [])


class NormalizeTest(unittest.TestCase):
    def test_prefers_chinese_summary(self) -> None:
        hit = market.normalize({
            "fullName": "a/b", "summary": "en", "summaryZh": "中文",
            "stars": "12", "license": "MIT", "riskFlags": ["terminal surface"],
            "inRegistry": 1, "url": "https://x",
        })
        self.assertEqual(hit["summary"], "中文")
        self.assertEqual(hit["stars"], 12)
        self.assertTrue(hit["inRegistry"])
        self.assertEqual(hit["riskFlags"], ["terminal surface"])

    def test_defaults(self) -> None:
        hit = market.normalize({})
        self.assertEqual(hit["license"], "none detected")
        self.assertEqual(hit["riskFlags"], [])


class SearchTest(unittest.TestCase):
    def _payload(self) -> dict:
        return {"total": 2, "results": [
            {"fullName": "MemTensor/MemOS#memos-local-plugin", "summaryZh": "分层记忆",
             "license": "MIT", "stars": 9, "url": "https://github.com/MemTensor/MemOS",
             "riskFlags": ["terminal surface"]},
            {"fullName": "x/y", "summary": "other", "license": "Apache-2.0"},
        ]}

    def test_search_success(self) -> None:
        seen: list[str] = []

        def fake_get(url, timeout=0):
            seen.append(url)
            return self._payload()

        with mock.patch.object(market, "_get", fake_get):
            hits, err = market.search("记忆", market.MarketSettings(limit=5))
        self.assertEqual(err, "")
        self.assertEqual(len(hits), 2)
        self.assertIn("/api/v1/plugins", seen[0])
        self.assertIn("q=", seen[0])

    def test_search_empty_query(self) -> None:
        hits, err = market.search("  ", market.MarketSettings())
        self.assertEqual(hits, [])
        self.assertIn("检索词", err)

    def test_search_failure(self) -> None:
        with mock.patch.object(market, "_get", side_effect=ValueError("市场请求失败：URLError")):
            hits, err = market.search("记忆", market.MarketSettings())
        self.assertEqual(hits, [])
        self.assertIn("失败", err)

    def test_bad_shape(self) -> None:
        with mock.patch.object(market, "_get", return_value={"results": "nope"}):
            hits, err = market.search("记忆", market.MarketSettings())
        self.assertEqual(hits, [])
        self.assertIn("格式", err)


class RenderTest(unittest.TestCase):
    hits = [
        {"fullName": "a/b", "summary": "做记忆的", "license": "MIT", "stars": 12,
         "inRegistry": True, "riskFlags": ["requires credentials"],
         "url": "https://github.com/a/b"},
        {"fullName": "c/d", "summary": "别的"},
    ]

    def test_hits_list(self) -> None:
        text = market.render_hits(self.hits)
        self.assertIn("a/b", text)
        self.assertIn("MIT", text)
        self.assertIn("12★", text)
        self.assertIn("社区登记", text)
        self.assertIn("风险：requires credentials", text)
        self.assertIn("https://github.com/a/b", text)

    def test_block_with_hits(self) -> None:
        block = market.render_block("记忆", self.hits)
        self.assertIn("<market_hits>", block)
        self.assertIn("有这些现成的", block)
        self.assertIn("不要编造", block)

    def test_block_without_hits(self) -> None:
        block = market.render_block("量子", [])
        self.assertIn("没有搜到现成的", block)
        self.assertIn("别编插件名", block)

    def test_chat(self) -> None:
        self.assertIn("搜到 2 个", market.render_chat("记忆", self.hits))
        self.assertIn("没有搜到", market.render_chat("量子", []))
        self.assertIn("失败", market.render_chat("x", [], "连不上"))


if __name__ == "__main__":
    unittest.main()

