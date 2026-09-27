"""背景图资源与面板接线的检查。

背景图现在**走插件自己的接口**返回（base64 data URI），不再依赖插件页的静态
资源链路 —— 那条链路（asset token / 路由 / 鉴权）在不同 AstrBot 版本里不一样，
而面板的数据接口是确定通的。之前用外部 bg.css + url() 的做法在真实环境里
加载不出来，页面只剩一片白。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parent.parent / "pages" / "mind"


class PanelBackgroundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
        self.images = sorted(p.name for p in (PAGE_DIR / "bg").glob("*.jpg"))

    def test_assets_exist(self) -> None:
        self.assertGreater(len(self.images), 0, "bg 目录里一张图都没有")

    def test_assets_are_reasonably_sized(self) -> None:
        """图片随插件分发，而且每次打开要经接口传一遍，别太大。"""
        sizes = [(PAGE_DIR / "bg" / name).stat().st_size for name in self.images]
        self.assertLess(max(sizes), 400 * 1024, "单张背景图不该超过 400KB")
        self.assertLess(sum(sizes), 4 * 1024 * 1024, "背景图总大小不该超过 4MB")

    def test_no_external_css_dependency(self) -> None:
        """不能再依赖外部 bg.css —— 那条路在真实环境里加载不出来。"""
        self.assertFalse((PAGE_DIR / "bg.css").exists(), "bg.css 应该已经删掉了")
        self.assertNotIn('href="bg.css"', self.html)
        self.assertNotIn("bg/01.jpg", self.html, "别把图片路径写进 HTML")

    def test_background_layers_exist(self) -> None:
        self.assertIn('class="bg-layer"', self.html)
        self.assertIn('class="bg-veil"', self.html)

    def test_blur_is_adjustable(self) -> None:
        """虚化要能在面板里调 —— 图片里只烤了很轻的一层底子。"""
        self.assertIn("--bg-blur", self.html)
        self.assertIn('id="bg-blur"', self.html, "找不到虚化滑杆")
        self.assertIn("function setBgBlur", self.html)
        self.assertIn("filter: blur(var(--bg-blur", self.html)

    def test_loads_through_plugin_api(self) -> None:
        """图片必须走插件接口拿，不能再靠页面资源链路。"""
        self.assertIn('request("background", "GET"', self.html)

    def test_script_tags_balanced(self) -> None:
        """踩过坑：补丁多插了一个 <script>，把引导脚本整段变成语法错误。"""
        opens = len(re.findall(r"<script\b", self.html))
        closes = self.html.count("</script>")
        self.assertEqual(opens, closes, "script 标签数量对不上")
        self.assertNotIn(
            "<script>" + chr(10) + "<script>", self.html,
            "两个 <script> 连在一起：第二个会被当成第一个的脚本内容，整段语法错误",
        )


if __name__ == "__main__":
    unittest.main()
