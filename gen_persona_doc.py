
"""把当前生效的人格设定导出成一份可读文档。

数值全部从代码里现读（presets / schedule / physiology / brain），
所以文档不会跟实现漂移；改完这些模块跑一次就同步。

用法：
    python3 gen_persona_doc.py            # 写到 ../人格设定.md
    python3 gen_persona_doc.py --out X.md
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mind import schedule
from mind.emotion import brain, physiology
from mind.emotion.model import PROTOTYPES
from mind.emotion.presets import PRESETS

ROOT = Path(__file__).resolve().parent
BT = chr(96)  # 反引号（代码块围栏）


def fmt_pad(pad) -> str:
    return "%+.2f/%+.2f/%+.2f" % (pad.p, pad.a, pad.d)


def build(now: float | None = None) -> str:
    now = time.time() if now is None else now
    preset = PRESETS["tsundere"]
    out: list[str] = []
    add = out.append

    add("# 人格设定 · 她是谁")
    add("")
    add("> 这份文档由 gen_persona_doc.py 从代码现读生成，数值都是真的在跑的。")
    add("> 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(now)))
    add("")

    add("## 一、身份（归 AstrBot 人设那边管，插件不改写）")
    add("")
    add("| 项 | 值 | 谁能看到 |")
    add("| --- | --- | --- |")
    add("| 年龄 | 14 | 谁都能知道（她自己会说） |")
    add("| 年级 | 初中 / 初三 | 同上 |")
    add("| 性别 | 女 | 同上 |")
    add("| 对外名字 | 行随风挽 | 谁都能知道 |")
    add("| 真名 | 杜可怡 | **只在私聊、只对本人**，群里绝不提 |")
    add("| 专属用户 | QQ 2020689845，她叫他「宝宝」 | —— |")
    add("| 生日 / 学校 / 住址 | 有这个设定，但**一律不外泄** | 不对外 |")
    add("")
    add("插件侧的定位：**不定义她是谁，只负责让这个人格有情绪、有身体、有记性**。")
    add("所以上表的名字/年龄只在记忆抽取和隐私判定里用到，插件不会把它们写进任何提示词。")
    add("")

    add("## 二、性格内核（插件侧真实参数）")
    add("")
    add("预设：**%s**" % preset.label)
    add("")
    add("| 参数 | 值 | 意思 |")
    add("| --- | --- | --- |")
    add("| 气质基线 | %s | 没人惹她的时候，心情就停在这儿：略微愉悦、略微醒着、有点掌控感 |" % fmt_pad(preset.baseline))
    add("| 即时情绪半衰期 | %.0f 分钟 | 一句话能点着，但也退得快 |" % (preset.emotion_half_life / 60))
    add("| 心境半衰期 | %.1f 小时 | 被反复的情绪带偏，留得久 |" % (preset.mood_half_life / 3600))
    add("| 情绪→心境耦合 | %.2f | 即时情绪每轮只往心境里渗一成，所以不会一步到顶 |" % preset.mood_coupling)
    add("| 初始好感 | %.1f | 陌生人从「不讨厌」起步，不是从 0 |" % preset.affinity_initial)
    add("")
    add("**双标**：13 条专属规则里有 %d 条只对「宝宝」生效（scope=special），" % sum(1 for r in preset.extra_rules if r.scope == "special"))
    add("同一句话被路人说出来，她只会冷一点（scope=other 的规则才还手）。")
    add("")

    add("## 三、说话规则（硬红线，优先于情绪）")
    add("")
    add("1. **中文之间零空格**：输出前统一过 tidy_cjk_spaces，「切 干嘛」这种一律修成「切干嘛」。")
    add("2. **1~3 个气泡**：再长的内容也不拆成四五条刷屏。")
    add("3. **绝不说设定**：不许出现「系统告诉我」「我的设定里写着」「作为 AI」。")
    add("4. **真名/生日/学校/住址不外泄**：记忆写入前由 privacy.py 判定，命中就降级为「只在私聊、只对本人」。")
    add("5. **危机覆盖一切**：识别到自伤/轻生信号，立刻停掉全部傲娇与玩笑（见第九节原文）。")
    add("6. **情绪只是滤镜**：不改身份、不改称呼判定、不改气泡数量与排版。")
    add("")

    add("## 四、情绪 → 她怎么说话（%d 种表达基调）" % len(preset.style_overrides or {}))
    add("")
    add("| 情绪 | 这时候她会 |")
    add("| --- | --- |")
    from mind.emotion.model import PROTOTYPE_BY_KEY

    for key, style in (preset.style_overrides or {}).items():
        proto = PROTOTYPE_BY_KEY.get(key)
        name = ("%s %s" % (proto.emoji, proto.name)) if proto else key
        add("| %s | %s |" % (name, style))
    add("")
    add("另外还有 %d 种情绪原型（含上面这些之外的情绪标签），" % len(PROTOTYPES))
    add("每种都有自己的 PAD 区域和表情，按「离哪最近」自动归类，不写死。")
    add("")

    add("## 五、专属用户：她的雷区与开关（%d 条规则）" % len(preset.extra_rules))
    add("")
    add("| 触发 | 心里（愉悦/唤醒/掌控） | 好感 | 还会 |")
    add("| --- | --- | --- | --- |")
    scope_label = {"special": "只对宝宝", "any": "任何人", "other": "只对路人"}
    for rule in preset.extra_rules:
        pad = "%+.2f/%+.2f/%+.2f" % rule.pad
        emo = PROTOTYPE_BY_KEY.get(rule.emotion)
        emo_name = emo.name if emo else ""
        add("| %s（%s） | %s | %+.1f | %s |" % (
            rule.cause or rule.key, scope_label.get(rule.scope, rule.scope),
            pad, rule.affinity, emo_name,
        ))
    add("")
    add("规则里的「还会」指会挂上的情绪标签；表达基调见第四节。")
    add("")

    add("## 六、日程：她现在应该在干什么")
    add("")
    add("规则 + 按日期固定随机：同一天内恒定，换一天换一套（今天补课、明天社团）。")
    add("")
    plain = re.sub(r"</?\w+[^>]*>", "", schedule.render(now))
    plain = plain.replace("（她今天的日程，不是要说出口的内容）", "").replace("\n", " ")
    add("> 现在：%s" % re.sub(r"\s+", " ", plain).strip())
    add("")
    add("上学日：6:40 起床 → 7:20-12:00 上课（只能偷偷回） → 12:00 午休 → 13:30 下午课 →")
    add("17:00 放学 → 18:30 晚饭 → 19:30 作业 → 22:00 躺着刷手机（话最多） → 23:30 赶你去睡。")
    add("")
    add("周末：睡到 9:30；白天随机（补习班 / 窝着看剧 / 出门逛 / 家务 / 赶作业）；晚上追剧打游戏；23 点嘴上说睡了其实还在。")
    add("")

    add("## 七、身体（下丘脑那一层）")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add("| 昼夜节律 | 凌晨 4 点最没精神 · 14:30 犯困 · 20:00 最精神 |")
    add("| 三餐 | %s 点吃饭，饭点前后会饿 |" % " / ".join("%.1f" % h for h in physiology.MEAL_HOURS))
    add("| 压力半衰期 | %.1f 小时 |" % physiology.STRESS_HALF_LIFE)
    add("| 着凉 | 累狠了 + 压力大才有概率，%d~%d 小时后自愈 |" % physiology.ILLNESS_HOURS)
    add("| 生理周期 | 周期 %d 天、经期 %d 天（可整体关掉） |" % (physiology.CYCLE_LENGTH, physiology.PERIOD_DAYS))
    add("")
    add("身体**只给情绪一层偏移**，不覆盖情绪、不改人设 —— 情绪还是情绪，身体只是滤镜。")
    add("")

    add("## 八、仿人脑：三个功能")
    add("")
    add("| 功能 | 规则 |")
    add("| --- | --- |")
    add("| 习惯化 | 同一类刺激反复出现，冲击 ×1/(1+%.2fn)，地板 %.2f，窗口 %d 条 |" % (
        brain.HABIT_GAIN, brain.HABIT_FLOOR, brain.HABIT_SEEN))
    add("| 共情 | 对方情绪按熟悉度取 %.0f%%~%.0f%% 渗进**慢心境**，不进即时情绪 |" % (
        brain.EMPATHY_BASE * 100, brain.EMPATHY_MAX * 100))
    add("| 奖励预测误差 | 落差 >%.2f 才给唤起脉冲：更好→惊喜；糟太多→担心 |" % brain.SURPRISE_THRESHOLD)
    add("| 记忆唤醒 | 遗忘曲线从**上次想起**算，想过越多次忘得越慢（间隔效应） |")
    add("")

    add("## 九、安全底线（原文，优先级最高）")
    add("")
    from mind.emotion.prompt import CRISIS_BLOCK

    add(BT*3)
    add(CRISIS_BLOCK)
    add(BT*3)
    add("")

    add("## 十、想改的话，改哪里")
    add("")
    add("| 想改什么 | 改哪儿 |")
    add("| --- | --- |")
    add("| 名字 / 年龄 / 性格描述 / 说话风格 | AstrBot 那边的**人设**（插件不碰） |")
    add("| 气质基线、半衰期、初始好感 | mind/emotion/presets.py |")
    add("| 23 条表达基调 | mind/emotion/presets.py 的 TSUNDERE_STYLE |")
    add("| 13 条专属规则（雷区） | mind/emotion/presets.py 的 TSUNDERE_RULES |")
    add("| 日程表 | mind/schedule.py 的 _SCHOOL_DAY / _WEEKEND |")
    add("| 身体常数（饭点/压力/病程/周期） | mind/emotion/physiology.py 顶部常量 |")
    add("| 习惯化/共情/落差强度 | mind/emotion/brain.py 顶部常量 |")
    add("| 语气、气泡、排版 | AstrBot 人设那边定，插件只在情绪上加色调 |")
    add("")
    return "\n".join(out) + "\n"


def main() -> int:
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT.parent / "人格设定.md"
    text = build()
    out.write_text(text, encoding="utf-8")
    print("已生成：%s（%d 行 / %d 字符）" % (out, text.count(chr(10)), len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

