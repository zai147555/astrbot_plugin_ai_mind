"""「认人」那一块的渲染。

用轻量替身而不是真的 Snapshot：这个函数只按属性取值，
替身能把「专属 / 熟人 / 陌生人 / 群里」几种组合写清楚。

背景是真实反馈：人设里写着「要叫他宝宝」，插件也标了 ★，
但注入的只有「好感度 63/100 · ★宝宝」这种仪表盘读数 ——
模型并不知道面前这个人就是宝宝，于是开口就是「报上名号来」「你是谁呀」。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from mind import RELATION_RULES_BLOCK, render_relationship_block
from mind.emotion.engine import Relation


def snapshot(
    uid: str = "1000000001",
    name: str = "他",
    count: int = 12,
    special: bool = True,
    label: str = "宝宝",
    relation: object = "default",
):
    if relation == "default":
        relation = Relation(
            uid=uid, name=name, affinity=63.0, familiarity=40.0,
            msg_count=count, special=special,
        )
    return SimpleNamespace(
        relation=relation,
        is_special=special,
        special_label=label,
        affinity_title=lambda: "很亲近",
        familiarity_title=lambda: "有点熟",
    )


class RelationshipBlockTest(unittest.TestCase):
    def test_no_relation_means_no_block(self) -> None:
        self.assertEqual(render_relationship_block(snapshot(relation=None)), "")

    def test_special_user_is_declared_as_fact(self) -> None:
        block = render_relationship_block(snapshot())
        self.assertIn("<relationship>", block)
        self.assertIn("他是你的宝宝", block)
        self.assertIn("1000000001", block)
        self.assertIn("他", block)
        self.assertIn("12", block)
        self.assertIn("别问「你是谁」", block)

    def test_style_is_left_to_the_persona(self) -> None:
        """只给身份，不许顺带规定说话方式 —— 否则就是在跟人设打架。"""
        block = render_relationship_block(snapshot())
        self.assertIn("完全按你原本的设定来", block)
        self.assertNotIn("傲娇", block)
        self.assertNotIn("表达基调", block)

    def test_group_adds_the_discretion_clause(self) -> None:
        block = render_relationship_block(snapshot(), group=True)
        self.assertIn("现在是在群里", block)
        self.assertIn("不许透露", block)

    def test_no_discretion_clause_in_private(self) -> None:
        self.assertNotIn("现在是在群里", render_relationship_block(snapshot(), group=False))

    def test_discretion_can_be_turned_off(self) -> None:
        block = render_relationship_block(snapshot(), group=True, discretion=False)
        self.assertNotIn("现在是在群里", block)
        self.assertIn("他是你的宝宝", block)

    def test_falls_back_to_a_generic_label(self) -> None:
        block = render_relationship_block(snapshot(label=""))
        self.assertIn("他是你的特别的人", block)

    def test_works_without_a_nickname(self) -> None:
        block = render_relationship_block(snapshot(name=""))
        self.assertIn("ID 1000000001", block)
        self.assertNotIn("（ID 1000000001）", block)
        self.assertIn("ID 1000000001", block)

    def test_stranger_is_not_claimed_as_known(self) -> None:
        block = render_relationship_block(snapshot(special=False, count=1), min_messages=3)
        self.assertEqual(block, "")

    def test_acquaintance_gets_a_lighter_block(self) -> None:
        block = render_relationship_block(snapshot(special=False, count=5), min_messages=3)
        self.assertIn("你认识他", block)
        self.assertIn("你们聊过 5 次", block)
        self.assertNotIn("他是你的", block)

    def test_min_messages_boundary(self) -> None:
        self.assertEqual(
            render_relationship_block(snapshot(special=False, count=2), min_messages=3), ""
        )
        self.assertNotEqual(
            render_relationship_block(snapshot(special=False, count=3), min_messages=3), ""
        )

    def test_special_user_is_always_recognized(self) -> None:
        """就算只说过一句话，专属用户也必须被认出来。"""
        block = render_relationship_block(snapshot(special=True, count=1), min_messages=99)
        self.assertIn("他是你的宝宝", block)

    def test_config_schema_matches_the_code(self) -> None:
        """_conf_schema.json 里写的键和默认值，必须和代码里读的一致。

        对不上的后果很隐蔽：WebUI 上那个开关能改、能存，但插件根本不看它。
        """
        import json
        from pathlib import Path

        from mind.emotion.engine import Settings

        schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        rel = schema["emotion"]["items"]["relationship"]["items"]
        settings = Settings.from_config({})
        for key, attr in (
            ("inject_identity", "inject_identity"),
            ("identity_min_messages", "identity_min_messages"),
            ("group_discretion", "group_discretion"),
            ("special_label", "special_label"),
        ):
            self.assertIn(key, rel, "_conf_schema 缺少 relationship.%s" % key)
            self.assertIn("description", rel[key])
            self.assertEqual(
                rel[key]["default"], getattr(settings, attr),
                "relationship.%s 的默认值和代码里对不上" % key,
            )

    def test_rules_block_explains_how_to_behave(self) -> None:
        self.assertIn("<relationship_system>", RELATION_RULES_BLOCK)
        self.assertIn("别问「你是谁」", RELATION_RULES_BLOCK)
        self.assertIn("会用 <relationship> 告诉你", RELATION_RULES_BLOCK)
        self.assertIn("完全按你原本的设定来", RELATION_RULES_BLOCK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
