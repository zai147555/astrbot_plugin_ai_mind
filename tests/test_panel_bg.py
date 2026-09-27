"""背景图资源的一致性检查。

三处数量必须对得上，否则会出现「随机到一张不存在的图」：
  1. pages/mind/bg/ 里的图片数
  2. pages/mind/bg.css 里的规则数
  3. index.html 里的 BG_COUNT
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parent.parent / "pages" / "mind"
RULE_RE = re.compile(r'body\.bg-(\d+) \.bg-layer \{ background-image: url\("bg/([^"]+)"\); \}')


class PanelBackgroundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
        self.css = (PAGE_DIR / "bg.css").read_text(encoding="utf-8")
        self.images = sorted(p.name for p in (PAGE_DIR / "bg").glob("*.jpg"))

    def test_assets_exist(self) -> None:
        self.assertGreater(len(self.images), 0, "bg 目录里一张图都没有")

    def test_rules_and_files_match(self) -> None:
        rules = RULE_RE.findall(self.css)
        self.assertEqual(len(rules), len(self.images), "bg.css 的规则数和 bg/ 里的图片数对不上")
        self.assertEqual([name for _idx, name in rules], self.images)
        self.assertEqual([int(idx) for idx, _name in rules], list(range(1, len(self.images) + 1)))

    def test_bg_count_matches(self) -> None:
        found = re.search(r"var BG_COUNT = (\d+);", self.html)
        self.assertIsNotNone(found, "index.html 里找不到 BG_COUNT")
        self.assertEqual(int(found.group(1)), len(self.images), "BG_COUNT 和实际图片数对不上")

    def test_assets_are_small(self) -> None:
        """图片是随插件一起分发的，别把原图塞进来。"""
        sizes = [(PAGE_DIR / "bg" / name).stat().st_size for name in self.images]
        self.assertLess(max(sizes), 200 * 1024, "单张背景图不该超过 200KB")
        self.assertLess(sum(sizes), 3 * 1024 * 1024, "背景图总大小不该超过 3MB")

    def test_css_is_linked_not_inlined(self) -> None:
        """必须走外部 <link>：内联 style 里的 url() 不会被宿主重写成带 token 的地址，会 404。"""
        self.assertIn('<link rel="stylesheet" href="bg.css">', self.html)
        self.assertNotIn("bg/01.jpg", self.html, "别把图片路径内联进 HTML")

    def test_background_layers_exist(self) -> None:
        self.assertIn('class="bg-layer"', self.html)
        self.assertIn('class="bg-veil"', self.html)
        self.assertIn("var BG_COUNT", self.html)
        self.assertIn("no-bg", self.html, "要有关闭背景的口子")


if __name__ == "__main__":
    unittest.main()
