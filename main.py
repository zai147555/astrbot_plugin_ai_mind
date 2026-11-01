"""AstrBot 心智插件 —— 情绪 + 长期记忆，外加一块可视化面板。

把原来两个插件合成了一个：

- **情绪**：PAD 三维模型，随时间自然衰减，按消息内容做规则化情绪评估，
  用傲娇预设注入"表达基调"，识别到危机信号时强制进入认真关心模式。
- **记忆**：攒批交给模型抽取长期事实，按「常驻 + 最近 + 相关」三路检索注入，
  隐私优先（敏感信息只在本人私聊出现）。

合并后新增：

- **情绪曲线**：情绪按时间采样存进 SQLite，面板上能看到它怎么涨、怎么落、
  怎么自己衰减回基线。
- **可控**：可以在面板上直接拖点改历史、设定此刻的心情、冻结/重置情绪。
- **可查**：面板上能直接看到 AI 到底抽取了哪些记忆，并增删改。
- **拟人分段**：一条回复按标点拆成几口气，按下一段的字数拟人延迟发出去；
  代码块、表格、引号里的内容整块保护，不会被切碎（参考 astrbot_plugin_splitter 的做法）。

面板页面在 `pages/mind/index.html`，由 AstrBot 自动发现；
后端接口通过 `context.register_web_api` 挂在 `/api/plug/<插件目录名>/` 下。

数据统一落在 data/plugin_data/astrbot_plugin_ai_mind/ 下的两个文件：
`emotion_state.json`（状态快照）与 `mind.db`（记忆 + 情绪曲线）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import threading
import random
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path
from urllib import parse as urllib_parse
from uuid import uuid4
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

# ---------------------------------------------------------------------------
# 抢回自己的包名：绝不让别的插件的同名 mind 包顶替
# ---------------------------------------------------------------------------
# Python 里 sys.modules 谁先注册谁赢。如果另一个插件（合并前那两个）已经
# import 过它自己的 mind 包，我们再 from mind.xxx import ... 拿到的就是
# **别人那份代码** —— 日志里的模块名会是光秃秃的 mind.samples，而不是我们的
# astrbot_plugin_ai_mind.mind.samples。后果：改自己的文件改十几版，
# 跑的还是别人的代码。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _reclaim_mind_package() -> list:
    """把不属于本插件目录的 mind.* 从 sys.modules 里清掉。"""
    dropped: list = []
    for name in [n for n in list(sys.modules) if n == "mind" or n.startswith("mind.")]:
        module = sys.modules.get(name)
        where = os.path.abspath(str(getattr(module, "__file__", "") or ""))
        if where and _HERE in where:
            continue                      # 已经是我们自己的了
        sys.modules.pop(name, None)
        dropped.append(name)
    return dropped


_RECLAIMED = _reclaim_mind_package()
try:  # AstrBot 以包形式加载插件（data.plugins.<插件名>.main）
    from .mind import (
        EMOTION_RULES_BLOCK,
        EMOTION_RULES_MARKER,
        MEMORY_RULES_BLOCK,
        MEMORY_RULES_MARKER,
        EMOTION_TAG_RE,
        PRESETS,
        PROTOTYPES,
        all_presets,
        delete_custom_preset,
        get_preset,
        load_custom_presets,
        preset_to_dict,
        save_custom_preset,
        MODE_CONTAINS,
        MODE_LABELS,
        MODES,
        Cooldown,
        ImageStore,
        IMAGES_MARKER,
        PIC_TAG_RE,
        guess_extension,
        human_size,
        render_available_images,
        PAD,
        EmotionEngine,
        GuardSettings,
        HUMANIZE_MODE_LABELS,
        PrivacySettings,
        PromptStore,
        StyleExample,
        StyleSettings,
        StyleStore,
        decide_scope,
        detect_sensitive,
        GROUP_LABEL_PHRASES,
        HUMANIZE_MODES,
        HumanizeEntry,
        HumanizeSettings,
        HumanizeStore,
        KINDS,
        KIND_LABELS,
        Memory,
        MemoryExtractor,
        MemoryStore,
        MindSettings,
        Retriever,
        LexiconStore,
        SampleStore,
        theory_payload,
        SessionMood,
        Settings,
        Snapshot,
        Turn,
        allows,
        RELATION_MARKER,
        RELATION_RULES_BLOCK,
        build_emotion_injection,
        render_relationship_block,
        render_relationship_meter,
        cfg_get,
        classify,
        decide_scope,
        describe_session,
        detect_sensitive,
        emotion_payload,
        format_existing_for_prompt,
        heuristic_self_emotion,
        is_group_session,
        memory_payload,
        parse_extraction,
        render_memory_block,
        resolve_prototype,
        session_entries,
        should_sample,
    )
    from .mind.debugmode import DEBUG_CODE, DEBUG_UID, DebugSessions
    from .mind.groupswitch import COMMANDS as GROUP_COMMANDS, GroupSwitch
    from .mind.textfix import tidy_cjk_spaces
    from .mind.emotion.display import detail_panel, help_panel, history_panel, mood_panel, rank_panel, relation_panel
    from .mind.memory.display import clear_panel, forget_panel, help_panel as memory_help_panel, main_panel as memory_main_panel, remember_panel, search_panel
except ImportError:  # 兜底：以单模块方式加载时，把插件目录加进 sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mind import (  # type: ignore[no-redef]
        EMOTION_RULES_BLOCK,
        EMOTION_RULES_MARKER,
        MEMORY_RULES_BLOCK,
        MEMORY_RULES_MARKER,
        EMOTION_TAG_RE,
        PRESETS,
        PROTOTYPES,
        all_presets,
        delete_custom_preset,
        get_preset,
        load_custom_presets,
        preset_to_dict,
        save_custom_preset,
        MODE_CONTAINS,
        MODE_LABELS,
        MODES,
        Cooldown,
        ImageStore,
        IMAGES_MARKER,
        PIC_TAG_RE,
        guess_extension,
        human_size,
        render_available_images,
        PAD,
        EmotionEngine,
        GuardSettings,
        HUMANIZE_MODE_LABELS,
        PrivacySettings,
        PromptStore,
        StyleExample,
        StyleSettings,
        StyleStore,
        decide_scope,
        detect_sensitive,
        GROUP_LABEL_PHRASES,
        HUMANIZE_MODES,
        HumanizeEntry,
        HumanizeSettings,
        HumanizeStore,
        KINDS,
        KIND_LABELS,
        Memory,
        MemoryExtractor,
        MemoryStore,
        MindSettings,
        Retriever,
        LexiconStore,
        SampleStore,
        theory_payload,
        SessionMood,
        Snapshot,
        Turn,
        allows,
        RELATION_MARKER,
        RELATION_RULES_BLOCK,
        build_emotion_injection,
        render_relationship_block,
        render_relationship_meter,
        cfg_get,
        classify,
        decide_scope,
        describe_session,
        detect_sensitive,
        emotion_payload,
        format_existing_for_prompt,
        heuristic_self_emotion,
        is_group_session,
        memory_payload,
        parse_extraction,
        render_memory_block,
        resolve_prototype,
        session_entries,
        should_sample,
    )
    from mind.debugmode import DEBUG_CODE, DEBUG_UID, DebugSessions  # type: ignore[no-redef]
    from mind.groupswitch import COMMANDS as GROUP_COMMANDS, GroupSwitch  # type: ignore[no-redef]
    from mind.textfix import tidy_cjk_spaces  # type: ignore[no-redef]
    from mind.emotion.display import detail_panel, help_panel, history_panel, mood_panel, rank_panel, relation_panel  # type: ignore[no-redef]
    from mind.memory.display import clear_panel, forget_panel, help_panel as memory_help_panel, main_panel as memory_main_panel, remember_panel, search_panel  # type: ignore[no-redef]

try:
    import astrbot.api.message_components as Comp
except ImportError:  # 极老版本没有消息组件模块：配图功能自动关闭
    Comp = None  # type: ignore[assignment]
try:  # 会话级的语音开关（有人用指令关掉某个会话的 TTS）
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except ImportError:  # pragma: no cover - 老版本没有这个模块
    SessionServiceManager = None  # type: ignore[assignment]

try:  # 主动发送消息要自己拼一条链
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover - 老版本换了导出位置
    try:
        from astrbot.api.message_components import MessageChain  # type: ignore[no-redef]
    except ImportError:
        MessageChain = None  # type: ignore[assignment]

try:  # 拟人分段内核（纯函数，脱离 astrbot 也能单测）
    from .mind import splitter as splitter_engine
except ImportError:  # pragma: no cover - 单模块方式加载时
    from mind import splitter as splitter_engine  # type: ignore[no-redef]

try:  # 消息防抖内核（纯规则，零依赖）
    from .mind import debounce as debounce_engine
except ImportError:  # pragma: no cover - 单模块方式加载时
    from mind import debounce as debounce_engine  # type: ignore[no-redef]

try:  # 去 AI 味检测器（纯本地词表与统计）
    from .mind import antiai
except ImportError:  # pragma: no cover
    from mind import antiai  # type: ignore[no-redef]

try:  # 表达示例（学她怎么说话 + 待审队列）
    from .mind import style as style_engine
except ImportError:  # pragma: no cover
    from mind import style as style_engine  # type: ignore[no-redef]

try:  # 模仿指定 QQ 的说话习惯（采集 + 指纹 + 注入参考）
    from .mind import imitate as imitate_engine
except ImportError:  # pragma: no cover
    from mind import imitate as imitate_engine  # type: ignore[no-redef]

try:  # 生图（OpenAI Images 兼容接口 + 适配器层）
    from .mind import imagegen as imagegen_engine
except ImportError:  # pragma: no cover
    from mind import imagegen as imagegen_engine  # type: ignore[no-redef]

try:  # 后台任务（长操作交给它，前端轮询）
    from .mind import job as job_engine
except ImportError:  # pragma: no cover
    from mind import job as job_engine  # type: ignore[no-redef]

try:  # AI 声聊（先发文字，再补一条语音）
    from .mind import voicechat as voice_engine
except ImportError:  # pragma: no cover
    from mind import voicechat as voice_engine  # type: ignore[no-redef]

try:  # 被戳一下的反应（参考 poke_to_llm，MIT）
    from .mind import poke as poke_engine
except ImportError:  # pragma: no cover
    from mind import poke as poke_engine  # type: ignore[no-redef]

try:  # 内置 ffmpeg（找 / 下载 / 校验）
    from .mind import ffmpeg_setup
except ImportError:  # pragma: no cover
    from mind import ffmpeg_setup  # type: ignore[no-redef]

try:  # 表情包库（收 → 打标 → 按情绪发）
    from .mind import sticker as sticker_engine
except ImportError:  # pragma: no cover
    from mind import sticker as sticker_engine  # type: ignore[no-redef]

try:  # QQ 空间：日记 + 配图 + 说说
    from .mind import qzone as qzone_engine
except ImportError:  # pragma: no cover
    from mind import qzone as qzone_engine  # type: ignore[no-redef]

try:  # 按人格生成日程
    from .mind import schedule_gen
except ImportError:  # pragma: no cover
    from mind import schedule_gen  # type: ignore[no-redef]

try:  # 上下文压缩（聊长了只留要紧句）
    from .mind import contextfit
except ImportError:  # pragma: no cover
    from mind import contextfit  # type: ignore[no-redef]

try:  # 需求先搜市场（DSH 插件目录）
    from .mind import market as market_engine
except ImportError:  # pragma: no cover
    from mind import market as market_engine  # type: ignore[no-redef]

try:  # 记忆价值回传与经验结晶（移植 MemOS 的 Reflect2Evolve 思路）
    from .mind.memory import evolve as evolve_engine
except ImportError:  # pragma: no cover
    from mind.memory import evolve as evolve_engine  # type: ignore[no-redef]

try:  # 知识树（按会话激活的结构化知识，模型改件待审）
    from .mind import knowledge as knowledge_engine
except ImportError:  # pragma: no cover
    from mind import knowledge as knowledge_engine  # type: ignore[no-redef]

try:  # 动图逐帧（GIF / 动态 WebP → 带时间的帧序列）
    from .mind import animation as animation_engine
except ImportError:  # pragma: no cover
    from mind import animation as animation_engine  # type: ignore[no-redef]

try:  # 听音频（响度 / 动态 / 峰值 → 一句人话）
    from .mind import soundcheck as soundcheck_engine
except ImportError:  # pragma: no cover
    from mind import soundcheck as soundcheck_engine  # type: ignore[no-redef]

try:  # 结果投递：群里发指令 → 大块结果走私聊
    from .mind import delivery as delivery_engine
except ImportError:  # pragma: no cover
    from mind import delivery as delivery_engine  # type: ignore[no-redef]

try:  # 刷 B 站（分区最新 + 字幕 + 直链）—— 跟分镜合到一条链上
    from .mind import bili as bili_engine
except ImportError:  # pragma: no cover
    from mind import bili as bili_engine  # type: ignore[no-redef]

try:  # 视频分镜（ffmpeg 宫格图 + 时间码清单）
    from .mind import storyboard as storyboard_engine
except ImportError:  # pragma: no cover
    from mind import storyboard as storyboard_engine  # type: ignore[no-redef]

try:  # 中国日历（今天是什么日子：法定假 / 寒暑假 / 调休）
    from .mind import calendar_cn as calendar_engine
except ImportError:  # pragma: no cover
    from mind import calendar_cn as calendar_engine  # type: ignore[no-redef]

try:  # 她的邮箱（smtplib / imaplib，授权码只从 data/mail.json 读）
    from .mind import mailbox as mail_engine
except ImportError:  # pragma: no cover
    from mind import mailbox as mail_engine  # type: ignore[no-redef]

try:  # 提示词仓库（注入给模型的原话都从这里取）
    from .mind import prompts as prompt_engine
except ImportError:  # pragma: no cover
    from mind import prompts as prompt_engine  # type: ignore[no-redef]

try:  # 鉴权与护栏（纯函数：身份判定 / 消息拦截 / 工具名单 / 发送前清理）
    from .mind import guard as guard_engine
except ImportError:  # pragma: no cover
    from mind import guard as guard_engine  # type: ignore[no-redef]

try:  # 记忆图谱（把记忆库铺成节点与边）
    from .mind import graph as graph_engine
except ImportError:  # pragma: no cover
    from mind import graph as graph_engine  # type: ignore[no-redef]

try:
    from astrbot.api.star import register
except ImportError:  # 极老版本没有 register，退化成空装饰器
    def register(*_args: Any, **_kwargs: Any):
        def decorator(cls):
            return cls

        return decorator

try:
    from astrbot.api.web import error_response, json_response, request as web_request
except ImportError:
    # 老版本没有这套助手：直接返回 dict 即可，FastAPI 会自己序列化成 JSON。
    # 这样"面板后端"就不会因为一个可选导入而整个失效。
    web_request = None  # type: ignore[assignment]

    def json_response(data: Any = None, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return {} if data is None else data

    def error_response(message: str, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return {"status": "error", "message": message, "data": None}

#: 一键推荐：新手照这个配就行（只写覆盖层，随时能还原）
#: 人格锚点：这段话是所有人格/情绪的底座，放在最稳定的位置吃缓存
PERSONA_ANCHOR_MARK = "<persona_anchor>"

WIZARD_RECOMMENDED: dict[str, Any] = {
    "humanize": {"enabled": True, "mode": "check", "threshold": 65.0, "max_rounds": 2},
    "style": {"enabled": True, "auto_approve": False, "min_score": 78.0, "inject_count": 3},
    "guard": {"enabled": True, "block_sensitive": True, "block_tools": True,
              "tool_mode": "whitelist", "trust_admins": True},
    "splitter": {"enabled": True, "max_segments": 3, "delay_strategy": "linear",
                 "smart": True, "tts_for_segments": True},
    "emotion": {"lexicon": True, "lexicon_gain": 0.25, "trusted": True},
}



#: 主动发送的三种结果。分这么细是因为「抛异常」和「确定没发出去」必须区别对待：
#: 前者可能已经发出去了，再补一遍就是刷屏。
def _optional_hook(name: str):
    """拿一个可能不存在的钩子装饰器。

    老版本 AstrBot 没有 on_using_llm_tool，直接 getattr 会在导入期就炸掉整个插件。
    拿不到就返回一个空装饰器：功能降级，但插件照常加载。
    """
    decorator = getattr(filter, name, None)
    if callable(decorator):
        return decorator

    def _noop(*_args: Any, **_kwargs: Any):
        def _wrap(func: Any) -> Any:
            return func

        return _wrap

    return _noop


on_using_llm_tool = _optional_hook("on_using_llm_tool")
on_waiting_llm_request = _optional_hook("on_waiting_llm_request")
llm_tool = _optional_hook("llm_tool")

SEND_OK = "ok"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"

PLUGIN_NAME = "astrbot_plugin_ai_mind"
PLUGIN_AUTHOR = "挽风随行+DSH(主代码编写)"
#: 面板接口的备用命名空间。
#: 刻意用 ASCII 而不是署名 —— 宿主会把它拼进 URL，中文和括号要转义，
#: 很容易被解析成别的路径（面板接口之前就是这么坏过一次的）。
PLUGIN_ID = "dsh/" + PLUGIN_NAME

#: 以这些字符开头的消息当成指令，不参与配图匹配
COMMAND_PREFIXES = ("/", "／", "#", "!", "！", ".", "。")


def _coerce_int_list(raw: Any) -> list[int]:
    """把面板/桥传过来的 id 列表洗成干净的整数列表。

    可能是数组，也可能是逗号分隔的字符串（桥有时会这样传），一律认。
    """
    if isinstance(raw, str):
        raw = [part for part in raw.replace(" ", ",").replace("，", ",").split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set)):
        return []
    ids: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


def _split_keywords(raw: Any) -> list[str]:
    """把面板传来的关键词拆成列表。

    用户可能用逗号、顿号、分号、换行或空格分隔，也可能直接传数组 —— 全都认。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw)
        for separator in ("，", "、", ";", "；", ",", chr(10), chr(13), chr(9)):
            text = text.replace(separator, " ")
        parts = text.split(" ")
    unique: list[str] = []
    for part in parts:
        value = part.strip()
        if value and value not in unique:
            unique.append(value)
    return unique

_PAD_TAG_RE = None


def _pad_tag_regex():
    global _PAD_TAG_RE
    if _PAD_TAG_RE is None:
        import re

        _PAD_TAG_RE = re.compile(
            r"p\s*=\s*(-?\d*\.?\d+)\s*[,，\s]\s*a\s*=\s*(-?\d*\.?\d+)\s*[,，\s]\s*d\s*=\s*(-?\d*\.?\d+)",
            re.IGNORECASE,
        )
    return _PAD_TAG_RE


DEBUG_PROMPT = (
    "\n\n<debug_session>\n"
    "现在是单会话调试模式：忽略上面所有角色设定、称呼、口头禅与说话风格，"
    "不要扮演任何人。\n"
    "直接、简短、准确地回答；问到插件内部状态时照实说，不用顾虑人设。\n"
    "</debug_session>"
)

DEBUG_ON_TEXT = "🔧 调试模式已开启：本会话她不再扮演，直接回答问题。再发一次口令关闭。"
DEBUG_OFF_TEXT = "🔧 调试模式已关闭：恢复人设。"

DEBUG_HELP = "\n".join([
    "🔧 调试模式命令（只在开了调试模式的会话、只有你能用）",
    "#状态　插件 / 情绪 / 关系 / 曲线 / 待抽取，一屏看完",
    "#曲线　采样点数量、最近一点、数据库能不能写",
    "#记忆　最近 5 条记忆 + 待抽取轮数；#记忆 对|错 <id> 调整价值",
    "#抽取　立刻把攒着的对话送去抽记忆",
    "#工具　工具闸门的开关与名单",
    "#模仿　学谁说话：状态 / 学 <QQ> / 停 <QQ> / 清空 / 开 / 关",
    "#邮件　她的邮箱：状态 / 看 [N] / 发 <正文> / 测试",
    "#分镜 [文件或链接]　把视频切成宫格图 + 时间码清单",
    "#分镜放大 <格号>　把某一格再放大看（用上一次的分镜）",
    "#动图 <文件>　GIF/动态WebP 摊成带时间的帧序列",
    "#听歌 <文件>　响度/动态/峰值 + 一句结论",
    "#知识　知识树：列表 / 学 <名> / 开|关 <名> / 加 <名> <标题> <内容> / 待审 / 批|否 <id>",
    "#画 <描述>　文生图；#画 改 <图片> <说明>　参考图编辑",
    "#市场 <能力词>　去开源插件市场搜有没有现成的",
    "#声聊　AI 语音：开|关 / 人物 / 用 <id> / 测试",
    "#在看 [N]　登录后看我最近在 B 站看什么",
    "#表情　表情包库：状态 / 开|关 / 标签 <id> <情绪> / 删 <id> / 清",
    "#日记　QQ空间：列表 / 写 / 发 [id] / 删 <id> / 刷新",
    "#刷B站 [条数]　刷一轮：挑值得的 + 值得的做分镜 + 出日报",
    "#帮助　这份清单",
    "",
    "调试模式里插件完全让路：不注入情绪 / 记忆 / 身份，不分段，不记记忆。",
    "再发一次 " + DEBUG_CODE + " 退出。",
])

class _SampleRow:
    """一条采样点。面板那边是按**属性**读的（.t / .pad()），所以不能给 dict。"""

    __slots__ = ("id", "session_id", "t", "p", "a", "d", "kind")

    def __init__(self, item: Any) -> None:
        self.id = int(item["id"])
        self.session_id = str(item["session_id"])
        self.t = float(item["t"])
        self.p = float(item["p"])
        self.a = float(item["a"])
        self.d = float(item["d"])
        self.kind = str(item["kind"] or "auto")

    def pad(self) -> Any:
        try:
            from .mind.emotion.model import PAD
        except ImportError:  # pragma: no cover
            from mind.emotion.model import PAD  # type: ignore[no-redef]
        return PAD(p=self.p, a=self.a, d=self.d)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "t": self.t, "p": self.p, "a": self.a,
            "d": self.d, "kind": self.kind,
        }


class _SampleStore:
    """情绪曲线采样点仓库（自足版）。

    为什么不放在 mind/samples.py：那个包曾经被另一个插件的同名 mind 包顶替过
    （sys.modules 里先注册的那个赢），于是 mind/samples.py 换了多少次都不生效 ——
    采样一路写进「已关闭的数据库」，日志里只有一句 warning。main.py 不会认错，
    它就在插件目录里。所以曲线这块改成自足：自己的连接、自己的表、坏了就丢开重连。
    """

    _DDL = (
        "CREATE TABLE IF NOT EXISTS emotion_samples ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, t REAL NOT NULL,"
        " p REAL NOT NULL, a REAL NOT NULL, d REAL NOT NULL,"
        " kind TEXT NOT NULL DEFAULT {q}auto{q});"
        "CREATE INDEX IF NOT EXISTS idx_sample_session"
        " ON emotion_samples (session_id, t);"
        "CREATE TABLE IF NOT EXISTS relation_samples ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, uid TEXT NOT NULL, t REAL NOT NULL,"
        " affinity REAL NOT NULL, familiarity REAL NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_rel_session"
        " ON relation_samples (session_id, uid, t);"
    ).format(q=chr(39))

    # 兼容旧的测试钩子：以前这些字段挂在 ConnectionSource 上。
    # _db_path 非空时以它为准 —— 测试靠它模拟「这个路径写不进去」。
    _db_path: Any = None
    _source: Any = None
    _spare_conn: Any = None
    _schema_ready: bool = False

    def __init__(self, path: Any, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self.last_error = ""
        self._conn: Any = None

    def _file(self) -> str:
        return str(self._db_path or self.path)

    # -- 连接 -----------------------------------------------------------
    def _open(self) -> Any:
        try:
            conn = sqlite3.connect(self._file(), check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.executescript(self._DDL)
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"打开曲线库失败：{type(exc).__name__}: {exc}"
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] {self.last_error}")
            return None
        self._conn = conn
        self.last_error = ""
        return conn

    @property
    def conn(self) -> Any:
        conn = self._conn
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
        return self._open()

    @conn.setter
    def conn(self, value: Any) -> None:
        self._conn = value

    def _sql(self, sql: str, params: Iterable[Any] = ()) -> Any:
        for attempt in (0, 1):
            conn = self.conn
            if conn is None:
                return None
            try:
                cursor = conn.execute(sql, tuple(params))
                conn.commit()
                return cursor
            except sqlite3.Error as exc:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                if attempt:
                    self.last_error = f"曲线 SQL 失败：{type(exc).__name__}: {exc}"
                    if self.logger is not None:
                        self.logger.warning(f"[ai_mind] {self.last_error}")
                    return None
        return None

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[Any]:
        cursor = self._sql(sql, params)
        if cursor is None:
            return []
        try:
            return list(cursor.fetchall())
        except sqlite3.Error:
            return []

    @staticmethod
    def _row(item: Any) -> _SampleRow:
        return _SampleRow(item)

    # -- 写 -------------------------------------------------------------
    def append(self, session_id: str, t: float, pad: Any, kind: str = "auto") -> None:
        self._sql(
            "INSERT INTO emotion_samples (session_id, t, p, a, d, kind)"
            " VALUES (?,?,?,?,?,?)",
            (str(session_id), float(t), float(pad.p), float(pad.a), float(pad.d), str(kind)),
        )

    def replace_at(self, session_id: str, t: float, pad: Any,
                   *, tolerance: float = 1.0) -> int:
        cursor = self._sql(
            "UPDATE emotion_samples SET p = ?, a = ?, d = ?, kind = ?"
            " WHERE session_id = ? AND t BETWEEN ? AND ?",
            (float(pad.p), float(pad.a), float(pad.d), "manual", str(session_id),
             float(t) - float(tolerance), float(t) + float(tolerance)),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def delete_at(self, session_id: str, t: float, *, tolerance: float = 1.0) -> int:
        cursor = self._sql(
            "DELETE FROM emotion_samples WHERE session_id = ? AND t BETWEEN ? AND ?",
            (str(session_id), float(t) - float(tolerance), float(t) + float(tolerance)),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def clear(self, session_id: str | None = None) -> int:
        if session_id is None:
            cursor = self._sql("DELETE FROM emotion_samples")
        else:
            cursor = self._sql(
                "DELETE FROM emotion_samples WHERE session_id = ?", (str(session_id),)
            )
        return int(cursor.rowcount) if cursor is not None else 0

    def prune_all(self, max_points: int) -> int:
        if int(max_points) <= 0:
            return 0
        removed = 0
        for key in self.sessions():
            cursor = self._sql(
                "DELETE FROM emotion_samples WHERE session_id = ? AND id NOT IN ("
                " SELECT id FROM emotion_samples WHERE session_id = ?"
                " ORDER BY t DESC LIMIT ?)",
                (key, key, int(max_points)),
            )
            if cursor is not None:
                removed += int(cursor.rowcount)
        return removed

    # -- 读 -------------------------------------------------------------
    def since(self, session_id: str, since: float, limit: int = 1500) -> list[Any]:
        rows = self._rows(
            "SELECT * FROM emotion_samples WHERE session_id = ? AND t >= ?"
            " ORDER BY t ASC LIMIT ?",
            (str(session_id), float(since), int(limit)),
        )
        return [self._row(item) for item in rows]

    def latest(self, session_id: str) -> Any:
        rows = self._rows(
            "SELECT * FROM emotion_samples WHERE session_id = ?"
            " ORDER BY t DESC LIMIT 1",
            (str(session_id),),
        )
        return self._row(rows[0]) if rows else None

    def count(self, session_id: str | None = None) -> int:
        if session_id is None:
            rows = self._rows("SELECT COUNT(*) AS c FROM emotion_samples")
        else:
            rows = self._rows(
                "SELECT COUNT(*) AS c FROM emotion_samples WHERE session_id = ?",
                (str(session_id),),
            )
        return int(rows[0]["c"]) if rows else 0

    def sessions(self) -> list[str]:
        rows = self._rows("SELECT DISTINCT session_id FROM emotion_samples")
        return [
            str(item["session_id"]) for item in rows
            if item["session_id"] and not str(item["session_id"]).startswith("__")
        ]

    def record_relation(
        self, session_id: str, uid: str, affinity: float,
        familiarity: float, t: float,
    ) -> None:
        self._sql(
            "INSERT INTO relation_samples"
            " (session_id, uid, t, affinity, familiarity) VALUES (?,?,?,?,?)",
            (str(session_id), str(uid), float(t), float(affinity), float(familiarity)),
        )

    def relation_series(
        self, session_id: str, since: float, limit: int = 800
    ) -> dict[str, list[list[float]]]:
        """这个会话里每个人的关系随时间。返回 {uid: [[t, 好感], ...]}。"""
        rows = self._rows(
            "SELECT uid, t, affinity FROM relation_samples"
            " WHERE session_id = ? AND t >= ? ORDER BY t ASC LIMIT ?",
            (str(session_id), float(since), int(limit)),
        )
        out: dict[str, list[list[float]]] = {}
        for item in rows:
            out.setdefault(str(item["uid"]), []).append(
                [float(item["t"]), float(item["affinity"])]
            )
        return out

    def selfcheck(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "ok": False, "conn": self.conn is not None, "table": True,
            "write": False, "rows": 0, "error": "", "path": self._file(),
            "exists": False, "size": 0,
        }
        try:
            target = Path(self._file())
            info["exists"] = target.exists()
            info["size"] = target.stat().st_size if info["exists"] else 0
        except OSError:
            pass
        conn = self.conn
        if conn is None:
            info["error"] = self.last_error or "曲线库打不开"
            return info
        probe = "__selfcheck__"
        err = ""
        try:
            conn.execute("DELETE FROM emotion_samples WHERE session_id = ?", (probe,))
            conn.execute(
                "INSERT INTO emotion_samples (session_id, t, p, a, d, kind)"
                " VALUES (?,?,?,?,?,?)",
                (probe, time.time(), 0.0, 0.0, 0.0, "probe"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) FROM emotion_samples WHERE session_id = ?", (probe,)
            ).fetchone()
            info["write"] = bool(row and int(row[0]) > 0)
            conn.execute("DELETE FROM emotion_samples WHERE session_id = ?", (probe,))
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            err = f"{type(exc).__name__}: {exc}"
        info["ok"] = bool(info["write"])
        info["error"] = "" if info["write"] else (err or self.last_error or "插入没有生效")
        info["rows"] = self.count()
        return info

@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    "AI 心智：会波动的情绪 + 长期记忆 + 可视化面板",
    "1.0.0",
)

class AIMindPlugin(Star):
    """情绪与记忆合体后的主类。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.data_dir = self._resolve_data_dir()
        #: 插件自己的目录（自带 ffmpeg 在 vendor/ffmpeg 下）
        self.plugin_dir = Path(__file__).resolve().parent
        #: 实例编号：用来判断「是不是有更新的实例在跑」（见 _is_stale）
        type(self)._seq = int(getattr(type(self), "_seq", 0) or 0) + 1
        self._born = int(type(self)._seq)
        #: 装 ffmpeg 时避免两次下载同时跑
        self._ffmpeg_lock = threading.Lock()
        # 面板上填的"专属用户 / 称呼"盖在配置之上（存在 data 目录里）
        self._relationship_data = self._load_relationship_overrides()
        self._humanize_data = self._load_humanize_overrides()
        self._guard_data = self._load_simple_overrides("guard_overrides.json", "鉴权")
        self.prompts = PromptStore(self.data_dir, logger)
        self._style_data = self._load_simple_overrides("style_overrides.json", "表达示例")
        self._privacy_data = self._load_simple_overrides("privacy_overrides.json", "隐私")
        self._debounce_data = self._load_simple_overrides("debounce_overrides.json", "消息防抖")
        self._special_warned = False
        self._schema_cache: dict[str, Any] | None = None
        self._settings_saved_at = 0.0
        self._guard_blocked = 0
        self._guard_leaks = 0
        self.settings = MindSettings.from_config(
            self.config, self.data_dir, self._relationship_data, self._humanize_data,
            self._guard_data,
            self._style_data,
            self._privacy_data,
            self._debounce_data,
        )

        self.emotion_injection_mode = str(
            cfg_get(self.config, "emotion.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.emotion_inject_rules = bool(cfg_get(self.config, "emotion.injection.inject_rules", True))
        self.emotion_trusted = bool(cfg_get(self.config, "emotion.injection.trusted", True))
        self.memory_injection_mode = str(
            cfg_get(self.config, "memory.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.memory_inject_rules = bool(cfg_get(self.config, "memory.injection.inject_rules", True))
        self.embedding_timeout = float(cfg_get(self.config, "memory.embedding.timeout_seconds", 3) or 3)


        # ---- 情绪 ----
        self.engine = EmotionEngine(self.settings.emotion, self.data_dir, logger)
        self.engine.set_custom_rules(cfg_get(self.config, "emotion.persona.custom_rules", []))

        # ---- 记忆 ----
        # 单会话调试模式：聊天里发口令开关，刻意不做进面板（见 mind/debugmode.py）
        self.debug = DebugSessions(self.data_dir / "debug_sessions.json", logger)
        # 群聊对话开关（从 astrbot_plugin_group_switch 并进来的）
        self.groupswitch = GroupSwitch(self.data_dir / "group_switch.json", logger)
        db_file = self.data_dir / "mind.db"
        self.store = MemoryStore(db_file, logger)
        # 传的是仓库本身而不是 self.store.connection：宿主重载插件会先调
        # terminate()（我们把库关掉），老实例却可能还在收消息 —— 存下来的裸连接
        # 就成了「已关闭的数据库」，之后所有写入静默失败（曲线永远空、记忆写不进）。
        # 传仓库进去，每次都现取连接，库被重开也能跟上。
        # 曲线存储自足：自己的连接、自己的表 —— 不再和别人共用连接对象
        # （以前那个 mind/samples.py 会被别的插件的同名包顶替，改了也不生效）
        self.samples = _SampleStore(db_file, logger)
        self.lexicons = LexiconStore(self.store, logger)
        self.humanize_log = HumanizeStore(self.store, logger)
        self.styles = StyleStore(self.store, logger)
        self.imitate = imitate_engine.ImitateStore(self.store, logger)
        self.imitate_settings = imitate_engine.load_settings(
            Path(self.data_dir) / "imitate.json"
        )
        self.mail = mail_engine.Mailbox(
            mail_engine.load_config(Path(self.data_dir) / "mail.json"), logger
        )
        #: 发出去的邮件记一份（面板可见；失败也记，方便排查）
        self._mail_sent_path = Path(self.data_dir) / "mail" / "sent_log.json"
        self.storyboard_settings = storyboard_engine.load_settings(
            Path(self.data_dir) / "storyboard.json"
        )
        self.bili_settings = bili_engine.load_settings(
            Path(self.data_dir) / "bili.json"
        )
        self.bili_seen = bili_engine.SeenStore(
            Path(self.data_dir) / "bili_seen.json"
        )
        #: 上一次分镜（#分镜放大 要用）
        self._last_storyboard: dict[str, Any] = {}
        #: 每天那轮报告发回哪儿（主人在私聊里说过话就记下来）
        self._bili_target: str = ""
        self.delivery_settings = delivery_engine.load_settings(
            Path(self.data_dir) / "delivery.json"
        )
        self.knowledge = knowledge_engine.load(Path(self.data_dir) / "knowledge.json")
        self.imagegen_settings = imagegen_engine.load_settings(
            Path(self.data_dir) / "imagegen.json"
        )
        #: 上一轮注入给模型的记忆 id（下一轮用户说「不对」就是一次评价）
        self._last_memory_ids: dict[str, list[int]] = {}
        self._feedback_applied = 0
        self._evolve_tick = 0
        # 启动时先自己找一遍 ffmpeg（含插件内置目录 / winget / scoop）
        try:
            _ff = ffmpeg_setup.status(
                self.data_dir, self.storyboard_settings.ffmpeg_path, self.plugin_dir
            )
            if _ff.get("ok"):
                self._ffmpeg_remember(_ff)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_mind] 查找 ffmpeg 失败：{exc}")
        self.market_settings = market_engine.load_settings(
            Path(self.data_dir) / "market.json"
        )
        self.market_history = market_engine.load_history(
            Path(self.data_dir) / "market_history.json"
        )
        self._market_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.stickers = sticker_engine.StickerStore(self.store, logger)
        self.sticker_settings = sticker_engine.load_settings(
            Path(self.data_dir) / "sticker.json"
        )
        #: 每个会话最近发过的表情（避免连续同一张）与发送时间
        self._sticker_recent: dict[str, list[int]] = {}
        self._sticker_sent: dict[str, list[float]] = {}
        self.poke_settings = poke_engine.load_settings(
            Path(self.data_dir) / "poke.json"
        )
        self.poke_gate = poke_engine.PokeGate(self.poke_settings)
        self.voice_settings = voice_engine.load_settings(
            Path(self.data_dir) / "voice.json"
        )
        #: 每个会话上次发语音的时间（冷却用）
        self._voice_last: dict[str, float] = {}
        self.qzone_settings = qzone_engine.load_settings(
            Path(self.data_dir) / "qzone.json"
        )
        self.diaries = qzone_engine.DiaryStore(self.store, logger)
        #: 发说说要 bot api：事件里拿到就存下来，面板/定时任务复用
        self._qzone_api_cache: Any = None
        #: 空间资料（昵称/头像）缓存
        self._qzone_profile: dict[str, str] = {}
        self._qzone_session: str = ""
        self.jobs = job_engine.JobRegistry()
        self._job_tasks: set[Any] = set()
        self.images = ImageStore(self.store, self.data_dir, logger)
        # 把库文件的路径也交给各仓库：主连接要是被别处关掉，
        # 它们能自己连上这个文件继续干活，而不是一路静默失败。
        for _store in (self.samples, self.lexicons, self.humanize_log,
                       self.styles, self.images):
            _store._db_path = db_file
        self.image_cooldown = Cooldown(self.settings.images.cooldown_seconds)
        self.extractor = MemoryExtractor(self.settings.memory, logger)
        self.retriever = Retriever(self.settings.memory, logger)
        # ---- 拟人分段 ----
        # 面板上调的参数落在这里，盖在插件配置之上：不懂 YAML 的人也能改，
        # 改坏了按一下「还原成配置文件」就回去了。
        self._splitter_data = self._load_splitter_overrides()
        self.splitter = splitter_engine.SplitterSettings.from_config(
            self.config, self._splitter_data
        )
        if Comp is not None and getattr(Comp, "Plain", None) is not None:
            splitter_engine.set_plain_factory(Comp.Plain)
        self._splitter_locks: dict[str, asyncio.Lock] = {}
        self._split_sent = 0

        # ---- 运行时状态 ----
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._llm_messages: OrderedDict[str, float] = OrderedDict()
        self._last_sender: dict[str, str] = {}
        # 消息防抖：待合并的「用户接着说的一句」与被并走的消息 id
        self._debounce_pending: dict[str, list[tuple[str, str, float]]] = {}
        self._debounce_eaten: dict[str, float] = {}
        self._buffers: dict[str, list[Turn]] = {}
        self._last_append: dict[str, float] = {}
        self._extracting: set[str] = set()
        self._extract_tasks: set[asyncio.Task[Any]] = set()
        self._last_sample: dict[str, Any] = {}
        self._prune_tick = 0
        self._last_rel_sample: dict[Any, float] = {}
        self._sample_broken = False
        self._sample_warned = False
        # 曲线为什么不画线 —— 面板上要能一眼看出卡在哪一步。
        # 计数只在本次进程内有效，重启就归零，所以文案写「本次启动以来」。
        #: 上一次分段结果（#分段 诊断用）
        self._last_split: dict[str, Any] = {}
        #: 今天按人格生成的日程（坏了就退回规则版）
        self._schedule_generated: dict[str, Any] | None = None
        self._schedule_gen_date: str = ""
        self._diag = {
            "turns": 0, "prepares": 0, "gated": 0, "frozen": 0,
            "samples": 0, "skipped": 0, "writes": 0,
        }
        self._last_flush = 0.0
        self._task: asyncio.Task[None] | None = None
        self._embedding_key = ""
        self._frozen: set[str] = set()   # 被面板冻结的会话：不自动更新情绪

        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        getter = getattr(StarTools, "get_data_dir", None)
        if callable(getter):
            try:
                return Path(getter(PLUGIN_NAME))
            except Exception:  # noqa: BLE001
                pass
        return Path(__file__).resolve().parent / "data"

    async def initialize(self) -> None:
        self.engine.load()
        stats = self.store.stats()
        logger.info(
            f"[ai_mind] 已就绪：{self.engine.session_count} 个会话的情绪、"
            f"{stats['total']} 条记忆、{self.samples.count()} 个曲线采样点"
        )
        # 代码指纹 + 代码目录：装的是不是新代码、加载的是哪个文件夹，
        # 一眼就能看出来（混装过，查了好几轮才发现）。
        try:
            from .mind import dbconn as _dbconn
            from .mind import samples as _samples
        except ImportError:  # pragma: no cover
            import mind.dbconn as _dbconn  # type: ignore[no-redef]
            import mind.samples as _samples  # type: ignore[no-redef]
        logger.info(
            "[ai_mind] 代码指纹："
            f"{getattr(_dbconn, 'STAMP', '?')} / {getattr(_samples, 'STAMP', '?')}"
            f"｜目录 {os.path.dirname(os.path.abspath(__file__))}"
        )
        try:
            from .mind import panel as _panel
        except ImportError:  # pragma: no cover
            import mind.panel as _panel  # type: ignore[no-redef]
        logger.info(
            "[ai_mind] 实际加载的模块："
            f"samples={getattr(_samples, '__file__', '?')}"
            f"｜panel={getattr(_panel, '__file__', '?')}"
            + (f"｜抢回被顶替的包 {_RECLAIMED}" if _RECLAIMED else "")
        )
        logger.info(f"[ai_mind] {splitter_engine.describe(self.splitter)}")
        logger.info(f"[ai_mind] {debounce_engine.describe(self.settings.debounce)}")
        try:
            from .mind.emotion import lexicon as _lexicon
        except ImportError:  # pragma: no cover
            from mind.emotion import lexicon as _lexicon
        logger.info(f"[ai_mind] {_lexicon.describe_lexicon()}")
        type(self)._LIVE = self      # 认领：以后旧实例会把请求转给我
        self._load_pending()
        self._warn_missing_special()
        self._ensure_task()

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            await self.flush_memories(force=True)
            pending = set(self._extract_tasks)
            if pending:
                await asyncio.wait(pending, timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 退出前整理记忆失败：{exc}")
        try:
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 退出前保存情绪失败：{exc}")
        if type(self)._LIVE is self:
            type(self)._LIVE = None
        self.store.close()

    def _is_stale(self) -> bool:
        """插件重载后，旧实例的钩子还在；它手里的配置/人格都是旧的，必须让路。

        判定不能只看「_LIVE 是不是我」：要是新实例还没来得及认领，
        它就会把自己当成旧实例，结果一条都不回（分段、情绪全没了）。
        所以只认「有比我更新的实例还活着」这一种情况。
        """
        live = type(self)._LIVE
        if live is None or live is self:
            return False
        return int(getattr(live, "_born", 0) or 0) > int(self._born or 0)

    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        interval = self.settings.maintenance_interval
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.time()
                self.engine.tick_all(now)
                self._heartbeat(now)
                self._style_maintenance()
                self._debounce_gc()
                await self._bili_daily_tick()
                await self._bili_history_tick()
                await self._schedule_tick()
                await self._qzone_daily_tick()
                await self._mail_auto_tick()
                self._evolve_tick += 1
                if self._evolve_tick >= 20:
                    self._evolve_tick = 0
                    made = await asyncio.to_thread(self._crystallize_memories)
                    if made:
                        logger.info(f"[ai_mind] 结晶出 {made} 条经验")
                if self.engine.dirty:
                    await asyncio.to_thread(self.engine.flush, now)
                await self.flush_memories()
                await self.embed_pending()
                # 裁剪不必每轮都做：它要扫全库、还要按重要度排序，
                # 是这一圈里最贵的一步。每 10 圈跑一次足够 ——
                # 记忆多到该裁的程度，不会在这几分钟里突然爆掉。
                self._prune_tick += 1
                if self._prune_tick >= 10:
                    self._prune_tick = 0
                    self.store.prune(
                        max_memories=self.settings.memory.max_memories,
                        importance_floor=self.settings.memory.prune_importance_floor,
                    )
                    # 垃圾桶留 1 天，过期才真正删掉
                    purged = self.store.purge_trash(now - 24 * 3600.0)
                    if purged:
                        logger.info(f"[ai_mind] 垃圾桶到期：真正删除 {purged} 条记忆")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[ai_mind] 后台维护出错：{exc}", exc_info=True)

    # ------------------------------------------------------------------
    # 情绪曲线采样
    # ------------------------------------------------------------------
    def _maybe_sample(self, session_key: str, pad: PAD, kind: str, now: float, force: bool = False) -> None:
        last = self._last_sample.get(session_key)
        if last is None:
            last = self.samples.latest(session_key)
            self._last_sample[session_key] = last
        previous = None
        if last is not None and self.settings.emotion.emotion_half_life > 0:
            previous = last.pad().decay_toward(
                self.settings.emotion.baseline,
                self.settings.emotion.emotion_half_life,
                max(0.0, now - last.t),
            )
        self._diag["samples"] += 1
        if not should_sample(previous, pad, self.settings.panel.sample_epsilon, force=force):
            self._diag["skipped"] += 1
            return
        try:
            self.samples.append(session_key, now, pad, kind)
        except Exception as exc:  # noqa: BLE001
            # 以前这里不兜，异常会一路冒到 on_llm_request 被记成「注入失败」，
            # 真正的原因（曲线写不进去）反而看不见了。
            self._sample_broken = True
            if not self._sample_warned:
                self._sample_warned = True
                logger.warning(f"[ai_mind] 曲线采样失败，面板上的情绪曲线会退化：{exc}")
            return
        self._last_sample[session_key] = self.samples.latest(session_key)
        self._diag["writes"] += 1
        why = ""
        if self.samples.conn is None:
            why = "曲线表不可用"
        elif getattr(self.samples, "last_error", ""):
            why = str(self.samples.last_error)
        if why:
            # 别静默失败 —— 否则面板上就是一片空白，还查不出原因
            self._sample_broken = True
            if not self._sample_warned:
                self._sample_warned = True
                logger.warning(f"[ai_mind] 曲线采样写不进去，情绪曲线会退化（{why}）")

    def _maybe_relation_sample(self, session_key: str, relation: Any, now: float) -> None:
        """给「感情线」打一个点。

        10 分钟一个点：关系变化本来就慢，打密了只是在写库 ——
        而且这条路径每条消息都会走到。"""
        try:
            key = (session_key, str(getattr(relation, "uid", "")))
            last = self._last_rel_sample.get(key, 0.0)
            if last and now - last < 600.0:
                return
            self._last_rel_sample[key] = now
            self.samples.record_relation(
                session_key, key[1],
                float(getattr(relation, "affinity", 0.0) or 0.0),
                float(getattr(relation, "familiarity", 0.0) or 0.0),
                now,
            )
        except Exception as exc:  # noqa: BLE001 - 打点失败不能影响聊天
            if self.settings.debug_log:
                logger.debug(f"[ai_mind] 感情线打点失败：{exc}")

    def _heartbeat(self, now: float) -> None:
        """没有事件时也定期打点，曲线上才看得见"自己衰减回去"的过程。"""
        for key, session in list(self.engine.sessions.items()):
            last = self._last_sample.get(key)
            if last is None:
                last = self.samples.latest(key)
                self._last_sample[key] = last
            if last is None:
                self._maybe_sample(key, session.emotion, "auto", now, force=True)
                continue
            if now - last.t < self.settings.panel.heartbeat_seconds:
                continue
            self._maybe_sample(key, session.emotion, "auto", now)
        self.samples.prune_all(self.settings.panel.curve_max_points)

    # ------------------------------------------------------------------
    # 对话采集（记忆）
    # ------------------------------------------------------------------
    @staticmethod
    def _event_key(event: AstrMessageEvent) -> str:
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        return f"{event.unified_msg_origin}|{message_id}" if message_id else ""

    def _mark_llm(self, event: AstrMessageEvent) -> None:
        key = self._event_key(event)
        if not key:
            return
        self._llm_messages[key] = time.time()
        while len(self._llm_messages) > 500:
            self._llm_messages.popitem(last=False)

    def _record_turn(self, event: AstrMessageEvent, reply: str) -> None:
        if self._sender_denied(event):
            return
        user_text = (event.message_str or "").strip()
        memory = self.settings.memory
        if len(user_text) < memory.min_turn_chars and len(reply) < memory.min_turn_chars:
            return
        session_id = event.unified_msg_origin
        buffer = self._buffers.setdefault(session_id, [])
        buffer.append(Turn(user=user_text, assistant=reply.strip(), at=time.time()))
        overflow = len(buffer) - memory.max_turns_kept
        if overflow > 0:
            del buffer[:overflow]
        self._last_append[session_id] = time.time()
        self._save_pending()

    def _save_pending(self) -> None:
        """把还没抽取的对话写到磁盘。

        这些对话原来只存在内存里：攒够 6 轮、或者静默 3 分钟才会被抽取，
        中途重启一次就全没了 —— 表现就是「手动写入可以，自动写入从来不见效」，
        而且越常重启越明显（更新插件就是在重启）。
        """
        try:
            path = self.data_dir / "pending_turns.json"
            data = {
                key: [
                    {"user": t.user, "assistant": t.assistant, "at": t.at}
                    for t in turns
                ]
                for key, turns in self._buffers.items() if turns
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 存不下不能影响聊天
            if self.settings.debug_log:
                logger.debug(f"[ai_mind] 暂存待抽取对话失败：{exc}")

    def _load_pending(self) -> None:
        """把上次没来得及抽取的对话捡回来。"""
        try:
            path = self.data_dir / "pending_turns.json"
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取待抽取对话失败：{exc}")
            return
        restored = 0
        for key, items in (data or {}).items():
            if not isinstance(items, list):
                continue
            turns = [
                Turn(
                    user=str(item.get("user") or ""),
                    assistant=str(item.get("assistant") or ""),
                    at=float(item.get("at") or 0.0),
                )
                for item in items if isinstance(item, dict)
            ]
            if turns:
                self._buffers[str(key)] = turns
                restored += len(turns)
        if restored:
            logger.info(f"[ai_mind] 捡回上次没抽取的 {restored} 轮对话，稍后一起抽记忆")

    def _ready_sessions(self, now: float, force: bool = False) -> list[str]:
        memory = self.settings.memory
        ready: list[str] = []
        for session_id, buffer in list(self._buffers.items()):
            if not buffer or session_id in self._extracting:
                continue
            idle = now - self._last_append.get(session_id, now)
            if force or len(buffer) >= memory.batch_turns or idle >= memory.idle_flush_seconds:
                ready.append(session_id)
        return ready

    async def flush_memories(self, force: bool = False) -> None:
        if not self.settings.memory.enabled:
            return
        now = time.time()
        for session_id in self._ready_sessions(now, force=force):
            turns = self._buffers.get(session_id) or []
            if not turns:
                continue
            sender_id = str(self._last_sender.get(session_id, "") or "")
            self._buffers[session_id] = []
            self._save_pending()
            task = asyncio.create_task(self._extract_session(session_id, sender_id, list(turns)))
            self._extract_tasks.add(task)
            task.add_done_callback(self._extract_tasks.discard)

    async def _extract_session(self, session_id: str, sender_id: str, turns: list[Turn]) -> None:
        if session_id in self._extracting:
            return
        self._extracting.add(session_id)
        try:
            provider = await self._resolve_provider(session_id)
            if provider is None:
                # 静默返回过一次就够难受的了：用户看到的是「自动写入从来不生效」，
                # 却没有任何线索指向「拿不到对话模型」。
                logger.warning(
                    "[ai_mind] 记忆抽取：拿不到对话模型，这一批先跳过"
                    "（去 AstrBot 里确认默认对话模型设置好了）"
                )
                return
            existing = [m for m in self.store.list_memories(limit=40) if not m.sensitive]
            prompt = self.extractor.build_prompt(turns, format_existing_for_prompt(existing, 40))
            raw = await self.extractor.request(provider, prompt)
            if not raw:
                return
            items = parse_extraction(raw, max_items=self.settings.memory.max_items_per_batch)
            if not items:
                return
            added, merged, skipped = self.extractor.integrate(
                self.store, items, session_id=session_id, sender_id=sender_id
            )
            if added or merged:
                logger.info(f"[ai_mind] 记忆抽取：新增 {added} / 合并 {merged} / 跳过 {skipped}")
            elif self.settings.debug_log:
                logger.debug(f"[ai_mind] 记忆抽取：这一批没什么可记的（跳过 {skipped}）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记忆抽取出错：{exc}", exc_info=True)
        finally:
            self._extracting.discard(session_id)

    async def _resolve_provider(self, session_id: str) -> Any:
        context = getattr(self, "context", None)
        if context is None:
            return None
        provider_id = self.settings.memory.provider_id
        if provider_id:
            try:
                provider = context.get_provider_by_id(provider_id=provider_id)
                if provider is not None:
                    return provider
            except Exception:  # noqa: BLE001
                pass
        try:
            return await context.get_using_provider_async(umo=session_id)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 向量检索（可选）
    # ------------------------------------------------------------------
    def _embedding_provider(self) -> Any:
        if not self.settings.memory.use_embedding:
            return None
        context = getattr(self, "context", None)
        if context is None:
            return None
        try:
            if self.settings.memory.embedding_provider_id:
                return context.get_provider_by_id(provider_id=self.settings.memory.embedding_provider_id)
            providers = context.get_all_embedding_providers()
            return providers[0] if providers else None
        except Exception:  # noqa: BLE001
            return None

    def _embedding_model_key(self, provider: Any) -> str:
        if self._embedding_key:
            return self._embedding_key
        try:
            dim = int(provider.get_dim())
        except Exception:  # noqa: BLE001
            dim = 0
        self._embedding_key = f"{self.settings.memory.embedding_provider_id or 'auto'}|{dim}"
        return self._embedding_key

    async def _embed_query(self, text: str) -> list[float] | None:
        provider = self._embedding_provider()
        if provider is None or not text:
            return None
        try:
            return await asyncio.wait_for(provider.get_embedding(text), timeout=self.embedding_timeout)
        except Exception:  # noqa: BLE001
            return None

    async def embed_pending(self) -> None:
        provider = self._embedding_provider()
        if provider is None:
            return
        key = self._embedding_model_key(provider)
        pending = self.store.unembedded(model=key, limit=32)
        if not pending:
            return
        texts = [m.searchable_text() for m in pending]
        try:
            if hasattr(provider, "get_embeddings"):
                vectors = await asyncio.wait_for(
                    provider.get_embeddings(texts), timeout=max(10.0, self.embedding_timeout * 4)
                )
            else:
                vectors = [
                    await asyncio.wait_for(provider.get_embedding(t), timeout=self.embedding_timeout)
                    for t in texts
                ]
        except Exception:  # noqa: BLE001
            return
        for memory, vector in zip(pending, vectors or []):
            if vector:
                self.store.set_embedding(memory.id, list(vector), key)

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------
    def _prepare_emotion(self, event: AstrMessageEvent) -> tuple[SessionMood, Any, Snapshot]:
        now = time.time()
        self.engine.load()
        session_key = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        name = event.get_sender_name() or ""

        session = self.engine.resolve(session_key, now)
        relation = self.engine.relation_of(session, uid, name, now)

        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        dedup_key = f"{session_key}|{message_id}" if message_id else f"{session_key}|{round(now, 2)}"

        frozen = session_key in self._frozen
        fresh = self._mark_seen(dedup_key)
        self._diag["prepares"] += 1
        kind = "stimulus"
        if frozen:
            self._diag["frozen"] += 1
        else:
            if not fresh:
                self._diag["gated"] += 1
            else:
                gap = max(0.0, now - session.last_interaction)
                self.engine.apply_idle(session, now)
                self.engine.touch(session, relation, now)
                session.last_gap = gap
                stimulus = self.engine.appraiser.appraise(
                    event.message_str or "", relation=relation, is_special=relation.special
                )
                if stimulus is not None:
                    if (stimulus.source == "crisis"
                            and not self.settings.emotion.crisis_enabled):
                        stimulus = None
                    else:
                        self.engine.apply_stimulus(session, stimulus, relation, now)
                        hits = getattr(stimulus, "lexicon_hits", ()) or ()
                        if hits:
                            self._record_lexicon_hits(
                                session_key, uid, hits, getattr(stimulus, "emotion", "")
                            )
                        if stimulus.source == "crisis":
                            kind = "crisis"
                        self._maybe_sample(
                            session_key, session.emotion, kind, now, force=True,
                        )
            # 曲线是「观测」，不该因为这条消息被判成重复就整条断掉。
            # 以前采样和去重绑在同一个 if 里：去重一旦误判，曲线就永远空着。
            self._maybe_relation_sample(session_key, relation, now)
            self._maybe_sample(session_key, session.emotion, kind, now)
            self._schedule_flush()

        return session, relation, self.engine.snapshot(session, relation, now)

    def _record_lexicon_hits(self, session_key: str, uid: str, hits: Any, proto: str) -> None:
        """把这一轮词表命中的词记下来：面板上要看「她是因为哪句话有反应的」。"""
        try:
            self.lexicons.record(hits, session_id=session_key, uid=uid, proto=proto)
        except Exception as exc:  # noqa: BLE001 - 记不上不能影响聊天
            logger.warning(f"[ai_mind] 记录词表命中失败：{exc}")

    def _mark_seen(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return True

    def _schedule_flush(self) -> None:
        now = time.time()
        if now - self._last_flush < max(10.0, float(cfg_get(self.config, "advanced.save_interval", 60) or 60)):
            return
        self._last_flush = now
        try:
            self.engine.flush(now)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 保存情绪失败：{exc}")

    # ------------------------------------------------------------------
    # 消息防抖：用户连发的几条攒成一轮再交给模型
    #
    # 做法参考 astrbot_plugin_debounce（advent259141），但实现换了：
    # 原版用「stop_event + 伪造事件回灌 EventBus」做超时补发，这里改成
    # 在 on_llm_request 里睡一个短窗口 —— on_waiting_llm_request 在会话锁
    # 之前触发，睡的时候用户接着说的一句已经被记下来了，醒来直接合并。
    # 不伪造事件，也没有缓冲区清空的竞态。
    # ------------------------------------------------------------------
    def _debounce_id(self, event: AstrMessageEvent) -> str:
        """这条消息的 id。拿不到就没法安全合并，直接放弃防抖。"""
        obj = getattr(event, "message_obj", None)
        return str(getattr(obj, "message_id", "") or "")

    def _debounce_take(self, session_key: str, parts: list[str], keep: str) -> bool:
        """把这一刻攒下的下几条取走并进 parts，返回有没有拿到新的。"""
        queue = self._debounce_pending.get(session_key)
        if not queue:
            return False
        rest: list[tuple[str, str, float]] = []
        got = False
        for msg_id, text, stamp in queue:
            if msg_id and msg_id == keep:
                continue  # 自己那条不算「接着说」
            if msg_id and msg_id in self._debounce_eaten:
                continue
            parts.append(text)
            if msg_id:
                self._debounce_eaten[msg_id] = stamp
            got = True
        self._debounce_pending[session_key] = rest
        return got

    def _debounce_gc(self, now: float | None = None) -> None:
        """清掉过期的待合并项与被并走的标记。"""
        stamp = now if now is not None else time.time()
        for key in list(self._debounce_pending):
            fresh = [item for item in self._debounce_pending[key] if stamp - item[2] < 30.0]
            if fresh:
                self._debounce_pending[key] = fresh
            else:
                self._debounce_pending.pop(key, None)
        for msg_id in [k for k, v in self._debounce_eaten.items() if stamp - v > 60.0]:
            self._debounce_eaten.pop(msg_id, None)

    @on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        """消息确定要调 LLM、但还没排队等锁 —— 先记下它。

        正在等窗口的那一轮就是靠这里看到「用户又补了一句」的。
        """
        db = self.settings.debounce
        if not db.enabled or not self.settings.enabled:
            return
        if self.debug.is_active(event.unified_msg_origin):
            return
        try:
            msg_id = self._debounce_id(event)
            if not msg_id or msg_id in self._debounce_eaten:
                return
            text = (event.message_str or "").strip()
            if not text or debounce_engine.looks_like_command(text):
                return
            self._debounce_pending.setdefault(event.unified_msg_origin, []).append(
                (msg_id, text, time.time())
            )
        except Exception as exc:  # noqa: BLE001
            if self.settings.debug_log:
                logger.debug(f"[ai_mind] 防抖记录失败：{exc}")

    @filter.on_llm_request(priority=100)
    async def on_llm_request_debounce(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """连发合并：看着还没说完就先等一个窗口，期间补的那句并进来。"""
        db = self.settings.debounce
        if not db.enabled or not self.settings.enabled:
            return
        session_key = event.unified_msg_origin
        msg_id = self._debounce_id(event)
        # 已经被上一轮并走：这一轮不再回一次
        if msg_id and msg_id in self._debounce_eaten:
            self._debounce_eaten.pop(msg_id, None)
            event.stop_event()
            return
        try:
            if not msg_id:
                return
            text = (event.message_str or "").strip()
            if not text or debounce_engine.looks_like_command(text):
                self._debounce_pending.pop(session_key, None)
                return
            if not debounce_engine.scope_allows(db, bool(is_group_session(session_key))):
                self._debounce_pending.pop(session_key, None)
                return
            self._debounce_gc()
            # 排队等锁期间补进来的下一条，先并进来
            parts = [text]
            merged_more = self._debounce_take(session_key, parts, msg_id)
            windows = debounce_engine.wait_windows(debounce_engine.merge_texts(parts), db)
            if windows <= 0 and not merged_more:
                return
            if windows > 0:
                started = time.monotonic()
                empty = 0
                while True:
                    left = float(db.max_wait_seconds) - (time.monotonic() - started)
                    step = min(float(db.grace_seconds), left)
                    if step <= 0:
                        break
                    await asyncio.sleep(step)
                    got = self._debounce_take(session_key, parts, msg_id)
                    if len(parts) >= int(db.max_messages):
                        break
                    if got:
                        empty = 0
                        windows = debounce_engine.wait_windows(
                            debounce_engine.merge_texts(parts), db
                        )
                        if windows <= 0:
                            break
                        continue
                    empty += 1
                    if empty >= max(1, windows):
                        break
            merged = debounce_engine.merge_texts(parts)
            if merged and merged != text:
                req.prompt = merged
                try:
                    event.message_str = merged
                except Exception:  # noqa: BLE001
                    pass
                logger.info(f"[ai_mind] 防抖把 {len(parts)} 条并成一句：{merged[:60]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 防抖出错，这一轮照常发：{exc}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """请求 LLM 前：结算情绪 + 检索记忆，然后把两样注入这一轮。"""
        if not self.settings.enabled:
            return
        if self._is_stale():
            return
        if self.debug.is_active(event.unified_msg_origin):
            # 调试模式：插件完全让路 —— 不注入情绪 / 身份 / 记忆 / 语气，
            # 只在系统提示词末尾盖一条覆盖指令，把人格压下去。
            req.system_prompt = ((req.system_prompt or "") + DEBUG_PROMPT).strip()
            return
        # 防抖把这条并进上一轮了：这一轮是被吃掉的那条，别再回一次，
        # 也别重复计入情绪与记忆。
        try:
            if event.is_stopped():
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            self.engine.load()
            self._ensure_task()
            session_key = event.unified_msg_origin
            sender_id = str(event.get_sender_id() or "")
            # 拒绝名单：既不注入，也不采集（放在 _mark_llm 之前，免得被当成一轮对话记下来）
            if self._sender_denied(event):
                if self.settings.debug_log:
                    logger.info(f"[ai_mind] {sender_id} 在拒绝名单里，这一轮什么都不做")
                return
            self._last_sender[session_key] = sender_id
            if sender_id == DEBUG_UID and "FriendMessage" in str(session_key):
                self._bili_target = str(session_key)
            self._mark_llm(event)

            # ---------- 人格锚点 ----------
            # 身份和说话底线不随情绪、场合、谁在说话而变；放在最强前缀位，
            # 既压住「跑偏」，也让前缀缓存一直命中。
            try:
                anchor = self._persona_anchor(req)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 人格锚点组装失败：{exc}")
                anchor = ""
            if anchor and PERSONA_ANCHOR_MARK not in (req.system_prompt or ""):
                self._place_trusted(req, anchor)

            # 每轮都会变的内容攒在这里，最后统一贴到请求尾部（见 _cache_or_place）
            cache_tail: list[str] = []

            # ---------- 他问起「你看过什么视频」 ----------
            try:
                _share = self._bili_share_block(event)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[ai_mind] 组装最近视频失败：{exc}")
                _share = ""
            if _share:
                cache_tail.append(_share)

            # ---------- 群里被陌生人用「专属称呼」叫到 ----------
            # 这是最容易露馅的一处：一声「宝宝」要是被她当成自己人，
            # 或者回一句「在群里这么叫……」，关系就摆到群里了。
            try:
                _label_guard = self._group_label_guard(event, session_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 组装称呼护栏失败：{exc}")
                _label_guard = ""
            if _label_guard:
                cache_tail.append(_label_guard)

            # ---------- 每日日程 ----------
            # 放在这里是有意的：上面那些守卫（事件被拦、用户被拒、防抖吃掉）
            # 一旦命中就该什么都不注入 —— 日程也不例外。
            try:
                from .mind import schedule as _schedule
            except ImportError:  # pragma: no cover
                import mind.schedule as _schedule  # type: ignore[no-redef]
            _slot = self._schedule_block()
            if _slot:
                req.system_prompt = ((req.system_prompt or "") + chr(10) * 2 + _slot).strip()

            # ---------- 知识树（只注入这个会话激活的） ----------
            try:
                _kb = self._knowledge_block(session_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 知识注入失败：{exc}")
                _kb = ""
            if _kb:
                req.system_prompt = ((req.system_prompt or "") + chr(10) * 2 + _kb).strip()

            # ---------- 需求先搜市场 ----------
            try:
                _market = await self._market_for_turn(event)
            except Exception as exc:  # noqa: BLE001 - 搜市场失败不能影响聊天
                logger.warning(f"[ai_mind] 搜市场失败：{exc}")
                _market = ""
            if _market:
                req.system_prompt = ((req.system_prompt or "") + chr(10) * 2 + _market).strip()

            # ---------- 情绪 ----------
            self._diag["turns"] += 1
            if self.settings.emotion.enabled:
                _session, _relation, snapshot = self._prepare_emotion(event)
                if self.emotion_inject_rules:
                    current = req.system_prompt or ""
                    if EMOTION_RULES_MARKER not in current:
                        req.system_prompt = (
                            current + "\n\n" + self._emotion_rules_block()
                        ).strip()
                block = build_emotion_injection(
                    snapshot,
                    verbosity=str(cfg_get(self.config, "emotion.injection.verbosity", "normal") or "normal"),
                    bot_name=self.settings.emotion.bot_name,
                    with_trace=bool(cfg_get(self.config, "emotion.injection.with_trace", False)),
                    crisis_template=(
                        self.prompts.get("emotion.crisis")
                        if self.prompts.is_custom("emotion.crisis") else ""
                    ),
                    state_template=(
                        self.prompts.get("emotion.state")
                        if self.prompts.is_custom("emotion.state") else ""
                    ),
                )
                self._cache_or_place(
                    req, block, cache_tail,
                    trusted=self.emotion_trusted, mode=self.emotion_injection_mode,
                )

                # ---------- 认人：告诉模型"现在跟它说话的人是谁" ----------
                # 少了这一块，模型只知道"好感度 63/100 · ★宝宝"这种仪表盘读数，
                # 并不知道面前这个人就是宝宝 —— 于是会出现「报上名号来」「你是谁呀」。
                if self.settings.emotion.inject_identity:
                    try:
                        relation_block = render_relationship_block(
                            snapshot,
                            group=bool(is_group_session(session_key)),
                            min_messages=int(self.settings.emotion.identity_min_messages),
                            discretion=bool(self.settings.emotion.group_discretion),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[ai_mind] 组装认人块失败：{exc}")
                        relation_block = ""
                    if relation_block:
                        if self.emotion_inject_rules:
                            current = req.system_prompt or ""
                            if RELATION_MARKER not in current:
                                req.system_prompt = (
                                    current
                                    + chr(10) + chr(10)
                                    + self._prompt("relation.rules", RELATION_RULES_BLOCK)
                                ).strip()
                        if self.settings.cache_friendly:
                            # 身份块拆两半：**不变的**部分留在受信任位
                            # （群里任何人都覆盖不了、伪造不了），
                            # 会变的数字（聊过几次、好感度）贴到尾部。
                            stable_block = render_relationship_block(
                                snapshot,
                                group=bool(is_group_session(session_key)),
                                min_messages=int(self.settings.emotion.identity_min_messages),
                                discretion=bool(self.settings.emotion.group_discretion),
                                stable=True,
                            )
                            if stable_block:
                                self._place_trusted(req, stable_block)
                            meter = render_relationship_meter(snapshot)
                            if meter:
                                cache_tail.append(meter)
                        elif self.emotion_trusted:
                            self._place_trusted(req, relation_block)
                        else:
                            self._place(req, relation_block, self.emotion_injection_mode)

            # ---------- 记忆反馈（对上一轮注入的那几条） ----------
            if self.settings.memory.enabled:
                try:
                    self._apply_memory_feedback(session_key, event.message_str or "")
                except Exception as exc:  # noqa: BLE001 - 反馈算错不能影响聊天
                    logger.warning(f"[ai_mind] 记忆反馈失败：{exc}")

            # ---------- 记忆 ----------
            if self.settings.memory.enabled:
                query = event.message_str or ""
                query_vector = await self._embed_query(query) if self.settings.memory.use_embedding else None
                result = self.retriever.retrieve(
                    self.store,
                    query=query,
                    session_id=session_key,
                    sender_id=sender_id,
                    query_vector=query_vector,
                )
                # 注意：RetrievalResult 是三个桶（pinned/recent/relevant）+ memories()，
                # 没有 .items —— 之前这里按 .items 用，AttributeError 被 except 吞掉，
                # 结果拒绝名单根本没生效，那些人的记忆照样会被端上来。
                kept = None
                try:
                    kept = self._filter_denied_memories(result.memories())
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[ai_mind] 过滤拒绝名单失败，这一轮不注入记忆：{exc}")
                if kept is None:
                    # 过滤不了就一条都别注入：拒绝名单是隐私承诺，
                    # 宁可这一轮不回忆，也不能把不该出现的人的记忆端上去。
                    result.retain([])
                else:
                    allowed = self._persona_names() | set(self._declared_names(req.system_prompt or ""))
                    identity_kept = self._filter_identity_memories(kept, allowed)
                    if len(identity_kept) != len(kept):
                        logger.info(
                            "[ai_mind] 记忆里有 %d 条自称别的名字，已跳过注入"
                            % (len(kept) - len(identity_kept))
                        )
                        self._diag["identity_skipped"] = (
                            int(self._diag.get("identity_skipped") or 0)
                            + (len(kept) - len(identity_kept))
                        )
                    the_kept = identity_kept if identity_kept else []
                    if len(the_kept) != len(result.memories()):
                        result.retain(the_kept)
                memory_block = render_memory_block(
                    result, settings=self.settings.memory, session_id=session_key
                )
                if memory_block:
                    if self.memory_inject_rules:
                        current = req.system_prompt or ""
                        if MEMORY_RULES_MARKER not in current:
                            req.system_prompt = (
                            current
                            + chr(10) + chr(10)
                            + self._prompt("memory.rules", MEMORY_RULES_BLOCK)
                        ).strip()
                    self._cache_or_place(
                        req, memory_block, cache_tail,
                        trusted=False, mode=self.memory_injection_mode,
                    )
                    self.store.touch(result.ids())
                    # 记下来：下一轮用户要是说「不对」，评价的就是这几条
                    self._last_memory_ids[session_key] = list(result.ids())

            # ---------- 表达示例（先审后注入） ----------
            try:
                style_block = self._style_block(session_key, event.message_str or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 组装表达示例失败：{exc}")
                style_block = ""
            if style_block:
                self._cache_or_place(
                    req, style_block, cache_tail,
                    trusted=self.emotion_trusted, mode="content_part",
                )

            # ---------- 模仿指定 QQ 的说话习惯 ----------
            try:
                imitate_block = self._imitate_block(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 组装模仿块失败：{exc}")
                imitate_block = ""
            if imitate_block:
                self._cache_or_place(
                    req, imitate_block, cache_tail,
                    trusted=True, mode="content_part",
                )

            # ---------- 工具守卫（请求级） ----------
            self._apply_tool_guard(event, req)
            # 排查「某个工具到底在不在」用的：打开 debug_log 后，这一行会列出
            # 模型这一轮真正拿到的工具名。如果那个工具压根不在列表里，
            # 就说明它没注册成功；如果被我们摘掉了，上面会另有一条日志。
            if self.settings.debug_log:
                try:
                    tool_set = getattr(req, "func_tool", None)
                    names = [
                        str(getattr(item, "name", "") or "?")
                        for item in (getattr(tool_set, "tools", None) or [])
                    ]
                    logger.info(
                        f"[ai_mind] 本轮模型可用工具 {len(names)} 个：{', '.join(names) or '（一个都没有）'}"
                    )
                except Exception:  # noqa: BLE001
                    pass

            # ---------- 让 AI 知道它自己能发图 ----------
            if (
                self.settings.images.enabled
                and self.settings.images.ai_send_enabled
                and Comp is not None
            ):
                gallery = render_available_images(
                    self.images.enabled_triggers(sender_id),
                    max_per_message=self.settings.images.ai_send_max,
                )
                # 图库是按「谁在说话」挑的，群里换个人就变 —— 也算动态内容
                if gallery and self.settings.cache_friendly:
                    cache_tail.append(gallery)
                elif gallery and IMAGES_MARKER not in (req.system_prompt or ""):
                    req.system_prompt = ((req.system_prompt or "") + "\n\n" + gallery).strip()

            # ---------- 上下文压缩：聊太长只留要紧句 ----------
            if self.settings.memory.context_enabled:
                try:
                    plan = contextfit.plan(
                        req.contexts or [],
                        max_turns=int(self.settings.memory.context_max_turns),
                        keep_recent=int(self.settings.memory.context_keep_recent),
                        keep_important=int(self.settings.memory.context_keep_important),
                    )
                except Exception as exc:  # noqa: BLE001 - 压缩失败就按原样聊
                    logger.warning(f"[ai_mind] 压缩上下文失败：{exc}")
                    plan = None
                if plan and plan.get("dropped"):
                    req.contexts = plan["contexts"]
                    if plan.get("digest"):
                        cache_tail.append(plan["digest"])
                    self._diag["trimmed"] = (
                        int(self._diag.get("trimmed") or 0) + int(plan.get("dropped") or 0)
                    )
                    self._diag["saved_tokens"] = (
                        int(self._diag.get("saved_tokens") or 0)
                        + int(plan.get("saved_tokens") or 0)
                    )

            # ---------- 把每轮都变的内容贴到请求最后 ----------
            # 放这里而不是开头，是为了让前缀（persona + 历史）保持稳定，
            # 前缀缓存才命得中。
            if cache_tail:
                joined = "\n\n".join(cache_tail)
                if not self._append_content_part(req, joined):
                    # 这个 AstrBot 版本不支持附加到用户消息：退回受信任位。
                    # 宁可少省点 token，也不能把情绪和记忆弄丢。
                    self._place_trusted(req, joined)
            # 记个大概的注入量，面板「管理」页会显示（只估算，不精确）
            try:
                history = "".join(
                    str((row.get("content") if isinstance(row, dict) else "") or "")
                    for row in (req.contexts or [])
                )
                self._diag["last_tokens"] = contextfit.estimate_tokens(
                    (req.system_prompt or "") + history + "".join(cache_tail)
                )
                self._diag["last_chars"] = len(req.system_prompt or "") + len(history)
                self._last_prompt_seen = str(req.system_prompt or "")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 钩子里的异常绝不能打断对话
            logger.error(f"[ai_mind] 注入失败：{exc}", exc_info=True)

    def _prompt(self, key: str, fallback: str = "") -> str:
        """取一段可配置的提示词；仓库出问题就用代码里的默认值。"""
        try:
            text = self.prompts.get(key)
        except Exception:  # noqa: BLE001
            text = ""
        return (text or "").strip() or fallback

    def _persona_names(self) -> set[str]:
        """她自己可能叫的名字：配置里的 bot_name + 人格预设的 label。"""
        names: set[str] = set()
        for value in (
            self.settings.emotion.bot_name,
            getattr(getattr(self.engine, "preset", None), "label", ""),
        ):
            text = str(value or "").strip()
            if text:
                names.add(text)
        return names

    def _declared_names(self, system_prompt: str) -> list[str]:
        """从已有的系统提示词里找「你叫什么」：别的插件/人设卡可能已经写过。"""
        blob = str(system_prompt or "")
        if not blob:
            return []
        found: list[str] = []
        for pattern in (
            r"你(?:的名字)?(?:叫做|叫|名为|名叫|是)\s*[「『“\"']?([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{0,9})",
            r"你的名字[是为叫：:\s]+[「『“\"']?([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{0,9})",
        ):
            for match in re.finditer(pattern, blob):
                name = match.group(1).strip()
                if name and name not in ("谁", "什么", "我", "你", "她", "他") and name not in found:
                    found.append(name)
        return found

    def _persona_anchor(self, req: Any = None) -> str:
        """人格底座：身份、说话底线、危机覆盖。任何情绪都不能盖掉它。

        名字这里特别小心：插件不能硬塞一个和宿主人设不一样的名字 ——
        那会让她一会儿说自己是 A、一会儿说自己是 B（用户反馈过）。
        所以：配置里有名字才用它；系统里已有名字就照那个；出现多个就明确挑一个。
        """
        configured = str(self.settings.emotion.bot_name or "").strip()
        declared = self._declared_names(getattr(req, "system_prompt", "") or "")
        names = [value for value in ([configured] if configured else []) if value]
        for value in declared:
            if value not in names:
                names.append(value)
        note = ""
        try:
            note = (self.engine.preset.style_note or "").strip()
        except Exception:  # noqa: BLE001
            note = ""
        lines = [PERSONA_ANCHOR_MARK]
        # 宿主人设卡里已经写了名字时，以它为准 —— 插件再塞一个名字
        # 就会变成「一会儿流莹、一会儿桃子」。
        if declared:
            lines.append("你的名字以系统设定里的「%s」为准；身份不因情绪、场合、谁在说话而改变。"
                         % declared[0])
        elif configured:
            lines.append("你是%s。这个身份不因情绪、场合、谁在说话而改变。" % configured)
        else:
            lines.append("你的名字以系统设定为准；没有明确名字时不要自报名字。")
        if len(names) > 1:
            lines.append(
                "注意：系统里出现过多个名字（%s）。以人格设定里的那个为准，"
                "不确定就说「不告诉你」，绝不能在同一轮里换名字。" % "、".join(names[:5])
            )
        lines.extend([
            "说话底线（任何时候都成立）：",
            "1. 一次最多 3 条消息，每条要短；中文和中文之间不要空格；",
            "2. 不用客服腔、不写总结段、不排比、不写「作为…」这种句子；",
            "3. 出现自伤/轻生信号时，立刻放下口癖和玩笑，认真回应；",
            "4. 群里不点破你和用户的关系，不透露真名、学校、住址、生日；",
            "5. 别人问你叫什么，只用人设里的名字；不要自创第二个名字，"
            "也不要用聊天记录、记忆、示例里出现过的别人的名字。",
        ])
        if note:
            lines.append("人格设定里的味道（优先遵守）：" + note)
        lines.append("</persona_anchor>")
        return chr(10).join(lines)

    def _emotion_rules_block(self) -> str:
        """规则块 + 预设里那段自由文本的说话风格。"""
        block = self._prompt("emotion.rules", EMOTION_RULES_BLOCK)
        try:
            note = (self.engine.preset.style_note or "").strip()
        except Exception:  # noqa: BLE001
            note = ""
        if not note:
            return block
        return (
            block + "\n\n<persona_style>\n"
            "这是作者为她定下的说话味道，优先遵守：\n" + note + "\n</persona_style>"
        )

    def _cache_or_place(
        self,
        req: ProviderRequest,
        block: str,
        tail: list[str],
        *,
        trusted: bool = True,
        mode: str = "content_part",
    ) -> None:
        """放一块「每轮都会变」的内容。

        为什么要有这个：DeepSeek / OpenAI 的前缀缓存是按「从头开始的最长公共
        前缀」命中的。以前这些块塞在 System Prompt 后面（受信任位），每轮一变
        就把后面的整段历史全废掉，缓存一次都命中不了 —— 越聊越贵、越聊越慢。

        缓存友好模式（默认开）下攒进 tail，最后统一贴到当前用户消息后面；
        这样前缀 = persona + 历史记录，稳定不动，缓存能一直吃到。
        """
        if not block:
            return
        if self.settings.cache_friendly:
            tail.append(block)
            return
        if trusted:
            self._place_trusted(req, block)
        else:
            self._place(req, block, mode)

    @staticmethod
    def _place_trusted(req: ProviderRequest, block: str, tail: list[str] | None = None) -> bool:
        """把身份信息放进系统提示词。

        这里**曾经**往 req.contexts 里塞一条 role=system，那是个要命的误解：
        AstrBot 里 req.contexts 就是**对话历史本体**（每轮从 conv.history
        json.loads 出来的消息数组），不是「系统提示词之后的空位」。塞进去等于
        在历史末尾追加一条系统指令 —— 模型会把末尾的 system 当成**新一轮的设定**，
        于是前面刚聊过的内容像被洗掉一样，问过的事又问一遍。

        现在只写系统提示词：那是真正受信任的位置，而且身份块本身是稳定的，
        放在最前面照样吃满前缀缓存。
        """
        if not block:
            return False
        req.system_prompt = ((req.system_prompt or "") + chr(10) + chr(10) + block).strip()
        return True

    def _place(self, req: ProviderRequest, block: str, mode: str) -> None:
        if not block:
            return
        if mode not in {"content_part", "system_prompt", "both"}:
            mode = "content_part"
        placed = False
        if mode in {"content_part", "both"}:
            placed = self._append_content_part(req, block)
        if mode == "system_prompt" or (mode == "content_part" and not placed) or mode == "both":
            req.system_prompt = ((req.system_prompt or "") + "\n\n" + block).strip()

    @staticmethod
    def _append_content_part(req: ProviderRequest, text: str) -> bool:
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            return False
        part: Any
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                part = mark() or part
        except Exception:  # noqa: BLE001
            part = {"type": "text", "text": text}
        try:
            parts.append(part)
            return True
        except Exception:  # noqa: BLE001
            return False

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """消息发出前：摘掉情绪标记 + 记录这一轮对话。"""
        if not self.settings.enabled:
            return
        if self._is_stale():
            return
        if self.debug.is_active(event.unified_msg_origin):
            # 调试模式：不分段、不去 AI 味、不记记忆 —— 要的就是原文。
            return
        session_key = event.unified_msg_origin
        try:
            reply = await self._emotion_self_feedback(event, session_key)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 自表达回读失败：{exc}", exc_info=True)
            reply = ""
        try:
            reply = self._expand_pic_tags(event, session_key, reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 展开配图标记失败：{exc}", exc_info=True)
        is_llm = False
        try:
            key = self._event_key(event)
            is_llm = bool(key) and key in self._llm_messages
            if key:
                self._llm_messages.pop(key, None)
            # 去 AI 味：发送前的阀门。放在记记忆之前，记的是真正发出去的那版。
            await self._humanize_valve(event, session_key, is_llm=is_llm)
            await self._guard_reply(event, session_key)
            # 人设硬规则：中文之间不留空格。放在记录之前 —— 记的是真正发出去的那版。
            try:
                self._tidy_reply_spaces(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 清理中文空格失败：{exc}")
            self._learn_style(
                event, session_key, self._reply_text(event) or reply, is_llm
            )
            if is_llm and self.settings.memory.enabled:
                final_reply = self._reply_text(event) or reply
                if final_reply:
                    self._record_turn(event, final_reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记录对话失败：{exc}", exc_info=True)
        try:
            await self._split_and_send(event, session_key, is_llm=is_llm)
        except Exception as exc:  # noqa: BLE001 - 分段出问题也绝不能吞掉回复
            logger.error(f"[ai_mind] 分段发送失败：{exc}", exc_info=True)
        try:
            await self._maybe_sticker(event)
        except Exception as exc:  # noqa: BLE001 - 表情发不出去不影响正文
            logger.debug(f"[ai_mind] 发表情失败：{exc}")
        try:
            await self._maybe_voice(event, self._reply_text(event) or reply)
        except Exception as exc:  # noqa: BLE001 - 语音发不出去不影响正文
            logger.debug(f"[ai_mind] 发语音失败：{exc}")

    #: 当前活着的实例。插件重载时旧实例不会立刻消失，它的路由还会抢走
    #: 面板请求 —— 而旧实例的库是关着的、计数器停在 0，于是面板显示成
    #: 「写不进去、收到 0 轮」，用户只能靠重启整个进程才能甩掉它。
    #: 有了这个指针，旧实例就把请求转交给新实例，不用再重启。
    _LIVE: Any = None

    @staticmethod
    def _addressed_to_me(event: AstrMessageEvent) -> bool:
        """这条群消息是不是冲着我来的（@ 了、或者用了唤醒词）。

        AstrBot 的事件上带这几个标记，但不同版本名字不太一样，所以挨个试；
        一个都拿不到就**按「是冲我来的」处理** —— 宁可多扫一遍，不可漏拦。
        """
        for name in ("is_at_or_wake_command", "is_wake", "is_at_or_wake"):
            value = getattr(event, name, None)
            if isinstance(value, bool):
                return value
        return True

    def _live_peer(self) -> Any:
        live = type(self)._LIVE
        if live is not None and live is not self:
            return live
        return None

    def _disabled_notice(self) -> str:
        """我们是不是被 AstrBot 停用了？是的话返回一句人话。

        AstrBot 把停用的插件记在 data/shared_preferences.json 的
        inactivated_plugins 里。停用的插件会正常加载、面板也打得开，
        但所有钩子都不会被调用 —— 从插件内部看不出任何异常。
        """
        try:
            name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
            root = Path(self.data_dir).parent.parent
            pref = root / "shared_preferences.json"
            if not pref.exists():
                return ""
            data = json.loads(pref.read_text(encoding="utf-8"))
            inactive = data.get("inactivated_plugins") or []
        except Exception:  # noqa: BLE001 - 读不到就当没停用
            return ""
        for item in inactive:
            if name and name in str(item):
                return (
                    "插件在 AstrBot 里是【停用】状态 —— 面板打得开，但她的钩子"
                    "一次都不会被调用。去 AstrBot 面板 › 插件管理 把 " + name
                    + " 打开（或删掉 data/shared_preferences.json 里"
                    " inactivated_plugins 的这一条），然后重启 AstrBot。"
                )
        return ""

    def _tidy_reply_spaces(self, event: AstrMessageEvent) -> int:
        """把她这句话里中文中间的空格去掉。

        模型会写出「切 干嘛」「好 吧」这种 —— 人设要求她说话不留空格。
        这是硬规则，不能靠提示词碰运气。只动中文与中文之间的空白，
        「AstrBot 很好用」里的那个空格保持原样。
        """
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return 0
        chain = getattr(result, "chain", None) or []
        fixed = 0
        for comp in chain:
            text = getattr(comp, "text", None)
            if not isinstance(text, str) or not text:
                continue
            tidy = tidy_cjk_spaces(text)
            if tidy == text:
                continue
            try:
                comp.text = tidy
                fixed += 1
            except Exception:  # noqa: BLE001 - 改不动就算了，绝不能弄丢回复
                continue
        if fixed:
            logger.debug(f"[ai_mind] 清掉了 {fixed} 段里中文中间的空格")
        return fixed

    async def _emotion_self_feedback(self, event: AstrMessageEvent, session_key: str) -> str:
        if not self.settings.emotion.enabled or self.settings.emotion.self_feedback == "off":
            return self._reply_text(event)
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None)
        except Exception:  # noqa: BLE001
            return ""
        if not chain:
            return ""

        pieces: list[str] = []
        tags: list[str] = []
        for component in chain:
            if type(component).__name__ != "Plain":
                continue
            raw = getattr(component, "text", None)
            if not isinstance(raw, str) or not raw:
                continue
            cleaned, found = self._strip_tags(raw)
            if found:
                tags.extend(found)
                try:
                    component.text = cleaned
                except Exception:  # noqa: BLE001
                    pass
            pieces.append(cleaned)

        text = "\n".join(pieces).strip()
        if not text:
            return ""

        proto_key: str | None = None
        strength = 0.0
        for tag in tags:
            match = _pad_tag_regex().search(tag)
            if match:
                pad = PAD.of(float(match.group(1)), float(match.group(2)), float(match.group(3)))
                proto_key, strength = classify(pad).key, 1.0
                break
            proto = resolve_prototype(tag)
            if proto is not None:
                proto_key, strength = proto.key, 0.85
                break

        if proto_key is None and self.settings.emotion.self_feedback == "heuristic":
            hint = heuristic_self_emotion(text)
            if hint is not None:
                proto_key, strength = hint

        if proto_key is not None and session_key not in self._frozen:
            now = time.time()
            session = self.engine.resolve(session_key, now)
            if self.engine.apply_self_expression(session, proto_key, strength, now):
                self._maybe_sample(session_key, session.emotion, "self", now, force=True)
                self._schedule_flush()
        return text

    @staticmethod
    def _strip_tags(text: str) -> tuple[str, list[str]]:
        import re

        found: list[str] = []

        def _collect(match: Any) -> str:
            found.append(match.group(1).strip())
            return ""

        cleaned = EMOTION_TAG_RE.sub(_collect, text)
        if found:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, found

    @staticmethod
    def _reply_text(event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return ""
        chain = getattr(result, "chain", None)
        if not chain:
            return ""
        pieces: list[str] = []
        for component in chain:
            text = getattr(component, "text", None)
            if isinstance(text, str) and text.strip():
                pieces.append(text.strip())
        return "\n".join(pieces).strip()

    def _expand_pic_tags(self, event: AstrMessageEvent, session_key: str, fallback: str) -> str:
        """把回复里的 <pic>关键词</pic> 换成真正的图片段。

        标记无论能不能解析都必须摘掉 —— 否则用户会看到一串 <pic>xxx</pic>。
        """
        if Comp is None:
            return fallback
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None)
        except Exception:  # noqa: BLE001
            return fallback
        if not chain:
            return fallback

        allowed = self.settings.images.enabled and self.settings.images.ai_send_enabled
        limit = max(1, int(self.settings.images.ai_send_max))
        sender_id = str(event.get_sender_id() or "")
        new_chain: list[Any] = []
        pieces: list[str] = []
        used = 0
        touched = False

        for component in chain:
            text = getattr(component, "text", None)
            if type(component).__name__ != "Plain" or not isinstance(text, str) or not text:
                new_chain.append(component)
                continue
            if PIC_TAG_RE.search(text) is None:
                new_chain.append(component)
                pieces.append(text)
                continue

            touched = True
            position = 0
            for match in PIC_TAG_RE.finditer(text):
                before = text[position : match.start()]
                if before.strip():
                    new_chain.append(Comp.Plain(before))
                    pieces.append(before)
                position = match.end()
                token = (match.group("a") or match.group("b") or "").strip()
                if not allowed or used >= limit or not token:
                    continue
                trigger = self.images.resolve(
                    token,
                    sender_id=sender_id,
                    fuzzy_threshold=self.settings.images.fuzzy_threshold,
                )
                asset = self.images.get_image(trigger.image_id) if trigger else None
                path = self.images.path_of(asset.stored_name) if asset else None
                if path is not None and path.is_file():
                    new_chain.append(Comp.Image.fromFileSystem(str(path)))
                    used += 1
                    logger.info(f"[ai_mind] AI 主动发图：{token!r} -> {asset.stored_name}")
                else:
                    logger.info(f"[ai_mind] AI 想发图但没匹配到：{token!r}")
            tail = text[position:]
            if tail.strip():
                new_chain.append(Comp.Plain(tail))
                pieces.append(tail)

        if touched:
            try:
                chain[:] = new_chain
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 替换配图段失败：{exc}")
        return chr(10).join(part for part in pieces if part.strip()).strip()

    # ------------------------------------------------------------------
    # 拟人分段（把一条回复拆成几口气说出来）
    # ------------------------------------------------------------------
    def _splitter_file(self) -> Path:
        return self.data_dir / "splitter_overrides.json"

    def _load_splitter_overrides(self) -> dict[str, Any]:
        """面板上改的分段参数存在这，覆盖在插件配置之上。"""
        path = self._splitter_file()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取分段覆盖配置失败，先用配置文件：{exc}")
        return {}

    def _save_splitter_overrides(self, data: dict[str, Any]) -> bool:
        """直接写、写完读回来验一遍。

        不用「临时文件 + rename」那套：AstrBot 的数据目录有可能落在
        sdcardfs 上，那里 rename 会被拒。
        """
        path = self._splitter_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            json.loads(path.read_text(encoding="utf-8"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 保存分段覆盖配置失败：{exc}", exc_info=True)
            return False

    def _reload_splitter(self) -> None:
        self.splitter = splitter_engine.SplitterSettings.from_config(
            self.config, self._splitter_data
        )

    def _splitter_lock(self, session_key: str) -> asyncio.Lock:
        """同一个会话里分段发送串行化，免得两次回复的气泡交叉着冒出来。"""
        lock = self._splitter_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._splitter_locks[session_key] = lock
        if len(self._splitter_locks) > 256:  # 别让字典无限长大
            for key in list(self._splitter_locks)[:128]:
                if key != session_key and not self._splitter_locks[key].locked():
                    self._splitter_locks.pop(key, None)
        return lock

    async def _send_segment(self, event: AstrMessageEvent, segment: list[Any]) -> str:
        """把一段用主动发送吐出去。

        返回值必须分三种：确定发出去了、确定没发出去、抛异常（可能已经发出去了）。
        第三种绝不能当成「没发」再补一遍 —— 那正是刷屏的来源之一。
        """
        if splitter_engine.is_blank_segment(segment):
            return SEND_OK
        if MessageChain is None:
            return SEND_FAILED
        chain = None
        try:
            chain = MessageChain()
            chain.chain = list(segment)
        except Exception:  # noqa: BLE001 - 不同版本构造方式不一样
            try:
                chain = MessageChain(list(segment))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 拼不出分段消息链：{exc}")
                return SEND_FAILED
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
            return SEND_OK
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 分段发送失败：{exc}")
            return SEND_UNKNOWN

    async def _rollback_chain(self, chain: list[Any], leftovers: list[list[Any]]) -> None:
        """把还没发出去的分段塞回消息链，交给框架兜底。"""
        merged: list[Any] = []
        for segment in leftovers:
            merged.extend(segment)
        if not merged:
            return
        try:
            chain[:] = splitter_engine.merge_adjacent_plain(merged)
            logger.warning("[ai_mind] 分段发送中断，剩余内容已交回框架发送")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 分段回退失败，内容可能不完整：{exc}", exc_info=True)

    # ---- 语音兼容 ----
    async def _tts_enabled_for_session(self, event: AstrMessageEvent) -> bool:
        """会话级的 TTS 开关（有人用指令把某个会话的语音关掉了）。"""
        should = None
        if SessionServiceManager is not None:
            should = getattr(SessionServiceManager, "should_process_tts_request", None)
        if not callable(should):
            return True
        try:
            return bool(await should(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 查会话语音开关失败，按开启处理：{exc}")
            return True

    async def _read_framework_tts(
        self, event: AstrMessageEvent
    ) -> tuple[bool, float, Any, bool]:
        """框架内置 TTS 这条回复会不会生效。

        返回 (会不会走语音, 触发概率, TTS provider, 是否语音文字都要)。
        读不到、没配、会话关了，一律按「不走语音」处理 —— 读配置失败绝不能
        连带把发送也搞坏。
        """
        try:
            get_config = getattr(self.context, "get_config", None)
            if not callable(get_config):
                return False, 0.0, None, False
            try:
                all_cfg = get_config(event.unified_msg_origin)
            except TypeError:  # 老版本没这个参数
                all_cfg = get_config()
            if not hasattr(all_cfg, "get"):
                return False, 0.0, None, False
            tts_cfg = all_cfg.get("provider_tts_settings", {}) or {}
            if not hasattr(tts_cfg, "get") or not tts_cfg.get("enable", False):
                return False, 0.0, None, False
            get_tts = getattr(self.context, "get_using_tts_provider", None)
            provider = get_tts(event.unified_msg_origin) if callable(get_tts) else None
            if provider is None:
                return False, 0.0, None, False
            if not await self._tts_enabled_for_session(event):
                return False, 0.0, None, False
            try:
                probability = float(tts_cfg.get("trigger_probability", 1.0))
            except (TypeError, ValueError):
                probability = 1.0
            probability = max(0.0, min(1.0, probability))
            dual = bool(tts_cfg.get("dual_output", False))
            return True, probability, provider, dual
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读框架 TTS 配置失败，按不启用处理：{exc}")
            return False, 0.0, None, False

    def _make_record(self, path: Any, text: str) -> Any:
        if Comp is None:
            return None
        factory = getattr(Comp, "Record", None)
        if factory is None:
            return None
        try:
            return factory(file=str(path), url=str(path), text=text)
        except TypeError:
            try:
                return factory(file=str(path), text=text)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    async def _tts_segment(self, segment: list[Any], provider: Any, dual: bool) -> list[Any]:
        """把一段里的文字自己转成语音。

        为什么必须自己转：主动发送绕过了框架的 result_decorate 阶段，
        不自己转的话就是「前几段文字 + 只有尾段变语音」。
        """
        if provider is None or not segment:
            return segment
        out: list[Any] = []
        for comp in segment:
            if not splitter_engine.is_plain(comp):
                out.append(comp)
                continue
            text = splitter_engine.plain_text(comp)
            if len(text) <= 1:  # 框架自己也跳过一两个字的
                out.append(comp)
                continue
            try:
                path = await provider.get_audio(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 语音合成失败，这一段照旧发文字：{exc}")
                path = None
            record = self._make_record(path, text) if path else None
            if record is None:
                out.append(comp)
                continue
            out.append(record)
            if dual:  # 框架的 dual_output：语音和文字都要
                out.append(comp)
        return out

    async def _send_voice_then_whole_text(
        self, event: AstrMessageEvent, chain: list[Any]
    ) -> bool:
        """策略「语音 + 整条文字」：先发语音，再把整条文字一条发出去（不拆分）。"""
        voices = [comp for comp in chain if splitter_engine.is_record(comp)]
        others = [comp for comp in chain if not splitter_engine.is_record(comp)]
        texts = [comp for comp in others if splitter_engine.is_plain(comp)]
        rest = [comp for comp in others if not splitter_engine.is_plain(comp)]
        ok = True
        if voices:
            ok = (await self._send_segment(event, voices)) == SEND_OK and ok
        if texts or rest:
            ok = (await self._send_segment(event, texts + rest)) == SEND_OK and ok
        return ok

    async def _split_and_send(
        self, event: AstrMessageEvent, session_key: str, is_llm: bool = True
    ) -> None:
        """把这一轮的回复切成几段，前面的先主动发掉，最后一段留给框架。

        和语音有关的三个坑都在这里挡掉（都是真的被刷屏刷出来的）：

        1. 回复里带 [TTS] 这类标记 —— 有语音插件要拿整条去合成语音，
           分段会把标记当正文发出去，语音插件又只拿到最后一段；
        2. 消息链里同时有语音和文字（第三方 TTS 插件「文字+语音同发」）——
           再分段就等于同一句话念一遍、又打字打一遍；
        3. 框架内置 TTS 开着 —— 尾段留给框架会被它单独转成语音，
           于是「前几段文字 + 最后一段语音」。所以这时候全部自己发，
           并且整条只掷一次概率。
        """
        settings = self.splitter
        if not settings.enabled:
            self._last_split = {"segments": [], "sent": 0, "reason": "分段关着"}
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        chain = getattr(result, "chain", None)
        if not chain:
            return
        # 标记打在 result 上而不是事件上：Agent 工具调用那种流程，一个事件里会
        # 连着产出好几个全新的 result 对象，用事件级标记会漏掉后面几个。
        if getattr(result, "__ai_mind_segmented", False):
            return
        try:
            result.__ai_mind_segmented = True
        except Exception:  # noqa: BLE001
            pass

        # ---- 0. 语音标记：整条交给语音插件，一个字都别切 ----
        if settings.voice_tag_disable_split:
            tag = splitter_engine.find_voice_tag(chain, settings.voice_tags)
            if tag:
                logger.info(
                    f"[ai_mind] 回复里带语音标记 {tag}，整条交给语音插件处理，本次不分段"
                )
                return

        # ---- 1. 语音和文字同时在：按策略去掉重复的那一份 ----
        if splitter_engine.chain_has_voice(chain) and splitter_engine.chain_has_text(chain):
            policy = settings.voice_conflict_policy
            if policy == splitter_engine.VOICE_KEEP_TEXT:
                chain[:] = [comp for comp in chain if not splitter_engine.is_record(comp)]
                logger.info("[ai_mind] 消息链同时含语音与文字，按配置只保留文字")
            elif policy == splitter_engine.VOICE_THEN_TEXT:
                async with self._splitter_lock(session_key):
                    ok = await self._send_voice_then_whole_text(event, chain)
                if ok:
                    chain[:] = []
                    return
                logger.warning("[ai_mind] 语音优先发送失败，这条改回交给框架")
            elif policy != splitter_engine.VOICE_KEEP_BOTH:
                dropped = sum(1 for comp in chain if splitter_engine.is_plain(comp))
                chain[:] = [comp for comp in chain if not splitter_engine.is_plain(comp)]
                logger.info(
                    f"[ai_mind] 语音已经念过这条回复，丢掉 {dropped} 段重复文字（策略：仅发语音）"
                )

        text = "".join(splitter_engine.plain_text(comp) for comp in chain)
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001 - 判不出来当群聊处理（更保守）
            group = True
        if not splitter_engine.should_split(
            settings, session_id=session_key, is_group=group, text=text, is_llm=is_llm
        ):
            reason = "没到条件"
            if settings.llm_only and not is_llm:
                reason = "这不是模型的回复（llm_only）"
            elif settings.min_length and len(text) < settings.min_length:
                reason = "太短（%d < %d）" % (len(text), settings.min_length)
            elif settings.max_length and len(text) > settings.max_length:
                reason = "太长（%d > %d）" % (len(text), settings.max_length)
            elif not splitter_engine.scope_allows(settings, group):
                reason = "范围不允许（当前是" + ("群聊" if group else "私聊") + "）"
            self._last_split = {"segments": [], "sent": 0, "reason": reason, "text": text}
            return

        rules = splitter_engine.parse_replace_rules(settings.replace_rules)
        if rules or settings.clean_before_items or settings.clean_before_regex:
            for comp in chain:
                if not splitter_engine.is_plain(comp):
                    continue
                value = splitter_engine.plain_text(comp)
                if not value:
                    continue
                value = splitter_engine.apply_replace_rules(value, rules)
                value = splitter_engine.clean_text(value, settings, "before")
                splitter_engine.set_plain_text(comp, value)

        segments = splitter_engine.split_chain(list(chain), settings)
        if len(segments) <= 1:
            self._last_split = {"segments": [text], "sent": 0,
                                "reason": "只有一段，不用拆"}
            return  # 只有一段就别折腾了，原样交回框架
        self._last_split = {
            "segments": [splitter_engine.segment_text(seg) for seg in segments],
            "sent": 0,
            "reason": "",
        }

        if settings.clean_after_items or settings.clean_after_regex:
            for segment in segments:
                for comp in segment:
                    if not splitter_engine.is_plain(comp):
                        continue
                    splitter_engine.set_plain_text(
                        comp,
                        splitter_engine.clean_text(
                            splitter_engine.plain_text(comp), settings, "after"
                        ),
                    )

        # ---- 2. 框架内置 TTS：整条只掷一次骰子 ----
        tts_on = False
        tts_probability = 0.0
        tts_provider = None
        tts_dual = False
        if settings.tts_for_segments:
            tts_on, tts_probability, tts_provider, tts_dual = await self._read_framework_tts(event)
        tts_force: bool | None = None
        if tts_on:
            if settings.tts_probability_mode == splitter_engine.TTS_MODE_PER_SEGMENT:
                tts_force = None  # 逐段各掷一次（旧行为）
            else:
                tts_force = random.random() <= tts_probability
                if not tts_force:
                    logger.info("[ai_mind] 框架语音这次没触发，整条按文字发")
        # 框架 TTS 开着时尾段不能留给框架：它会自己再掷一次骰子，掷中就变成
        # 「前几段文字 + 最后一段语音」。所以这种情况下全部分段自己发完。
        takeover_all = tts_on

        def _should_tts() -> bool:
            if not tts_on:
                return False
            if tts_force is not None:
                return tts_force
            return random.random() <= tts_probability

        async def _shape(segment: list[Any]) -> list[Any]:
            """发送前最后一道：该转语音的转语音，带语音的摘掉引用。"""
            if not segment:
                return segment
            if settings.tts_for_segments and _should_tts():
                segment = await self._tts_segment(segment, tts_provider, tts_dual)
            if splitter_engine.chain_has_voice(segment):
                # 语音单独成条，带上引用容易变成空引用
                segment = [comp for comp in segment if not splitter_engine.is_reply(comp)]
            return segment

        limit = len(segments) if takeover_all else len(segments) - 1
        sent = 0
        async with self._splitter_lock(session_key):
            seen: set[tuple] = set()
            for index in range(limit):
                segment = await _shape(segments[index])
                if splitter_engine.is_blank_segment(segment):
                    continue
                fingerprint = splitter_engine.segment_fingerprint(segment)
                if fingerprint and fingerprint in seen:
                    logger.info("[ai_mind] 跳过同一条回复里的重复分段")
                    continue
                delay = 0.0
                if index + 1 < len(segments):
                    delay = splitter_engine.calculate_delay(
                        settings, splitter_engine.segment_text(segments[index + 1])
                    )
                status = await self._send_segment(event, segment)
                if status == SEND_FAILED:
                    # 确定没发出去：连同后面的一起塞回链表，宁可一次发完也不丢内容
                    await self._rollback_chain(chain, segments[index:])
                    return
                if status == SEND_UNKNOWN:
                    # 抛异常了，可能已经发出去了。语音最怕重复（会被再念一遍，
                    # 这正是用户反馈的那个刷屏），所以带语音的这段就当它发出去了；
                    # 纯文字则宁可重发一次，也不把这句话弄丢。
                    skip = index + 1 if splitter_engine.chain_has_voice(segment) else index
                    await self._rollback_chain(chain, segments[skip:])
                    return
                seen.add(fingerprint)
                sent += 1
                if delay > 0:
                    await asyncio.sleep(delay)
            if takeover_all:
                # 全发完了，清空链，别让框架再发一次（也免得它把尾段转成语音）
                chain[:] = []
            else:
                final = await _shape(segments[-1])
                try:
                    chain[:] = final
                except Exception:  # noqa: BLE001 - 有的链是只读的
                    try:
                        result.chain = final
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[ai_mind] 换不上最后一段：{exc}", exc_info=True)
                        return

        self._split_sent += sent
        try:
            self._last_split["sent"] = sent
        except Exception:  # noqa: BLE001
            pass
        if self.settings.debug_log:
            preview_text = " ｜ ".join(
                value[:20] for value in (splitter_engine.segment_text(seg) for seg in segments)
            )
            logger.info(f"[ai_mind] 分段发送 {sent} 段（{session_key}）：{preview_text}")
    # ------------------------------------------------------------------
    # 关键词配图
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=998)
    async def on_sticker_capture(self, event: AstrMessageEvent) -> None:
        """群友发的图/表情，收进表情包库（按内容哈希去重）。"""
        try:
            conf = self.sticker_settings
            if not conf.enabled or not conf.collect:
                return
            key = str(getattr(event, "unified_msg_origin", "") or "")
            if conf.sessions and key not in conf.sessions:
                return
            sender = str(event.get_sender_id() or "")
            if self._sender_denied(event):
                return
            for name, blob in self._event_image_blobs(event, conf.max_bytes):
                item = await asyncio.to_thread(
                    self.stickers.add, blob, Path(self.data_dir) / "stickers",
                    name, sticker_engine.guess_emotions(name, sender), "", "auto",
                    conf.max_bytes,
                )
                if item is not None and self.settings.debug_log:
                    logger.info(f"[ai_mind] 收到表情 {item.hash[:8]}（{item.path}）")
        except Exception as exc:  # noqa: BLE001 - 收图失败绝不能影响聊天
            logger.debug(f"[ai_mind] 收集表情失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def on_imitate_capture(self, event: AstrMessageEvent) -> None:
        """只学指定 QQ 说过的话（模仿用）。默认关，显式打开才开始记。"""
        try:
            conf = self.imitate_settings
            if not conf.enabled or not conf.targets:
                return
            sender = str(event.get_sender_id() or "")
            if not sender or sender not in conf.targets:
                return
            text = (event.message_str or "").strip()
            if not text or self._looks_like_command(text):
                return
            self.imitate.add_sample(
                sender, text, session_id=str(event.unified_msg_origin or "")
            )
        except Exception as exc:  # noqa: BLE001 - 采样失败绝不能影响聊天
            logger.debug(f"[ai_mind] 模仿采样失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """消息进来时先看配图触发词；命中就直接发图。

        这里用的是"消息事件监听器"而不是 on_llm_request —— 因为要能在
        调用 LLM **之前**就决定不回她了（event.stop_event()）。
        """
        if not self.settings.enabled or not self.settings.images.enabled or Comp is None:
            return
        try:
            text = (event.message_str or "").strip()
            if not text:
                return
            if self.settings.images.skip_commands and self._looks_like_command(text):
                return

            session_key = event.unified_msg_origin
            sender_id = str(event.get_sender_id() or "")
            trigger = self.images.match(
                text,
                sender_id=sender_id,
                fuzzy_threshold=self.settings.images.fuzzy_threshold,
            )
            if trigger is None:
                return
            if not self.image_cooldown.allow(session_key, trigger.id):
                return
            asset = self.images.get_image(trigger.image_id)
            if asset is None:
                return
            path = self.images.path_of(asset.stored_name)
            if not path.is_file():
                logger.warning(f"[ai_mind] 配图文件不在了：{path}")
                return

            if self.settings.debug_log:
                logger.info(
                    f"[ai_mind] 配图命中 {trigger.keyword!r}（{trigger.mode}）-> {asset.stored_name}"
                )

            chain: list[Any] = []
            if self.settings.images.reply_text:
                chain.append(Comp.Plain(self.settings.images.reply_text))
            chain.append(Comp.Image.fromFileSystem(str(path)))

            if self.settings.images.mode == "replace":
                # 只发图、不再走 LLM。但她的心情还是该被这句话影响。
                if self.settings.images.react_emotion:
                    try:
                        self._prepare_emotion(event)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[ai_mind] 配图时的情绪结算失败：{exc}")
                yield event.chain_result(chain)
                event.stop_event()
                return

            # before 模式：先发图，然后照常让 LLM 回复
            yield event.chain_result(chain)
        except Exception as exc:  # noqa: BLE001 - 配图失败绝不能影响聊天
            logger.error(f"[ai_mind] 配图处理失败：{exc}", exc_info=True)

    # ------------------------------------------------------------------
    # Web API（面板后端）
    # ------------------------------------------------------------------
    def _route_defs(self) -> list[tuple[str, Any, str]]:
        """面板的全部后端接口：(子路径, 处理函数, 说明)。"""
        return [
            ("sessions", self._api_sessions, "会话列表"),
            ("relationship", self._api_relationship, "专属用户与称呼"),
            ("settings", self._api_settings, "全部功能设置"),
            ("privacy", self._api_privacy, "隐私与数据"),
            ("privacy/save", self._api_privacy_save, "保存隐私设置"),
            ("privacy/deny", self._api_privacy_deny, "拒绝名单"),
            ("privacy/forget", self._api_privacy_forget, "忘掉某个人"),
            ("privacy/export", self._api_privacy_export, "导出档案"),
            ("wizard", self._api_wizard, "自检与推荐配置"),
            ("wizard/apply", self._api_wizard_apply, "一键套用推荐"),
            ("wizard/mode", self._api_wizard_mode, "傻瓜/专家模式"),
            ("style", self._api_style, "表达示例与审查队列"),
            ("style/save", self._api_style_save, "保存表达示例设置"),
            ("style/review", self._api_style_review, "批准/拒绝/回滚示例"),
            ("style/add", self._api_style_add, "手动添加示例"),
            ("style/clear", self._api_style_clear, "清空表达示例"),
            ("prompts", self._api_prompts, "提示词模板"),
            ("prompts/save", self._api_prompts_save, "保存提示词"),
            ("prompts/reset", self._api_prompts_reset, "还原提示词"),
            ("prompts/preview", self._api_prompts_preview, "预览提示词"),
            ("humanize", self._api_humanize, "去 AI 味设置与记录"),
            ("humanize/save", self._api_humanize_save, "保存去 AI 味设置"),
            ("humanize/reset", self._api_humanize_reset, "还原去 AI 味设置"),
            ("humanize/test", self._api_humanize_test, "试测一段话"),
            ("humanize/clear", self._api_humanize_clear, "清空体检记录"),
            ("settings/save", self._api_settings_save, "保存设置"),
            ("manage", self._api_manage, "管理与维护"),
            ("manage/memory", self._api_manage_memory, "批量清理记忆"),
            ("manage/data", self._api_manage_data, "维护数据"),
            ("theory", self._api_theory, "理论基础与词表命中"),
            ("theory/clear", self._api_theory_clear, "清空词表命中记录"),
            ("relationship/save", self._api_relationship_save, "保存专属用户"),
            ("relationship/relation", self._api_relationship_relation, "改好感度与熟悉度"),
            ("relationship/reset", self._api_relationship_reset, "还原专属用户"),
            ("background", self._api_background, "面板背景图"),
            ("debounce", self._api_debounce, "消息防抖状态"),
            ("debounce/preview", self._api_debounce_preview, "试判一段文字"),
            ("debounce/save", self._api_debounce_save, "保存防抖设置"),
            ("splitter", self._api_splitter, "分段设置"),
            ("splitter/preview", self._api_splitter_preview, "试切一段文字"),
            ("splitter/save", self._api_splitter_save, "保存分段设置"),
            ("splitter/reset", self._api_splitter_reset, "还原分段设置"),
            ("emotion", self._api_emotion, "情绪状态与曲线"),
            ("emotion/set", self._api_emotion_set, "设定当前情绪"),
            ("emotion/point", self._api_emotion_point, "增删改曲线上的点"),
            ("emotion/reset", self._api_emotion_reset, "重置情绪"),
            ("emotion/curve/clear", self._api_curve_clear, "清空曲线"),
            ("emotion/freeze", self._api_freeze, "冻结/解冻自动情绪"),
            ("memory", self._api_memory, "记忆列表"),
            ("graph", self._api_graph, "记忆图谱"),
            ("memory/add", self._api_memory_add, "手动添加记忆"),
            ("memory/trash", self._api_memory_trash, "垃圾桶：列表 / 恢复 / 彻底删除"),
            ("relation/series", self._api_relation_series, "感情线：关系随时间"),
            ("memory/update", self._api_memory_update, "修改记忆"),
            ("memory/delete", self._api_memory_delete, "删除记忆"),
            ("memory/batch", self._api_memory_batch, "批量管理记忆"),
            ("_echo", self._api_echo, "自检：原样返回收到的请求信息"),
            ("images", self._api_images, "配图列表"),
            ("images/upload", self._api_image_upload, "上传配图"),
            ("images/trigger/add", self._api_image_trigger_add, "新增触发词"),
            ("images/trigger/update", self._api_image_trigger_update, "修改触发词"),
            ("images/trigger/delete", self._api_image_trigger_delete, "删除触发词"),
            ("images/delete", self._api_image_delete, "删除配图"),
            ("images/batch", self._api_image_batch, "批量管理配图"),
            ("presets", self._api_presets, "人格预设列表"),
            ("presets/save", self._api_preset_save, "保存自定义预设"),
            ("presets/delete", self._api_preset_delete, "删除自定义预设"),
            ("presets/activate", self._api_preset_activate, "启用某个预设"),
            ("presets/import", self._api_preset_import, "导入预设 JSON"),
            ("imitate", self._api_imitate, "模仿指定 QQ"),
            ("imitate/save", self._api_imitate_save, "保存模仿设置"),
            ("imitate/clear", self._api_imitate_clear, "清空模仿样本"),
            ("mail", self._api_mail, "她的邮箱状态"),
            ("mail/save", self._api_mail_save, "保存邮箱配置"),
            ("mail/inbox", self._api_mail_inbox, "读收件箱"),
            ("mail/send", self._api_mail_send, "发一封邮件"),
            ("mail/test", self._api_mail_test, "发测试邮件"),
            ("mail/write", self._api_mail_write, "让她给好友写一封并寄出"),
            ("daily", self._api_daily, "今天是什么日子"),
            ("storyboard", self._api_storyboard, "视频分镜设置与自检"),
            ("storyboard/save", self._api_storyboard_save, "保存分镜设置"),
            ("storyboard/build", self._api_storyboard_build, "跑一次分镜"),
            ("storyboard/install-ffmpeg", self._api_storyboard_install_ffmpeg, "内置 ffmpeg"),
            ("bili", self._api_bili, "刷B站设置与已读"),
            ("bili/save", self._api_bili_save, "保存刷B站设置"),
            ("bili/scroll", self._api_bili_scroll, "刷一轮（同步）"),
            ("bili/scroll/start", self._api_bili_scroll_start, "刷一轮（后台任务）"),
            ("bili/scroll/state", self._api_bili_scroll_state, "查刷站进度"),
            ("bili/storyboard", self._api_bili_storyboard, "给单条视频做分镜"),
            ("bili/clear", self._api_bili_clear, "清空已读记录"),
            ("bili/login", self._api_bili_login, "校验登录凭据"),
            ("bili/history", self._api_bili_history, "我最近在看"),
            ("bili/image", self._api_bili_image, "取一张分镜图（缩略）"),
            ("bili/qr/start", self._api_bili_qr_start, "生成扫码登录二维码"),
            ("bili/qr/poll", self._api_bili_qr_poll, "轮询扫码结果"),
            ("voice", self._api_voice, "AI 声聊设置"),
            ("voice/save", self._api_voice_save, "保存声聊设置"),
            ("voice/characters", self._api_voice_characters, "拉音色列表"),
            ("poke", self._api_poke, "戳一戳设置"),
            ("poke/save", self._api_poke_save, "保存戳一戳设置"),
            ("sticker", self._api_sticker, "表情包库"),
            ("sticker/save", self._api_sticker_save, "保存表情设置"),
            ("sticker/tag", self._api_sticker_tag, "给表情打标"),
            ("sticker/delete", self._api_sticker_delete, "删一张表情"),
            ("sticker/clear", self._api_sticker_clear, "清空表情库"),
            ("qzone", self._api_qzone, "QQ空间：日记与说说"),
            ("qzone/save", self._api_qzone_save, "保存空间设置"),
            ("qzone/write", self._api_qzone_write, "写一篇日记（可配图）"),
            ("qzone/publish", self._api_qzone_publish, "发到 QQ 空间"),
            ("qzone/delete", self._api_qzone_delete, "删一条日记"),
            ("qzone/refresh", self._api_qzone_refresh, "拉空间最新说说"),
            ("market", self._api_market, "插件市场搜索"),
            ("market/search", self._api_market_search, "搜市场"),
            ("market/save", self._api_market_save, "保存市场搜索设置"),
            ("imagegen", self._api_imagegen, "生图设置与最近作品"),
            ("imagegen/save", self._api_imagegen_save, "保存生图配置"),
            ("imagegen/generate", self._api_imagegen_generate, "文生图"),
            ("imagegen/edit", self._api_imagegen_edit, "参考图编辑"),
            ("knowledge", self._api_knowledge, "知识树"),
            ("knowledge/tree", self._api_knowledge_tree, "建树 / 加节点"),
            ("knowledge/active", self._api_knowledge_active, "给会话挂上/摘掉"),
            ("knowledge/review", self._api_knowledge_review, "批准/丢弃模型改动"),
            ("animation", self._api_animation, "动图逐帧"),
            ("soundcheck", self._api_soundcheck, "听音频"),
        ]

    def _register_web_apis(self) -> None:
        context = getattr(self, "context", None)
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[ai_mind] 这个 AstrBot 版本没有 context.register_web_api，"
                "心智面板的后端接口不可用（聊天功能不受影响）"
            )
            return

        routes = self._route_defs()
        self._routes = {name: handler for name, handler, _ in routes}

        # 桥既有 GET 风格也有 POST 风格；而 AstrBot 的路由匹配会先看 HTTP 方法，
        # 只注册 GET 的话，一旦宿主用 POST 发就会直接落进「未找到该路由」。
        # 所以两种都注册，参数也从 query 与 body 两处一起读。
        methods = ["GET", "POST"]
        # 宿主可能用目录名，也可能用 plugin_id（作者/插件名）做命名空间，两边都铺。
        namespaces = (PLUGIN_NAME, PLUGIN_ID)

        ok = 0
        for namespace in namespaces:
            for name, handler, desc in routes:
                try:
                    register(f"/{namespace}/{name}", handler, methods, f"心智面板：{desc}")
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_mind] 注册面板接口 {namespace}/{name} 失败：{exc}")
            # 兜底：这个命名空间下没直接匹配上的路径都进这里。
            # 一来把宿主真正发的路径记进日志（上次排查就缺这个），
            # 二来按后缀分发，尽量把事情做成。
            try:
                register(f"/{namespace}/<path:rest>", self._api_fallback, methods, "心智面板：兜底")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 注册兜底路由失败：{exc}")

        logger.info(
            f"[ai_mind] 心智面板后端已注册 {ok} 个路由"
            f"（命名空间 {'、'.join(namespaces)}，方法 GET+POST）"
        )

    async def _params(self) -> dict[str, Any]:
        """把 query 与请求体合并成一份参数表。

        桥有 GET 和 POST 两种风格，参数放哪儿也不一定，所以三处都读：
        查询串、JSON body、以及"读不出 JSON 就直接读原始 body"兜底。

        注意不要给 web_request.json() 传 default 之类的关键字参数 ——
        那是后加的，老版本会直接 TypeError，然后被这里的 except 吞掉，
        结果是所有 POST 接口都收不到参数（这个坑真踩过）。
        """
        merged: dict[str, Any] = {}
        if web_request is None:
            return merged
        try:
            for key in list(web_request.query.keys()):
                merged[key] = web_request.query.get(key)
        except Exception:  # noqa: BLE001
            pass

        body = await self._read_body()
        if isinstance(body, dict):
            # 只把**这些名字**当成包装层展开；别的字典是普通参数值
            # （比如预设的 styles），不能再被当成包装层丢掉 ——
            # 这个坑 list 和 dict 各踩过一次。
            wrappers = ("params", "data", "query", "body")
            for wrapper in wrappers:
                inner = body.get(wrapper)
                if isinstance(inner, dict):
                    merged.update(inner)
            for key, value in body.items():
                if key in merged or key in wrappers:
                    continue
                merged[key] = value
        return merged

    async def _read_body(self) -> Any:
        """尽最大努力把请求体读成 dict。读不到就返回 None，绝不抛。"""
        if web_request is None:
            return None
        # 1) 先按 JSON 读（不传任何关键字参数，兼容老版本）
        try:
            data = await web_request.json()
            if isinstance(data, dict) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
        # 2) 读不出就自己啃原始 body：JSON 或表单编码都认
        try:
            raw = await web_request.body()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:  # noqa: BLE001
            return None
        text = text.strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
        # 表单编码
        try:
            parsed_form = {key: values[0] for key, values in urllib_parse.parse_qs(text).items() if values}
            return parsed_form or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _int_param(params: dict[str, Any], name: str) -> tuple[int | None, str]:
        """取一个必填整数参数；取不到时，把"后端到底收到了什么"一并带回去。

        这样界面上弹出的错误就能直接说明问题，不用再靠猜。
        """
        try:
            value = int(params.get(name))
            if value:
                return value, ""
        except (TypeError, ValueError):
            pass
        keys = "、".join(sorted(str(key) for key in params.keys())) or "（空）"
        return None, "缺少参数 " + name + "（后端实际收到：" + keys + "）"

    async def _api_echo(self) -> Any:
        """自检接口：把后端看到的路径与方法原样吐回来，方便定位桥的约定。"""
        info: dict[str, Any] = {
            "ok": True,
            "plugin": PLUGIN_NAME,
            "plugin_id": PLUGIN_ID,
            "author": PLUGIN_AUTHOR,
        }
        if web_request is not None:
            try:
                info["path"] = web_request.path
                info["method"] = web_request.method
                info["plugin_name_seen"] = web_request.plugin_name
                info["username"] = web_request.username
                info["query"] = {k: web_request.query.get(k) for k in web_request.query.keys()}
            except Exception:  # noqa: BLE001
                pass
        info["params"] = await self._params()
        return json_response(info)

    async def _api_fallback(self, rest: str = "") -> Any:
        """兜底路由：命名空间下没有直接匹配上的请求都会落到这里。"""
        live = self._live_peer()
        if live is not None:
            # 我是重载时留下的旧实例：库关了、计数器是 0。
            # 转交给活着的那个，用户就不用重启整个 AstrBot 了。
            return await live._api_fallback(rest)
        sub = str(rest or "").strip("/")
        # 宿主可能又套了一层命名空间（.../astrbot_plugin_ai_mind/astrbot_plugin_ai_mind/...），
        # 这里把多余的剥掉，免得只能靠后缀去猜
        for prefix in (PLUGIN_NAME + "/", PLUGIN_ID + "/"):
            while sub.startswith(prefix):
                sub = sub[len(prefix):]
        table = getattr(self, "_routes", {})
        logger.info(f"[ai_mind] 面板兜底路由收到：{sub!r}")
        handler = table.get(sub)
        if handler is None:
            for name in sorted(table, key=len, reverse=True):
                if sub == name or sub.endswith("/" + name):
                    handler = table[name]
                    break
        if handler is None:
            return error_response(f"未知的面板接口：{sub}", status_code=404)
        try:
            return await handler()
        except Exception as exc:  # noqa: BLE001
            # 面板接口抛异常时，宿主只会回一个 502，用户看到的就是
            # 「Request failed with status code 502」—— 什么线索都没有。
            # 这里自己兜住：原始异常进日志，人话进面板。
            logger.error(f"[ai_mind] 面板接口 {sub} 出错：{exc}", exc_info=True)
            return error_response(
                f"{sub} 出错：{type(exc).__name__}: {exc}", status_code=500
            )

    async def _api_sessions(self) -> Any:
        entries = session_entries(
            engine=self.engine, sample_store=self.samples, memory_store=self.store
        )
        for entry in entries:
            entry["frozen"] = entry["key"] in self._frozen
        return json_response({"sessions": entries, "stats": self.store.stats()})

    async def _api_emotion(self) -> Any:
        params = await self._params()
        key = str(params.get("session") or "").strip() or self._session_from_request()
        if not key:
            return json_response({"sessions": [], "empty": True})
        try:
            hours = float(params.get("hours") or self.settings.panel.curve_hours)
        except (TypeError, ValueError):
            hours = self.settings.panel.curve_hours
        hours = max(0.5, min(hours, 24 * 365.0))
        payload = emotion_payload(
            engine=self.engine,
            sample_store=self.samples,
            session_key=key,
            hours=hours,
            max_points=self.settings.panel.max_points_returned,
        )
        payload["frozen"] = key in self._frozen
        # 让面板能直接说出「采样坏了、为什么坏」，而不是只显示一片空白
        sample_error = str(getattr(self.samples, "last_error", "") or "")
        payload["samples_broken"] = bool(self._sample_broken) or bool(sample_error)
        payload["samples_error"] = sample_error
        # 最坑的一种「坏了」：插件根本没启用。AstrBot 里停用的插件照样加载、
        # 面板照样打得开，但钩子一次都不会被调用 —— 于是情绪结算 0 次、
        # 曲线永远是空的，而日志里一个错都没有。
        payload["plugin_disabled"] = self._disabled_notice()
        try:
            payload["db_check"] = self.samples.selfcheck()
        except Exception as exc:  # noqa: BLE001
            payload["db_check"] = {"ok": False, "write": False, "error": str(exc), "rows": 0}
        payload["diag"] = dict(self._diag)
        payload["diag"]["emotion_enabled"] = bool(self.settings.emotion.enabled)
        payload["settings"] = {
            "curve_hours": self.settings.panel.curve_hours,
            "sample_epsilon": self.settings.panel.sample_epsilon,
            "heartbeat_seconds": self.settings.panel.heartbeat_seconds,
        }
        return json_response(payload)

    async def _api_emotion_set(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        try:
            pad = PAD.of(float(params.get("p", 0)), float(params.get("a", 0)), float(params.get("d", 0)))
        except (TypeError, ValueError):
            return error_response("情绪数值不合法", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        self.engine.set_emotion(session, pad, now)
        self._maybe_sample(key, session.emotion, "manual", now, force=True)
        self.engine.flush(now)
        return json_response({"ok": True, "emotion": pad.to_dict()})

    async def _api_emotion_point(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        action = str(params.get("action") or "update").lower()
        try:
            t = float(params.get("t"))
        except (TypeError, ValueError):
            return error_response("时间点不合法", status_code=400)
        try:
            tolerance = max(0.5, float(params.get("tolerance") or 2.0))
        except (TypeError, ValueError):
            tolerance = 2.0

        if action == "delete":
            removed = self.samples.delete_at(key, t, tolerance=tolerance)
            return json_response({"ok": True, "removed": removed})

        try:
            pad = PAD.of(float(params.get("p", 0)), float(params.get("a", 0)), float(params.get("d", 0)))
        except (TypeError, ValueError):
            return error_response("情绪数值不合法", status_code=400)

        if action == "insert":
            self.samples.append(key, t, pad, "manual")
            self._last_sample.pop(key, None)
            return json_response({"ok": True, "inserted": True})

        changed = self.samples.replace_at(key, t, pad, tolerance=tolerance)
        if not changed:
            self.samples.append(key, t, pad, "manual")
            changed = 1
        self._last_sample.pop(key, None)
        return json_response({"ok": True, "changed": changed})

    async def _api_emotion_reset(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        self.engine.reset(session, now, keep_relations=bool(params.get("keep_relations", True)))
        self._maybe_sample(key, session.emotion, "manual", now, force=True)
        self.engine.flush(now)
        return json_response({"ok": True})

    async def _api_curve_clear(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        removed = self.samples.clear(key)
        self._last_sample.pop(key, None)
        return json_response({"ok": True, "removed": removed})

    async def _api_freeze(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        frozen = bool(params.get("frozen", True))
        if frozen:
            self._frozen.add(key)
        else:
            self._frozen.discard(key)
        return json_response({"ok": True, "frozen": frozen})

    async def _api_memory(self) -> Any:
        params = await self._params()

        def _pick(name: str, default: Any) -> Any:
            value = params.get(name)
            return default if value in (None, "") else value

        try:
            limit = int(_pick("limit", 50) or 50)
            offset = int(_pick("offset", 0) or 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0
        payload = memory_payload(
            store=self.store,
            query=str(_pick("q", "") or ""),
            scope=str(_pick("scope", "") or ""),
            kind=str(_pick("kind", "") or ""),
            pinned=_pick("pinned", None),
            sensitive=_pick("sensitive", None),
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
        return json_response(payload)

    async def _api_graph(self) -> Any:
        """记忆图谱：节点 + 边 + 簇，布局交给浏览器算。"""
        params = await self._params()

        def _number(name: str, default: float, low: float, high: float) -> float:
            raw = params.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        kinds = _split_keywords(params.get("kind") or params.get("kinds"))
        scopes = _split_keywords(params.get("scope") or params.get("scopes"))
        return json_response(
            graph_engine.build_graph(
                self.store,
                kinds=kinds,
                scopes=scopes,
                session_id=str(params.get("session_id") or ""),
                query=str(params.get("q") or ""),
                limit=int(_number("limit", 260, 10, 800)),
                min_importance=_number("min_importance", 0.0, 0.0, 1.0),
                pinned_only=str(params.get("pinned_only") or "").lower()
                in {"1", "true", "yes", "on"},
            )
        )

    async def _api_relation_series(self) -> Any:
        """感情线：这个会话里每个人的好感度随时间。"""
        params = await self._params()
        key = str(params.get("session") or "").strip() or self._session_from_request()
        try:
            hours = float(params.get("hours") or 336.0)
        except (TypeError, ValueError):
            hours = 336.0
        hours = max(1.0, min(hours, 24 * 365.0))
        try:
            series = self.samples.relation_series(key, time.time() - hours * 3600.0)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读取感情线失败：{exc}", status_code=500)
        return json_response({"session": key, "series": series, "hours": hours})

    async def _api_memory_trash(self) -> Any:
        """垃圾桶：GET 列表，POST 恢复或彻底删除。

        删除先进垃圾桶、1 天后后台才真正清掉。
        注意：/忘我 那种隐私擦除**不走这里**，它是立即真删。"""
        request = self._request()
        method = str(getattr(request, "method", "GET") or "GET").upper()
        params = await self._params()
        if method == "GET":
            items = self.store.trash_list(limit=200)
            now = time.time()
            out = []
            for item in items:
                gone = float(item.get("deleted_at") or 0.0)
                out.append({
                    "id": int(item.get("id") or 0),
                    "content": str(item.get("content") or ""),
                    "kind": str(item.get("kind") or ""),
                    "scope": str(item.get("scope") or ""),
                    "deleted_at": gone,
                    # 还剩几小时被真正清掉
                    "expires_in_hours": max(0.0, 24.0 - (now - gone) / 3600.0),
                })
            return json_response({"items": out, "keep_hours": 24})
        action = str(params.get("action") or "").strip().lower()
        raw_id = params.get("id")
        try:
            memory_id = int(raw_id)
        except (TypeError, ValueError):
            return error_response("缺少 id", status_code=400)
        if action == "restore":
            return json_response({"ok": bool(self.store.restore(memory_id))})
        if action == "purge":
            removed = self.store.purge_trash(time.time() + 1.0)
            return json_response({"ok": True, "purged": removed})
        return error_response("未知操作", status_code=400)

    async def _api_memory_add(self) -> Any:
        params = await self._params()
        content = str(params.get("content") or "").strip()
        if len(content) < 2:
            return error_response("内容太短", status_code=400)
        sensitive = bool(detect_sensitive(content)) if self.settings.memory.scan_sensitive else False
        scope = str(params.get("scope") or "").strip() or decide_scope(
            session_id=self._session_or_latest(params) or "manual",
            sensitive=sensitive,
            group_scope=self.settings.memory.group_scope,
            private_scope=self.settings.memory.private_scope,
        )
        if sensitive and scope != "user":
            scope = "user"
        keywords = params.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.replace("，", ",").split(",")]
        now = time.time()
        memory = Memory(
            content=content,
            keywords=tuple(str(k).strip() for k in keywords if str(k).strip()),
            kind=str(params.get("kind") or "fact"),
            scope=scope,
            session_id=self._session_or_latest(params) if scope == "session" else "",
            owner_id=str(params.get("owner_id") or "") if scope == "user" else "",
            importance=float(params.get("importance") or 1.0),
            pinned=bool(params.get("pinned", True)),
            sensitive=sensitive,
            sensitive_reasons=tuple(detect_sensitive(content)) if sensitive else (),
            source="manual",
            created_at=now,
            updated_at=now,
        )
        memory.id = self.store.add(memory)
        if not memory.id:
            return error_response("写入失败", status_code=500)
        return json_response({"ok": True, "id": memory.id, "sensitive": sensitive})

    async def _api_memory_update(self) -> Any:
        params = await self._params()
        try:
            memory_id = int(params.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少 id", status_code=400)
        memory = self.store.get(memory_id)
        if memory is None:
            return error_response("记忆不存在", status_code=404)
        content = str(params.get("content") or memory.content).strip()
        if len(content) < 2:
            return error_response("内容太短", status_code=400)
        sensitive = bool(detect_sensitive(content)) if self.settings.memory.scan_sensitive else False
        now = time.time()
        try:
            importance = float(params.get("importance", memory.importance))
        except (TypeError, ValueError):
            importance = memory.importance
        self.store._execute(
            "UPDATE memories SET content = ?, pinned = ?, importance = ?, sensitive = ?, updated_at = ? WHERE id = ?",
            (
                content,
                1 if bool(params.get("pinned", memory.pinned)) else 0,
                importance,
                1 if sensitive else 0,
                now,
                memory_id,
            ),
        )
        return json_response({"ok": True})

    async def _api_memory_batch(self) -> Any:
        """批量管理记忆：删除 / 设为常驻 / 取消常驻 / 改作用域。"""
        params = await self._params()
        ids = _coerce_int_list(params.get("ids"))
        if not ids:
            return error_response("没有选中任何记忆", status_code=400)
        action = str(params.get("action") or "").strip().lower()
        if action in {"delete", "remove", "删除"}:
            affected = self.store.trash_many(ids)
        elif action in {"pin", "set_pinned", "常驻"}:
            affected = self.store.update_many(ids, pinned=True)
        elif action in {"unpin", "unset_pinned", "取消常驻"}:
            affected = self.store.update_many(ids, pinned=False)
        elif action in {"scope", "set_scope", "作用域"}:
            scope = str(params.get("scope") or "").strip().lower()
            if scope not in {"session", "user", "global"}:
                return error_response("作用域不合法", status_code=400)
            fields: dict[str, Any] = {"scope": scope}
            # 换作用域时必须把不属于它的归属字段清掉，
            # 否则会留下"scope=global 却还挂着某个 session_id"的孤儿记忆
            if scope == "global":
                fields["session_id"] = ""
                fields["owner_id"] = ""
            elif scope == "user":
                fields["session_id"] = ""
            else:
                fields["owner_id"] = ""
            affected = self.store.update_many(ids, **fields)
        else:
            return error_response("不认识的操作：" + (action or "（空）"), status_code=400)

        return json_response({"ok": True, "action": action, "affected": affected, "ids": ids})

    async def _api_memory_delete(self) -> Any:
        params = await self._params()
        memory_id, why = self._int_param(params, "id")
        if memory_id is None:
            return error_response(why, status_code=400)
        return json_response({"ok": bool(self.store.trash(memory_id))})

    def _session_from_request(self) -> str:
        sessions = self.engine.session_keys()
        if sessions:
            sessions.sort(key=lambda k: self.engine.sessions[k].last_interaction, reverse=True)
            return sessions[0]
        return ""

    def _session_or_latest(self, params: dict[str, Any]) -> str:
        key = str((params or {}).get("session") or "").strip()
        return key or self._session_from_request()

    # -- 关键词配图 -----------------------------------------------------
    def _preview_data_url(self, asset: Any) -> str | None:
        """把图片转成 data URL 给面板预览；太大就不带，免得接口变慢。"""
        limit = self.settings.images.preview_max_bytes
        if asset.size and asset.size > limit:
            return None
        path = self.images.path_of(asset.stored_name)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > limit:
            return None
        mime = asset.mime or "image/png"
        if not mime.startswith("image/"):
            mime = "image/png"
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")

    async def _api_images(self) -> Any:
        params = await self._params()
        want_preview = str(params.get("preview", "1")).lower() not in {"0", "false", "no"}
        by_image: dict[int, list[dict[str, Any]]] = {}
        for trigger in self.images.list_triggers():
            by_image.setdefault(trigger.image_id, []).append(trigger.to_dict())
        items: list[dict[str, Any]] = []
        for asset in self.images.list_images():
            item = asset.to_dict()
            item["triggers"] = by_image.get(asset.id, [])
            item["preview"] = self._preview_data_url(asset) if want_preview else None
            items.append(item)
        return json_response(
            {
                "images": items,
                "stats": self.images.stats(),
                "modes": MODE_LABELS,
                "max_bytes": self.settings.images.max_bytes,
                "preview_max_bytes": self.settings.images.preview_max_bytes,
            }
        )

    async def _save_upload(self, upload: Any, target: Any) -> tuple[int | None, str]:
        """把上传的文件落到磁盘。

        PluginUploadFile.save() 的 max_bytes 参数是后加的 ——
        v4.28.1 上还没有，传了会直接 TypeError。所以先按新签名试，
        签名对不上再退回老签名。不能因为一个可选参数就让整个功能不可用。
        """
        try:
            written = await upload.save(target, max_bytes=self.settings.images.max_bytes)
            return int(written or 0), ""
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 把原因带回去，别让用户只看到"失败了"
            logger.warning(f"[ai_mind] 保存上传文件失败：{exc}")
            return None, str(exc)
        try:
            written = await upload.save(target)
            return int(written or 0), ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 保存上传文件失败（老签名）：{exc}")
            return None, str(exc)

    async def _api_image_upload(self) -> Any:
        params = await self._params()
        if web_request is None:
            return error_response("当前环境不支持文件上传", status_code=400)
        files_getter = getattr(web_request, "files", None)
        if not callable(files_getter):
            return error_response("这个 AstrBot 版本不支持读取上传文件", status_code=400)
        try:
            files = await files_getter()
        except Exception as exc:  # noqa: BLE001
            return error_response("读取上传内容失败：" + str(exc), status_code=400)
        items = list(files.items()) if files else []
        if not items:
            return error_response("没有收到文件", status_code=400)
        _field, upload = items[0]
        original_name = str(getattr(upload, "filename", "") or "")
        mime = str(getattr(upload, "content_type", "") or "")
        ext = guess_extension(mime, original_name)
        if not ext:
            return error_response("只支持 PNG / JPG / WEBP / GIF / BMP 图片", status_code=400)

        stored_name = uuid4().hex[:16] + ext
        target = self.images.path_of(stored_name)
        written, why = await self._save_upload(upload, target)
        if written is None:
            return error_response(
                "保存上传的文件失败：" + (why or "未知原因"), status_code=400
            )
        try:
            await upload.close()
        except Exception:  # noqa: BLE001
            pass

        size = int(written or 0)
        if size <= 0:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
        # save() 的 max_bytes 是后加的，老版本没有，所以自己再兜一道
        if size > self.settings.images.max_bytes:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return error_response(
                "图片太大了（" + human_size(size) + "，上限 "
                + human_size(self.settings.images.max_bytes) + "）",
                status_code=400,
            )
        image_id = self.images.add_image(
            stored_name=stored_name, original_name=original_name, mime=mime, size=size
        )
        if not image_id:
            return error_response("写入图片记录失败", status_code=500)

        added = self._add_triggers_for(image_id, params)
        return json_response(
            {"ok": True, "image_id": image_id, "triggers": added, "size": size}
        )

    def _add_triggers_for(self, image_id: int, params: dict[str, Any]) -> list[str]:
        keywords = _split_keywords(params.get("keywords"))
        mode = str(params.get("mode") or MODE_CONTAINS).strip().lower()
        if mode not in MODES:
            mode = MODE_CONTAINS
        scope = str(params.get("scope") or "global").strip().lower()
        owner_id = str(params.get("owner_id") or "").strip()
        added: list[str] = []
        for keyword in keywords:
            trigger_id = self.images.add_trigger(
                keyword=keyword,
                image_id=image_id,
                mode=mode,
                scope="user" if scope == "user" else "global",
                owner_id=owner_id if scope == "user" else "",
            )
            if trigger_id:
                added.append(keyword)
        return added

    async def _api_image_trigger_add(self) -> Any:
        params = await self._params()
        try:
            image_id = int(params.get("image_id"))
        except (TypeError, ValueError):
            return error_response("缺少 image_id", status_code=400)
        if self.images.get_image(image_id) is None:
            return error_response("图片不存在", status_code=404)
        added = self._add_triggers_for(image_id, params)
        if not added:
            return error_response("至少给一个关键词", status_code=400)
        return json_response({"ok": True, "triggers": added})

    async def _api_image_trigger_update(self) -> Any:
        params = await self._params()
        try:
            trigger_id = int(params.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少 id", status_code=400)
        fields: dict[str, Any] = {}
        if params.get("keyword") not in (None, ""):
            fields["keyword"] = str(params.get("keyword")).strip()
        if str(params.get("mode") or "") in MODES:
            fields["mode"] = str(params.get("mode"))
        if "enabled" in params:
            raw = params.get("enabled")
            if isinstance(raw, str):
                fields["enabled"] = raw.strip().lower() not in {"0", "false", "no", ""}
            else:
                fields["enabled"] = bool(raw)
        if str(params.get("scope") or "") in {"global", "user"}:
            fields["scope"] = str(params.get("scope"))
        if "owner_id" in params:
            fields["owner_id"] = str(params.get("owner_id") or "")
        if "priority" in params:
            try:
                fields["priority"] = int(params.get("priority") or 0)
            except (TypeError, ValueError):
                pass
        if not fields:
            return error_response("没有要改的字段", status_code=400)
        return json_response({"ok": bool(self.images.update_trigger(trigger_id, **fields))})

    async def _api_image_trigger_delete(self) -> Any:
        params = await self._params()
        try:
            trigger_id = int(params.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少 id", status_code=400)
        return json_response({"ok": self.images.delete_trigger(trigger_id)})

    async def _api_image_batch(self) -> Any:
        """批量管理配图：删除 / 启停触发词 / 改匹配方式 / 改作用域。"""
        params = await self._params()
        ids = _coerce_int_list(params.get("ids"))
        if not ids:
            return error_response("没有选中任何图片", status_code=400)
        action = str(params.get("action") or "").strip().lower()
        if action in {"delete", "remove", "删除"}:
            affected = self.images.delete_many(ids)
        elif action in {"enable", "on", "启用"}:
            affected = self.images.update_triggers_of_images(ids, enabled=True)
        elif action in {"disable", "off", "停用"}:
            affected = self.images.update_triggers_of_images(ids, enabled=False)
        elif action in {"scope", "作用域"}:
            scope = str(params.get("scope") or "").strip().lower()
            if scope not in {"global", "user"}:
                return error_response("作用域只能是 global 或 user", status_code=400)
            fields: dict[str, Any] = {"scope": scope}
            if scope == "global":
                fields["owner_id"] = ""
            affected = self.images.update_triggers_of_images(ids, **fields)
        elif action in {"mode", "匹配方式"}:
            mode = str(params.get("mode") or "").strip().lower()
            if mode not in MODES:
                return error_response("匹配方式不合法", status_code=400)
            affected = self.images.update_triggers_of_images(ids, mode=mode)
        else:
            return error_response("不认识的操作：" + (action or "（空）"), status_code=400)
        return json_response({"ok": True, "action": action, "affected": affected, "ids": ids})

    async def _api_image_delete(self) -> Any:
        params = await self._params()
        image_id, why = self._int_param(params, "id")
        if image_id is None:
            return error_response(why, status_code=400)
        return json_response({"ok": self.images.delete_image(image_id)})

    # -- 人格预设 -------------------------------------------------------
    # ------------------------------------------------------------------
    # 拟人分段：面板接口
    # ------------------------------------------------------------------
    def _splitter_known_keys(self) -> set[str]:
        return set(splitter_engine.SplitterSettings.from_mapping({}).to_dict().keys())

    def _splitter_payload(self, preview_text: str = "") -> dict[str, Any]:
        settings = self.splitter
        payload: dict[str, Any] = {
            "settings": settings.to_dict(),
            "defaults": splitter_engine.SplitterSettings.from_mapping({}).to_dict(),
            "schema": splitter_engine.panel_schema(),
            "describe": splitter_engine.describe(settings),
            "overrides": dict(self._splitter_data),
            "has_overrides": bool(self._splitter_data),
            "sent_segments": self._split_sent,
            "has_message_chain": MessageChain is not None,
        }
        if preview_text:
            payload["preview"] = splitter_engine.preview(preview_text, settings)
        return payload

    @staticmethod
    def _merge_splitter_settings(raw: Any, base: dict[str, Any], known: set[str]) -> dict[str, Any]:
        merged = dict(base)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in known and value is not None:
                    merged[key] = value
        return merged

    async def _api_background(self) -> Any:
        """面板背景图：按编号返回一张 data URI。

        为什么走这个接口、而不是让页面直接 <img src="bg/01.jpg">：
        插件页的静态资源链路（asset token / 路由 / 鉴权）在不同 AstrBot 版本里
        不一样，而面板的数据接口是确定通的 —— 所有面板数据都走它。
        图片本来就是虚化过的，base64 一遍也大不到哪去。
        """
        params = await self._params()
        folder = Path(__file__).resolve().parent / "pages" / "mind" / "bg"
        try:
            names = sorted(item.name for item in folder.glob("*.jpg"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取背景图目录失败：{exc}")
            names = []
        if not names:
            return error_response("找不到背景图（pages/mind/bg 是空的）")
        try:
            index = int(params.get("n") or 0)
        except (TypeError, ValueError):
            index = 0
        if index < 1 or index > len(names):
            # 没指定（或越界）就随机挑一张 —— 面板每次打开都会调这里
            index = random.randint(1, len(names))
        target = folder / names[index - 1]
        try:
            raw = await asyncio.to_thread(target.read_bytes)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读不到背景图 {names[index - 1]}：{exc}")
        return json_response(
            {
                "n": index,
                "count": len(names),
                # 字段名故意不叫 data —— 响应本身是 {"status":"ok","data":{...}}，
                # 再套一个 "data" 会让某些中转层拆错一层，把 base64 直接当结果返回。
                "image": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
            }
        )

    async def _api_debounce(self) -> Any:
        """防抖当前状态。"""
        settings = debounce_engine.DebounceSettings.from_config(
            self.config, self._debounce_data
        )
        return json_response(
            {
                "settings": settings.to_dict(),
                "describe": debounce_engine.describe(settings),
                "overrides": dict(self._debounce_data),
                "schema": debounce_engine.panel_schema(),
            }
        )

    async def _api_debounce_preview(self) -> Any:
        """试判：这句话会被立刻发，还是先等一等。"""
        params = await self._params()
        body = await self._read_body()
        text = ""
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("sample") or "")
        if not text:
            text = str(params.get("text") or params.get("sample") or "")
        settings = debounce_engine.DebounceSettings.from_config(
            self.config, self._debounce_data
        )
        result = debounce_engine.preview(text, settings)
        result["describe"] = debounce_engine.describe(settings)
        return json_response(result)

    async def _api_debounce_save(self) -> Any:
        """面板上改防抖开关与参数，写的是插件数据目录里的覆盖层。"""
        params = await self._params()
        body = await self._read_body()
        raw: Any = None
        if isinstance(body, dict):
            raw = body.get("settings") if isinstance(body.get("settings"), dict) else body
        if not isinstance(raw, dict):
            raw = params.get("settings") if isinstance(params.get("settings"), dict) else params
        known = set(debounce_engine.DebounceSettings().to_dict().keys())
        incoming = {
            key: value
            for key, value in (raw or {}).items()
            if key in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的防抖设置")
        merged = dict(self._debounce_data)
        merged.update(incoming)
        self._debounce_data = merged
        if not self._save_simple_overrides("debounce_overrides.json", merged, "消息防抖"):
            return error_response("写入防抖设置失败，详细原因见 AstrBot 日志")
        self._reload_from_config()
        settings = debounce_engine.DebounceSettings.from_config(
            self.config, self._debounce_data
        )
        return json_response(
            {
                "ok": True,
                "settings": settings.to_dict(),
                "describe": debounce_engine.describe(settings),
                "overrides": dict(merged),
            }
        )

    async def _api_splitter(self) -> Any:
        params = await self._params()
        sample = params.get("sample")
        return json_response(self._splitter_payload(sample if isinstance(sample, str) else ""))

    async def _api_splitter_preview(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        text = ""
        raw_settings: Any = None
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("sample") or "")
            raw_settings = body.get("settings")
        if not text:
            text = str(params.get("text") or params.get("sample") or "")
        if raw_settings is None:
            raw_settings = params.get("settings")
        known = self._splitter_known_keys()
        draft = self._merge_splitter_settings(raw_settings, self._splitter_data, known)
        settings = splitter_engine.SplitterSettings.from_config(self.config, draft)
        result = splitter_engine.preview(text, settings)
        result["describe"] = splitter_engine.describe(settings)
        result["dirty"] = draft != self._splitter_data
        if not text.strip():
            result["hint"] = "上面那框里写一段她可能会说的话，点「试切」看会被拆成几口气。"
        return json_response(result)

    async def _api_splitter_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        raw: Any = None
        if isinstance(body, dict):
            raw = body.get("settings") if isinstance(body.get("settings"), dict) else body
        if not isinstance(raw, dict):
            raw = params.get("settings") if isinstance(params.get("settings"), dict) else params
        known = self._splitter_known_keys()
        incoming = {
            key: value
            for key, value in (raw or {}).items()
            if key in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的分段设置")
        merged = dict(self._splitter_data)
        merged.update(incoming)
        self._splitter_data = merged
        if not self._save_splitter_overrides(merged):
            return error_response("写入分段设置失败，详细原因见 AstrBot 日志")
        self._reload_splitter()
        logger.info(f"[ai_mind] 分段设置已更新：{splitter_engine.describe(self.splitter)}")
        payload = self._splitter_payload()
        payload["saved"] = sorted(incoming.keys())
        return json_response(payload)

    async def _api_splitter_reset(self) -> Any:
        self._splitter_data = {}
        path = self._splitter_file()
        try:
            if path.is_file():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除分段覆盖配置失败：{exc}")
        self._reload_splitter()
        payload = self._splitter_payload()
        payload["reset"] = True
        return json_response(payload)
    def _preset_payload(self) -> dict[str, Any]:
        active = self.settings.emotion.preset_key
        builtin = [{"key": key, "label": value.label} for key, value in PRESETS.items()]
        custom = [
            {"key": key, "label": value.label}
            for key, value in load_custom_presets(self.data_dir).items()
        ]
        fallback_styles = PRESETS["tsundere"].style_overrides or {}
        emotions = [
            {
                "key": proto.key,
                "name": proto.name,
                "emoji": proto.emoji,
                "default_style": fallback_styles.get(proto.key, proto.style),
            }
            for proto in PROTOTYPES
        ]
        return {
            "builtin": builtin,
            "custom": custom,
            "active": active,
            "emotions": emotions,
            "draft": preset_to_dict(get_preset(active, self.data_dir)),
        }

    # ------------------------------------------------------------------
    # 去 AI 味：像阀门一样卡在发送之前
    # ------------------------------------------------------------------
    def _humanize_file(self) -> Path:
        return self.data_dir / "humanize_overrides.json"

    def _load_simple_overrides(self, filename: str, label: str) -> dict[str, Any]:
        """读一份「面板覆盖配置」（存在插件数据目录里，盖在 config 之上）。"""
        path = self.data_dir / filename
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[ai_mind] 读取{label}覆盖配置失败：{exc}')
        return {}

    def _save_simple_overrides(self, filename: str, data: dict[str, Any], label: str) -> bool:
        path = self.data_dir / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            json.loads(path.read_text(encoding='utf-8'))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f'[ai_mind] 保存{label}覆盖配置失败：{exc}', exc_info=True)
            return False

    def _load_humanize_overrides(self) -> dict[str, Any]:
        path = self._humanize_file()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取去 AI 味覆盖配置失败：{exc}")
        return {}

    def _save_humanize_overrides(self, data: dict[str, Any]) -> bool:
        path = self._humanize_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            json.loads(path.read_text(encoding="utf-8"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 保存去 AI 味覆盖配置失败：{exc}", exc_info=True)
            return False

    def _humanize_kwargs(self) -> dict[str, Any]:
        settings = self.settings.humanize
        return {
            "extra_ai_words": list(settings.extra_ai_words),
            "extra_templates": list(settings.extra_templates),
            "allow_words": list(settings.allow_words),
        }

    async def _rewrite_reply(self, session_key: str, text: str, report: Any) -> str:
        """把被打回的一段话交给模型重写。失败一律返回空串（调用方保留原文）。"""
        provider = await self._resolve_provider(session_key)
        if provider is None:
            return ""
        prompt = antiai.build_rewrite_prompt(
            report, text, template=self._prompt("humanize.rewrite", "") or None
        )
        timeout = max(5.0, float(self.settings.humanize.timeout))
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=self._prompt("humanize.rewrite_system", antiai.REWRITE_SYSTEM),
                ),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 重写失败绝不能影响发送
            logger.warning(f"[ai_mind] 去 AI 味重写失败，保留原文：{exc}")
            return ""
        raw = getattr(response, "completion_text", "") or ""
        return antiai.clean_rewrite(raw, text)

    async def _humanize_valve(
        self, event: AstrMessageEvent, session_key: str, is_llm: bool = True
    ) -> None:
        """发送前的阀门：体检这条回复像不像机器写的，不达标就打回重写（可迭代）。"""
        settings = self.settings.humanize
        if not settings.enabled or settings.mode == "off":
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        chain = getattr(result, "chain", None)
        if not chain:
            return
        if settings.llm_only and not is_llm:
            return
        plains = [comp for comp in chain if splitter_engine.is_plain(comp)]
        if not plains or len(plains) != len(chain):
            return  # 链里有图片之类的东西，只体检不改写，免得把图文顺序弄乱
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001
            group = True
        if settings.scope == "group" and not group:
            return
        if settings.scope == "private" and group:
            return
        text = "".join(splitter_engine.plain_text(comp) for comp in plains).strip()
        if len(text) < max(2, int(settings.min_length)):
            return

        kwargs = self._humanize_kwargs()
        report = antiai.analyse(text, **kwargs)
        before = report.score
        final_text = text
        rounds = 0
        note = ""
        if settings.mode == "rewrite" and before < settings.threshold:
            best_text, best_report = text, report
            for _round in range(max(1, int(settings.max_rounds))):
                candidate = await self._rewrite_reply(session_key, best_text, best_report)
                if not candidate or candidate.strip() == best_text.strip():
                    note = note or "重写没拿到有用结果，保留原文"
                    break
                rounds += 1
                candidate_report = antiai.analyse(candidate, **kwargs)
                if candidate_report.score > best_report.score or not settings.keep_best:
                    best_text, best_report = candidate.strip(), candidate_report
                else:
                    note = "改完分数反而更低，留了原来那版"
                    break
                if best_report.score >= settings.threshold:
                    break
            if best_text.strip() and best_text.strip() != text:
                final_text = best_text.strip()
                report = best_report
        after = report.score
        if final_text != text:
            for index, comp in enumerate(plains):
                splitter_engine.set_plain_text(comp, final_text if index == 0 else "")
        try:
            self.humanize_log.record(HumanizeEntry(
                t=time.time(),
                session_id=session_key,
                before_score=before,
                after_score=after,
                rounds=rounds,
                mode=settings.mode,
                issues="、".join(check.label for check in report.issues),
                original=text,
                final=final_text,
                note=note,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 记录去 AI 味体检失败：{exc}")
        if rounds and self.settings.debug_log:
            logger.info(
                f"[ai_mind] 去 AI 味：{before:.0f} → {after:.0f}（改了 {rounds} 轮）{text[:18]}"
            )

    # ------------------------------------------------------------------
    # 隐私：拒绝名单 / 忘我 / 导出
    # ------------------------------------------------------------------
    def _is_denied(self, uid: str, platform: str = "", platform_id: str = "") -> bool:
        """这个人在拒绝名单里吗（既不采集也不注入）。"""
        for entry in self.settings.privacy.denied_users or ():
            if guard_engine.identity_matches(entry, uid, platform, platform_id):
                return True
        return False

    def _sender_denied(self, event: AstrMessageEvent) -> bool:
        try:
            uid, platform, platform_id = self._identity_of(event)
        except Exception:  # noqa: BLE001
            return False
        return self._is_denied(uid, platform, platform_id)

    def _filter_denied_memories(self, memories: Any) -> list[Any]:
        """把"关于被拒绝的人"的记忆从注入里摘掉。"""
        denied = set()
        for entry in self.settings.privacy.denied_users or ():
            scope, target = guard_engine.parse_identity(entry)
            if target and target != "*":
                denied.add(target)
        if not denied:
            return list(memories or ())
        return [
            item for item in (memories or ())
            if str(getattr(item, "owner_id", "") or "") not in denied
        ]

    def _privacy_summary(self, uid: str) -> dict[str, Any]:
        """某个人在插件里留下了什么（忘我和导出都用它）。"""
        if not uid:
            return {"memories": 0, "relations": 0, "styles": 0, "lexicon_hits": 0}
        memories = 0
        try:
            rows, total = self.store.query_page(limit=1, offset=0)
            _ = rows
            memories = int(self.store.count_for_owner(uid))
        except Exception:  # noqa: BLE001
            memories = 0
        relations = 0
        try:
            relations = len([
                relation for _session, relation in self.engine.all_relations()
                if str(getattr(relation, "uid", "") or "") == uid
            ])
        except Exception:  # noqa: BLE001
            relations = 0
        styles = 0
        try:
            styles = len(self.styles.list(query="", limit=1)[0])
            rows = self.styles.list(query="", limit=500)[0]
            styles = len([item for item in rows if uid in (item.session_id or "")])
        except Exception:  # noqa: BLE001
            styles = 0
        hits = 0
        try:
            conn = self.lexicons.conn
            if conn is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM lexicon_hits WHERE uid = ?", (uid,)
                ).fetchone()
                hits = int(row["c"] or 0) if row else 0
        except Exception:  # noqa: BLE001
            hits = 0
        return {"memories": memories, "relations": relations, "styles": styles,
                "lexicon_hits": hits}

    def _forget_user(self, uid: str, *, include_group: bool = False) -> dict[str, int]:
        """把他留下的东西全删掉。"""
        removed = {"memories": 0, "relations": 0, "styles": 0, "lexicon_hits": 0}
        if not uid:
            return removed
        try:
            removed["memories"] = int(self.store.clear(owner_id=uid) or 0)
            if include_group:
                for session in self.store.distinct_sessions():
                    if uid in str(session):
                        removed["memories"] += int(
                            self.store.clear(session_id=str(session)) or 0
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除记忆失败：{exc}")
        try:
            removed["relations"] = int(self.engine.forget_user(uid) or 0)
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除关系失败：{exc}")
        try:
            removed["styles"] = int(self.styles.clear_by_owner(uid) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除表达示例失败：{exc}")
        try:
            removed["lexicon_hits"] = int(self.lexicons.clear_by_uid(uid) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除词表命中失败：{exc}")
        return removed

    def _export_user(self, uid: str) -> dict[str, Any]:
        """导出某个人的档案（给自己留一份，或者交给本人）。"""
        payload: dict[str, Any] = {
            "uid": uid,
            "exported_at": time.time(),
            "plugin": PLUGIN_NAME,
            "version": "v2.2.0",
            "memories": [],
            "relations": [],
            "styles": [],
        }
        try:
            rows, _total = self.store.query_page(limit=1000, offset=0)
            payload["memories"] = [
                {
                    "content": item.content,
                    "kind": item.kind,
                    "scope": item.scope,
                    "pinned": bool(item.pinned),
                    "sensitive": bool(item.sensitive),
                    "importance": round(float(item.importance or 0.0), 2),
                    "created_at": item.created_at,
                }
                for item in rows
                if str(getattr(item, "owner_id", "") or "") == uid
            ]
        except Exception:  # noqa: BLE001
            pass
        try:
            for session, relation in self.engine.all_relations():
                if str(getattr(relation, "uid", "") or "") != uid:
                    continue
                payload["relations"].append({
                    "session": session,
                    "name": relation.name,
                    "affinity": round(float(relation.affinity or 0.0), 1),
                    "familiarity": round(float(relation.familiarity or 0.0), 1),
                    "msg_count": int(relation.msg_count or 0),
                    "special": bool(relation.special),
                })
        except Exception:  # noqa: BLE001
            pass
        try:
            rows = self.styles.list(query="", limit=500)[0]
            payload["styles"] = [
                {"user": item.user_text, "reply": item.reply_text,
                 "status": item.status, "score": item.score}
                for item in rows if uid in (item.session_id or "")
            ]
        except Exception:  # noqa: BLE001
            pass
        return payload

    async def _api_privacy(self) -> Any:
        params = await self._params()
        uid = str(params.get("uid") or "").strip() or str(self._last_sender.get(
            str(params.get("session") or ""), ""
        ) or "")
        return json_response({
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "uid": uid,
            "summary": self._privacy_summary(uid) if uid else {},
            "has_overrides": bool(self._privacy_data),
        })

    async def _api_privacy_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        raw = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        known = set(PrivacySettings().to_dict().keys())
        incoming = {
            str(key): value for key, value in (raw or {}).items()
            if str(key) in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的设置")
        merged = dict(self._privacy_data)
        merged.update(incoming)
        self._privacy_data = merged
        if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
            return error_response("写入隐私设置失败，详细原因见 AstrBot 日志")
        self._reload_from_config()
        result = {
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "has_overrides": True,
            "saved": sorted(incoming.keys()),
        }
        return json_response(result)

    async def _api_privacy_deny(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        entry = str(payload.get("uid") or "").strip()
        action = str(payload.get("action") or "add").strip().lower()
        if not entry:
            return error_response("要指定一个人")
        current = list(self.settings.privacy.denied_users)
        if action in {"remove", "allow", "移除", "允许"}:
            current = [item for item in current if item != entry]
        else:
            if entry not in current:
                current.append(entry)
        merged = dict(self._privacy_data)
        merged["denied_users"] = current
        self._privacy_data = merged
        if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
            return error_response("写入拒绝名单失败")
        self._reload_from_config()
        logger.info(f"[ai_mind] 拒绝名单更新：{action} {entry}（共 {len(current)} 人）")
        return json_response({
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "action": action,
        })

    async def _api_privacy_forget(self) -> Any:
        """忘我 / 管理员删人：把某个人留下的东西全删掉。"""
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return error_response("要指定删谁的")
        include_group = str(payload.get("include_group") or "").lower() in {
            "1", "true", "yes", "on"
        }
        removed = self._forget_user(uid, include_group=include_group)
        logger.warning(f"[ai_mind] 忘掉 {uid}：{removed}")
        return json_response({"ok": True, "uid": uid, "removed": removed,
                              "summary": self._privacy_summary(uid)})

    async def _api_privacy_export(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return error_response("要指定导出谁的")
        data = self._export_user(uid)
        logger.info(
            f"[ai_mind] 导出 {uid} 的档案：{len(data['memories'])} 条记忆、"
            f"{len(data['relations'])} 条关系"
        )
        return json_response({"ok": True, "archive": data})

    # ------------------------------------------------------------------
    # 让她自己会回忆、会记事（LLM 工具）
    # ------------------------------------------------------------------
    @llm_tool(name="send_sticker")
    async def tool_send_sticker(self, event: AstrMessageEvent, mood: str = "") -> str:
        """从表情包里挑一张发出去。想用表情回应、或者气氛适合甩一张图时调用。

        Args:
            mood(string): 想要的情绪，joy/angry/sad/shy/surprised/bored/tired/affection/confused/proud；留空按此刻心情挑
        """
        if Comp is None or not self.sticker_settings.enabled:
            return "表情包没开。"
        key = str(event.unified_msg_origin or "")
        item = self.stickers.pick(str(mood or ""), self._sticker_recent.get(key, []))
        if item is None:
            return "表情包里还没有合适的。"
        try:
            await self.context.send_message(key, [Comp.Image.fromFileSystem(item.path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 发表情失败：{exc}")
            return "发失败了。"
        self.stickers.mark_used(item.id)
        recent = self._sticker_recent.setdefault(key, [])
        recent.append(item.id)
        del recent[:-8]
        return "已经发了一张表情（%s）。" % (",".join(item.emotions) or "无标签")

    @llm_tool(name="search_sticker")
    async def tool_search_sticker(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """看看表情库里有哪些图，只查不发。想知道有没有能用的表情时调用。

        Args:
            keyword(string): 情绪或描述关键词，留空列最近的几张
        """
        rows = self.stickers.list(limit=300)
        word = str(keyword or "").strip().lower()
        hits = [
            item for item in rows
            if not word
            or word in " ".join(item.emotions).lower()
            or word in str(item.desc or "").lower()
        ]
        if not hits:
            return "没有相关的表情。"
        rows_text = "；".join(
            "#%d %s" % (item.id, ",".join(item.emotions) or "未打标") for item in hits[:5]
        )
        return "有 %d 张：%s" % (len(hits), rows_text)

    @llm_tool(name="recall_long_term_memory")
    async def tool_recall(self, event: AstrMessageEvent, query: str = "") -> str:
        """回忆关于对方的长期记忆。想不起他之前说过什么、要确认某个细节时调用。

        Args:
            query(string): 想回忆的关键词或话题；留空就返回最重要和最常被想起的几条
        """
        if not self.settings.memory.enabled:
            return "记忆功能没开。"
        session_id = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        try:
            if query:
                found = self._search(str(query).strip(), session_id, sender_id)
                if found:
                    return "想起来了：" + chr(10) + chr(10).join(
                        "- " + item.content for item in found[:8]
                    )
            result = self.retriever.retrieve(
                self.store, query=str(query or ""),
                session_id=session_id, sender_id=sender_id,
            )
            found_memories = result.memories()
            if not found_memories:
                return "关于他，我这儿什么都没记着。"
            self.store.touch(result.ids())
            return "我记得这些：" + chr(10) + chr(10).join(
                "- " + item.content for item in found_memories[:8]
            )
        except Exception as exc:  # noqa: BLE001 - 工具报错不能把对话搞崩
            logger.warning(f"[ai_mind] 回忆工具失败：{exc}")
            return "想不起来了（内部出错了）。"

    @llm_tool(name="web_search")
    async def tool_web_search(self, event: AstrMessageEvent, query: str = "") -> str:
        """联网搜索。要查最新消息、查资料、确认记不准的事实时调用。

        平时不用它；只有你不确定、或者事情明显是「最近才发生的」才用。
        搜不到就照实说没查到，别硬编。

        Args:
            query(string): 要搜的关键词，尽量短而准
        """
        text = str(query or "").strip()
        if not text:
            return "没给关键词，搜不了。"
        try:
            from .mind import websearch as _websearch
        except ImportError:  # pragma: no cover
            import mind.websearch as _websearch  # type: ignore[no-redef]
        try:
            # urllib 是阻塞的：必须丢到线程里，否则最长 8 秒会把事件循环卡住
            # （卡住的不只是这一条，是整个 AstrBot）。
            results = await asyncio.to_thread(_websearch.search, text, 5)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 联网搜索失败：{exc}")
            return "搜索出错了，这次先按你已知的回答。"
        if not results:
            return "没搜到（可能网络不通，或者对方限流）。可以凭已有知识回答，但要说清不确定。"
        logger.info(f"[ai_mind] 联网搜索 {text!r}：拿到 {len(results)} 条")
        return (
            "搜索结果：\n" + _websearch.render(results, limit=5)
            + "\n\n用这些内容回答，别把链接念出来，也别照抄整段。"
        )
    @llm_tool(name="memorize_long_term_memory")
    async def tool_memorize(
        self, event: AstrMessageEvent, content: str, kind: str = "fact"
    ) -> str:
        """把值得长期记住的信息记下来。对方说了重要的事、喜好、约定、身体情况时调用。

        Args:
            content(string): 要记住的内容，一句话写清楚
            kind(string): 类型：fact 事实 / preference 喜好 / event 经历 / promise 约定 / relation 关系 / other
        """
        if not self.settings.memory.enabled:
            return "记忆功能没开，没记。"
        body = str(content or "").strip()
        if len(body) < 2:
            return "内容太短，没记。"
        sender_id = str(event.get_sender_id() or "")
        session_id = event.unified_msg_origin
        try:
            group = bool(is_group_session(session_id))
        except Exception:  # noqa: BLE001
            group = True
        kind_value = str(kind or "fact").strip().lower()
        if kind_value not in set(KINDS):
            kind_value = "fact"
        try:
            sensitive = tuple(detect_sensitive(body))
        except Exception:  # noqa: BLE001
            sensitive = ()
        try:
            scope = decide_scope(
                sensitive=bool(sensitive), session_id=session_id, owner_id=sender_id,
                group=group, group_scope="session", private_scope="user",
            )
        except TypeError:
            scope = "session" if group else "user"
        memory = Memory(
            content=body[:300],
            keywords=tuple(style_engine.keywords_of(body)),
            kind=kind_value,
            scope=scope,
            session_id="" if scope != "session" else session_id,
            owner_id="" if scope == "global" else sender_id,
            importance=0.75,
            sensitive=bool(sensitive),
            sensitive_reasons=sensitive,
            source="manual",
        )
        try:
            memory_id = self.store.add(memory)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 记事工具失败：{exc}")
            return "没记住（内部出错了）。"
        if not memory_id:
            return "这条已经记过了。"
        logger.info(f"[ai_mind] 她自己记下一条（{kind_value}）：{body[:32]}")
        return "记住了。"

    # ------------------------------------------------------------------
    # 傻瓜模式：自检 + 一键推荐
    # ------------------------------------------------------------------
    def _wizard_checks(self, session: str = "") -> list[dict[str, Any]]:
        """把新手最容易卡住的地方逐条查一遍。"""
        checks: list[dict[str, Any]] = []
        emotion = self.settings.emotion
        memory = self.settings.memory
        guard = self.settings.guard
        styles = self.settings.style
        humanize = self.settings.humanize
        splitter = self.splitter

        masters = self._masters()
        if not masters:
            checks.append({
                "key": "owner", "level": "error", "title": "你还不是她的「特别的人」",
                "detail": "一个专属用户都没填。她不会认人，别人问「你是谁」她是真的不知道。",
                "fix": "去「人格定制 → 专属用户」把你自己填进去",
                "action": "goto:persona",
            })
        else:
            checks.append({
                "key": "owner_ok", "level": "ok",
                "title": f"已设置 {len(masters)} 个专属用户",
                "detail": "主人鉴权按这份名单判定。", "fix": "", "action": "",
            })
        if not self.settings.enabled:
            checks.append({
                "key": "disabled", "level": "error", "title": "插件总开关是关的",
                "detail": "关着的话情绪和记忆都不会介入对话。",
                "fix": "在「全部功能设置 → 总开关」打开", "action": "goto:settings",
            })
        if not emotion.enabled:
            checks.append({
                "key": "emotion_off", "level": "warn", "title": "情绪模块关着",
                "detail": "她不会有心情起伏，也不会认人（认人和情绪共用同一套关系数据）。",
                "fix": "在「全部功能设置 → 情绪」打开", "action": "goto:settings",
            })
        if not memory.enabled:
            checks.append({
                "key": "memory_off", "level": "warn", "title": "记忆模块关着",
                "detail": "她记不住任何事，每次都是从零开始。",
                "fix": "在「全部功能设置 → 记忆」打开", "action": "goto:settings",
            })
        elif not memory.provider_id:
            checks.append({
                "key": "extract_model", "level": "warn", "title": "没单独指定抽取用的模型",
                "detail": "会用当前对话模型抽取记忆，等于每几轮多花一次对话的钱。",
                "fix": "建议选一个便宜的小模型", "action": "goto:settings",
            })
        if not styles.auto_approve:
            counts = self.styles.counts()
            if int(counts.get("pending", 0) or 0) >= 5:
                checks.append({
                    "key": "style_pending", "level": "warn",
                    "title": f"有 {counts['pending']} 条表达示例等你批",
                    "detail": "批准之后才会被当成参考，一直不批就等于白学。",
                    "fix": "去「表达」页签点批准", "action": "goto:style",
                })
        if humanize.enabled and humanize.mode == "rewrite" and humanize.threshold < 50:
            checks.append({
                "key": "humanize_strict", "level": "warn", "title": "去 AI 味门槛偏低",
                "detail": f"低于 {humanize.threshold} 分才打回，几乎每次都要重写一遍，会明显变慢。",
                "fix": "建议调到 60~75", "action": "goto:humanize",
            })
        if splitter.enabled and splitter.max_segments > 5:
            checks.append({
                "key": "split_many", "level": "warn",
                "title": f"一条回复最多会拆成 {splitter.max_segments} 口气",
                "detail": "配合延迟会显得很啰嗦，人设里一般写的是 1~3 条。",
                "fix": "建议改成 3", "action": "goto:split",
            })
        if not emotion.lexicon_enabled:
            checks.append({
                "key": "lexicon_off", "level": "warn", "title": "词表兜底关着",
                "detail": "只按你写的规则判断情绪，日常表达她可能毫无反应。",
                "fix": "打开「情绪 → 情绪评估 → 用中文情感词汇本体兜底」", "action": "goto:settings",
            })
        try:
            probe = self.data_dir / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            checks.append({
                "key": "data_dir", "level": "error", "title": "插件数据目录写不进去",
                "detail": f"情绪、记忆、示例都存不下来：{exc}",
                "fix": "检查 AstrBot 的 data/plugin_data 权限", "action": "",
            })
        return checks

    def _wizard_payload(self, session: str = "") -> dict[str, Any]:
        order = {"error": 0, "warn": 1, "ok": 2}
        checks = sorted(
            self._wizard_checks(session), key=lambda item: order.get(item["level"], 3)
        )
        errors = len([item for item in checks if item["level"] == "error"])
        warns = len([item for item in checks if item["level"] == "warn"])
        if errors:
            summary = f"有 {errors} 个必须修的问题"
        elif warns:
            summary = f"一切正常，另有 {warns} 条建议"
        else:
            summary = "一切正常"
        return {
            "checks": checks, "errors": errors, "warns": warns, "summary": summary,
            "stats": self._manage_stats(),
            "toggles": {
                "emotion": bool(self.settings.emotion.enabled),
                "memory": bool(self.settings.memory.enabled),
                "lexicon": bool(self.settings.emotion.lexicon_enabled),
                "splitter": bool(self.splitter.enabled),
                "humanize": bool(self.settings.humanize.enabled),
                "style": bool(self.settings.style.enabled),
                "guard": bool(self.settings.guard.enabled),
            },
            "recommended": WIZARD_RECOMMENDED,
            "auto_learn": {
                "memory": bool(self.settings.memory.enabled),
                "style": bool(self.settings.style.enabled),
                "humanize": bool(self.settings.humanize.enabled),
                "sticker": bool(self.sticker_settings.collect),
            },
            "context": {
                "enabled": bool(self.settings.memory.context_enabled),
                "max_turns": int(self.settings.memory.context_max_turns),
                "keep_recent": int(self.settings.memory.context_keep_recent),
            },
            "tokens": {
                "last": int(self._diag.get("last_tokens") or 0),
                "trimmed": int(self._diag.get("trimmed") or 0),
                "saved": int(self._diag.get("saved_tokens") or 0),
                "cache_friendly": bool(self.settings.cache_friendly),
            },
            "mode": str(
                self._load_simple_overrides("panel_mode.json", "面板模式").get("mode") or "simple"
            ),
        }

    async def _api_wizard(self) -> Any:
        params = await self._params()
        session = str(params.get("session") or "").strip() or self._session_or_latest(params)
        return json_response(self._wizard_payload(session))

    async def _api_wizard_apply(self) -> Any:
        """一键套用推荐配置。

        只写「面板覆盖层」，不动用户的 config 文件 —— 想还原随时能还原。
        """
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        groups = payload.get("groups")
        wanted = set(groups) if isinstance(groups, list) and groups else set(WIZARD_RECOMMENDED)
        layers = (
            ("humanize", "humanize_overrides.json", "去 AI 味", "_humanize_data"),
            ("style", "style_overrides.json", "表达示例", "_style_data"),
            ("guard", "guard_overrides.json", "鉴权", "_guard_data"),
            ("splitter", "splitter_overrides.json", "分段", "_splitter_data"),
        )
        applied: list[str] = []
        for key, filename, label, attr in layers:
            if key not in wanted or key not in WIZARD_RECOMMENDED:
                continue
            current = dict(getattr(self, attr, None) or {})
            current.update(WIZARD_RECOMMENDED[key])
            if self._save_simple_overrides(filename, current, label):
                setattr(self, attr, current)
                applied.append(key)
        emotion_changes = WIZARD_RECOMMENDED.get("emotion") or {}
        if "emotion" in wanted and emotion_changes:
            mapping = {
                "lexicon": "emotion.appraisal.lexicon",
                "lexicon_gain": "emotion.appraisal.lexicon_gain",
                "trusted": "emotion.injection.trusted",
            }
            save = getattr(self.config, "save_config", None)
            if callable(save):
                for key, path in mapping.items():
                    if key in emotion_changes:
                        self._set_config_value(path, emotion_changes[key])
                try:
                    save()
                    applied.append("emotion")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_mind] 推荐配置写回 config 失败：{exc}")
        self._reload_from_config()
        result = self._wizard_payload(str(payload.get("session") or ""))
        result["applied"] = applied
        result["note"] = (
            "已套用推荐配置（写在面板覆盖层里，随时可以还原）" if applied
            else "没能写入任何配置，看看日志"
        )
        return json_response(result)

    async def _api_wizard_mode(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        current = dict(self._load_simple_overrides("panel_mode.json", "面板模式"))
        changed = False
        if "mode" in payload:
            mode = str(payload.get("mode") or "").strip().lower()
            if mode in ("simple", "expert"):
                current["mode"] = mode
                changed = True
        if "glass" in payload:
            current["glass"] = bool(payload.get("glass"))
            changed = True
        if "bg_blur" in payload:
            # 背景虚化强度：面板上拖滑杆就存这儿，不用改 config
            try:
                current["bg_blur"] = max(0, min(16, int(float(payload.get("bg_blur")))))
                changed = True
            except (TypeError, ValueError):
                pass
        if changed:
            self._save_simple_overrides("panel_mode.json", current, "面板模式")
        try:
            blur = max(0, min(16, int(current.get("bg_blur"))))
        except (TypeError, ValueError):
            blur = max(0, min(16, int(getattr(self.settings.panel, "bg_blur", 4))))
        return json_response({
            "mode": str(current.get("mode") or "simple"),
            "background": bool(getattr(self.settings.panel, "background", True)),
            "bg_blur": blur,
            "glass": bool(current.get("glass", getattr(self.settings.panel, "glass_blur", False))),
        })

    # ------------------------------------------------------------------
    # 鉴权：谁是主人
    # ------------------------------------------------------------------
    def _identity_of(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """(用户 ID, 平台类型, 平台实例 ID)。"""
        uid = str(event.get_sender_id() or "")
        platform = platform_id = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            platform_id = ""
        return uid, platform, platform_id

    def _masters(self) -> list[str]:
        """主人 = 「专属用户」+ 额外名单：面板上填了专属用户就等于认了主人。"""
        masters = [str(item) for item in self.settings.emotion.special_users if str(item)]
        masters.extend(str(item) for item in self.settings.guard.extra_masters if str(item))
        return masters

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        guard = self.settings.guard
        if not guard.enabled:
            return True
        uid, platform, platform_id = self._identity_of(event)
        if not uid:
            return False
        masters = self._masters()
        if masters and guard_engine.is_owner(uid, masters, platform, platform_id):
            return True
        if guard.trust_admins:
            try:
                if event.is_admin():
                    return True
            except Exception:  # noqa: BLE001
                pass
        if not masters:
            # 一个主人都没配的时候拦下来只会把自己的 Bot 拦死：放行，
            # 但启动日志里已经明确提醒过去填专属用户了。
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1005)
    async def on_group_switch(self, event: AstrMessageEvent) -> None:
        """群聊对话开关：一句话让她在所有群 / 单个群里闭嘴或回魂。

        优先级最高（1005）：闭嘴的群，消息要在**任何**处理之前就掐掉 ——
        不然别的钩子还会为她算情绪、抽记忆，甚至插一句「这个我不能帮你做」。

        判定顺序跟原插件一致：全局关 → 哪个群都不回；全局开 → 再看单群。
        """
        if not self.settings.enabled:
            return
        if self._is_stale():
            return
        try:
            key = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            return
        if not is_group_session(key):
            return                       # 只管群聊，私聊不受影响
        group_id = key.split(":")[-1]
        try:
            text = (event.message_str or "").strip()
        except Exception:  # noqa: BLE001
            return
        # ---- 开关指令：只有主人能发 ----
        if text in GROUP_COMMANDS:
            try:
                owner = self._is_owner(event)
            except Exception:  # noqa: BLE001
                owner = False
            if not owner:
                return                   # 别人发这四个词：当普通消息，别理
            if text == GROUP_OPEN_ALL:
                self.groupswitch.set_all(True)
                reply = "已开启对话 —— 所有群都回。"
            elif text == GROUP_CLOSE_ALL:
                self.groupswitch.set_all(False)
                reply = "已关闭对话 —— 所有群都不回。"
            elif text == GROUP_OPEN_ONE:
                self.groupswitch.set_one(group_id, True)
                reply = "本群已开启对话。"
            else:
                self.groupswitch.set_one(group_id, False)
                reply = "本群已关闭对话。"
            logger.info(f"[ai_mind] 群聊开关：{text} -> {group_id}")
            await self._debug_reply(event, reply)
            return
        # ---- 闭嘴的群：什么都不做，直接掐掉 ----
        if not self.groupswitch.is_open(group_id):
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1004)
    async def on_poke(self, event: AstrMessageEvent):
        """被戳一下：走标准 LLM 链路接一句，人设/情绪/记忆/分段都会生效。"""
        try:
            conf = self.poke_settings
            if not conf.enabled:
                return
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not poke_engine.is_poke(raw):
                return
            get_self = getattr(event, "get_self_id", None)
            self_id = str(get_self() if callable(get_self) else (raw or {}).get("self_id") or "")
            if not poke_engine.hits_me(raw, self_id):
                return
            sender = poke_engine.poke_sender(raw) or str(event.get_sender_id() or "")
            group = poke_engine.poke_group(raw)
            get_group = getattr(event, "get_group_id", None)
            if not group and callable(get_group):
                group = str(get_group() or "")
            is_admin = False
            try:
                is_admin = bool(event.is_admin())
            except Exception:  # noqa: BLE001
                is_admin = False
            allowed, reason = self.poke_gate.allow(
                time.time(), sender=sender, group=group, is_admin=is_admin
            )
            if not allowed:
                return
            request_llm = getattr(event, "request_llm", None)
            if not callable(request_llm):
                return          # 老版本框架没有这条链路
            self.poke_gate.mark(time.time(), sender=sender, group=group)
            name = ""
            try:
                name = str(event.get_sender_name() or "")
            except Exception:  # noqa: BLE001
                name = ""
            prompt = poke_engine.build_prompt(
                conf,
                username=name or sender or "对方",
                user_id=sender,
                scene="群聊" if group else "私聊",
                has_context=True,
            )
            yield request_llm(prompt=prompt)
        except Exception as exc:  # noqa: BLE001 - 戳一戳失败绝不能影响聊天
            logger.warning(f"[ai_mind] 戳一戳处理失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1002)
    async def on_force_search(self, event: AstrMessageEvent) -> None:
        """强制联网搜索：我说「搜一下 xxx」就搜，结果原样给你看。

        和她自己判断的那种（web_search 工具）是两回事：
        这条**不经过她改写**，直接把标题/摘要/链接摆出来，方便你自己核对。
        群聊里只认冲她来的消息，且只有主人能用 —— 免得被人当查询机刷。
        """
        if not self.settings.enabled:
            return
        try:
            text = (event.message_str or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not text:
            return
        # 先按首字/前两字筛掉绝大多数消息，这个钩子对每条消息都会跑
        if text[:1] not in ("/", "／", "#") and not text.startswith(("搜一下", "搜索", "查一下")):
            return
        query = ""
        for prefix in ("搜一下", "搜索", "查一下", "/搜", "／搜", "#搜", "/search", "#search"):
            if text.startswith(prefix):
                query = text[len(prefix):].strip(" ：:")
                break
        if not query:
            return
        session_key = event.unified_msg_origin
        # 群里必须是在跟她说话；私聊直接认
        if is_group_session(session_key) and not self._addressed_to_me(event):
            return
        # 只有主人能用：否则群里任何人都能拿她当查询机刷
        try:
            if not self._is_owner(event):
                return
        except Exception:  # noqa: BLE001
            return
        event.stop_event()          # 这条由我来回，别再让她答一遍
        try:
            from .mind import websearch as _websearch
        except ImportError:  # pragma: no cover
            import mind.websearch as _websearch  # type: ignore[no-redef]
        try:
            results = await asyncio.to_thread(_websearch.search, query, 5)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 强制搜索失败：{exc}")
            await self._debug_reply(event, "搜索出错了：" + str(exc))
            return
        logger.info(f"[ai_mind] 强制搜索 {query!r}：拿到 {len(results)} 条")
        if not results:
            await self._debug_reply(event, "没搜到（网络不通，或者对方限流了）。")
            return
        lines = ["🔍 " + query]
        for index, item in enumerate(results, 1):
            lines.append("")
            lines.append(str(index) + ". " + str(item.get("title") or ""))
            snippet = str(item.get("snippet") or "").strip()
            if snippet:
                lines.append("   " + snippet[:100])
            lines.append("   " + str(item.get("url") or ""))
        await self._debug_reply(event, chr(10).join(lines))
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1001)
    async def on_debug_code(self, event: AstrMessageEvent) -> None:
        """隐藏调试开关 + 调试命令，只作用于发口令的那一个会话。

        优先级比消息闸门还高，免得口令被当成可疑消息拦掉。
        刻意不做进面板、也不在任何接口里暴露 —— 面板是给人看的，
        这种能绕过人格的后门不该出现在上面被误触。
        """
        try:
            text = (event.message_str or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not text:
            return
        # 早退：这个钩子对**每一条消息**都会跑，先按首字符筛掉绝大多数，
        # 别为了一个口令把每条群消息都拖进来做比较。
        if text[:1] != "#" and text != DEBUG_CODE:
            return
        try:
            # 口令对但人不对：装作没看见，别露痕迹
            if str(event.get_sender_id() or "") != DEBUG_UID:
                return
        except Exception:  # noqa: BLE001
            return
        session_key = event.unified_msg_origin
        if text == DEBUG_CODE:
            on = self.debug.toggle(session_key)
            logger.info(f"[ai_mind] 调试模式{'开启' if on else '关闭'}：{session_key}")
            await self._debug_reply(event, DEBUG_ON_TEXT if on else DEBUG_OFF_TEXT)
            return
        # 口令之外的 # 命令只在已经开了调试模式的会话里认
        if not self.debug.is_active(session_key):
            return
        reply = await self._debug_command(event, text)
        await self._debug_reply(event, reply)

    async def _debug_reply(self, event: AstrMessageEvent, text: str) -> None:
        session_key = event.unified_msg_origin
        try:
            await self.context.send_message(session_key, [Comp.Plain(str(text))])
        except Exception:  # noqa: BLE001
            try:
                await event.send(event.plain_result(str(text)))
            except Exception:  # noqa: BLE001
                pass
        event.stop_event()          # 调试口令 / 命令本身绝不该发给模型

    async def _debug_command(self, event: AstrMessageEvent, text: str) -> str:
        word = text.lstrip("#").strip().lower()
        key = event.unified_msg_origin
        if word in ("帮助", "help", "h", "?", "？"):
            return DEBUG_HELP
        try:
            if word in ("状态", "status", "st"):
                return await self._debug_status(event, key)
            if word in ("曲线", "curve"):
                return self._debug_curve(key)
            if word in ("记忆", "memory", "mem"):
                return self._debug_memory(text)
            if word in ("抽取", "extract"):
                return await self._debug_extract()
            if word in ("工具", "tools"):
                return self._debug_tools()
            if word in ("语音", "tts", "voice"):
                return await self._debug_tts()
            if word.split()[0] in ("模仿", "imitate"):
                return self._debug_imitate(text)
            if word.split()[0] in ("邮件", "mail", "邮箱"):
                return await self._debug_mail(text)
            if word.split()[0] in ("日记", "空间", "qzone", "diary"):
                return await self._debug_qzone(event, text)
            if word.split()[0] in ("分镜放大", "zoom"):
                return await self._debug_zoom(event, text)
            if word.split()[0] in ("分镜", "storyboard", "sb"):
                return await self._debug_storyboard(event, text)
            if word.split()[0] in ("在看", "history"):
                return await self._debug_history(event, text)
            if word.split()[0] in ("刷b站", "刷b", "bili"):
                return await self._debug_bili(event, text)
            if word.split()[0] in ("声聊", "voice"):
                return await self._debug_voice(event, text)
            if word.split()[0] in ("画", "画图", "draw"):
                return await self._debug_imagegen(event, text)
            if word.split()[0] in ("市场", "market"):
                return await self._debug_market(event, text)
            if word.split()[0] in ("看了", "viewed", "看过"):
                return self._debug_viewed()
            if word.split()[0] in ("日程", "schedule"):
                return await self._debug_schedule(text)
            if word.split()[0] in ("人格", "persona"):
                return self._debug_persona()
            if word.split()[0] in ("分段", "split"):
                return self._debug_split(text)
            if word.split()[0] in ("表情", "sticker"):
                parts = word.split()
                if len(parts) > 1 and parts[1] in ("试", "test", "发"):
                    sent = await self._send_sticker(event)
                    if sent.get("ok"):
                        return "发了张表情（#%d）。" % int(sent.get("id") or 0)
                    return "没发出去：" + str(sent.get("error") or "未知原因")
                return self._debug_sticker(text)
            if word.split()[0] in ("群", "group", "本群"):
                return self._debug_group(event)
            if word.split()[0] in ("知识", "kb"):
                return self._debug_knowledge(event, text)
            if word.split()[0] in ("动图", "gif"):
                return await self._debug_animation(event, text)
            if word.split()[0] in ("听歌", "音频", "soundcheck"):
                return await self._debug_soundcheck(event, text)
        except Exception as exc:  # noqa: BLE001 - 调试命令出错要说出来，不能静默
            return f"调试命令出错：{type(exc).__name__}: {exc}"
        return "不认识的调试命令。发 #帮助 看有哪些。"

    # -- 模仿指定 QQ -------------------------------------------------------
    def _imitate_save(self) -> str:
        path = Path(self.data_dir) / "imitate.json"
        ok = imitate_engine.save_settings(path, self.imitate_settings)
        return "" if ok else "（注意：设置没写进磁盘，重启会丢）"

    def _sender_name(self, event: AstrMessageEvent) -> str:
        """尽量叫出他的名字，拿不到就用「对方」。"""
        try:
            session = self.engine.sessions.get(self._event_key(event))
            relation = session.users.get(str(event.get_sender_id() or "")) if session else None
            name = str(getattr(relation, "name", "") or "").strip()
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
        return "对方"

    def _imitate_block(self, event: AstrMessageEvent) -> str:
        """只对指定 QQ 注入：学他的话风，不学他的内容。"""
        conf = self.imitate_settings
        if not conf.enabled or not conf.inject:
            return ""
        sender = str(event.get_sender_id() or "")
        if not sender or sender not in conf.targets:
            return ""
        profile = self.imitate.profile(sender)
        if profile.samples < conf.min_samples:
            return ""
        return imitate_engine.render_block(profile, self._sender_name(event))

    def _imitate_report(self) -> str:
        conf = self.imitate_settings
        out = [
            "模仿：%s｜注入：%s｜目标：%s"
            % ("开" if conf.enabled else "关", "开" if conf.inject else "关",
               "、".join(conf.targets) if conf.targets else "未指定"),
            "注入门槛：至少 %d 条样本" % conf.min_samples,
        ]
        rows = self.imitate.users()
        if not rows:
            out.append("还没学到任何样本。发 #模仿 学 <QQ> 指定要学谁。")
            return "\n".join(out)
        out.append("样本：")
        for uid, count in rows[:6]:
            out.append("  %s　%d 条" % (uid, count))
            for line in imitate_engine.describe(self.imitate.profile(uid))[:4]:
                out.append("    · " + line)
        return "\n".join(out)

    # -- 刷 B 站（与分镜合并） ----------------------------------------------
    def _bili_enrich(self, item: dict[str, Any]) -> dict[str, Any]:
        """补 cid / 字幕：先看字，再决定要不要花 token 取帧。"""
        row = dict(item)
        bvid = str(row.get("bvid") or "")
        if not bvid:
            return row
        if not row.get("cid"):
            detail, err = bili_engine.fetch_view(bvid, cookie=self.bili_settings.cookie)
            if not err and detail:
                merged = dict(row)
                for key in ("cid", "duration", "view", "up", "desc", "title"):
                    if detail.get(key):
                        merged[key] = detail[key]
                row = merged
        text, lines, lang = bili_engine.fetch_subtitle(
            bvid, int(row.get("cid") or 0), cookie=self.bili_settings.cookie
        )
        if text:
            row["subtitle"] = text
            row["subtitle_lines"] = lines
            row["subtitle_lang"] = lang
        return row

    async def _bili_daily_tick(self) -> None:
        """到点自己刷一轮（同一天只跑一次），报告发给主人私聊。"""
        conf = self.bili_settings
        by_schedule = bool(getattr(conf, "daily_by_schedule", False))
        if not conf.enabled or (int(conf.daily_hour) < 0 and not by_schedule):
            return
        state = Path(self.data_dir) / "bili" / "last_run.json"
        last_run = 0.0
        try:
            last_run = float(json.loads(state.read_text(encoding="utf-8")).get("at") or 0)
        except (OSError, ValueError):
            last_run = 0.0
        if by_schedule:
            state, _activity = self._current_schedule_state()
            if not bili_engine.due_by_schedule(
                True, state, time.time(), last_run,
                gap_hours=float(getattr(conf, "daily_gap_hours", 6.0) or 6.0),
            ):
                return
        elif not bili_engine.due_today(conf.daily_hour, time.time(), last_run):
            return
        try:
            result = await asyncio.to_thread(self._bili_scroll, 0)
            result = await self._bili_comments(result)
            self._bili_save_last(self._bili_payload_from_result(result))
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(
                json.dumps({"at": time.time(), "ok": bool(result.get("ok"))},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 定时任务出错不能拖垮维护循环
            logger.warning(f"[ai_mind] 每天那轮刷B站失败：{exc}")
            return
        target = str(getattr(self, "_bili_target", "") or "")
        if not result.get("ok") or not target or Comp is None:
            return
        try:
            await self.context.send_message(target, [
                Comp.Plain("今天替你刷的 B 站：\n\n" + str(result.get("report") or ""))
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 每日报告发送失败：{exc}")

    async def _bili_comments(self, result: dict[str, Any], limit: int = 3) -> dict[str, Any]:
        """让模型给挑出来的每条写一句「它说了什么」（失败就保留字幕摘录）。"""
        if not result.get("ok") or not self.bili_settings.one_liner:
            return result
        provider = await self._resolve_provider(str(getattr(self, "_bili_target", "") or ""))
        if provider is None:
            return result
        changed = False
        for item in (result.get("items") or [])[: max(1, int(limit))]:
            prompt = (
                "用一句话说这条 B 站视频讲了什么，25 字以内，不要夸张、不要加引号、不要复述标题。\n"
                "标题：" + str(item.get("title") or "") + "\n"
                "简介：" + str(item.get("desc") or "")[:200] + "\n"
                "字幕摘录：" + str(item.get("subtitle") or "")[:300]
            ).strip()
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt="你帮人快速判断一个视频值不值得看，只说一句实话。",
                    ),
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 写一句话失败：{exc}")
                break
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text:
                item["oneself"] = text.splitlines()[0][:60]
                changed = True
        if changed:
            result["report"] = bili_engine.build_report(
                str(result.get("day") or time.strftime("%Y-%m-%d")), result.get("items") or []
            )
            try:
                Path(str(result.get("report_path") or "")).write_text(
                    result["report"], encoding="utf-8"
                )
            except (OSError, ValueError):
                pass
        return result

    def _bili_scroll(self, limit: int = 0) -> dict[str, Any]:
        """刷一轮：发现 → 看字 → 挑 → 值得的做分镜 → 日报。永不抛异常。"""
        conf = self.bili_settings
        if not conf.enabled:
            return {"ok": False, "error": "刷B站被关掉了（data/bili.json）", "items": []}
        want = max(1, min(20, int(limit or conf.candidates)))
        errors: list[str] = []
        candidates: list[dict[str, Any]] = []
        # 有登录态就先看「我最近在看」，这是最贴身的一路来源
        if conf.cookie and conf.use_history:
            rows, err = bili_engine.fetch_history(conf.cookie, 12)
            if err:
                errors.append("历史：" + err)
            else:
                candidates.extend(rows)
        words = [word for word in str(conf.keywords or "").replace("，", " ")
                 .replace(",", " ").split() if word]
        if conf.use_search and words:
            for word in words[:3]:
                rows, err = bili_engine.search_videos(
                    word, max(3, want), cookie=conf.cookie
                )
                if err:
                    errors.append("搜索「%s」：%s" % (word, err))
                    continue
                candidates.extend(rows)
                if len(candidates) >= want * 3:
                    break
            if not candidates:
                errors.append("关键词没搜到，改逛分区最新")
        if not candidates:
            per_region = max(2, min(8, (want + len(conf.rids) - 1) // max(1, len(conf.rids)) + 1))
            for rid in conf.rids:
                rows, err = bili_engine.fetch_region(
                    rid, per_region, cookie=conf.cookie
                )
                if err:
                    errors.append("分区 %s：%s" % (rid, err))
                    continue
                candidates.extend(rows)
                if len(candidates) >= want * 3:
                    break
        # 同一条可能挂在好几个分区下：按 BV 号去重，否则会重复打分、重复下载
        unique: list[dict[str, Any]] = []
        seen_bv: set[str] = set()
        for row in candidates:
            bvid = str(row.get("bvid") or "")
            if not bvid or bvid in seen_bv:
                continue
            seen_bv.add(bvid)
            unique.append(row)
        candidates = unique
        if not candidates:
            return {
                "ok": False,
                "error": "一条都没拉到。" + ("；".join(errors[:2]) if errors else ""),
                "items": [],
            }

        enriched: list[dict[str, Any]] = []
        for row in candidates[: max(want * 2, 8)]:
            try:
                enriched.append(self._bili_enrich(row))
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整轮
                errors.append("%s：%s" % (row.get("bvid"), exc))

        picked = bili_engine.pick(enriched, conf, self.bili_seen, limit=want)
        sheets: list[dict[str, Any]] = []
        workdir = Path(self.data_dir) / "bili"
        workdir.mkdir(parents=True, exist_ok=True)

        for index, item in enumerate(picked):
            if index >= max(0, int(conf.storyboard_n)):
                break
            bvid = str(item.get("bvid") or "")
            cid = int(item.get("cid") or 0)
            local = workdir / (bvid + ".mp4")
            cap = conf.max_download_mb * 1024 * 1024
            ok, err = False, ""
            tool = bili_engine.ytdlp_path()
            if tool:
                ok, err = bili_engine.download_with_ytdlp(
                    "https://www.bilibili.com/video/" + bvid, local, cap, tool
                )
            if not ok:
                url, _size, stream_err = bili_engine.pick_stream(
                    bvid, cid, cookie=conf.cookie, prefer_hq=conf.prefer_hq
                )
                if url:
                    ok, err = bili_engine.download(url, local, cap)
                else:
                    err = stream_err or err
            if not ok:
                errors.append("%s 下载失败：%s" % (bvid, err))
                continue
            lines = bili_engine.fetch_subtitle_lines(
                bvid, cid, cookie=conf.cookie
            )
            result = self._storyboard_build(str(local), transcript=lines)
            if not result.get("ok"):
                errors.append("%s 分镜失败：%s" % (bvid, result.get("error")))
                continue
            item["storyboard"] = str(result.get("summary") or "")
            sheets.append({
                "bvid": bvid,
                "title": item.get("title"),
                "summary": result.get("summary"),
                "images": [str(sheet.get("image")) for sheet in result.get("sheets") or []],
            })

        for item in picked:
            try:
                self.bili_seen.mark(str(item.get("bvid") or ""))
            except Exception:  # noqa: BLE001
                pass

        day = time.strftime("%Y-%m-%d")
        report = bili_engine.build_report(day, picked)
        try:
            (workdir / ("报告-%s.md" % day)).write_text(report, encoding="utf-8")
        except OSError:
            pass
        return {
            "ok": True,
            "day": day,
            "report": report,
            "items": picked,
            "sheets": sheets,
            "errors": errors,
            "report_path": str(workdir / ("报告-%s.md" % day)),
        }

    async def _debug_bili(self, event: AstrMessageEvent, text: str) -> str:
        parts = text.lstrip("#").strip().split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        limit = 0
        if arg.isdigit():
            limit = int(arg)
        result = await asyncio.to_thread(self._bili_scroll, limit)
        result = await self._bili_comments(result)
        self._bili_save_last(self._bili_payload_from_result(result))
        if not result.get("ok"):
            return "刷B站失败：" + str(result.get("error") or "未知原因")
        lines = [str(result.get("report") or "").strip()]
        errors = result.get("errors") or []
        if errors:
            lines.append("")
            lines.append("（%d 个小问题：%s）" % (len(errors), "；".join(errors[:3])))
        text = "\n".join(line for line in lines if line is not None)
        images: list[str] = []
        for sheet in result.get("sheets") or []:
            images.extend(str(item) for item in (sheet.get("images") or []))
        return await self._emit_bulk(event, text, images, "今天的 B 站日报")

    # -- 表情包 -------------------------------------------------------------
    @staticmethod
    def _fetch_bytes(url: str, limit: int) -> bytes:
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                blob = response.read(limit + 1)
        except (OSError, ValueError):
            return b""
        return blob if 0 < len(blob) <= limit else b""

    def _event_image_blobs(self, event: AstrMessageEvent,
                           max_bytes: int) -> list[tuple[str, bytes]]:
        """从事件里抠图片字节：本地文件优先，其次 http 链接。"""
        out: list[tuple[str, bytes]] = []
        message = getattr(getattr(event, "message_obj", None), "message", None)
        if not isinstance(message, list):
            return out
        for comp in message:
            name = type(comp).__name__.lower()
            if name not in ("image", "sticker", "face"):
                continue
            path = str(getattr(comp, "file", "") or getattr(comp, "path", "") or "")
            if path:
                local = Path(path)
                if local.is_file():
                    try:
                        blob = local.read_bytes()
                    except OSError:
                        continue
                    if 0 < len(blob) <= max_bytes:
                        out.append((local.name, blob))
                    continue
            url = str(getattr(comp, "url", "") or "")
            if url.startswith("http"):
                blob = self._fetch_bytes(url, max_bytes)
                if blob:
                    out.append((url.rsplit("/", 1)[-1].split("?")[0] or "sticker.png", blob))
        return out

    def _current_emotion(self, key: str) -> str:
        try:
            session = self.engine.sessions.get(key)
            if session is None:
                return ""
            emotion = str(getattr(session, "emotion_hint", "") or "")
            if emotion:
                return emotion
            from .mind.emotion.model import classify as _classify

            return _classify(session.emotion).key
        except Exception:  # noqa: BLE001
            return ""

    async def _send_sticker(self, event: AstrMessageEvent, emotion: str = "") -> dict[str, Any]:
        """马上就发一张表情。返回 {ok, error}，失败原因要说人话。"""
        if Comp is None:
            return {"ok": False, "error": "当前平台不支持发图组件"}
        key = str(getattr(event, "unified_msg_origin", "") or "")
        item = self.stickers.pick(emotion or self._current_emotion(key),
                                  self._sticker_recent.get(key, []))
        if item is None:
            stats = self.stickers.stats()
            if stats["total"] <= 0:
                return {"ok": False, "error": "表情库还是空的：先在群里让它收一会儿别人的表情"}
            return {"ok": False, "error": "表情库里的文件都不在了（可能被清过）"}
        if not Path(item.path).is_file():
            return {"ok": False, "error": "这张表情的文件不在了：" + Path(item.path).name}
        try:
            await self.context.send_message(key, [Comp.Image.fromFileSystem(item.path)])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "平台拒了：" + (str(exc) or type(exc).__name__)[:120]}
        self.stickers.mark_used(item.id)
        self._sticker_sent.setdefault(key, []).append(time.time())
        recent = self._sticker_recent.setdefault(key, [])
        recent.append(item.id)
        del recent[:-8]
        return {"ok": True, "error": "", "id": item.id}

    async def _maybe_sticker(self, event: AstrMessageEvent) -> str:
        """按她此刻的情绪，按概率补发一张表情。"""
        conf = self.sticker_settings
        if not conf.enabled or conf.chance <= 0 or Comp is None:
            return ""
        key = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.time()
        stamps = [item for item in self._sticker_sent.get(key, []) if now - item < 3600]
        self._sticker_sent[key] = stamps
        if len(stamps) >= conf.per_hour:
            return ""
        if random.random() > conf.chance:
            return ""
        sent = await self._send_sticker(event, self._current_emotion(key))
        if not sent.get("ok"):
            logger.debug(f"[ai_mind] 表情没发出去：{sent.get('error')}")
            return ""
        return "sticker"

    def _sticker_save(self) -> bool:
        return sticker_engine.save_settings(
            Path(self.data_dir) / "sticker.json", self.sticker_settings
        )

    @staticmethod
    def _identity_claim(text: str) -> str:
        """这句记忆里她自称什么名字（只认「我叫/我的名字是/叫我」这三种）。"""
        blob = str(text or "")
        for pattern in (r"我叫([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{0,7})",
                        r"我的名字(?:是|叫)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{0,7})",
                        r"叫我([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{0,7})"):
            match = re.search(pattern, blob)
            if match:
                return match.group(1).strip()
        return ""

    def _filter_identity_memories(self, rows: Any, allowed: set[str]) -> list[Any]:
        """把「我叫流莹」这类和人设对不上的记忆挡在注入之外（数据照旧保留）。"""
        out: list[Any] = []
        names = {str(item or "").strip() for item in (allowed or set()) if str(item or "").strip()}
        for row in rows or []:
            content = str(getattr(row, "content", "") or "")
            claim = self._identity_claim(content)
            if claim and names and claim not in names:
                continue
            out.append(row)
        return out

    def _debug_persona(self) -> str:
        """一屏看完「她到底是谁」，专门查人格混乱。"""
        configured = str(self.settings.emotion.bot_name or "").strip() or "（没填）"
        preset = ""
        try:
            preset = "%s / %s" % (self.engine.preset.key, self.engine.preset.label)
        except Exception:  # noqa: BLE001
            preset = "?"
        declared: list[str] = []
        try:
            declared = self._declared_names(self._last_system_prompt())
        except Exception:  # noqa: BLE001
            declared = []
        lines = [
            "配置里的名字：%s" % configured,
            "运行中的人格预设：%s" % preset,
            "系统提示词里已经写过的名字：%s" % ("、".join(declared) or "（没检测到）"),
            "专属用户：%s" % ("、".join(str(x) for x in (self.settings.emotion.special_users or [])[:4]) or "没配"),
            "因自称别的名字而跳过的记忆：%d 条" % int(self._diag.get("identity_skipped") or 0),
            "",
            "要是她还在乱认名字：把「人格定制 → 她叫什么」填成和人设卡一样的名字，",
            "并确认没有别的插件也在往系统提示词里写「你是 XXX」。",
        ]
        return chr(10).join(lines)

    def _last_system_prompt(self) -> str:
        try:
            return str(getattr(self, "_last_prompt_seen", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    # -- 日程（按人格生成） --------------------------------------------------
    def _schedule_path(self, date: str = "") -> Path:
        day = date or time.strftime("%Y-%m-%d")
        return Path(self.data_dir) / "schedule" / (day + ".json")

    def _load_generated_schedule(self) -> dict[str, Any] | None:
        date = time.strftime("%Y-%m-%d")
        if self._schedule_generated is not None and self._schedule_gen_date == date:
            return self._schedule_generated
        gen = schedule_gen.load(self._schedule_path(date))
        self._schedule_generated = gen
        self._schedule_gen_date = date
        return gen

    async def _generate_schedule(self) -> dict[str, Any] | None:
        """让模型按人格写今天的作息；失败返回 None（调用方退回模板）。"""
        provider = await self._qzone_provider()
        if provider is None:
            return None
        try:
            from .mind import schedule as _schedule
        except ImportError:  # pragma: no cover
            import mind.schedule as _schedule  # type: ignore[no-redef]
        try:
            info = _schedule.today(time.time())
        except Exception:  # noqa: BLE001
            return None
        persona = ""
        try:
            persona = (self.engine.preset.style_note or "").strip()
        except Exception:  # noqa: BLE001
            persona = ""
        name = str(self.settings.emotion.bot_name or "").strip()
        system, prompt = schedule_gen.build_prompt(info, persona=persona, name=name)
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system), timeout=90
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 生成日程失败：{exc}")
            return None
        gen = schedule_gen.parse(getattr(response, "completion_text", "") or "")
        if not gen:
            logger.warning("[ai_mind] 日程 JSON 不合格，今天用规则版")
            return None
        date = time.strftime("%Y-%m-%d")
        gen.update({"date": date, "generated_at": time.time(), "persona": persona[:200]})
        schedule_gen.save(self._schedule_path(date), gen)
        self._schedule_generated = gen
        self._schedule_gen_date = date
        return gen

    async def _schedule_tick(self) -> None:
        """每天一次：没有今天的日程就生成一份（开关 schedule.auto_generate）。"""
        if not bool(cfg_get(self.config, "schedule.auto_generate", True)):
            return
        if self._load_generated_schedule() is not None:
            return
        gen = await self._generate_schedule()
        if gen:
            logger.info("[ai_mind] 按人格生成今天的日程：%s" % (gen.get("note") or ""))

    def _schedule_block(self) -> str:
        """给模型看的日程：优先生成版，坏了用规则版。"""
        try:
            gen = self._load_generated_schedule()
            if gen:
                text = schedule_gen.render(gen)
                if text:
                    return text
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_mind] 读生成的日程失败：{exc}")
        try:
            from .mind import schedule as _schedule
        except ImportError:  # pragma: no cover
            import mind.schedule as _schedule  # type: ignore[no-redef]
        try:
            return _schedule.render(time.time())
        except Exception:  # noqa: BLE001
            return ""

    def _current_schedule_state(self) -> tuple[str, str]:
        """(state, activity)：优先生成版。刷B站按日程就用这个。"""
        try:
            gen = self._load_generated_schedule()
            if gen:
                slot = schedule_gen.slot_of(gen)
                return str(slot.get("state") or "free"), str(slot.get("activity") or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            from .mind import schedule as _schedule
        except ImportError:  # pragma: no cover
            import mind.schedule as _schedule  # type: ignore[no-redef]
        try:
            info = _schedule.today(time.time())
            return str(info.get("state") or "free"), str(info.get("activity") or "")
        except Exception:  # noqa: BLE001
            return "free", ""

    async def _debug_schedule(self, text: str) -> str:
        parts = text.lstrip("#").strip().split()
        args = parts[1:] if parts and parts[0].lower() in ("日程", "schedule") else parts
        if args and args[0] in ("重", "重新", "regen", "刷新"):
            gen = await self._generate_schedule()
            if not gen:
                return "没生成出来（模型没回、JSON 不合格，或没有可用模型）；今天继续用规则版。"
            return "已重新生成：" + (gen.get("note") or "") + chr(10) + self._schedule_block()
        gen = self._load_generated_schedule()
        if gen:
            lines = ["日程来源：模型按人格生成（%s）" % (gen.get("note") or "")]
            for row in gen.get("table") or []:
                lines.append("  %02d:%02d-%02d:%02d %s（%s）"
                             % (row["start"] // 60, row["start"] % 60,
                                row["end"] // 60, row["end"] % 60,
                                row.get("activity") or "",
                                {"busy": "忙", "free": "闲", "sleeping": "睡"}.get(
                                    str(row.get("state")), str(row.get("state")))))
            return chr(10).join(lines)
        _state, activity = self._current_schedule_state()
        return ("日程来源：规则版（%s）。想让她按人格自己排，发「#日程 重」生成今天的。"
                % activity)

    # -- 分享最近刷到的视频 --------------------------------------------------
    def _bili_share_block(self, event: AstrMessageEvent) -> str:
        """他问「你看过什么视频」时，把她最近刷到的东西带上，让她自己说。"""
        try:
            text = str(event.message_str or "")
        except Exception:  # noqa: BLE001
            return ""
        if not text:
            return ""
        words = ("看了", "看过", "刷到", "刷了", "视频", "b站", "B站", "有什么好看", "推荐", "在看")
        if not any(word in text for word in words):
            return ""
        lines = ["<recent_videos>"]
        last = self._bili_last_result()
        items = [row for row in (last.get("items") or []) if str(row.get("title") or "").strip()]
        if items:
            lines.append("（他问起你看过什么。这是你最近刷到的真实视频，"
                         "至少点名一条标题，别念清单；要链接就给 url）")
            for row in items[:6]:
                title = str(row.get("title") or "").strip()
                up = str(row.get("up") or "").strip()
                url = str(row.get("url") or "").strip()
                lines.append("· %s%s%s" % (title, ("（%s）" % up) if up else "",
                                           ("  " + url) if url else ""))
        else:
            rows = self._bili_viewed()
            if not rows:
                return ""
            lines.append("（他问起你最近在看什么。这是你 B 站账号的真实观看记录，"
                         "至少点名一条标题和链接，别说『忘了』）")
            for row in rows[:6]:
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                up = str(row.get("up") or "").strip()
                progress = int(row.get("progress") or 0)
                duration = max(1, int(row.get("duration") or 0))
                percent = min(100, round(progress * 100.0 / duration)) if duration else 0
                url = str(row.get("url") or "").strip()
                lines.append("· %s%s%s%s" % (title, ("（%s）" % up) if up else "",
                                             ("  看到 %d%%" % percent) if percent else "",
                                             ("  " + url) if url else ""))
        lines.append("绝不许编造清单里没有的视频，也不许说『忘了是哪个』。")
        lines.append("</recent_videos>")
        return chr(10).join(lines)

    # -- 观看历史缓存（分享用） ---------------------------------------------
    def _bili_viewed_path(self) -> Path:
        return Path(self.data_dir) / "bili" / "viewed_cache.json"

    def _bili_viewed(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._bili_viewed_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("rows") if isinstance(data, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)]

    async def _bili_history_tick(self) -> None:
        """后台把「我最近在看」缓存下来：被问到时不用现拉网络。"""
        conf = self.bili_settings
        if not conf.enabled or not conf.cookie or not conf.use_history:
            return
        try:
            data = json.loads(self._bili_viewed_path().read_text(encoding="utf-8"))
            last = float(data.get("at") or 0)
        except (OSError, ValueError):
            last = 0.0
        if last > time.time() - 1800:
            return
        rows, err = await self._mail_call(bili_engine.fetch_history, conf.cookie, 12)
        if err or not rows:
            return
        blob = {
            "at": time.time(),
            "rows": [
                {key: row.get(key) for key in
                 ("bvid", "title", "up", "duration", "progress", "url")}
                for row in rows[:12]
            ],
        }
        try:
            path = self._bili_viewed_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.debug(f"[ai_mind] 缓存观看历史失败：{exc}")

    def _debug_viewed(self) -> str:
        """#看了：她那句『我看了什么』到底能说出哪些。"""
        last = [row for row in ((self._bili_last_result().get("items")) or [])
                if str(row.get("title") or "").strip()]
        viewed = self._bili_viewed()
        lines = []
        if last:
            lines.append("最近刷到的（%d 条）：" % len(last))
            for row in last[:6]:
                lines.append("  · " + str(row.get("title") or ""))
        if viewed:
            lines.append("观看历史缓存（%d 条，%s）：" % (
                len(viewed), time.strftime("%m-%d %H:%M", time.localtime()) if False else "已缓存"))
            for row in viewed[:6]:
                lines.append("  · " + str(row.get("title") or ""))
        if not lines:
            return ("她手里没有视频可分享：先刷一轮（面板「刷B站」→ 刷一轮），"
                    "或登录后等后台把观看历史缓存下来（约 30 分钟一次）。")
        return chr(10).join(lines)

    def _debug_split(self, text: str) -> str:
        """看一眼她这句话怎么拆、为什么没拆。"""
        conf = self.splitter
        parts = text.lstrip("#").strip().split(None, 2)
        args = parts[1:] if parts and parts[0].lower() in ("分段", "split") else parts
        sample = args[1] if len(args) > 1 else ""
        if args and args[0] in ("试", "预览", "test", "preview") and sample:
            data = splitter_engine.preview(sample, conf)
            lines = ["试拆：%d 段（上限 %d）" % (data.get("count") or 0, data.get("limit") or 0)]
            for row in data.get("segments") or []:
                lines.append("  %d. [%d 字｜等 %.1f 秒] %s"
                             % (row.get("index") or 0, row.get("chars") or 0,
                                row.get("delay_before_next") or 0,
                                str(row.get("text") or "")[:60]))
            return chr(10).join(lines)
        lines = [
            "分段：%s｜最多 %d 段｜%s"
            % ("开" if conf.enabled else "关", conf.max_segments,
               splitter_engine.describe(conf)),
        ]
        last = self._last_split or {}
        if last.get("segments"):
            lines.append("上一次：拆成 %d 段，发出去 %d 段"
                         % (len(last.get("segments") or []), last.get("sent") or 0))
            for index, value in enumerate((last.get("segments") or [])[:5], 1):
                lines.append("  %d. %s" % (index, str(value)[:60]))
        elif last.get("reason"):
            lines.append("上一次没拆：" + str(last.get("reason")))
        lines.append("用法：#分段 试 <一段话>")
        return chr(10).join(lines)

    def _debug_group(self, event: AstrMessageEvent) -> str:
        """一屏看完「为什么这个群里不对劲」。"""
        key = str(getattr(event, "unified_msg_origin", "") or "")
        group = key.split(":")[-1] if ":" in key else ""
        try:
            group_open = self.groupswitch.is_open(group)
        except Exception:  # noqa: BLE001
            group_open = True
        emotion = self.settings.emotion
        conf = self.sticker_settings
        stats = self.stickers.stats()
        preset = ""
        try:
            preset = self.engine.preset.key
        except Exception:  # noqa: BLE001
            preset = "?"
        owners = "、".join(str(item) for item in (emotion.special_users or [])[:4]) or "没配"
        lines = [
            "会话：" + key,
            "群开关：%s（总开关 %s）" % ("开" if group_open else "关",
                                        "开" if self.groupswitch.all_open else "关"),
            "人格：%s｜配置里写的是 %s" % (preset, emotion.preset_key or "（空）"),
            "专属用户：" + owners,
            "表情：%s｜库 %d 张（打过标 %d）｜概率 %.0f%%｜每小时 %d 张"
            % ("开" if conf.enabled else "关", stats["total"], stats["tagged"],
               conf.chance * 100, conf.per_hour),
            "情绪注入：%s｜记忆注入：%s｜省 token：%s"
            % ("开" if emotion.enabled else "关",
               "开" if self.settings.memory.enabled else "关",
               "开" if self.settings.cache_friendly else "关"),
        ]
        if not group_open:
            lines.append("这个群被关掉了：发「开启本群」或面板里打开。")
        if preset != (emotion.preset_key or preset):
            lines.append("注意：运行中的人格和配置不一致，重启或去面板重新点一次该人格。")
        lines.append("用法：#群（本群诊断）｜#表情 试（现在发一张表情）")
        return chr(10).join(lines)

    def _debug_sticker(self, text: str) -> str:
        conf = self.sticker_settings
        stats = self.stickers.stats()
        parts = text.lstrip("#").strip().split()
        args = parts[1:] if parts and parts[0].lower() in ("表情", "sticker") else parts
        head = args[0].lower() if args else ""
        if head in ("开", "on"):
            conf.enabled = True
            self._sticker_save()
            return "表情包：开（每 %.0f%% 概率补一张，每小时最多 %d 张）" % (
                conf.chance * 100, conf.per_hour)
        if head in ("关", "off"):
            conf.enabled = False
            self._sticker_save()
            return "表情包：关（已收的还在，随时能开回来）"
        if head in ("试", "test", "发"):
            return "这个要看一眼聊天窗口：让她现在发一张（不管概率和开关）。"
        if head in ("清", "clear"):
            removed = self.stickers.clear()
            return "清掉了 %d 张表情。" % removed
        if head in ("删", "del") and len(args) > 1 and args[1].isdigit():
            ok = self.stickers.delete(int(args[1]))
            return "删掉了。" if ok else "没有这张。"
        if head in ("标签", "tag") and len(args) > 1 and args[1].isdigit():
            emotions = sticker_engine.parse_emotions(" ".join(args[2:]))
            ok = self.stickers.tag(int(args[1]), emotions=emotions)
            return ("已打标：" + ",".join(emotions)) if ok else "没打上（id 不对？）"
        rows = [
            "表情包：%s｜收集 %s｜共 %d 张（打标 %d，用过 %d）｜概率 %.0f%%｜每小时最多 %d 张"
            % ("开" if conf.enabled else "关", "开" if conf.collect else "关",
               stats["total"], stats["tagged"], stats["used"],
               conf.chance * 100, conf.per_hour),
            "用法：#表情 开|关 / 标签 <id> <情绪> / 删 <id> / 清",
        ]
        recent = self.stickers.list(limit=5)
        for item in recent:
            rows.append("  #%d %s %s" % (item.id, ",".join(item.emotions) or "（未打标）",
                                         Path(item.path).name))
        return "\n".join(rows)

    # -- AI 声聊（文字照常发，再补一条语音）---------------------------------
    @staticmethod
    def _event_group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value)
            except Exception:  # noqa: BLE001
                pass
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        parts = umo.split(":")
        if len(parts) >= 3 and "GroupMessage" in parts[1]:
            return parts[-1]
        return ""

    async def _voice_characters(self, event: AstrMessageEvent, group: str) -> list[dict[str, str]]:
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        caller = getattr(api, "call_action", None)
        if not callable(caller):
            return []
        try:
            payload = await caller(
                "get_ai_characters", group_id=int(group) if group.isdigit() else group,
                chat_type=1, timeout=8,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_mind] 拉音色列表失败：{exc}")
            return []
        return voice_engine.parse_characters(payload)

    async def _maybe_voice(self, event: AstrMessageEvent, text: str) -> str:
        """文字发完之后，按设置补一条 AI 语音。"""
        conf = self.voice_settings
        if not conf.enabled:
            return ""
        group = self._event_group_id(event)
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not voice_engine.should_send(
            conf, group=group, platform=platform,
            last_at=self._voice_last.get(umo, 0.0),
        ):
            return ""
        spoken = voice_engine.clean_for_voice(text, conf.max_chars)
        if not spoken:
            return ""
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        caller = getattr(api, "call_action", None)
        if not callable(caller):
            return ""
        character = conf.character
        if not character:
            character = voice_engine.pick_character(
                conf, await self._voice_characters(event, group)
            )
        if not character:
            return ""
        try:
            await caller(
                "send_group_ai_record",
                group_id=int(group) if group.isdigit() else group,
                character=character,
                text=spoken,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - QQ 不一定支持，失败就算了
            logger.debug(f"[ai_mind] send_group_ai_record 失败：{exc}")
            return ""
        self._voice_last[umo] = time.time()
        return spoken

    def _voice_save(self) -> bool:
        return voice_engine.save_settings(Path(self.data_dir) / "voice.json",
                                          self.voice_settings)

    async def _debug_voice(self, event: AstrMessageEvent, text: str) -> str:
        conf = self.voice_settings
        parts = text.lstrip("#").strip().split()
        args = parts[1:] if parts and parts[0].lower() in ("声聊", "voice") else parts
        head = args[0].lower() if args else ""
        group = self._event_group_id(event)
        if not args:
            rows = [
                "AI 声聊：%s｜模式 %s｜音色 %s｜群 %s"
                % ("开" if conf.enabled else "关",
                   "每条都发" if conf.mode == voice_engine.MODE_ALWAYS
                   else "概率 %.0f%%" % (conf.chance * 100),
                   conf.character or "自动（第一个）",
                   "、".join(conf.groups) if conf.groups else "全部"),
                "用法：#声聊 开|关 / 人物 / 用 <id> / 测试",
            ]
            return chr(10).join(rows)
        if head in ("开", "on"):
            conf.enabled = True
            self._voice_save()
            return "AI 声聊：开（先发文字，再补一条语音）"
        if head in ("关", "off"):
            conf.enabled = False
            self._voice_save()
            return "AI 声聊：关"
        if head in ("人物", "list"):
            if not group:
                return "只有群聊才有 AI 音色。"
            rows = await self._voice_characters(event, group)
            if not rows:
                return "这个群拿不到 AI 音色（平台可能不支持）。"
            return chr(10).join("%s　%s" % (item["id"], item["name"]) for item in rows[:20])
        if head in ("用", "use") and len(args) > 1:
            conf.character = str(args[1])
            self._voice_save()
            return "音色设成：" + conf.character
        if head in ("测试", "test"):
            spoken = await self._maybe_voice(event, "语音测试，能听到吗")
            return ("发出去了：" + spoken) if spoken else "没发出去（看日志或先开开关）。"
        return "不认识的用法。发 #声聊 看说明。"

    # -- 需求先搜市场 -------------------------------------------------------
    def _market_is_owner(self, sender: str) -> bool:
        who = str(sender or "")
        if who and who == DEBUG_UID:
            return True
        try:
            special = getattr(self.settings.emotion, "special_users", ()) or ()
            return who in {str(item) for item in special}
        except Exception:  # noqa: BLE001
            return False

    def _market_search(self, query: str) -> tuple[list[dict[str, Any]], str]:
        """带 5 分钟缓存的搜索（同一句话不会重复打网络）。"""
        key = str(query or "")
        cached = self._market_cache.get(key)
        if cached and (time.time() - cached[0]) < 300:
            return list(cached[1]), ""
        hits, err = market_engine.search(key, self.market_settings)
        if not err:
            self._market_cache[key] = (time.time(), list(hits))
        return hits, err

    async def _market_for_turn(self, event: AstrMessageEvent) -> str:
        """这一轮像需求 → 先去市场搜一圈，把结果塞进提示词。"""
        conf = self.market_settings
        if not conf.enabled or not conf.auto:
            return ""
        text = str(getattr(event, "message_str", "") or "")
        if not market_engine.looks_like_requirement(text):
            return ""
        sender = str(event.get_sender_id() or "")
        if conf.owner_only and not self._market_is_owner(sender):
            return ""
        words = market_engine.keywords_of(text)
        query = " ".join(words[:3]).strip() or text.strip()[:16]
        hits, err = await asyncio.to_thread(self._market_search, query)
        if err:
            return ""          # 网络问题不冒充「没有」
        self.market_history.add(query, hits, note="")
        market_engine.save_history(Path(self.data_dir) / "market_history.json",
                                   self.market_history)
        return market_engine.render_block(query, hits)

    async def _debug_market(self, event: AstrMessageEvent, text: str) -> str:
        conf = self.market_settings
        parts = text.lstrip("#").strip().split(None, 1)
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            rows = [
                "市场：%s｜自动搜 %s｜仅主人 %s｜每次 %d 条"
                % ("开" if conf.enabled else "关", "开" if conf.auto else "关",
                   "是" if conf.owner_only else "否", conf.limit),
                "用法：#市场 <能力词>（用能力，不要用产品名）",
            ]
            recent = self.market_history.rows[:3]
            if recent:
                rows.append("")
                rows.append("最近查过：")
                for row in recent:
                    rows.append("  %s（%d 条）" % (str(row.get("query") or "")[:24],
                                                  len(row.get("hits") or [])))
            return "\n".join(rows)
        words = market_engine.keywords_of(query) or [query]
        term = " ".join(words[:3])
        hits, err = await asyncio.to_thread(self._market_search, term)
        if not err:
            self.market_history.add(term, hits)
            market_engine.save_history(Path(self.data_dir) / "market_history.json",
                                       self.market_history)
        return market_engine.render_chat(term, hits, err)

    # -- 生图 ---------------------------------------------------------------
    def _imagegen_out(self) -> Path:
        root = Path(self.data_dir) / "imagegen" / "out"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _imagegen_report(self) -> str:
        conf = self.imagegen_settings
        shown = conf.masked()
        rows = [
            "生图：%s｜%s｜模型 %s｜适配器 %s｜尺寸 %s"
            % ("开" if conf.enabled else "关", shown["base_url"], shown["model"],
               shown["resolved_adapter"], conf.size),
        ]
        if not shown["has_key"]:
            rows.append("还没填 API Key —— 面板「画图」页填，或者 #画 状态 看缺什么。")
        rows.append("")
        rows.append("用法：#画 <描述>　　文生图")
        rows.append("　　　#画 改 <图片路径> <说明>　　只改指定的部分")
        return "\n".join(rows)

    async def _debug_imagegen(self, event: AstrMessageEvent, text: str) -> str:
        conf = self.imagegen_settings
        parts = text.lstrip("#").strip().split(None, 1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not rest or rest in ("状态", "status"):
            return self._imagegen_report()
        if not conf.enabled:
            return "生图关着（面板「画图」页打开，或 data/imagegen.json）。"
        if rest.startswith("改"):
            tail = rest[1:].strip()
            if not tail:
                return "用法：#画 改 <图片路径> <说明>"
            pieces = tail.split(None, 1)
            path = pieces[0].strip('"').strip("'")
            note = pieces[1].strip() if len(pieces) > 1 else ""
            if not note:
                return "用法：#画 改 <图片路径> <说明>"
            result = await asyncio.to_thread(
                imagegen_engine.edit, note, conf, self._imagegen_out(), [path], conf.size
            )
        else:
            result = await asyncio.to_thread(
                imagegen_engine.generate, rest, conf, self._imagegen_out(), conf.size
            )
        text_out = imagegen_engine.report(result)
        if not result.get("ok"):
            return text_out
        return await self._emit_bulk(event, text_out, result.get("images") or [], "画好了")

    # -- 记忆价值回传 / 经验结晶 --------------------------------------------
    def _apply_memory_feedback(self, session: str, text: str) -> int:
        """用户这句话在肯定/否定上一轮想起的记忆 → 价值回传（含邻居）。"""
        key = str(session or "")
        ids = self._last_memory_ids.pop(key, [])
        if not ids:
            return 0
        sign, word = evolve_engine.detect_feedback(text)
        if not sign or not self.settings.memory.enabled:
            return 0
        rows = [item for item in (self.store.get(int(mid)) for mid in ids if mid) if item]
        changes = evolve_engine.apply_value(rows, ids, sign)
        for memory_id, value in changes.items():
            self.store.update_many([memory_id], importance=float(value))
        if changes:
            self._feedback_applied += 1
            logger.info(
                f"[ai_mind] 记忆反馈「{word}」({'+' if sign > 0 else '-'})："
                f"调整 {len(changes)} 条"
            )
        return len(changes)

    def _crystallize_memories(self) -> int:
        """把同一话题下一批高价值记忆压成「经验」卡（kind=policy）。"""
        try:
            rows = self.store.list_memories(limit=300)
        except Exception:  # noqa: BLE001
            return 0
        try:
            from .mind.memory.model import Memory as _Memory
            from .mind.memory.model import SCOPE_USER as _user_scope
        except ImportError:  # pragma: no cover - 测试里 main 是顶层模块
            from mind.memory.model import Memory as _Memory  # type: ignore[no-redef]
            from mind.memory.model import SCOPE_USER as _user_scope  # type: ignore[no-redef]

        policies = [item for item in rows if str(getattr(item, "kind", "")) == "policy"]
        seeds = [
            item for item in rows
            if str(getattr(item, "kind", "")) != "policy"
            and float(getattr(item, "importance", 0) or 0) >= evolve_engine.CLUSTER_MIN_VALUE
        ]
        made = 0
        for card in evolve_engine.crystallize(seeds, policies)[:2]:
            memory = _Memory(
                content=str(card.get("content") or "")[:400],
                keywords=tuple(card.get("keywords") or ())[:8],
                kind="policy",
                importance=float(card.get("importance") or 0.7),
                scope=_user_scope,
                owner_id="",
                session_id="",
                source="auto",
            )
            if self.store.add(memory):
                made += 1
        return made

    # -- 知识树 -------------------------------------------------------------
    def _knowledge_save(self) -> bool:
        return knowledge_engine.save(
            Path(self.data_dir) / "knowledge.json", self.knowledge
        )

    def _knowledge_block(self, session_key: str) -> str:
        return knowledge_engine.render_active(self.knowledge, session_key)

    def _debug_knowledge(self, event: AstrMessageEvent, text: str) -> str:
        session = str(event.unified_msg_origin or "")
        parts = text.lstrip("#").strip().split(None, 4)
        args = parts[1:]
        kb = self.knowledge

        if not args:
            rows = ["知识树：%d 棵，%d 个节点，待审 %d 条"
                    % (len(kb.trees), sum(len(tree.nodes) for tree in kb.trees),
                       len(kb.pending))]
            active = [tree.name for tree in kb.active_trees(session)]
            rows.append("本会话激活：" + ("、".join(active) if active else "（没有）"))
            for tree in kb.trees:
                mark = "*" if tree.id in (kb.active.get(session) or []) else " "
                rows.append(" %s %s（%d 个节点）" % (mark, tree.name, len(tree.nodes)))
            if not kb.trees:
                rows.append("还没有知识树。发 #知识 学 本群规矩 建一棵。")
            rows.append("")
            rows.append("#知识 开|关 <名>　给本会话挂上/摘掉")
            rows.append("#知识 加 <树名> <标题> <内容>　往树上加一条")
            rows.append("#知识 待审 / #知识 批|否 <id>　模型提的改动")
            return "\n".join(rows)

        head = args[0].lower()
        rest = args[1:]
        if head in ("学", "new", "建"):
            name = " ".join(rest).strip()
            if not name:
                return "用法：#知识 学 <名字>"
            tree = kb.add_tree(name)
            self._knowledge_save()
            return "建好了：%s。发 #知识 开 %s 给本会话挂上。" % (tree.name, tree.name)
        if head in ("开", "on", "激活"):
            name = " ".join(rest).strip()
            tree = kb.by_name(name)
            if tree is None:
                return "没有这棵树：" + (name or "（空）")
            ok = kb.activate(session, tree.id)
            self._knowledge_save()
            return ("已给本会话挂上：" + tree.name) if ok else "已经挂着了：" + tree.name
        if head in ("关", "off", "摘"):
            name = " ".join(rest).strip()
            tree = kb.by_name(name)
            if tree is None:
                return "没有这棵树：" + (name or "（空）")
            ok = kb.deactivate(session, tree.id)
            self._knowledge_save()
            return ("已摘掉：" + tree.name) if ok else "本来就没挂：" + tree.name
        if head in ("加", "add"):
            if len(args) < 4:
                return "用法：#知识 加 <树名> <标题> <内容>"
            tree = kb.by_name(args[1])
            if tree is None:
                return "没有这棵树：" + args[1]
            node = kb.add_node(tree, args[2], args[3])
            if node is None:
                return "这棵树满了（%d 个节点）" % knowledge_engine.MAX_NODES
            self._knowledge_save()
            return "已加到「%s」：%s" % (tree.name, node.title)
        if head in ("待审", "pending", "审"):
            if not kb.pending:
                return "没有待审的改动。"
            rows = ["待审 %d 条：" % len(kb.pending)]
            for edit in kb.pending:
                tree = kb.tree(edit.tree_id)
                node = tree.get(edit.node_id) if tree else None
                rows.append("  %s｜%s / %s｜%s"
                            % (edit.id, tree.name if tree else "?",
                               node.title if node else "?",
                               (edit.content or edit.title or "")[:40]))
            rows.append("批准：#知识 批 <id>　丢弃：#知识 否 <id>")
            return "\n".join(rows)
        if head in ("批", "approve"):
            if not rest:
                return "用法：#知识 批 <id>"
            ok = kb.approve(rest[0])
            self._knowledge_save()
            return "已批准并生效。" if ok else "没找到这条待审（或节点已不在）。"
        if head in ("否", "reject"):
            if not rest:
                return "用法：#知识 否 <id>"
            ok = kb.reject(rest[0])
            self._knowledge_save()
            return "已丢弃。" if ok else "没找到这条待审。"
        return "不认识的用法。发 #知识 看说明。"

    async def _debug_animation(self, event: AstrMessageEvent, text: str) -> str:
        parts = text.lstrip("#").strip().split(None, 1)
        path = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""
        if not path:
            return "用法：#动图 <GIF 或动态 WebP 的路径>"
        await self._mail_call(self.ensure_ffmpeg, False)
        result = await asyncio.to_thread(animation_engine.build, path)
        if not result.get("ok"):
            return "看不了动图：" + str(result.get("error") or "未知原因")
        return await self._emit_bulk(
            event, str(result.get("manifest") or ""), [str(result.get("image") or "")], "动图"
        )

    async def _debug_soundcheck(self, event: AstrMessageEvent, text: str) -> str:
        parts = text.lstrip("#").strip().split(None, 1)
        path = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""
        if not path:
            return "用法：#听歌 <音频文件路径>"
        await self._mail_call(self.ensure_ffmpeg, False)
        conf = self.storyboard_settings
        result = await asyncio.to_thread(
            soundcheck_engine.analyze, path, conf.ffmpeg_path, conf.ffprobe_path
        )
        images: list[str] = []
        if result.get("ok"):
            try:
                workdir = Path(self.data_dir) / "soundcheck"
                workdir.mkdir(parents=True, exist_ok=True)
                out = workdir / "spectrum.png"
                if soundcheck_engine.spectrum(
                    path, out, ffmpeg_hint=conf.ffmpeg_path,
                    ffprobe_hint=conf.ffprobe_path,
                ):
                    images.append(str(out))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 画频谱失败：{exc}")
        return await self._emit_bulk(
            event, soundcheck_engine.report(result), images, "音频"
        )

    # -- QQ 空间：写日记 / 配图 / 发说说 ------------------------------------
    def _qzone_api_of(self, event: AstrMessageEvent | None = None) -> Any:
        """拿能发说说的 bot api（NapCat 这类端才有空间扩展接口）。"""
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is not None:
            self._qzone_api_cache = api
            try:
                self._qzone_session = str(event.unified_msg_origin or "")
            except Exception:  # noqa: BLE001
                pass
            return api
        if self._qzone_api_cache is not None:
            return self._qzone_api_cache
        manager = getattr(getattr(self, "context", None), "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        try:
            insts = list(getter() or []) if callable(getter) else []
        except Exception:  # noqa: BLE001
            insts = []
        for inst in insts:
            api = (getattr(getattr(inst, "bot", None), "api", None)
                   or getattr(inst, "api", None))
            if api is not None and callable(getattr(api, "call_action", None)):
                self._qzone_api_cache = api
                return api
        return None

    def _qzone_mood(self, session_key: str = "") -> str:
        """此刻的心情：能读到情绪就用情绪原型的名字，读不到就随便挑一个。"""
        try:
            session = self.engine.sessions.get(str(session_key or ""))
            if session is not None:
                snap = self.engine.snapshot(session)
                proto = getattr(snap, "proto", None)
                label = str(getattr(proto, "label", "") or getattr(proto, "key", "") or "")
                if label:
                    return label
        except Exception:  # noqa: BLE001
            pass
        return random.choice(list(qzone_engine.MOODS))

    async def _qzone_provider(self) -> Any:
        provider = await self._resolve_provider(str(self._qzone_session or ""))
        if provider is not None:
            return provider
        try:
            providers = self.context.get_all_providers()
        except Exception:  # noqa: BLE001
            providers = []
        return providers[0] if providers else None

    async def _qzone_illustrate(self, title: str, body: str) -> str:
        """给日记配一张图；配图失败不能拦住写日记。"""
        conf = self.qzone_settings
        try:
            ready = bool(self.imagegen_settings.ready())
        except Exception:  # noqa: BLE001
            ready = False
        if not conf.with_image or not ready:
            return ""
        prompt = qzone_engine.image_prompt(title, body, conf.image_style)
        folder = Path(self.data_dir) / "qzone" / "img"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            result = await asyncio.to_thread(
                imagegen_engine.generate, prompt, self.imagegen_settings, folder, ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 日记配图失败：{exc}")
            return ""
        paths = result.get("images") or []
        return str(paths[0]) if result.get("ok") and paths else ""

    async def _qzone_write(self, session_key: str = "") -> dict[str, Any]:
        """让模型写今天的日记（可选配图），存成草稿。"""
        conf = self.qzone_settings
        if not conf.enabled:
            return {"ok": False, "error": "空间功能关着（面板「空间」页打开）"}
        if session_key:
            self._qzone_session = str(session_key)
        provider = await self._qzone_provider()
        if provider is None:
            return {"ok": False, "error": "没有可用的模型，先配一个对话模型"}
        day = qzone_engine.day_text()
        mood = self._qzone_mood(str(session_key or ""))
        system, prompt = qzone_engine.diary_prompt(
            day, mood, persona=conf.write_system, max_chars=conf.max_chars
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system), timeout=90
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "写日记失败了：" + (str(exc) or type(exc).__name__)[:160]}
        parsed = qzone_engine.parse_diary(getattr(response, "completion_text", "") or "")
        if not parsed.get("body"):
            return {"ok": False, "error": "模型没写出正文，再试一次"}
        image = await self._qzone_illustrate(parsed["title"], parsed["body"])
        ident = self.diaries.add(parsed["title"], parsed["body"], parsed["mood"],
                                 image=image, day=day)
        if not ident:
            return {"ok": False, "error": "日记没存进库（数据库不可写？）"}
        entry = self.diaries.get(ident)
        return {"ok": True, "entry": entry.to_dict() if entry else {}, "day": day, "mood": mood}

    async def _qzone_publish(self, diary_id: int = 0,
                             event: AstrMessageEvent | None = None) -> dict[str, Any]:
        """把一条草稿发到空间。成功才改状态，失败留 failed 让人重试。"""
        conf = self.qzone_settings
        entry = self.diaries.get(int(diary_id)) if diary_id else self.diaries.latest_draft()
        if entry is None:
            return {"ok": False, "error": "没有可发的日记"}
        if entry.status == "published" and entry.tid:
            return {"ok": False, "error": "这条已经发过了", "entry": entry.to_dict()}
        api = self._qzone_api_of(event)
        if api is None:
            return {"ok": False,
                    "error": "没连上 QQ（空间接口要 NapCat / LLOneBot 这类支持扩展接口的端）"}
        content = qzone_engine.compose_content(entry, conf.signature)
        if not content.strip():
            return {"ok": False, "error": "正文是空的"}
        images: list[str] = []
        uri = qzone_engine.image_data_uri(entry.image) if entry.image else ""
        if uri:
            images.append(uri)
        params = qzone_engine.build_send_params(content, images, conf.ugc_right)
        try:
            payload = await asyncio.wait_for(
                api.call_action("send_qzone_msg", **params), timeout=60
            )
        except Exception as exc:  # noqa: BLE001
            message = (str(exc) or type(exc).__name__)[:200]
            self.diaries.mark_failed(entry.id, message)
            return {"ok": False, "error": "发送失败：" + message}
        parsed = qzone_engine.parse_send(payload)
        if not parsed.get("ok"):
            error = str(parsed.get("error") or "没发出去")
            self.diaries.mark_failed(entry.id, error)
            return {"ok": False, "error": error}
        published_at = float(parsed.get("time") or 0) or time.time()
        self.diaries.mark_published(entry.id, str(parsed.get("tid") or ""), published_at)
        logger.info(f"[ai_mind] 日记已发到空间：id={entry.id} tid={parsed.get('tid')}")
        return {"ok": True, "tid": parsed.get("tid") or "",
                "entry": (self.diaries.get(entry.id) or entry).to_dict()}

    async def _qzone_refresh(self) -> dict[str, Any]:
        """拉空间最新说说，顺手更新本地已发那几条的赞/评论数。"""
        api = self._qzone_api_of()
        if api is None:
            return {"ok": False, "error": "没连上 QQ（空间接口要 NapCat）"}
        try:
            payload = await asyncio.wait_for(
                api.call_action("get_qzone_msg_list", pos=0, num=20), timeout=40
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False,
                    "error": "拉空间列表失败：" + (str(exc) or type(exc).__name__)[:160]}
        feeds = qzone_engine.parse_feeds(payload)
        for feed in feeds:
            self.diaries.update_stats(str(feed.get("tid") or ""),
                                      int(feed.get("likes") or 0),
                                      int(feed.get("comments") or 0))
        return {"ok": True, "feeds": feeds, "count": len(feeds)}

    async def _qzone_profile_refresh(self) -> dict[str, str]:
        api = self._qzone_api_of()
        if api is None:
            return {}
        try:
            payload = await asyncio.wait_for(
                api.call_action("get_qzone_profile"), timeout=20
            )
        except Exception:  # noqa: BLE001 - 有的端没这个接口，不影响发说说
            return {}
        profile = qzone_engine.parse_profile(payload)
        if profile.get("nickname") or profile.get("uin"):
            self._qzone_profile = profile
        return profile

    def _qzone_payload(self) -> dict[str, Any]:
        entries = [item.to_dict() for item in self.diaries.recent(30)]
        # 配图只在时间线最上面几条带（base64 塞太多会把面板撑爆）
        wanted = [str(item.get("image") or "") for item in entries[:8]
                  if item.get("image")]
        images = {row.get("path"): row.get("image")
                  for row in self._images_payload(wanted)}
        for entry in entries:
            entry["image_url"] = images.get(str(entry.get("image") or ""), "")
        return {
            "settings": self.qzone_settings.to_dict(),
            "stats": self.diaries.stats(),
            "entries": entries,
            "profile": dict(self._qzone_profile),
            "has_api": self._qzone_api_of() is not None,
            "moods": list(qzone_engine.MOODS),
        }

    async def _qzone_daily_tick(self) -> None:
        """到点自己写一篇、发到空间（默认关；开了就是替主人往空间发东西）。"""
        conf = self.qzone_settings
        if not conf.enabled or not conf.auto_post:
            return
        state = Path(self.data_dir) / "qzone" / "last_run.json"
        last = 0.0
        try:
            last = float(json.loads(state.read_text(encoding="utf-8")).get("at") or 0)
        except (OSError, ValueError):
            last = 0.0
        if not qzone_engine.due_daily(conf, time.time(), last):
            return
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
        except OSError:
            return
        written = await self._qzone_write(str(getattr(self, "_bili_target", "") or ""))
        if not written.get("ok"):
            logger.warning("[ai_mind] 定时写日记失败：" + str(written.get("error") or ""))
            return
        entry = written.get("entry") or {}
        posted = await self._qzone_publish(int(entry.get("id") or 0))
        if posted.get("ok"):
            logger.info("[ai_mind] 定时日记已发到空间")
        else:
            logger.warning("[ai_mind] 定时发空间失败：" + str(posted.get("error") or ""))

    def _qzone_report(self) -> str:
        conf = self.qzone_settings
        stats = self.diaries.stats()
        lines = [
            "空间：%s｜自动发 %s（%02d:%02d）｜权限 %s"
            % ("开" if conf.enabled else "关",
               "开" if conf.auto_post else "关", conf.auto_hour, conf.auto_minute,
               qzone_engine.UGC_RIGHTS.get(conf.ugc_right, "?")),
            "日记 %d 条（草稿 %d / 已发 %d）｜配图 %s"
            % (stats["total"], stats["drafts"], stats["published"],
               "开" if conf.with_image else "关"),
        ]
        for entry in self.diaries.recent(6):
            lines.append("  #%d [%s] %s %s"
                         % (entry.id, entry.status_text, entry.title,
                            entry.time_text if hasattr(entry, "time_text") else ""))
        lines.append("")
        lines.append("用法：#日记 写 / 发 [id] / 删 <id> / 刷新")
        return chr(10).join(lines)

    async def _debug_qzone(self, event: AstrMessageEvent, text: str) -> str:
        parts = text.lstrip("#").strip().split(None, 2)
        args = parts[1:]
        key = str(event.unified_msg_origin or "")
        head = (args[0].lower() if args else "")
        if not head:
            return self._qzone_report()
        if head in ("写", "write", "new"):
            result = await self._qzone_write(key)
            if not result.get("ok"):
                return "写不出来：" + str(result.get("error") or "")
            entry = result.get("entry") or {}
            return (("写好了 #%d《%s》" + chr(10) + "%s" + chr(10) * 2
                     + "要发就说：#日记 发")
                    % (int(entry.get("id") or 0), entry.get("title") or "",
                       str(entry.get("body") or "")[:400]))
        if head in ("发", "send", "post", "publish"):
            ident = 0
            if len(args) > 1 and str(args[1]).strip().isdigit():
                ident = int(str(args[1]).strip())
            result = await self._qzone_publish(ident, event)
            if not result.get("ok"):
                return "没发出去：" + str(result.get("error") or "")
            return "已发到空间（tid %s）" % (result.get("tid") or "?")
        if head in ("删", "delete", "rm"):
            if len(args) < 2 or not str(args[1]).strip().isdigit():
                return "用法：#日记 删 <id>"
            ident = int(str(args[1]).strip())
            ok = self.diaries.remove(ident)
            return "删掉了 #%d" % ident if ok else "没找到 #%d" % ident
        if head in ("刷新", "refresh", "list"):
            result = await self._qzone_refresh()
            if not result.get("ok"):
                return "刷新失败：" + str(result.get("error") or "")
            return "空间最近 %d 条说说（本地已同步赞/评论数）" % int(result.get("count") or 0)
        return self._qzone_report()

    # -- 面板：QQ 空间 ------------------------------------------------------
    async def _api_qzone(self) -> Any:
        data = self._qzone_payload()
        if not self._qzone_profile:
            profile = await self._qzone_profile_refresh()
            if profile:
                data["profile"] = profile
        return json_response(data)

    async def _api_qzone_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        merged = self.qzone_settings.to_dict()
        merged.update({key: value for key, value in payload.items() if key in merged})
        self.qzone_settings = qzone_engine.QzoneSettings.from_dict(merged)
        ok = qzone_engine.save_settings(
            Path(self.data_dir) / "qzone.json", self.qzone_settings
        )
        data = self._qzone_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入空间设置失败，详细原因见 AstrBot 日志")
        return json_response(data)

    async def _api_qzone_write(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        result = await self._qzone_write(str(payload.get("session") or ""))
        data = self._qzone_payload()
        data.update({"ok": bool(result.get("ok")),
                     "error": str(result.get("error") or ""),
                     "entry": result.get("entry") or {}})
        return json_response(data)

    async def _api_qzone_publish(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        try:
            ident = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            ident = 0
        result = await self._qzone_publish(ident)
        data = self._qzone_payload()
        data.update({"ok": bool(result.get("ok")),
                     "error": str(result.get("error") or ""),
                     "tid": str(result.get("tid") or "")})
        return json_response(data)

    async def _api_qzone_delete(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        try:
            ident = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            ident = 0
        ok = bool(ident) and self.diaries.remove(ident)
        data = self._qzone_payload()
        data["done"] = ok
        return json_response(data)

    async def _api_qzone_refresh(self) -> Any:
        result = await self._qzone_refresh()
        data = self._qzone_payload()
        data.update({"ok": bool(result.get("ok")),
                     "error": str(result.get("error") or ""),
                     "count": int(result.get("count") or 0),
                     "feeds": result.get("feeds") or []})
        return json_response(data)

    # -- 结果投递（群里 → 指令 QQ 私聊） -----------------------------------
    def _delivery_save(self) -> str:
        ok = delivery_engine.save_settings(
            Path(self.data_dir) / "delivery.json", self.delivery_settings
        )
        return "" if ok else "（投递设置没写进磁盘）"

    def _private_target(self, event: AstrMessageEvent) -> str:
        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        return delivery_engine.private_umo(
            str(event.unified_msg_origin or ""), str(event.get_sender_id() or ""), platform
        )

    async def _emit_bulk(self, event: AstrMessageEvent, text: str,
                         images: list[str] | None = None,
                         label: str = "") -> str:
        """大块结果：群里发指令就发到指令那个 QQ 的私聊，私聊就原地给。

        返回**群里该说的话**：原地发时返回原文；改走私聊时返回一句提示。
        """
        umo = str(event.unified_msg_origin or "")
        want_private = (
            self.delivery_settings.bulk_to_private
            and delivery_engine.is_group(umo)
            and Comp is not None
        )
        if not want_private:
            await self._send_chains(umo, text, images, label)
            return text
        target = self._private_target(event)
        if not target:
            await self._send_chains(umo, text, images, label)
            return text
        sent = 0
        chains: list[tuple[str, list[Any]]] = []
        head = (label or "结果") + "："
        chains.append((target, [Comp.Plain(text)]))
        for index, path in enumerate(images or [], 1):
            item = Path(str(path))
            if not item.is_file():
                continue
            chains.append((target, [Comp.Plain("%s第 %d 张" % (head, index)),
                                    Comp.Image.fromFileSystem(str(item))]))
        for where, chain in chains:
            try:
                await self.context.send_message(where, chain)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - 私聊发不出去就退回群里
                logger.warning(f"[ai_mind] 发私聊失败：{exc}")
                break
        if not sent:
            await self._send_chains(umo, text, images, label)
            return text
        return "已经发到你私聊了（%d 条）。" % sent

    async def _send_chains(self, umo: str, text: str, images: list[str] | None = None,
                           label: str = "") -> int:
        """在原地把文字和图片发出去。"""
        if Comp is None:
            return 0
        sent = 0
        head = (label or "结果") + "："
        try:
            await self.context.send_message(umo, [Comp.Plain(text)])
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 发送结果失败：{exc}")
            return sent
        for index, path in enumerate(images or [], 1):
            item = Path(str(path))
            if not item.is_file():
                continue
            try:
                await self.context.send_message(umo, [
                    Comp.Plain("%s第 %d 张" % (head, index)),
                    Comp.Image.fromFileSystem(str(item)),
                ])
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 发送图片失败：{exc}")
                break
        return sent

    # -- 视频分镜 -----------------------------------------------------------
    def _storyboard_workdir(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = Path(self.data_dir) / "storyboard" / stamp
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _storyboard_build(self, source: str,
                          transcript: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """拿路径或链接跑一次分镜。永不抛异常。"""
        conf = self.storyboard_settings
        if not conf.enabled:
            return {"ok": False, "error": "分镜功能被关掉了（data/storyboard.json）", "hint": ""}
        workdir = self._storyboard_workdir()
        target = str(source or "").strip()
        if not target:
            return {"ok": False, "error": "没给视频路径或链接", "hint": ""}
        # 文件不存在这种错先让 build 自己报（别拿 ffmpeg 提示盖掉真问题）
        if storyboard_engine.is_url(target) or Path(target).exists():
            ready = self.ensure_ffmpeg(auto=False)
            if not ready.get("ok"):
                return {"ok": False,
                        "error": "ffmpeg 没就绪：" + str(ready.get("message") or ""),
                        "hint": "面板「分镜」页点「一键内置」，或把 ffmpeg 填进设置。"}
        if storyboard_engine.is_url(target):
            local = workdir / "download.mp4"
            ok, err = storyboard_engine.download(
                target, local, conf.max_download_mb * 1024 * 1024
            )
            if not ok:
                return {"ok": False, "error": err, "hint": ""}
            target = str(local)
        result = storyboard_engine.build(
            target, need=conf.need, workdir=workdir,
            ffmpeg_hint=conf.ffmpeg_path, ffprobe_hint=conf.ffprobe_path,
            cache_dir=(Path(self.data_dir) / "storyboard" / "cache") if conf.cache else None,
            annotate=conf.annotate, audio_bar=conf.audio_bar,
            asr_command=("" if transcript is not None else conf.asr_command),
            transcript=transcript, font_hint=conf.font_path,
        )
        result["workdir"] = str(workdir)
        result["source"] = str(target)
        if result.get("ok"):
            # 记住这一次，#分镜放大 要回来取它
            self._last_storyboard = {
                "source": str(target),
                "plan": result.get("plan") or {},
                "workdir": str(workdir),
                "at": time.time(),
            }
        return result

    @staticmethod
    def _storyboard_text(result: dict[str, Any], max_lines: int = 40) -> str:
        """聊天里用的短版：摘要 + 清单（太长就截断）。"""
        if not result.get("ok"):
            text = "分镜失败：" + str(result.get("error") or "未知原因")
            hint = str(result.get("hint") or "").strip()
            if hint:
                text += "\n" + hint
            return text
        lines = [str(result.get("summary") or "")]
        manifest = str(result.get("manifest") or "").splitlines()
        if manifest:
            lines.append("")
            body = manifest[1:] if len(manifest) > 1 else manifest
            lines.extend(body[:max_lines])
            if len(body) > max_lines:
                lines.append("…（还有 %d 行，面板里看全部）" % (len(body) - max_lines))
        return "\n".join(line for line in lines if line is not None)

    async def _send_storyboard_images(self, event: AstrMessageEvent,
                                      result: dict[str, Any]) -> int:
        """把宫格图发进会话（发失败也不影响文字结果）。"""
        if Comp is None or not result.get("ok"):
            return 0
        key = event.unified_msg_origin
        sent = 0
        for sheet in result.get("sheets") or []:
            path = Path(str(sheet.get("image") or ""))
            if not path.is_file():
                continue
            try:
                chain = [Comp.Plain("第 %d 张分镜：" % int(sheet.get("i") or 0))]
                chain.append(Comp.Image.fromFileSystem(str(path)))
                await self.context.send_message(key, chain)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - 发图失败不该毁掉结果
                logger.warning(f"[ai_mind] 发送分镜图失败：{exc}")
                break
        return sent

    async def _debug_zoom(self, event: AstrMessageEvent, text: str) -> str:
        """把上一次分镜里的某一格再放大：在那几秒里多取几帧。"""
        parts = text.lstrip("#").strip().split()
        cell = 0
        for part in parts[1:]:
            if part.isdigit():
                cell = int(part)
                break
        if cell <= 0:
            return "用法：#分镜放大 <格号>（先 #分镜 跑一次）"
        last = getattr(self, "_last_storyboard", None)
        if not last:
            return "还没有分镜可放大：先 #分镜 <文件或链接> 跑一次。"
        conf = self.storyboard_settings
        workdir = Path(str(last.get("workdir") or (Path(self.data_dir) / "storyboard"))) / "zoom"
        result = await asyncio.to_thread(
            storyboard_engine.zoom,
            last.get("source", ""), last.get("plan") or {}, cell, 3, workdir,
            None, conf.font_path,
        )
        if not result.get("ok"):
            return "放大失败：" + str(result.get("error") or "未知原因")
        image = Path(str(result.get("image") or ""))
        lines = list(result.get("lines") or [])
        if result.get("text"):
            lines.append("这一格的台词：" + str(result["text"])[:200])
        text = "\n".join(lines) or "放大完成。"
        return await self._emit_bulk(
            event, text, [str(image)] if image.is_file() else [], "放大"
        )

    async def _debug_storyboard(self, event: AstrMessageEvent, text: str) -> str:
        conf = self.storyboard_settings
        parts = text.lstrip("#").strip().split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            report = storyboard_engine.selfcheck(conf.ffmpeg_path, conf.ffprobe_path)
            out = [
                "分镜：%s｜档位 %s｜每张最多 %d 格"
                % ("开" if conf.enabled else "关", conf.need,
                   storyboard_engine.max_cells_for_need(conf.need)),
                "ffmpeg：" + (report.get("version") or "没找到"),
            ]
            if not report.get("ok"):
                out.append(str(report.get("hint") or ""))
            out.append("")
            out.append("用法：#分镜 <视频文件路径 或 链接>")
            return "\n".join(out)
        result = await asyncio.to_thread(self._storyboard_build, arg) \
            if hasattr(asyncio, "to_thread") else self._storyboard_build(arg)
        text = self._storyboard_text(result)
        if not result.get("ok"):
            return text
        images = [str(sheet.get("image")) for sheet in result.get("sheets") or []]
        return await self._emit_bulk(event, text, images, "分镜")

    # -- 她的邮箱 -----------------------------------------------------------
    @staticmethod
    async def _mail_call(func, *args, **kwargs):
        """邮件是阻塞 IO，扔到线程里跑，别卡住事件循环。"""
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except AttributeError:  # pragma: no cover - 老 Python 没有 to_thread
            return func(*args, **kwargs)

    def _mail_report(self) -> str:
        conf = self.mail.config
        if not conf.enabled or not conf.address:
            return ("邮箱：还没配。\n"
                    "在 data/mail.json 里填 address（邮箱地址）、password（SMTP 授权码，"
                    "不是登录密码）、owner（她发信默认发给谁），enabled 改 true。\n"
                    "服务器地址留空会按域名自动猜（qq / 163 / 126 / gmail / outlook / sina）。")
        out = [
            "邮箱：" + conf.address
            + ("" if conf.password else "（还缺授权码）")
            + "｜发信 " + (conf.smtp_host or "没填")
            + "｜收信 " + (conf.imap_host or "没填"),
            "默认收件人：" + (conf.owner or conf.address),
        ]
        return "\n".join(out)

    # -- 好友名单 / 自己写信 -------------------------------------------------
    def _mail_log(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self._mail_sent_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        return [row for row in rows[-max(1, min(50, int(limit))):] if isinstance(row, dict)]

    def _mail_log_add(self, entry: dict[str, Any]) -> None:
        rows = self._mail_log(50)
        rows.append(entry)
        try:
            self._mail_sent_path.parent.mkdir(parents=True, exist_ok=True)
            self._mail_sent_path.write_text(
                json.dumps(rows[-50:], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug(f"[ai_mind] 记发信日志失败：{exc}")

    async def _mail_compose(self, friend: dict[str, str]) -> dict[str, str]:
        """让模型给这个好友写一封信。写不出来就返回空 body。"""
        provider = await self._qzone_provider()
        if provider is None:
            return {"subject": "", "body": "", "error": "没有可用的模型"}
        name = str(friend.get("name") or "朋友")
        system = (
            "你是杜可怡，14 岁，嘴硬心软、有点别扭。现在给一个朋友写一封短短的电子邮件。\n"
            "规矩：第一行只写主题（20 字以内，不加「主题：」）；空一行后写正文，150~300 字；\n"
            "像跟熟人说话，不排比、不总结升华、不用 emoji 连发；中文和中文之间不要空格；\n"
            "不写真名、学校、住址、生日、电话；不提你是 AI。"
        )
        prompt = "收信人：%s\n今天：%s\n写一封吧。" % (name, qzone_engine.day_text())
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system), timeout=90
            )
        except Exception as exc:  # noqa: BLE001
            return {"subject": "", "body": "", "error": "写信失败：" + str(exc)[:120]}
        letter = mail_engine.parse_letter(getattr(response, "completion_text", "") or "")
        if not letter.get("body", "").strip():
            return {"subject": "", "body": "", "error": "模型没写出正文"}
        return letter

    async def _mail_write_friend(self, friend: dict[str, str],
                                 notify: bool = False) -> dict[str, Any]:
        """写一封 + 寄出去，并把结果记进日志。"""
        conf = self.mail.config
        if not conf.ready_for_send():
            return {"ok": False, "error": "邮箱还没配好（address / password / SMTP）"}
        address = str(friend.get("address") or "")
        if "@" not in address:
            return {"ok": False, "error": "这个好友没有合法邮箱"}
        letter = await self._mail_compose(friend)
        if not letter.get("body"):
            return {"ok": False, "error": letter.get("error") or "没写出正文"}
        ok, message = await self._mail_call(
            self.mail.send, letter["subject"], letter["body"], address
        )
        self._mail_log_add({
            "at": time.time(), "to": address, "name": friend.get("name") or "",
            "subject": letter["subject"], "ok": bool(ok),
            "error": "" if ok else str(message)[:200],
        })
        if ok and notify:
            target = str(getattr(self, "_bili_target", "") or "")
            if target and Comp is not None:
                try:
                    await self.context.send_message(target, [
                        Comp.Plain("她给 %s 寄了一封信：%s"
                                   % (friend.get("name") or address, letter["subject"]))
                    ])
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": bool(ok), "message": message, "subject": letter["subject"],
                "body": letter["body"], "to": address,
                "name": friend.get("name") or ""}

    async def _mail_auto_tick(self) -> None:
        """到点自己写一封给好友（默认关；只发给名单里的人，每天有上限）。"""
        conf = self.mail.config
        if not (conf.enabled and conf.auto_enabled and conf.auto_daily > 0 and conf.friends):
            return
        if not conf.ready_for_send():
            return
        state_path = Path(self.data_dir) / "mail" / "auto_state.json"
        today = time.strftime("%Y-%m-%d")
        state: dict[str, Any] = {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        count = int(state.get("count") or 0) if state.get("day") == today else 0
        if count >= conf.auto_daily:
            return
        now = time.localtime()
        if conf.auto_hour < 0 or now.tm_hour < int(conf.auto_hour):
            return
        if float(state.get("last_at") or 0) > time.time() - 1800:
            return
        friend = conf.friends[count % len(conf.friends)]
        result = await self._mail_write_friend(friend, notify=True)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "day": today,
                "count": count + (1 if result.get("ok") else 0),
                "last_at": time.time(),
            }), encoding="utf-8")
        except OSError:
            pass
        if result.get("ok"):
            logger.info("[ai_mind] 自己给好友寄了一封信：" + str(friend.get("name") or ""))
        else:
            logger.warning("[ai_mind] 自动写信失败：" + str(result.get("error") or ""))

    async def _debug_mail(self, text: str) -> str:
        parts = text.lstrip("#").strip().split(None, 2)
        args = parts[1:]
        if args and args[0].lower() in ("mail", "邮箱"):
            args = args[1:]
        if not args:
            return self._mail_report()
        head = args[0].lower()
        if head in ("看", "收", "list", "inbox"):
            limit = 3
            if len(args) > 1 and str(args[1]).strip().isdigit():
                limit = max(1, min(20, int(str(args[1]).strip())))
            items, err = await self._mail_call(self.mail.recent, limit)
            if err:
                return "收信失败：" + err
            if not items:
                return "收件箱是空的（或者最近没有新邮件）。"
            lines = ["最近 %d 封：" % len(items)]
            for index, item in enumerate(items, 1):
                lines.append(
                    "%d. %s\n   %s｜%s%s"
                    % (index, item.subject, item.sender,
                       item.date[:31] or "时间不明",
                       ("\n   " + item.snippet) if item.snippet else "")
                )
            return await self._emit_bulk(event, "\n".join(lines), [], "收件箱")
        if head in ("发", "send"):
            body = " ".join(args[1:]).strip()
            if not body:
                return "用法：#邮件 发 <正文>（发到 data/mail.json 里的 owner）"
            subject = body.splitlines()[0][:24] or "来自她"
            ok, message = await self._mail_call(self.mail.send, subject, body)
            return ("✅ " if ok else "❌ ") + message
        if head in ("好友", "friends", "名单"):
            conf = self.mail.config
            if not conf.friends:
                return "好友名单是空的。面板「邮箱」页加一行：小明, xiaoming@qq.com"
            lines = ["好友名单（%d 人）：" % len(conf.friends)]
            for item in conf.friends:
                lines.append("· %s <%s>" % (item.get("name") or "?", item.get("address") or ""))
            auto = "开" if conf.auto_enabled else "关"
            lines.append("自己写信：%s（每天最多 %d 封，%02d 点后）"
                         % (auto, conf.auto_daily, max(0, conf.auto_hour)))
            return chr(10).join(lines)
        if head in ("给", "to", "friend"):
            rest = " ".join(args[1:]).strip()
            if not rest:
                return "用法：#邮件 给 <好友名>（她来写，寄给名单里的人）"
            friend = self.mail.config.friend_named(rest)
            if friend is None:
                return "名单里没有「%s」。先发 #邮件 好友 看有哪些人。" % rest
            result = await self._mail_write_friend(friend, notify=False)
            if not result.get("ok"):
                return "没寄出去：" + str(result.get("error") or "")
            return ("寄给 %s 了：%s" % (result.get("name") or result.get("to") or "",
                                       result.get("subject") or ""))
        if head in ("测试", "test"):
            ok, message = await self._mail_call(
                self.mail.send, "测试邮件", "这是一封测试邮件，说明邮箱配好了。"
            )
            return ("✅ " if ok else "❌ ") + message
        return ("用法：#邮件　　看状态\n"
                "#邮件 看 [N]　　看最近 N 封（会联网）\n"
                "#邮件 发 <正文>　　发到 owner\n"
                "#邮件 测试　　发一封测试信")

    def _debug_imitate(self, text: str) -> str:
        conf = self.imitate_settings
        args = [part for part in text.lstrip("#").strip().split() if part]
        if args and args[0].lower() in ("模仿", "imitate"):
            args = args[1:]
        if not args:
            return self._imitate_report()
        head = args[0].lower()
        rest = args[1:]
        if head in ("开", "on", "启用"):
            conf.enabled = True
            tip = "" if conf.targets else "，但还没指定 QQ，发 #模仿 学 <QQ>"
            return "模仿采样：开" + tip + self._imitate_save()
        if head in ("关", "off", "关闭"):
            conf.enabled = False
            return "模仿采样：关（已学的样本还在）" + self._imitate_save()
        if head in ("学", "learn", "add", "加"):
            if not rest:
                return "用法：#模仿 学 <QQ 号>，可以一次给多个（空格隔开）"
            added = [qq for qq in rest if conf.add_target(qq)]
            conf.enabled = True
            if not added:
                return "这些 QQ 已经在名单里了。当前目标：" + "、".join(conf.targets) + self._imitate_save()
            return ("开始学：" + "、".join(added)
                    + "（他之后说的话会被记下来，凑够 %d 条就会注入参考）" % conf.min_samples
                    + self._imitate_save())
        if head in ("停", "del", "remove", "删"):
            if not rest:
                return "用法：#模仿 停 <QQ 号>"
            gone = [qq for qq in rest if conf.remove_target(qq)]
            return ("不再学：" + "、".join(gone) if gone else "这些 QQ 本来就不在名单里。") + self._imitate_save()
        if head in ("清空", "clear"):
            target = rest[0] if rest else ""
            removed = self.imitate.clear(target)
            who = target or "全部"
            return ("已清掉 %d 条样本（%s）。" % (removed, "全部账号" if who == "全部" else who))
        if head in ("样本", "list", "列表"):
            return self._imitate_report()
        return ("用法：#模仿　　看状态\n"
                "#模仿 学 <QQ>　　开始学他说话\n"
                "#模仿 停 <QQ>　　不再学\n"
                "#模仿 清空 [QQ]　　删样本\n"
                "#模仿 开 / 关　　总开关")

    async def _debug_status(self, event: AstrMessageEvent, key: str) -> str:
        out = [
            "【会话】" + key,
            "调试模式：开（再发 " + DEBUG_CODE + " 退出）",
            "情绪 " + ("开" if self.settings.emotion.enabled else "关")
            + "｜记忆 " + ("开" if self.settings.memory.enabled else "关")
            + "｜分段 " + ("开" if self.splitter.enabled else "关")
            + "｜防抖 " + ("开" if self.settings.debounce.enabled else "关"),
        ]
        try:
            from .mind import schedule as _schedule
        except ImportError:  # pragma: no cover
            import mind.schedule as _schedule  # type: ignore[no-redef]
        try:
            slot = _schedule.today(time.time())
            out.append(
                "日程：" + str(slot.get("date") or "") + "｜"
                + str(slot.get("note") or "").rstrip("。")
                + "｜现在在：" + str(slot.get("activity") or "")
            )
        except Exception as exc:  # noqa: BLE001 - 日程算不出来不该让 #状态 挂掉
            out.append("日程算不出来：" + str(exc))
        session = self.engine.sessions.get(key)
        if session is None:
            out.append("情绪状态：还没建（在这个会话说一句就会建）")
        else:
            pad = session.emotion
            out.append("情绪 P/A/D：%+.2f / %+.2f / %+.2f" % (pad.p, pad.a, pad.d))
            relation = session.users.get(str(event.get_sender_id() or ""))
            if relation is not None:
                out.append(
                    "关系：好感 %.0f｜熟悉 %.0f｜累计 %d 条%s"
                    % (relation.affinity, relation.familiarity, relation.msg_count,
                       "｜★专属" if relation.special else "")
                )
        try:
            check = self.samples.selfcheck()
            out.append(
                "曲线：本会话 %d 点 / 全库 %d 点｜数据库%s"
                % (self.samples.count(key), check.get("rows", 0),
                   "可写" if check.get("write")
                   else ("写不进去 —— " + str(check.get("error") or "")))
            )
        except Exception as exc:  # noqa: BLE001
            out.append("曲线自检失败：" + str(exc))
        out.append(
            "待抽取：本会话 %d 轮 / 全部 %d 轮"
            % (len(self._buffers.get(key) or []),
               sum(len(v) for v in self._buffers.values()))
        )
        out.append("工具闸门：" + ("开" if self.settings.guard.block_tools else "关"))
        try:
            conf_mail = self.mail.config
            if conf_mail.enabled and conf_mail.address:
                out.append(
                    "邮件：" + conf_mail.address
                    + ("" if conf_mail.password else "（缺授权码）")
                )
            else:
                out.append("邮件：没配（data/mail.json）")
        except Exception as exc:  # noqa: BLE001
            out.append("邮件状态读不出来：" + str(exc))
        try:
            conf = self.imitate_settings
            learned = self.imitate.count(str(event.get_sender_id() or ""))
            out.append(
                "模仿：" + ("开" if conf.enabled else "关")
                + "｜目标 " + ("、".join(conf.targets) if conf.targets else "未指定")
                + "｜你这里学了 %d 条" % learned
            )
        except Exception as exc:  # noqa: BLE001
            out.append("模仿状态读不出来：" + str(exc))
        return "\n".join(out)

    def _debug_curve(self, key: str) -> str:
        check = self.samples.selfcheck()
        last = self.samples.latest(key)
        out = [
            "库文件：" + str(check.get("path") or "?")
            + ("（存在，%d 字节）" % int(check.get("size") or 0)
               if check.get("exists") else "（不存在）"),
            "写入自检：" + ("通过" if check.get("write")
                            else ("失败 —— " + str(check.get("error") or ""))),
            "采样点：本会话 %d / 全库 %d" % (self.samples.count(key), check.get("rows", 0)),
        ]
        if last is None:
            out.append("最近一点：无")
        else:
            pad = last.pad()
            out.append(
                "最近一点：%s（%.0f 分钟前）P/A/D %+.2f/%+.2f/%+.2f"
                % (time.strftime("%m-%d %H:%M", time.localtime(last.t)),
                   max(0.0, (time.time() - last.t) / 60.0), pad.p, pad.a, pad.d)
            )
        if self._sample_broken:
            out.append("注意：采样已被标记为不可用（面板上会出红条）")
        return "\n".join(out)

    def _debug_memory(self, text: str = "") -> str:
        args = [part for part in str(text or "").lstrip("#").strip().split() if part]
        if len(args) >= 3 and args[1] in ("对", "错"):
            sign = 1 if args[1] == "对" else -1
            targets = [item for item in args[2:4] if item.isdigit()]
            rows = [item for item in (self.store.get(int(mid)) for mid in targets) if item]
            changes = evolve_engine.apply_value(rows, targets, sign)
            for memory_id, value in changes.items():
                self.store.update_many([memory_id], importance=float(value))
            if not changes:
                return "没调整到任何记忆（id 不对，或者价值已经在边界上）。"
            return "已按「%s」调整 %d 条记忆的价值。\n" % (args[1], len(changes)) + "\n".join(
                "%d → %.2f" % (mid, value) for mid, value in changes.items()
            )
        rows = self.store.list_memories(limit=5)
        out = ["记忆共 %d 条，最近 5 条：" % int(self.store.stats().get("total", 0))]
        for item in rows:
            mark = "🔒" if item.sensitive else ("📌" if item.pinned else "·")
            try:
                from .mind.memory.model import SCOPE_LABELS as _scope_labels
                from .mind.panel import source_label as _source_label
            except ImportError:  # pragma: no cover
                from mind.memory.model import SCOPE_LABELS as _scope_labels  # type: ignore[no-redef]
                from mind.panel import source_label as _source_label  # type: ignore[no-redef]
            src = _source_label(getattr(item, "session_id", ""), item.scope)
            scope_text = _scope_labels.get(item.scope, item.scope)
            out.append(
                "%s [%s｜%s] %s" % (mark, scope_text, src, (item.content or "")[:60])
            )
        if not rows:
            out.append("（还没有记忆）")
        out.append("待抽取 %d 轮" % sum(len(v) for v in self._buffers.values()))
        out.append("价值回传 %d 次｜用法：#记忆 对|错 <id> 手动调整"
                   % int(getattr(self, "_feedback_applied", 0)))
        return "\n".join(out)

    async def _debug_extract(self) -> str:
        if not self.settings.memory.enabled:
            return "记忆功能关着，抽不了。"
        pending = sum(len(v) for v in self._buffers.values())
        if not pending:
            return ("没有待抽取的对话 —— 调试模式里这个会话不记，"
                    "换个普通会话聊两句再来。")
        await self.flush_memories(force=True)
        return ("已把 %d 轮排队抽取（要调一次模型，几秒后看日志里的"
                "「记忆抽取：新增 N / 合并 N / 跳过 N」）。" % pending)

    async def _debug_tts(self) -> str:
        """语音自检：这台机器现在能不能用 Edge TTS 合成。

        Edge 的接口是逆向的，而且会按 IP 拒绝（机房 / 代理 IP 通常直接 403）。
        所以先在你自己的机器上验一次：能出字节，再谈接进回复。"""
        try:
            from .mind import edgetts as _edgetts
        except ImportError:  # pragma: no cover
            import mind.edgetts as _edgetts  # type: ignore[no-redef]
        voice = _edgetts.DEFAULT_VOICE
        try:
            audio = await asyncio.to_thread(
                _edgetts.synthesize, "哼，你怎么才来呀。", voice, "+0%", "+0Hz", 15.0,
            )
        except Exception as exc:  # noqa: BLE001
            return "Edge TTS 调用异常：" + type(exc).__name__ + ": " + str(exc)
        if not audio:
            return (
                "Edge TTS 这次没拿到音频。最常见的原因是接口按 IP 拒绝"
                "（机房 / 代理 IP 会直接 403），其次是接口改版。"
                "出不来就当没有这个音色 —— 绝不弄丢回复。"
            )
        return "Edge TTS 可用：拿到 " + str(len(audio)) + " 字节 MP3（音色 " + voice + "）"

    def _debug_tools(self) -> str:
        guard = self.settings.guard
        return "\n".join([
            "工具闸门：" + ("开" if guard.block_tools else "关（默认关，装插件不该拿走工具）"),
            "模式：" + str(guard.tool_mode),
            "名单：" + (", ".join(str(x) for x in guard.kept_tools) or "（空 = 不限制）"),
            "想看模型实际拿到哪些工具：打开 advanced.debug_log，",
            "每轮日志里会有一行「本轮模型可用工具 N 个：…」。",
        ])

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_guard_message(self, event: AstrMessageEvent):
        """消息级鉴权：非主人的注入 / 冒充 / 索取密钥，拦在 LLM 之前。"""
        if not self.settings.enabled:
            return
        # 已经被别的插件（白名单之类）掐掉的会话：再扫一遍正则纯属浪费。
        # 这个钩子对每条消息都跑，早退一行就能省掉一次全量规则匹配。
        if event.is_stopped():
            return
        # ★ 群里跟机器人无关的消息（没 @、没唤醒词）压根不会进 LLM，
        #   鉴权不该管它们 —— 否则她会在**别人的对话里**插一句
        #   「这个我不能帮你做」，既冒犯人，又白白扫一遍全部规则。
        #   私聊不做这个判断：私聊每句都算在跟她说话，必须全查。
        if is_group_session(event.unified_msg_origin) and not self._addressed_to_me(event):
            return
        guard = self.settings.guard
        if not guard.enabled or not guard.block_sensitive:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        if self._is_owner(event):
            return
        if guard.block_all_commands and text[:1] in {"/", "!", "！", "／"}:
            hit = guard_engine.Hit("all_commands", "非主人使用指令", text[:24])
        else:
            hit = guard_engine.scan_message(
                text,
                extra=guard.extra_patterns,
                allow=guard.allow_patterns,
                block_admin_commands=True,
            )
        if hit is None:
            return
        self._guard_blocked += 1
        uid, platform, _pid = self._identity_of(event)
        logger.warning(
            f"[ai_mind] 拦下一条非主人消息（{hit.label} / {platform}:{uid}）：{text[:40]}"
        )
        if guard.reply:
            yield event.plain_result(guard.reply)
        event.stop_event()

    def _apply_tool_guard(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """请求级工具守卫：不该给非主人看的工具，直接从模型眼前摘掉。"""
        guard = self.settings.guard
        if not guard.enabled or not guard.block_tools:
            return
        try:
            if self._is_owner(event):
                return
        except Exception:  # noqa: BLE001
            return
        tool_set = getattr(req, "func_tool", None)
        tools = getattr(tool_set, "tools", None)
        if not isinstance(tools, list) or not tools:
            return
        kept = guard_engine.filter_tools(tools, guard.tool_mode, guard.kept_tools)
        if len(kept) == len(tools):
            return
        dropped = [str(getattr(item, "name", "") or "?") for item in tools if item not in kept]
        shown = ", ".join(dropped[:6])
        if kept:
            logger.info(
                f"[ai_mind] 非主人：可用工具 {len(tools)} 个 → {len(kept)} 个（摘掉：{shown}）"
            )
        else:
            logger.warning(
                f"[ai_mind] 非主人：工具被全部摘掉了（模式 {guard.tool_mode}，"
                f"名单 {guard.kept_tools}）—— 如果这不是你想要的，去面板"
                "「全部功能设置 → 主人鉴权」把 tool_mode 改成 blacklist，"
                "或把 block_tools 关掉。"
            )
        try:
            tool_set.tools = kept
        except Exception:  # noqa: BLE001
            try:
                req.func_tool = kept  # type: ignore[assignment]
            except Exception:  # noqa: BLE001
                logger.warning("[ai_mind] 这个版本改不了 func_tool，工具守卫只在执行期生效")

    @on_using_llm_tool()
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool: Any, tool_args: Any = None
    ) -> None:
        """执行期工具守卫：万一请求级没拦住（第三方 Agent 运行器），这里再挡一次。"""
        guard = self.settings.guard
        if not guard.enabled or not guard.block_tools:
            return
        try:
            if self._is_owner(event):
                return
        except Exception:  # noqa: BLE001
            return
        name = str(getattr(tool, "name", "") or "")
        if guard_engine.tool_allowed(name, guard.tool_mode, guard.kept_tools):
            return
        self._neutralize_tool(tool, name)

    def _neutralize_tool(self, tool: Any, name: str) -> None:
        """把工具本体换成「拒绝执行」。"""
        if getattr(tool, "_ai_mind_guarded", False):
            return
        marker = "[ai_mind] 这个工具只有主人能用，已拦截"

        async def _refuse(*_args: Any, **_kwargs: Any) -> str:
            logger.warning(f"[ai_mind] 非主人试图执行工具 {name}，已拦截")
            return marker

        for attr in ("handler", "call", "run", "func"):
            original = getattr(tool, attr, None)
            if original is None or not callable(original):
                continue
            try:
                setattr(tool, attr, _refuse)
            except Exception:  # noqa: BLE001
                continue
        try:
            setattr(tool, "_ai_mind_guarded", True)
        except Exception:  # noqa: BLE001
            pass

    def _group_label_guard(self, event: AstrMessageEvent, session_key: str) -> str:
        """群里有人用专属称呼叫她时，明确告诉她：别认。"""
        try:
            if not is_group_session(session_key):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        if not bool(self.settings.emotion.group_discretion):
            return ""
        try:
            if self._is_owner(event):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        label = str(self.settings.emotion.special_label or "").strip()
        if not label or label == "特别的人":
            return ""
        try:
            text = str(event.message_str or "")
        except Exception:  # noqa: BLE001
            return ""
        if label not in text:
            return ""
        return (
            "<group_discretion>"
            "群里有人用「%s」叫你。那不是他对你的称呼，也不代表他知道什么。"
            "不要承认、不要问「你怎么这么叫」、不要显得意外；就当他叫错了或叫别人，"
            "用你自己的方式简短带过。"
            "</group_discretion>" % label
        )

    async def _guard_reply(self, event: AstrMessageEvent, session_key: str) -> None:
        """发送前护栏：不许出现内部标记，也不许把机制说破。"""
        guard = self.settings.guard
        if not guard.enabled or not guard.scrub_reply:
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        chain = getattr(result, "chain", None)
        if not chain:
            return
        plains = [comp for comp in chain if splitter_engine.is_plain(comp)]
        if not plains or len(plains) != len(chain):
            return
        text = "".join(splitter_engine.plain_text(comp) for comp in plains)
        if not text.strip():
            return
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001
            group = False
        report = guard_engine.scrub_reply(text)
        cleaned = report.cleaned
        # 群里还有一类词不能出现：把「专属称呼」说得像私密约定。
        # 这类话只摘掉那半句，剩下的还能照常发。
        group_hits: list[str] = []
        if group:
            group_hits = [
                phrase for phrase in guard_engine.GROUP_LABEL_PHRASES
                if phrase and phrase in cleaned
            ]
            for phrase in group_hits:
                cleaned = cleaned.replace(phrase, "")
            if group_hits:
                cleaned = cleaned.strip("，。、…—？！?! ")
                if not cleaned:
                    cleaned = "嗯？"
        if not report.leaked and not group_hits:
            return
        self._guard_leaks += 1
        logger.warning(
            f"[ai_mind] 回复里出现了内部内容 {report.markers + report.phrases}，已清理"
        )
        for index, comp in enumerate(plains):
            splitter_engine.set_plain_text(comp, cleaned if index == 0 else "")

    def _humanize_payload(self, sample: str = "") -> dict[str, Any]:

        settings = self.settings.humanize
        payload: dict[str, Any] = {
            "settings": settings.to_dict(),
            "defaults": HumanizeSettings().to_dict(),
            "modes": list(HUMANIZE_MODES),
            "mode_labels": dict(HUMANIZE_MODE_LABELS),
            "has_overrides": bool(self._humanize_data),
            "scopes": list(splitter_engine.SCOPES),
            "scope_labels": dict(splitter_engine.SCOPE_LABELS),
            "stats": self.humanize_log.stats(time.time() - 7 * 86400),
            "stats_all": self.humanize_log.stats(0.0),
            "recent": [entry.to_dict() for entry in self.humanize_log.recent(20)],
        }
        if sample:
            payload["preview"] = antiai.analyse(sample, **self._humanize_kwargs()).to_dict()
        return payload

    # ------------------------------------------------------------------
    # 表达示例：学她怎么说话，批准后当参考
    # ------------------------------------------------------------------
    def _style_allowed(self, session_key: str, is_llm: bool = True) -> bool:
        settings = self.settings.style
        if not settings.enabled:
            return False
        if settings.llm_only and not is_llm:
            return False
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001
            group = True
        if settings.scope == "group" and not group:
            return False
        if settings.scope == "private" and group:
            return False
        return True

    def _learn_style(
        self, event: AstrMessageEvent, session_key: str, reply: str, is_llm: bool
    ) -> None:
        """把这一轮对话攒成一条待审的表达示例。

        质量门槛直接用去 AI 味的打分：一段回复像不像人话，前面已经算过了。
        """
        settings = self.settings.style
        if not self._style_allowed(session_key, is_llm):
            return
        if self._sender_denied(event):
            return
        user_text = (event.message_str or "").strip()
        reply_text = (reply or "").strip()
        if not user_text or not reply_text:
            return
        if style_engine.is_followup(user_text):
            return
        length = len(reply_text)
        if length < settings.min_chars or length > settings.max_chars:
            return
        try:
            report = antiai.analyse(reply_text, **self._humanize_kwargs())
        except Exception:  # noqa: BLE001
            return
        if report.score < settings.min_score:
            return
        if not any(check.key == "bullets" for check in report.issues):
            pass
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001
            group = True
        example = StyleExample(
            user_text=user_text[:200],
            reply_text=reply_text[:400],
            created_at=time.time(),
            session_id=session_key,
            scene=style_engine.SCENE_GROUP if group else style_engine.SCENE_PRIVATE,
            keywords=tuple(style_engine.keywords_of(user_text)),
            grams=tuple(style_engine.ngrams(user_text)),
            score=report.score,
            status=(
                style_engine.STATUS_APPROVED if settings.auto_approve
                else style_engine.STATUS_PENDING
            ),
            source=style_engine.SOURCE_AUTO,
        )
        new_id = self.styles.add(example)
        if new_id and not settings.auto_approve:
            logger.info(f"[ai_mind] 学到一条表达示例（待审）：{reply_text[:24]}")

    def _style_block(self, session_key: str, query: str) -> str:
        """挑几条最像"这次该怎么说"的示例，渲染成注入块。"""
        settings = self.settings.style
        limit = int(settings.inject_count or 0)
        if not settings.enabled or limit <= 0:
            return ""
        try:
            group = bool(is_group_session(session_key))
        except Exception:  # noqa: BLE001
            group = True
        scene = style_engine.SCENE_GROUP if group else style_engine.SCENE_PRIVATE
        picked = self.styles.matched(query, scene=scene, limit=limit)
        if not picked:
            return ""
        template = self._prompt("style.examples", "")
        if not template:
            return ""
        rendered = prompt_engine.render(
            template, {"examples": style_engine.render_examples(picked)}
        )
        if rendered:
            self.styles.touch([item.id for item in picked])
        return rendered

    def _style_payload(self, query: str = "", status: str = "") -> dict[str, Any]:
        settings = self.settings.style
        rows, total = self.styles.list(status=status, query=query, limit=80)
        return {
            "settings": settings.to_dict(),
            "statuses": list(style_engine.STATUSES),
            "status_labels": dict(style_engine.STATUS_LABELS),
            "counts": self.styles.counts(),
            "items": [item.to_dict() for item in rows],
            "total": total,
            "recent_hits": self._style_recent_hits(),
        }

    def _style_recent_hits(self) -> list[dict[str, Any]]:
        try:
            rows = self.styles.list(status=style_engine.STATUS_APPROVED, limit=200)[0]
        except Exception:  # noqa: BLE001
            return []
        used = [item for item in rows if item.hit_count]
        used.sort(key=lambda item: -item.hit_count)
        return [item.to_dict() for item in used[:10]]

    # -- 面板：模仿 / 邮箱 / 今天 ------------------------------------------
    def _imitate_payload(self) -> dict[str, Any]:
        conf = self.imitate_settings
        rows = self.imitate.users()
        known = {uid for uid, _ in rows}
        profiles: list[dict[str, Any]] = []
        for uid, count in rows:
            profile = self.imitate.profile(uid)
            profiles.append({
                "uid": uid,
                "samples": count,
                "target": uid in conf.targets,
                "lines": imitate_engine.describe(profile),
                "profile": profile.to_dict(),
            })
        for uid in conf.targets:
            if uid not in known:
                profiles.append({"uid": uid, "samples": 0, "target": True,
                                 "lines": [], "profile": None})
        return {
            "settings": conf.to_dict(),
            "profiles": profiles,
            "stats": self.imitate.stats(),
        }

    async def _api_imitate(self) -> Any:
        return json_response(self._imitate_payload())

    async def _api_imitate_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.imitate_settings
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        if "inject" in payload:
            conf.inject = bool(payload.get("inject"))
        if payload.get("min_samples") is not None:
            try:
                conf.min_samples = max(3, min(500, int(payload["min_samples"])))
            except (TypeError, ValueError):
                pass
        raw = payload.get("targets")
        if isinstance(raw, list):
            conf.targets = []
            for item in raw:
                text = str(item).strip()
                if text and text not in conf.targets:
                    conf.targets.append(text)
        elif isinstance(raw, str):
            conf.targets = []
            for item in raw.replace(",", " ").replace("，", " ").split():
                if item.strip() and item.strip() not in conf.targets:
                    conf.targets.append(item.strip())
        ok = imitate_engine.save_settings(Path(self.data_dir) / "imitate.json", conf)
        data = self._imitate_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入模仿设置失败，详细原因见 AstrBot 日志")
        return json_response(data)

    async def _api_imitate_clear(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        uid = str(payload.get("uid") or "").strip()
        removed = self.imitate.clear(uid)
        data = self._imitate_payload()
        data["removed"] = removed
        return json_response(data)

    async def _api_mail(self) -> Any:
        data = self.mail.status()
        data["log"] = self._mail_log(12)
        return json_response(data)

    async def _api_mail_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.mail.config
        if payload.get("address") is not None:
            conf.address = str(payload.get("address") or "").strip()
        # 留空 = 不改授权码（面板上本来就看不到它）
        if str(payload.get("password") or "").strip():
            conf.password = str(payload.get("password")).strip()
        if payload.get("clear_password"):
            conf.password = ""
        if payload.get("owner") is not None:
            conf.owner = str(payload.get("owner") or "").strip()
        if "friends" in payload:
            conf.friends = mail_engine.parse_friends(payload.get("friends"))
        if "auto_enabled" in payload:
            conf.auto_enabled = bool(payload.get("auto_enabled"))
        if payload.get("auto_hour") is not None:
            try:
                conf.auto_hour = max(-1, min(23, int(payload.get("auto_hour"))))
            except (TypeError, ValueError):
                pass
        if payload.get("auto_daily") is not None:
            try:
                conf.auto_daily = max(0, min(5, int(payload.get("auto_daily"))))
            except (TypeError, ValueError):
                pass
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        if payload.get("smtp_host") is not None:
            conf.smtp_host = str(payload.get("smtp_host") or "").strip()
        if payload.get("imap_host") is not None:
            conf.imap_host = str(payload.get("imap_host") or "").strip()
        for key in ("smtp_port", "imap_port"):
            if payload.get(key) is not None:
                try:
                    setattr(conf, key, max(1, min(65535, int(payload.get(key)))))
                except (TypeError, ValueError):
                    pass
        for key in ("smtp_ssl", "imap_ssl"):
            if key in payload:
                setattr(conf, key, bool(payload.get(key)))
        conf = conf.filled()
        ok = mail_engine.save_config(Path(self.data_dir) / "mail.json", conf)
        self.mail = mail_engine.Mailbox(conf, logger)
        data = self.mail.status()
        data["saved"] = ok
        if not ok:
            return error_response("写入邮箱配置失败，详细原因见 AstrBot 日志")
        return json_response(data)

    async def _api_mail_inbox(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        try:
            limit = int(payload.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        items, err = await self._mail_call(self.mail.recent, max(1, min(20, limit)))
        return json_response({
            "items": [item.to_dict() for item in items],
            "error": err,
            "status": self.mail.status(),
        })

    async def _api_mail_send(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        content = str(payload.get("body") or "").strip()
        if not content:
            return json_response({"ok": False, "message": "正文不能为空"})
        subject = str(payload.get("subject") or "").strip()
        if not subject:
            subject = content.splitlines()[0][:24] or "来自她"
        target = ""
        name = str(payload.get("to_name") or "").strip()
        if name:
            friend = self.mail.config.friend_named(name)
            if friend is None:
                return json_response({"ok": False, "message": "名单里没有「%s」" % name})
            target = friend.get("address") or ""
        ok, message = await self._mail_call(self.mail.send, subject, content, target)
        return json_response({"ok": ok, "message": message})

    async def _api_mail_write(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        friend = self.mail.config.friend_named(str(payload.get("name") or ""))
        if friend is None:
            return json_response({"ok": False, "message": "名单里没有这个人"})
        result = await self._mail_write_friend(friend)
        data = self.mail.status()
        data["log"] = self._mail_log(12)
        data.update({"ok": bool(result.get("ok")),
                     "message": str(result.get("message") or ""),
                     "letter": {"subject": result.get("subject") or "",
                                "body": result.get("body") or ""}})
        return json_response(data)

    async def _api_mail_test(self) -> Any:
        ok, message = await self._mail_call(
            self.mail.send, "测试邮件", "这是一封测试邮件，说明邮箱配好了。"
        )
        return json_response({"ok": ok, "message": message})

    async def _api_daily(self) -> Any:
        now = time.time()
        info = calendar_engine.day_info(now)
        try:
            from .mind import schedule as _schedule
        except ImportError:  # pragma: no cover
            import mind.schedule as _schedule  # type: ignore[no-redef]
        try:
            slot = _schedule.today(now)
        except Exception:  # noqa: BLE001
            slot = {}
        generated = self._load_generated_schedule()
        if generated:
            try:
                live = schedule_gen.slot_of(generated, now)
                slot = dict(slot)
                slot["activity"] = live.get("activity") or slot.get("activity")
                slot["state"] = live.get("state") or slot.get("state")
                slot["tone"] = live.get("tone") or slot.get("tone", "")
                slot["note"] = generated.get("note") or slot.get("note", "")
            except Exception:  # noqa: BLE001
                pass
        year = time.localtime(now).tm_year
        spans = [
            {"name": name, "start": start.isoformat(), "end": end.isoformat(),
             "days": (end - start).days + 1}
            for start, end, name in sorted(
                list(calendar_engine.blocks(year)) + list(calendar_engine.vacations(year)),
                key=lambda item: item[0],
            )
        ]
        return json_response({
            "day": info.as_dict(),
            "slot": {key: slot.get(key) for key in
                     ("date", "now", "activity", "state", "tone", "note",
                      "kind", "day_index", "day_total")},
            "spans": spans,
        })

    # -- ffmpeg：插件自带优先，缺了自动装 -----------------------------------
    def _ffmpeg_tools(self) -> dict[str, Any]:
        conf = self.storyboard_settings
        return storyboard_engine.find_tools(conf.ffmpeg_path, conf.ffprobe_path)

    def _ffmpeg_status(self) -> dict[str, Any]:
        conf = self.storyboard_settings
        return ffmpeg_setup.status(self.data_dir, conf.ffmpeg_path, self.plugin_dir)

    def _ffmpeg_remember(self, found: dict[str, Any]) -> None:
        """把找到的 ffmpeg 写回设置，下次不用再找。"""
        ffmpeg = str(found.get("ffmpeg") or "")
        if not ffmpeg:
            return
        ffprobe = str(found.get("ffprobe") or "")
        conf = self.storyboard_settings
        if conf.ffmpeg_path == ffmpeg and conf.ffprobe_path == ffprobe:
            return
        conf.ffmpeg_path = ffmpeg
        conf.ffprobe_path = ffprobe
        try:
            storyboard_engine.save_settings(
                Path(self.data_dir) / "storyboard.json", conf
            )
        except OSError as exc:
            logger.debug(f"[ai_mind] 记 ffmpeg 路径失败：{exc}")

    def ensure_ffmpeg(self, auto: bool = True) -> dict[str, Any]:
        """确保能干活：设置 → 插件自带 → data/bin → PATH。

        auto=True 时才联网补装（只有面板「一键内置」会这么干；命令路径 auto=False，
        避免用户随口一句 #分镜 卡在几十 MB 的下载上）。
        同步函数，调用方自己扔线程（面板接口走 _mail_call，命令走 to_thread）。
        """
        found = ffmpeg_setup.locate(
            self.data_dir, self.storyboard_settings.ffmpeg_path, self.plugin_dir
        )
        if found.get("ok"):
            self._ffmpeg_remember(found)
            return {"ok": True, "installed": False, "source": str(found.get("source") or ""),
                    "path": str(found.get("ffmpeg") or ""), "message": ""}
        if not auto:
            return {"ok": False, "installed": False, "source": "", "path": "",
                    "message": "没找到 ffmpeg"}
        if not self._ffmpeg_lock.acquire(blocking=False):
            return {"ok": False, "installed": False, "source": "", "path": "",
                    "message": "正在装 ffmpeg，装完再试一次"}
        try:
            again = ffmpeg_setup.locate(
                self.data_dir, self.storyboard_settings.ffmpeg_path, self.plugin_dir
            )
            if again.get("ok"):
                self._ffmpeg_remember(again)
                return {"ok": True, "installed": False,
                        "source": str(again.get("source") or ""),
                        "path": str(again.get("ffmpeg") or ""), "message": ""}
            logger.info("[ai_mind] 没找到 ffmpeg，开始自动装（插件目录）")
            result = ffmpeg_setup.install(self.data_dir)
            if result.get("ok"):
                self._ffmpeg_remember(result)
            return {"ok": bool(result.get("ok")), "installed": True,
                    "source": "bundled" if result.get("ok") else "",
                    "path": str(result.get("ffmpeg") or ""),
                    "message": str(result.get("message") or "")}
        finally:
            self._ffmpeg_lock.release()

    # -- 面板：视频分镜 -----------------------------------------------------
    def _storyboard_payload(self) -> dict[str, Any]:
        conf = self.storyboard_settings
        report = storyboard_engine.selfcheck(conf.ffmpeg_path, conf.ffprobe_path)
        return {
            "settings": conf.to_dict(),
            "delivery": self.delivery_settings.to_dict(),
            "selfcheck": report,
            "ffmpeg": self._ffmpeg_status(),
            "needs": [
                {"key": key, "label": tier["label"], "w": tier["w"], "h": tier["h"],
                 "per_sheet": storyboard_engine.max_cells_for_need(key)}
                for key, tier in storyboard_engine.NEED_TIERS.items()
            ],
        }

    async def _api_storyboard(self) -> Any:
        return json_response(self._storyboard_payload())

    async def _api_storyboard_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.storyboard_settings
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        if payload.get("need") is not None:
            need = str(payload.get("need") or "").strip().lower()
            if need in storyboard_engine.NEED_TIERS:
                conf.need = need
        if payload.get("ffmpeg_path") is not None:
            conf.ffmpeg_path = str(payload.get("ffmpeg_path") or "").strip()
        if payload.get("ffprobe_path") is not None:
            conf.ffprobe_path = str(payload.get("ffprobe_path") or "").strip()
        if payload.get("max_download_mb") is not None:
            try:
                conf.max_download_mb = max(1, min(2048, int(payload["max_download_mb"])))
            except (TypeError, ValueError):
                pass
        if "bulk_to_private" in payload:
            self.delivery_settings.bulk_to_private = bool(payload.get("bulk_to_private"))
            self._delivery_save()
        ok = storyboard_engine.save_settings(
            Path(self.data_dir) / "storyboard.json", conf
        )
        data = self._storyboard_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入分镜设置失败，详细原因见 AstrBot 日志")
        return json_response(data)

    async def _api_storyboard_install_ffmpeg(self) -> Any:
        """一键把 ffmpeg 装进插件目录（data/bin），装完写进设置。"""
        result = await self._mail_call(self.ensure_ffmpeg, True)
        data = self._storyboard_payload()
        data["ok"] = bool(result.get("ok"))
        data["message"] = str(result.get("message") or "")
        data["source"] = str(result.get("source") or "")
        return json_response(data)

    async def _api_storyboard_build(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        source = str(payload.get("path") or "").strip()
        if not source:
            return json_response({"ok": False, "error": "先填一个视频路径或链接"})
        result = await self._mail_call(self._storyboard_build, source)
        images: list[dict[str, Any]] = []
        for sheet in result.get("sheets") or []:
            path = Path(str(sheet.get("image") or ""))
            if not path.is_file():
                continue
            try:
                blob = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            images.append({
                "i": int(sheet.get("i") or 0),
                "from": sheet.get("from"),
                "to": sheet.get("to"),
                "cells": len(sheet.get("cells") or []),
                "image": "data:image/png;base64," + blob,
            })
        return json_response({
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "hint": result.get("hint", ""),
            "summary": result.get("summary", ""),
            "manifest": result.get("manifest", ""),
            "images": images,
            "settings": self.storyboard_settings.to_dict(),
        })

    # -- 面板：刷 B 站 ------------------------------------------------------
    @staticmethod
    def _images_payload(paths) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(paths or [], 1):
            path = Path(str(item))
            if not path.is_file():
                continue
            try:
                blob = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            rows.append({"i": index, "image": "data:image/png;base64," + blob})
        return rows

    def _bili_state_file(self) -> Path:
        return Path(self.data_dir) / "bili" / "last_run.json"

    def _bili_save_last(self, payload: dict[str, Any]) -> None:
        """把这一轮结果落盘：面板关掉、插件重启后「今天挑的/日报」还在。"""
        try:
            folder = Path(self.data_dir) / "bili"
            folder.mkdir(parents=True, exist_ok=True)
            blob = dict(payload)
            blob.pop("images", None)    # base64 太大，重新打开时按 image_paths 重建
            blob["saved_at"] = time.time()
            self._bili_state_file().write_text(
                json.dumps(blob, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.debug(f"[ai_mind] 记刷站结果失败：{exc}")

    def _bili_last_result(self) -> dict[str, Any]:
        try:
            data = json.loads(self._bili_state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        data.pop("images", None)
        data["image_count"] = len(data.get("image_paths") or [])
        return data

    def _bili_payload(self) -> dict[str, Any]:
        return {
            "settings": self.bili_settings.masked(),
            "delivery": self.delivery_settings.to_dict(),
            "seen": self.bili_seen.count(),
            "storyboard": self.storyboard_settings.to_dict(),
            "has_ffmpeg": bool(self._ffmpeg_tools().get("ok")),
            "last": self._bili_last_result(),
            "regions": [
                {"rid": rid, "name": name}
                for rid, name in bili_engine.DISCOVER_RIDS.items()
            ],
        }

    async def _api_bili(self) -> Any:
        return json_response(self._bili_payload())

    async def _api_bili_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.bili_settings
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        if payload.get("keywords") is not None:
            conf.keywords = str(payload.get("keywords") or "").strip()
        if str(payload.get("cookie") or "").strip():
            conf.cookie = bili_engine.normalize_cookie(str(payload.get("cookie")))
        if payload.get("clear_cookie"):
            conf.cookie = ""
        if "prefer_hq" in payload:
            conf.prefer_hq = bool(payload.get("prefer_hq"))
        if "use_history" in payload:
            conf.use_history = bool(payload.get("use_history"))
        if "daily_by_schedule" in payload:
            conf.daily_by_schedule = bool(payload.get("daily_by_schedule"))
        if payload.get("daily_gap_hours") is not None:
            try:
                conf.daily_gap_hours = max(1.0, min(24.0, float(payload.get("daily_gap_hours"))))
            except (TypeError, ValueError):
                pass
        merged = conf.to_dict()
        for key in ("candidates", "storyboard_n", "min_seconds", "max_seconds", "max_download_mb"):
            if payload.get(key) is not None:
                merged[key] = payload.get(key)
        if isinstance(payload.get("rids"), list):
            merged["rids"] = payload.get("rids")
        conf = bili_engine.BiliSettings.from_dict(merged)
        self.bili_settings = conf
        if "bulk_to_private" in payload:
            self.delivery_settings.bulk_to_private = bool(payload.get("bulk_to_private"))
            self._delivery_save()
        ok = bili_engine.save_settings(Path(self.data_dir) / "bili.json", conf)
        data = self._bili_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入刷B站设置失败，详细原因见 AstrBot 日志")
        return json_response(data)

    def _bili_payload_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        # 分镜图不走这里：base64 一张就有几百 KB 到几 MB，塞进 JSON 会把
        # 面板请求拖到超时（前端只看到 Network Error）。这里只给路径，
        # 真要看图时再一张一张取（bili/image，取之前先缩到 720 宽）。
        image_paths = [
            {"path": str(path), "bvid": sheet.get("bvid"),
             "title": sheet.get("title")}
            for sheet in (result.get("sheets") or [])
            for path in (sheet.get("images") or [])
        ]
        return {
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "day": result.get("day", ""),
            "report": result.get("report", ""),
            "items": [
                {key: item.get(key) for key in
                 ("bvid", "title", "up", "duration", "view", "url", "score",
                  "reasons", "storyboard", "subtitle")}
                for item in (result.get("items") or [])
            ],
            "image_paths": image_paths,
            "image_count": len(image_paths),
            "errors": result.get("errors") or [],
            "seen": self.bili_seen.count(),
        }

    async def _run_bili_job(self, job: job_engine.Job, limit: int) -> None:
        """后台跑一轮：刷站 → 每条一句话。结果塞回 job。"""
        try:
            result = await asyncio.to_thread(self._bili_scroll, limit)
            result = await self._bili_comments(result)
            payload = self._bili_payload_from_result(result)
            self._bili_save_last(payload)
            job.done(payload)
        except Exception as exc:  # noqa: BLE001 - 后台任务失败要说给前端
            logger.warning(f"[ai_mind] 后台刷站失败：{exc}")
            job.fail(str(exc))

    async def _api_bili_scroll_start(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        job = self.jobs.create("刷一轮")
        task = asyncio.create_task(self._run_bili_job(job, limit))
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)
        return json_response({"ok": True, "job": job.id, "state": job.snapshot(with_result=False)})

    async def _api_bili_scroll_state(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload = body if isinstance(body, dict) else dict(params or {})
        job = self.jobs.get(str(payload.get("job") or ""))
        if job is None:
            return json_response({"ok": False, "error": "任务不在了（可能重启过）"})
        snap = job.snapshot(with_result=True)
        snap["ok"] = job.status != "error"
        return json_response(snap)

    async def _api_bili_scroll(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        result = await self._mail_call(self._bili_scroll, limit)
        result = await self._bili_comments(result)
        payload = self._bili_payload_from_result(result)
        self._bili_save_last(payload)
        return json_response(payload)

    def _bili_one_storyboard(self, bvid: str) -> dict[str, Any]:
        """单条视频的分镜（面板上那个按钮）。"""
        conf = self.bili_settings
        detail, err = bili_engine.fetch_view(bvid, cookie=conf.cookie)
        if err or not detail:
            return {"ok": False, "error": err or "没有这条视频"}
        url, _size, err = bili_engine.pick_stream(
            bvid, int(detail.get("cid") or 0), cookie=conf.cookie, prefer_hq=conf.prefer_hq,
        )
        if err or not url:
            return {"ok": False, "error": err or "没有可下载的直链"}
        workdir = Path(self.data_dir) / "bili"
        workdir.mkdir(parents=True, exist_ok=True)
        local = workdir / (bvid + ".mp4")
        ok, err = bili_engine.download(url, local, conf.max_download_mb * 1024 * 1024)
        if not ok:
            return {"ok": False, "error": err}
        result = self._storyboard_build(str(local))
        result["bvid"] = bvid
        result["title"] = detail.get("title", "")
        return result

    async def _api_bili_storyboard(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        bvid = str(payload.get("bvid") or "").strip()
        if not bvid:
            return json_response({"ok": False, "error": "先给一个 BV 号"})
        result = await self._mail_call(self._bili_one_storyboard, bvid)
        images: list[dict[str, Any]] = []
        for sheet in result.get("sheets") or []:
            for row in self._images_payload([sheet.get("image")]):
                row["from"] = sheet.get("from")
                row["to"] = sheet.get("to")
                images.append(row)
        return json_response({
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "hint": result.get("hint", ""),
            "bvid": bvid,
            "title": result.get("title", ""),
            "summary": result.get("summary", ""),
            "manifest": result.get("manifest", ""),
            "images": images,
        })

    async def _debug_history(self, event: AstrMessageEvent, text: str) -> str:
        conf = self.bili_settings
        if not conf.cookie:
            return "还没登录 B 站：面板「刷B站」页扫码或粘 Cookie，然后再发 #在看。"
        parts = text.lstrip("#").strip().split()
        limit = 10
        if len(parts) > 1 and parts[1].isdigit():
            limit = max(1, min(20, int(parts[1])))
        rows, err = await asyncio.to_thread(
            bili_engine.fetch_history, conf.cookie, limit
        )
        if err:
            return "看不了历史：" + err
        if not rows:
            return "最近没有观看记录。"
        lines = ["我最近在看（%d 条）：" % len(rows)]
        for item in rows:
            progress = int(item.get("progress") or 0)
            duration = max(1, int(item.get("duration") or 0))
            percent = min(100, round(progress * 100.0 / duration)) if duration else 0
            lines.append("· %s（%s）%s" % (
                str(item.get("title") or "")[:36],
                str(item.get("up") or "?"),
                ("看到 %d%%" % percent) if duration else "",
            ))
        return chr(10).join(lines)

    async def _api_bili_history(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        try:
            limit = int(payload.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        conf = self.bili_settings
        if not conf.cookie:
            return json_response({"ok": False, "error": "还没登录 B 站", "items": []})
        rows, err = await self._mail_call(bili_engine.fetch_history, conf.cookie, 12)
        return json_response({
            "ok": not err,
            "error": err,
            "items": [
                {
                    "bvid": item.get("bvid"), "title": item.get("title"),
                    "up": item.get("up"), "duration": item.get("duration"),
                    "progress": item.get("progress"), "url": item.get("url"),
                }
                for item in rows[: max(1, min(20, limit))]
            ],
        })

    def _bili_image(self, raw_path: str) -> dict[str, Any]:
        """取一张分镜图：只允许 data/bili 下的文件，先缩到 720 宽再回。"""
        root = (Path(self.data_dir) / "bili").resolve()
        try:
            target = Path(str(raw_path or "")).resolve()
        except OSError:
            return {"ok": False, "error": "路径不对"}
        if target != root and root not in target.parents:
            return {"ok": False, "error": "只给空间自己的分镜图"}
        if not target.is_file():
            return {"ok": False, "error": "这张图不在了（可能被清过）"}
        thumb = root / "thumbs" / (target.stem + "-720.png")
        if not thumb.is_file():
            tools = storyboard_engine.find_tools(
                self.storyboard_settings.ffmpeg_path,
                self.storyboard_settings.ffprobe_path,
            )
            if tools.get("ok"):
                try:
                    thumb.parent.mkdir(parents=True, exist_ok=True)
                    storyboard_engine._run([
                        str(tools["ffmpeg"]), "-y", "-i", str(target),
                        "-vf", "scale=720:-1", str(thumb),
                    ], timeout=60)
                except Exception:  # noqa: BLE001 - 缩不了就回原图
                    thumb = target
            else:
                thumb = target
        src = thumb if thumb.is_file() else target
        try:
            blob = base64.b64encode(src.read_bytes()).decode("ascii")
        except OSError:
            return {"ok": False, "error": "这张图读不出来"}
        return {"ok": True, "image": "data:image/png;base64," + blob,
                "path": str(target)}

    async def _api_bili_image(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        result = await self._mail_call(self._bili_image, str(payload.get("path") or ""))
        return json_response(result)

    async def _api_bili_login(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        raw = str(payload.get("cookie") or "").strip() or self.bili_settings.cookie
        report = await self._mail_call(bili_engine.login_check, raw)
        report["has_cookie"] = bool(self.bili_settings.cookie)
        return json_response(report)

    async def _api_bili_qr_start(self) -> Any:
        report = await self._mail_call(bili_engine.qr_generate)
        report["has_cookie"] = bool(self.bili_settings.cookie)
        return json_response(report)

    async def _api_bili_qr_poll(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        key = str(payload.get("key") or "").strip()
        if not key:
            return json_response({"status": "error", "message": "缺 key", "cookie": ""})
        result = await self._mail_call(bili_engine.qr_poll, key)
        saved = False
        login = {}
        if result.get("status") == "success":
            jar = bili_engine.normalize_cookie(str(result.get("cookie") or ""))
            if jar:
                self.bili_settings.cookie = jar
                bili_engine.save_settings(Path(self.data_dir) / "bili.json", self.bili_settings)
                saved = True
                try:
                    login = await self._mail_call(bili_engine.login_check, jar)
                except Exception:  # noqa: BLE001
                    login = {}
        result["saved"] = saved
        result["login"] = login
        result["has_cookie"] = bool(self.bili_settings.cookie)
        return json_response(result)

    async def _api_bili_clear(self) -> Any:
        removed = self.bili_seen.clear()
        data = self._bili_payload()
        data["removed"] = removed
        return json_response(data)

    # -- 面板：声聊 ---------------------------------------------------------
    async def _api_voice(self) -> Any:
        return json_response({"settings": self.voice_settings.to_dict()})

    async def _api_voice_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        merged = dict(self.voice_settings.to_dict())
        for key in ("enabled", "mode", "chance", "character", "groups",
                    "max_chars", "cooldown"):
            if payload.get(key) is not None:
                merged[key] = payload[key]
        conf = voice_engine.VoiceSettings.from_dict(merged)
        self.voice_settings = conf
        ok = self._voice_save()
        data = {"settings": conf.to_dict(), "saved": ok}
        if not ok:
            return error_response("写入声聊设置失败，详细原因见日志")
        return json_response(data)

    async def _api_voice_characters(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload = body if isinstance(body, dict) else dict(params or {})
        group = str(payload.get("group") or "").strip()
        if not group:
            return json_response({"ok": False, "error": "要先给一个群号", "characters": []})
        fake = type("_E", (), {})()
        setattr(fake, "get_group_id", lambda: group)
        rows = await self._voice_characters(fake, group)
        return json_response({"ok": bool(rows), "characters": rows,
                              "error": "" if rows else "拿不到音色（平台可能不支持）"})

    # -- 面板：戳一戳 -------------------------------------------------------
    async def _api_poke(self) -> Any:
        return json_response({"settings": self.poke_settings.to_dict()})

    async def _api_poke_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        merged = dict(self.poke_settings.to_dict())
        for key in ("enabled", "in_groups", "in_private", "user_cooldown",
                    "group_cooldown", "blacklist", "prompt"):
            if payload.get(key) is not None:
                merged[key] = payload[key]
        conf = poke_engine.PokeSettings.from_dict(merged)
        self.poke_settings = conf
        self.poke_gate = poke_engine.PokeGate(conf)
        ok = poke_engine.save_settings(Path(self.data_dir) / "poke.json", conf)
        data = {"settings": conf.to_dict(), "saved": ok}
        if not ok:
            return error_response("写入戳一戳设置失败，详细原因见日志")
        return json_response(data)

    # -- 面板：表情包 -------------------------------------------------------
    def _sticker_payload(self, limit: int = 60) -> dict[str, Any]:
        rows = self.stickers.list(limit=limit)
        items: list[dict[str, Any]] = []
        for item in rows:
            path = Path(item.path)
            if not path.is_file():
                continue
            try:
                blob = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            items.append({
                "id": item.id,
                "desc": item.desc,
                "emotions": list(item.emotions),
                "hits": item.hits,
                "name": path.name,
                "image": "data:image/png;base64," + blob,
            })
        return {
            "settings": self.sticker_settings.to_dict(),
            "stats": self.stickers.stats(),
            "items": items,
            "emotions": sorted(sticker_engine.EMOTION_WORDS.keys()),
        }

    async def _api_sticker(self) -> Any:
        return json_response(self._sticker_payload())

    async def _api_sticker_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        merged = dict(self.sticker_settings.to_dict())
        for key in ("enabled", "collect", "chance", "per_hour", "max_bytes"):
            if payload.get(key) is not None:
                merged[key] = payload[key]
        self.sticker_settings = sticker_engine.StickerSettings.from_dict(merged)
        ok = self._sticker_save()
        data = self._sticker_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入表情设置失败，详细原因见日志")
        return json_response(data)

    async def _api_sticker_tag(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        try:
            sticker_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sticker_id = 0
        if sticker_id <= 0:
            return error_response("缺表情 id")
        desc = payload.get("desc")
        emotions = payload.get("emotions")
        ok = self.stickers.tag(
            sticker_id,
            desc=None if desc is None else str(desc),
            emotions=None if emotions is None else [
                str(item) for item in (emotions if isinstance(emotions, list) else [])
            ],
        )
        data = self._sticker_payload()
        data["tagged"] = ok
        return json_response(data)

    async def _api_sticker_delete(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        try:
            sticker_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sticker_id = 0
        ok = self.stickers.delete(sticker_id) if sticker_id > 0 else False
        data = self._sticker_payload()
        data["deleted"] = ok
        return json_response(data)

    async def _api_sticker_clear(self) -> Any:
        removed = self.stickers.clear()
        data = self._sticker_payload()
        data["removed"] = removed
        return json_response(data)

    # -- 面板：市场 ---------------------------------------------------------
    def _market_payload(self) -> dict[str, Any]:
        return {
            "settings": self.market_settings.to_dict(),
            "history": list(self.market_history.rows[:10]),
        }

    async def _api_market(self) -> Any:
        return json_response(self._market_payload())

    async def _api_market_search(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        query = str(payload.get("query") or "").strip()
        if not query:
            return json_response({"ok": False, "error": "先给一个能力词", "hits": []})
        words = market_engine.keywords_of(query) or [query]
        term = " ".join(words[:3])
        hits, err = await self._mail_call(self._market_search, term)
        if not err:
            self.market_history.add(term, hits)
            market_engine.save_history(Path(self.data_dir) / "market_history.json",
                                       self.market_history)
        return json_response({"ok": not err, "error": err, "query": term,
                              "hits": hits, "history": list(self.market_history.rows[:10])})

    async def _api_market_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.market_settings
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        if "auto" in payload:
            conf.auto = bool(payload.get("auto"))
        if "owner_only" in payload:
            conf.owner_only = bool(payload.get("owner_only"))
        if payload.get("base_url") is not None:
            conf.base_url = str(payload.get("base_url") or "").strip() or conf.base_url
        if payload.get("limit") is not None:
            try:
                conf.limit = max(1, min(20, int(payload["limit"])))
            except (TypeError, ValueError):
                pass
        ok = market_engine.save_settings(Path(self.data_dir) / "market.json", conf)
        data = self._market_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入市场设置失败，详细原因见日志")
        return json_response(data)

    # -- 面板：生图 ---------------------------------------------------------
    def _imagegen_payload(self) -> dict[str, Any]:
        conf = self.imagegen_settings
        folder = Path(self.data_dir) / "imagegen" / "out"
        recent: list[dict[str, Any]] = []
        if folder.is_dir():
            files = sorted(
                (item for item in folder.iterdir() if item.suffix.lower() in (".png", ".jpg")),
                key=lambda item: item.stat().st_mtime, reverse=True,
            )[:12]
            for item in files:
                try:
                    blob = base64.b64encode(item.read_bytes()).decode("ascii")
                except OSError:
                    continue
                recent.append({"name": item.name, "image": "data:image/png;base64," + blob})
        return {
            "settings": conf.masked(),
            "adapters": [
                {"key": key, "label": value.get("label", key)}
                for key, value in imagegen_engine.ADAPTERS.items()
            ],
            "recent": recent,
        }

    async def _api_imagegen(self) -> Any:
        return json_response(self._imagegen_payload())

    async def _api_imagegen_save(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        conf = self.imagegen_settings
        if "enabled" in payload:
            conf.enabled = bool(payload.get("enabled"))
        for key in ("base_url", "model", "adapter", "size", "quality"):
            if payload.get(key) is not None:
                setattr(conf, key, str(payload.get(key) or "").strip())
        if str(payload.get("api_key") or "").strip():
            conf.api_key = str(payload.get("api_key")).strip()
        if payload.get("clear_key"):
            conf.api_key = ""
        for key, low, high in (("count", 1, 4), ("timeout", 10, 600)):
            if payload.get(key) is not None:
                try:
                    setattr(conf, key, max(low, min(high, int(payload[key]))))
                except (TypeError, ValueError):
                    pass
        ok = imagegen_engine.save_settings(Path(self.data_dir) / "imagegen.json", conf)
        data = self._imagegen_payload()
        data["saved"] = ok
        if not ok:
            return error_response("写入生图配置失败，详细原因见日志")
        return json_response(data)

    async def _api_imagegen_generate(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return json_response({"ok": False, "error": "先说画什么"})
        result = await self._mail_call(
            imagegen_engine.generate, prompt, self.imagegen_settings,
            self._imagegen_out(), str(payload.get("size") or self.imagegen_settings.size),
        )
        result["images_b64"] = self._images_payload(result.get("images") or [])
        result["text"] = imagegen_engine.report(result)
        return json_response(result)

    async def _api_imagegen_edit(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        prompt = str(payload.get("prompt") or "").strip()
        refs = payload.get("refs")
        paths = [str(item) for item in (refs if isinstance(refs, list) else []) if item]
        if not prompt or not paths:
            return json_response({"ok": False, "error": "要说明改什么，并给一张参考图"})
        result = await self._mail_call(
            imagegen_engine.edit, prompt, self.imagegen_settings,
            self._imagegen_out(), paths, str(payload.get("size") or self.imagegen_settings.size),
        )
        result["images_b64"] = self._images_payload(result.get("images") or [])
        result["text"] = imagegen_engine.report(result)
        return json_response(result)

    # -- 面板：知识树 / 动图 / 听音频 ---------------------------------------
    def _knowledge_payload(self, session: str = "") -> dict[str, Any]:
        kb = self.knowledge
        session = str(session or "")
        active = set(kb.active.get(session) or [])
        return {
            "session": session,
            "stats": knowledge_engine.summarize(kb),
            "trees": [
                {
                    "id": tree.id, "name": tree.name, "nodes": len(tree.nodes),
                    "active": tree.id in active,
                    "updated": tree.updated,
                    "outline": knowledge_engine.render_tree(tree, 1200),
                }
                for tree in kb.trees
            ],
            "pending": [
                {
                    "id": edit.id,
                    "tree": (kb.tree(edit.tree_id).name if kb.tree(edit.tree_id) else "?"),
                    "node": (kb.tree(edit.tree_id).get(edit.node_id).title
                             if kb.tree(edit.tree_id) and kb.tree(edit.tree_id).get(edit.node_id)
                             else edit.node_id),
                    "title": edit.title,
                    "content": edit.content,
                    "note": edit.note,
                    "created": edit.created,
                }
                for edit in kb.pending
            ],
        }

    async def _api_knowledge(self) -> Any:
        params = await self._params()
        return json_response(self._knowledge_payload(str((params or {}).get("session") or "")))

    async def _api_knowledge_tree(self) -> Any:
        body = await self._read_body()
        params = await self._params()
        payload = body if isinstance(body, dict) else dict(params or {})
        kb = self.knowledge
        action = str(payload.get("action") or "create").strip().lower()
        if action == "create":
            name = str(payload.get("name") or "").strip()
            if not name:
                return error_response("树名不能为空")
            if kb.by_name(name) is not None:
                return error_response("已经有这棵树了")
            kb.add_tree(name)
        elif action == "add":
            tree = kb.by_name(str(payload.get("tree") or ""))
            if tree is None:
                return error_response("没有这棵树")
            node = kb.add_node(
                tree, str(payload.get("title") or ""), str(payload.get("content") or ""),
                parent=str(payload.get("parent") or ""),
            )
            if node is None:
                return error_response("这棵树满了")
        elif action == "activate":
            tree = kb.by_name(str(payload.get("tree") or ""))
            session = str(payload.get("session") or "")
            if tree is None or not session:
                return error_response("缺树或会话")
            kb.activate(session, tree.id)
        elif action == "deactivate":
            tree = kb.by_name(str(payload.get("tree") or ""))
            session = str(payload.get("session") or "")
            if tree is None or not session:
                return error_response("缺树或会话")
            kb.deactivate(session, tree.id)
        else:
            return error_response("不认识的动作：" + action)
        if not self._knowledge_save():
            return error_response("写 knowledge.json 失败，详细原因见日志")
        return json_response(self._knowledge_payload(str(payload.get("session") or "")))

    async def _api_knowledge_active(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        tree = self.knowledge.by_name(str(payload.get("tree") or ""))
        session = str(payload.get("session") or "")
        if tree is None or not session:
            return error_response("缺树或会话")
        if bool(payload.get("on")):
            self.knowledge.activate(session, tree.id)
        else:
            self.knowledge.deactivate(session, tree.id)
        if not self._knowledge_save():
            return error_response("写 knowledge.json 失败")
        return json_response(self._knowledge_payload(session))

    async def _api_knowledge_review(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        edit_id = str(payload.get("id") or "")
        approve = bool(payload.get("approve"))
        ok = (self.knowledge.approve(edit_id) if approve else self.knowledge.reject(edit_id))
        self._knowledge_save()
        data = self._knowledge_payload(str(payload.get("session") or ""))
        data["done"] = ok
        return json_response(data)

    async def _api_animation(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        path = str(payload.get("path") or "").strip()
        if not path:
            return json_response({"ok": False, "error": "先填一个动图路径"})
        await self._mail_call(self.ensure_ffmpeg, False)
        conf = self.storyboard_settings
        result = await self._mail_call(
            animation_engine.build, path, animation_engine.DEFAULT_FRAMES, None,
            conf.annotate, conf.font_path, conf.ffmpeg_path, conf.ffprobe_path,
        )
        images = self._images_payload([result.get("image")]) if result.get("ok") else []
        return json_response({
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "summary": result.get("summary", ""),
            "manifest": result.get("manifest", ""),
            "images": images,
        })

    async def _api_soundcheck(self) -> Any:
        body = await self._read_body()
        payload = body if isinstance(body, dict) else {}
        path = str(payload.get("path") or "").strip()
        if not path:
            return json_response({"ok": False, "error": "先填一个音频路径"})
        await self._mail_call(self.ensure_ffmpeg, False)
        conf = self.storyboard_settings
        result = await self._mail_call(
            soundcheck_engine.analyze, path, conf.ffmpeg_path, conf.ffprobe_path
        )
        images: list[dict[str, Any]] = []
        if result.get("ok"):
            try:
                workdir = Path(self.data_dir) / "soundcheck"
                workdir.mkdir(parents=True, exist_ok=True)
                out = workdir / "spectrum.png"
                if soundcheck_engine.spectrum(
                    path, out, ffmpeg_hint=conf.ffmpeg_path,
                    ffprobe_hint=conf.ffprobe_path,
                ):
                    images = self._images_payload([out])
            except OSError:
                images = []
        result["images"] = images
        result["text"] = soundcheck_engine.report(result)
        return json_response(result)

    async def _api_style(self) -> Any:
        params = await self._params()
        return json_response(self._style_payload(
            query=str(params.get("q") or ""),
            status=str(params.get("status") or ""),
        ))

    async def _api_style_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        raw = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        known = set(StyleSettings().to_dict().keys())
        incoming = {
            str(key): value for key, value in (raw or {}).items()
            if str(key) in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的设置")
        merged = dict(self._style_data)
        merged.update(incoming)
        self._style_data = merged
        if not self._save_simple_overrides("style_overrides.json", merged, "表达示例"):
            return error_response("写入表达示例设置失败，详细原因见 AstrBot 日志")
        self._reload_from_config()
        result = self._style_payload()
        result["saved"] = sorted(incoming.keys())
        return json_response(result)

    async def _api_style_review(self) -> Any:
        """批准 / 拒绝 / 删除：这就是审查队列。"""
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        ids = _coerce_int_list(payload.get("ids") or payload.get("id"))
        action = str(payload.get("action") or "").strip().lower()
        if not ids:
            return error_response("没有选中任何示例")
        if action in {"approve", "批准"}:
            affected = self.styles.set_status(ids, style_engine.STATUS_APPROVED)
        elif action in {"reject", "拒绝"}:
            affected = self.styles.set_status(ids, style_engine.STATUS_REJECTED)
        elif action in {"pending", "撤销", "回滚"}:
            affected = self.styles.set_status(ids, style_engine.STATUS_PENDING)
        elif action in {"delete", "删除"}:
            affected = self.styles.delete(ids)
        else:
            return error_response("不认识的操作：" + (action or "（空）"))
        logger.info(f"[ai_mind] 表达示例 {action}：{affected} 条")
        result = self._style_payload()
        result["affected"] = affected
        result["action"] = action
        return json_response(result)

    async def _api_style_add(self) -> Any:
        """手动加一条示例（自己指定"以后遇到这种话就这么回"）。"""
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        user_text = str(payload.get("user") or "").strip()
        reply_text = str(payload.get("reply") or "").strip()
        if len(user_text) < 2 or len(reply_text) < 2:
            return error_response("「他会说什么」和「你回什么」都要填")
        try:
            report = antiai.analyse(reply_text, **self._humanize_kwargs())
            score = report.score
        except Exception:  # noqa: BLE001
            score = 100.0
        new_id = self.styles.add(StyleExample(
            user_text=user_text[:200],
            reply_text=reply_text[:400],
            created_at=time.time(),
            session_id="",
            scene=str(payload.get("scene") or style_engine.SCENE_PRIVATE),
            keywords=tuple(style_engine.keywords_of(user_text)),
            grams=tuple(style_engine.ngrams(user_text)),
            score=score,
            status=style_engine.STATUS_APPROVED,
            source=style_engine.SOURCE_MANUAL,
            note="手动添加",
        ))
        if not new_id:
            return error_response("这条已经存在了")
        result = self._style_payload()
        result["added"] = new_id
        return json_response(result)

    async def _api_style_clear(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        only_auto = str(payload.get("only_auto") or "").lower() in {"1", "true", "yes", "on"}
        status = str(payload.get("status") or "")
        removed = self.styles.clear(only_auto=only_auto, status=status)
        logger.info(f"[ai_mind] 清空表达示例：{removed} 条（only_auto={only_auto}）")
        result = self._style_payload()
        result["removed"] = removed
        return json_response(result)

    def _style_maintenance(self) -> None:
        """顺手清理：太老的待审示例没人管就丢掉。"""
        settings = self.settings.style
        days = float(settings.pending_ttl_days or 0)
        if days <= 0 or self.styles.conn is None:
            return
        cutoff = time.time() - days * 86400.0
        try:
            cursor = self.styles.conn.execute(
                "DELETE FROM style_examples WHERE status = ? AND created_at < ?",
                (style_engine.STATUS_PENDING, cutoff),
            )
            self.styles.conn.commit()
            if cursor.rowcount:
                logger.info(f"[ai_mind] 清理了 {cursor.rowcount} 条过期待审示例")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 提示词：注入给模型的原话，面板上直接改
    # ------------------------------------------------------------------
    async def _api_prompts(self) -> Any:
        return json_response(self.prompts.payload())

    async def _api_prompts_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params
        raw = payload.get("blocks") if isinstance(payload.get("blocks"), dict) else payload
        if not isinstance(raw, dict) or not raw:
            return error_response("没有收到任何要保存的提示词")
        merged = dict(self.prompts.overrides)
        touched = 0
        for key, value in raw.items():
            if key not in prompt_engine.BLOCK_BY_KEY:
                continue
            text = str(value or "")
            touched += 1
            # 空的和等于默认值的都不存，文件干净、还原也干净
            if not text.strip() or text == prompt_engine.default_of(key):
                merged.pop(key, None)
            else:
                merged[key] = text
        if not touched:
            return error_response("这些提示词块不认识")
        if not self.prompts.save(merged):
            return error_response("写入提示词失败，详细原因见 AstrBot 日志")
        logger.info(f"[ai_mind] 提示词已更新，当前有 {len(self.prompts.overrides)} 块被改过")
        result = self.prompts.payload()
        result["saved"] = touched
        return json_response(result)

    async def _api_prompts_reset(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        keys = None
        for source in (body, params):
            if isinstance(source, dict) and source.get("keys"):
                keys = [str(item) for item in source.get("keys")]
                break
        if not self.prompts.reset(keys):
            return error_response("还原失败，详细原因见 AstrBot 日志")
        result = self.prompts.payload()
        result["reset"] = True
        return json_response(result)

    async def _api_prompts_preview(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        key = ""
        text = ""
        for source in (body, params):
            if isinstance(source, dict):
                key = key or str(source.get("key") or "")
                text = text or str(source.get("text") or "")
        if not key or key not in prompt_engine.BLOCK_BY_KEY:
            return error_response("不认识这个提示词块")
        template = text if text.strip() else self.prompts.get(key)
        rendered = prompt_engine.render(template, prompt_engine.SAMPLE_VALUES)
        return json_response({
            "ok": True,
            "key": key,
            "rendered": rendered,
            "unknown": prompt_engine.check_placeholders(key, template),
        })

    async def _api_humanize(self) -> Any:
        params = await self._params()
        sample = params.get("sample")
        return json_response(self._humanize_payload(sample if isinstance(sample, str) else ""))

    async def _api_humanize_test(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        text = ""
        if isinstance(body, dict):
            text = str(body.get("text") or "")
        if not text:
            text = str(params.get("text") or "")
        report = antiai.analyse(text, **self._humanize_kwargs())
        return json_response({
            "ok": True,
            "report": report.to_dict(),
            "hint": antiai.build_rewrite_prompt(report, text) if report.issues else "",
        })

    async def _api_humanize_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params
        raw = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        known = set(HumanizeSettings().to_dict().keys())
        incoming = {
            str(key): value for key, value in (raw or {}).items()
            if str(key) in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的设置")
        merged = dict(self._humanize_data)
        merged.update(incoming)
        self._humanize_data = merged
        if not self._save_humanize_overrides(merged):
            return error_response("写入去 AI 味设置失败，详细原因见 AstrBot 日志")
        self._reload_from_config()
        result = self._humanize_payload()
        result["saved"] = sorted(incoming.keys())
        return json_response(result)

    async def _api_humanize_reset(self) -> Any:
        self._humanize_data = {}
        path = self._humanize_file()
        try:
            if path.is_file():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除去 AI 味覆盖配置失败：{exc}")
        self._reload_from_config()
        result = self._humanize_payload()
        result["reset"] = True
        return json_response(result)

    async def _api_humanize_clear(self) -> Any:
        removed = self.humanize_log.clear()
        return json_response({"ok": True, "removed": removed})
    # ------------------------------------------------------------------
    # 全部功能设置：把配置直接搬到面板上改
    # ------------------------------------------------------------------
    def _settings_schema(self) -> dict[str, Any]:
        """读插件自带的 _conf_schema.json（缓存住，不用每次读盘）。"""
        if self._schema_cache is not None:
            return self._schema_cache
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        try:
            self._schema_cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读不到 _conf_schema.json，面板设置页只能显示空的：{exc}")
            self._schema_cache = {}
        return self._schema_cache

    def _settings_leaf(self, path: str, child: dict[str, Any]) -> dict[str, Any]:
        """一个叶子字段（能直接渲染成控件的那些）。"""
        return {
            "path": path,
            "key": path.rsplit(".", 1)[-1],
            "label": str(child.get("description") or path),
            "hint": str(child.get("hint") or ""),
            "type": str(child.get("type") or "string").strip().lower(),
            "options": [str(item) for item in (child.get("options") or [])],
            "value": cfg_get(self.config, path, child.get("default")),
            "default": child.get("default"),
        }

    def _settings_fields(self, spec: Any, prefix: str = "") -> list[dict[str, Any]]:
        """把一个配置块摊平成字段列表（对象递归成 children）。"""
        items = spec.get("items") if isinstance(spec, dict) else None
        if not isinstance(items, dict):
            return []
        out: list[dict[str, Any]] = []
        for key, child in items.items():
            if not isinstance(child, dict):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(child.get("type") or "").strip().lower() == "object":
                children = self._settings_fields(child, path)
                if not children:
                    continue
                out.append({
                    "path": path,
                    "key": str(key),
                    "label": str(child.get("description") or key),
                    "hint": str(child.get("hint") or ""),
                    "type": "object",
                    "options": [],
                    "children": children,
                })
                continue
            out.append(self._settings_leaf(path, child))
        return out

    def _settings_leaf_paths(self, schema: Any = None) -> set[str]:
        schema = self._settings_schema() if schema is None else schema
        paths: set[str] = set()

        def walk(spec: Any, prefix: str) -> None:
            items = spec.get("items") if isinstance(spec, dict) else None
            if not isinstance(items, dict):
                return
            for key, child in items.items():
                if not isinstance(child, dict):
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                if str(child.get("type") or "").lower() == "object" and isinstance(
                    child.get("items"), dict
                ):
                    walk(child, path)
                else:
                    paths.add(path)

        # 顶层是「键 -> 配置块」的平面结构，没有包一层 object，这里补上
        walk({"items": schema}, "")
        return paths

    def _set_config_value(self, path: str, value: Any) -> bool:
        parts = [part for part in str(path).split(".") if part]
        if not parts:
            return False
        node: Any = self.config
        for part in parts[:-1]:
            nxt = node.get(part) if hasattr(node, "get") else None
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        return True

    def _settings_payload(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        for key, spec in self._settings_schema().items():
            if not isinstance(spec, dict):
                continue
            key = str(key)
            if str(spec.get("type") or "").strip().lower() == "object":
                fields = self._settings_fields(spec, key)
            else:
                # 顶层也可能是单个开关（例如 enabled），它自己就是一项
                fields = [self._settings_leaf(key, spec)]
            if not fields:
                continue
            groups.append({
                "key": str(key),
                "label": str(spec.get("description") or key),
                "hint": str(spec.get("hint") or ""),
                "fields": fields,
            })
        save = getattr(self.config, "save_config", None)
        config_file = ""
        raw_path = str(getattr(self.config, "config_path", "") or "")
        if raw_path:
            config_file = os.path.basename(raw_path)
        return {
            "groups": groups,
            "writable": callable(save),
            "config_file": config_file,
            "saved_at": float(getattr(self, "_settings_saved_at", 0.0) or 0.0),
            "overrides": {
                "relationship": bool(self._relationship_data),
                "splitter": bool(self._splitter_data),
            },
        }

    async def _api_settings(self) -> Any:
        return json_response(self._settings_payload())

    async def _api_settings_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            return error_response("没有收到任何要保存的设置")
        known = self._settings_leaf_paths()
        updates = {str(key): value for key, value in values.items() if str(key) in known}
        if not updates:
            return error_response("这些设置项不认识，可能是插件版本对不上")
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            return error_response(
                "这个 AstrBot 版本的插件配置不支持回写，请到「配置」页改，或者手动编辑配置文件"
            )
        for path, value in updates.items():
            self._set_config_value(path, value)
        try:
            save()
        except TypeError:
            try:
                save(dict(self.config))
            except Exception as exc:  # noqa: BLE001
                return error_response(f"写入配置文件失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            return error_response(f"写入配置文件失败：{exc}")
        self._settings_saved_at = time.time()
        self._reload_from_config()
        logger.info(
            f"[ai_mind] 面板改了 {len(updates)} 项设置：{'、'.join(sorted(updates)[:8])}"
        )
        result = self._settings_payload()
        result["saved"] = sorted(updates)
        return json_response(result)

    # ------------------------------------------------------------------
    # 管理：批量清理与维护
    # ------------------------------------------------------------------
    def _manage_stats(self) -> dict[str, Any]:
        try:
            memory = self.store.stats()
        except Exception:  # noqa: BLE001
            memory = {"total": 0, "pinned": 0, "sensitive": 0, "embedded": 0}
        db_bytes = 0
        try:
            path = self.data_dir / "mind.db"
            if path.is_file():
                db_bytes = int(path.stat().st_size)
        except Exception:  # noqa: BLE001
            db_bytes = 0
        try:
            sessions = int(self.engine.session_count)
        except Exception:  # noqa: BLE001
            sessions = 0
        return {
            "memory": memory,
            "sessions": sessions,
            "samples": int(self.samples.count()),
            "lexicon_hits": int(self.lexicons.count()),
            "db_bytes": db_bytes,
            "frozen": len(self._frozen),
        }

    async def _api_manage(self) -> Any:
        params = await self._params()
        session = str(params.get("session") or "").strip()
        if not session:
            session = self._session_or_latest(params)
        session_memories = 0
        if session:
            try:
                session_memories = self.store.count_for_session(session)
            except Exception:  # noqa: BLE001
                session_memories = 0
        return json_response({
            "stats": self._manage_stats(),
            "session": session,
            "session_memories": session_memories,
            "frozen": session in self._frozen,
            "kinds": list(KINDS),
            "kind_labels": dict(KIND_LABELS),
            "scopes": ["session", "user", "global"],
            "scope_labels": {"session": "本会话", "user": "本人", "global": "全局"},
        })

    async def _api_manage_memory(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "").strip().lower()
        session = str(params.get("session") or "").strip()
        removed = 0
        if action in {"clear_session", "清空会话"}:
            if not session:
                return error_response("没有指定要清理的会话")
            removed = self.store.clear(session_id=session)
        elif action in {"clear_all", "清空全部"}:
            removed = self.store.clear(everything=True)
        elif action in {"prune", "清理"}:
            rows, _total = self.store.query_page(
                kind=str(params.get("kind") or ""),
                scope=str(params.get("scope") or ""),
                pinned=params.get("pinned"),
                sensitive=params.get("sensitive"),
                limit=5000,
                offset=0,
            )
            try:
                floor = float(params.get("max_importance"))
            except (TypeError, ValueError):
                floor = None
            never_used = str(params.get("never_used") or "").lower() in {"1", "true", "yes", "on"}
            keep_pinned = str(params.get("keep_pinned") or "").lower() in {"1", "true", "yes", "on"}
            targets = []
            for memory in rows:
                if keep_pinned and memory.pinned:
                    continue
                if floor is not None and float(memory.importance or 0.0) > floor:
                    continue
                if never_used and int(memory.hit_count or 0) > 0:
                    continue
                targets.append(memory.id)
            removed = self.store.delete_many(targets) if targets else 0
        else:
            return error_response("不认识的操作：" + (action or "（空）"))
        logger.info(f"[ai_mind] 管理页执行 {action}，影响 {removed} 条记忆")
        return json_response({
            "ok": True, "action": action, "removed": removed, "stats": self._manage_stats()
        })

    async def _api_manage_data(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "").strip().lower()
        removed = 0
        if action in {"prune_samples", "压缩曲线"}:
            try:
                keep = int(float(params.get("keep") or 1500))
            except (TypeError, ValueError):
                keep = 1500
            keep = max(50, min(100000, keep))
            removed = self.samples.prune_all(keep)
        elif action in {"clear_lexicon", "清空词表命中"}:
            removed = self.lexicons.clear()
        elif action in {"vacuum", "整理数据库"}:
            conn = getattr(self.store, "connection", None)
            if conn is None:
                return error_response("数据库没打开")
            try:
                conn.execute("VACUUM")
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                return error_response(f"整理失败：{exc}")
        elif action in {"thaw_all", "解冻全部"}:
            removed = len(self._frozen)
            self._frozen.clear()
        else:
            return error_response("不认识的操作：" + (action or "（空）"))
        logger.info(f"[ai_mind] 管理页执行 {action}，影响 {removed} 条")
        return json_response({
            "ok": True, "action": action, "removed": removed, "stats": self._manage_stats()
        })
    # ------------------------------------------------------------------
    # 认人：专属用户（主人 / 宝宝）
    # ------------------------------------------------------------------
    def _relationship_file(self) -> Path:
        return self.data_dir / "relationship_overrides.json"

    def _load_relationship_overrides(self) -> dict[str, Any]:
        """面板上填的专属用户 / 称呼存在这，盖在插件配置之上。

        为什么要这么一层：插件是要发给别人用的，别人不一定愿意翻 JSON。
        面板上填完就能认人，比让他去找 emotion.relationship.special_users 靠谱得多。
        """
        path = self._relationship_file()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取专属用户覆盖配置失败，先用配置文件：{exc}")
        return {}

    def _save_relationship_overrides(self, data: dict[str, Any]) -> bool:
        path = self._relationship_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            json.loads(path.read_text(encoding="utf-8"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 保存专属用户覆盖配置失败：{exc}", exc_info=True)
            return False

    def _reload_settings(self) -> None:
        self.settings = MindSettings.from_config(
            self.config, self.data_dir, self._relationship_data, self._humanize_data,
            self._guard_data,
            self._style_data,
            self._privacy_data,
            self._debounce_data,
        )
        self.engine.settings = self.settings.emotion
        self._warn_missing_special()

    def _reload_from_config(self) -> None:
        """面板改完配置之后，把运行中用到的东西重建一遍（内存里的情绪状态不动）。"""
        self.settings = MindSettings.from_config(
            self.config, self.data_dir, self._relationship_data, self._humanize_data,
            self._guard_data,
            self._style_data,
            self._privacy_data,
            self._debounce_data,
        )
        self.emotion_injection_mode = str(
            cfg_get(self.config, "emotion.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.emotion_inject_rules = bool(cfg_get(self.config, "emotion.injection.inject_rules", True))
        self.emotion_trusted = bool(cfg_get(self.config, "emotion.injection.trusted", True))
        self.memory_injection_mode = str(
            cfg_get(self.config, "memory.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.memory_inject_rules = bool(cfg_get(self.config, "memory.injection.inject_rules", True))
        self.embedding_timeout = float(
            cfg_get(self.config, "memory.embedding.timeout_seconds", 3) or 3
        )
        try:
            self.engine.apply_settings(self.settings.emotion)
            self.engine.set_custom_rules(cfg_get(self.config, "emotion.persona.custom_rules", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 热更新情绪设置失败：{exc}")
        self._reload_splitter()
        self._warn_missing_special()

    def _warn_missing_special(self) -> None:
        """一个专属用户都没配的时候提醒一次。

        这是反馈里「她不认人」最常见的原因：人设里写着「要叫他宝宝」，
        但插件没被告知"谁是宝宝"，模型就真的不知道面前这个人是谁。
        """
        if self.settings.emotion.special_users:
            self._special_warned = False
            return
        if getattr(self, "_special_warned", False):
            return
        self._special_warned = True
        logger.warning(
            "[ai_mind] 还没有设置专属用户（主人 / 宝宝）：她现在谁都不认得，"
            "别人问「你是谁」她是真的不知道。"
            "去「心智面板 → 人格定制 → 专属用户」填上你的 ID，"
            "或者配置 emotion.relationship.special_users。"
        )

    def _relationship_candidates(self, limit: int = 80) -> list[dict[str, Any]]:
        """把所有会话里出现过的人汇总起来，方便面板上点一下加为专属。"""
        merged: dict[str, dict[str, Any]] = {}
        try:
            pairs = self.engine.all_relations()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 汇总关系列表失败：{exc}")
            pairs = []
        for session_key, relation in pairs:
            uid = str(getattr(relation, "uid", "") or "")
            if not uid or uid == "unknown":
                continue
            item = merged.get(uid)
            if item is None:
                item = {
                    "uid": uid,
                    "name": "",
                    "msg_count": 0,
                    "affinity": float(getattr(relation, "affinity", 0.0) or 0.0),
                    "familiarity": float(getattr(relation, "familiarity", 0.0) or 0.0),
                    "sessions": 0,
                    "group_sessions": 0,
                    "private_sessions": 0,
                }
                merged[uid] = item
            item["msg_count"] += int(getattr(relation, "msg_count", 0) or 0)
            item["affinity"] = max(item["affinity"], float(getattr(relation, "affinity", 0.0) or 0.0))
            item["familiarity"] = max(
                item["familiarity"], float(getattr(relation, "familiarity", 0.0) or 0.0)
            )
            item["sessions"] += 1
            try:
                grouped = bool(is_group_session(session_key))
            except Exception:  # noqa: BLE001
                grouped = True
            item["group_sessions" if grouped else "private_sessions"] += 1
            name = str(getattr(relation, "name", "") or "")
            if name and not item["name"]:
                item["name"] = name
        items = sorted(merged.values(), key=lambda it: (-it["msg_count"], it["uid"]))
        for item in items:
            item["special"] = bool(self.settings.emotion.is_special(item["uid"]))
        return items[: max(1, limit)]

    def _relationship_describe(self) -> str:
        emotion = self.settings.emotion
        users = list(emotion.special_users)
        if not users:
            return "还没有专属用户 —— 她现在谁都不认得，别人问「你是谁」她是真的不知道"
        label = emotion.special_label or "特别的人"
        shown = "、".join(users[:5]) + ("…" if len(users) > 5 else "")
        return f"专属用户 {len(users)} 人，称呼「{label}」：{shown}"

    def _relationship_payload(self) -> dict[str, Any]:
        emotion = self.settings.emotion
        candidates = self._relationship_candidates()
        by_uid = {item["uid"]: item for item in candidates}
        members = []
        for uid in emotion.special_users:
            item = by_uid.get(uid)
            if item is None:
                item = {"uid": uid, "name": "", "msg_count": 0, "affinity": 0.0,
                        "familiarity": 0.0, "sessions": 0, "group_sessions": 0,
                        "private_sessions": 0}
            members.append({**item, "special": True})
        return {
            "special_users": list(emotion.special_users),
            "special_label": emotion.special_label,
            "inject_identity": bool(emotion.inject_identity),
            "identity_min_messages": int(emotion.identity_min_messages),
            "group_discretion": bool(emotion.group_discretion),
            "has_overrides": bool(self._relationship_data),
            "overrides": dict(self._relationship_data),
            "members": members,
            "candidates": candidates,
            "describe": self._relationship_describe(),
        }

    async def _api_theory(self) -> Any:
        params = await self._params()
        hours = 72.0
        raw = params.get("hours")
        if raw is not None:
            try:
                hours = max(1.0, min(24.0 * 90.0, float(raw)))
            except (TypeError, ValueError):
                hours = 72.0
        emotion = self.settings.emotion
        return json_response(
            theory_payload(
                self.lexicons,
                enabled=bool(emotion.lexicon_enabled),
                gain=float(emotion.lexicon_gain),
                window_hours=hours,
            )
        )

    async def _api_theory_clear(self) -> Any:
        removed = self.lexicons.clear()
        logger.info(f"[ai_mind] 已清空 {removed} 条词表命中记录")
        return json_response({"ok": True, "removed": removed})

    async def _api_relationship_relation(self) -> Any:
        """手动改某人的好感度 / 熟悉度（同一个人在所有会话里一起改）。"""
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return error_response("没有指定要改谁")

        def _num(key: str) -> float | None:
            if key not in payload:
                return None
            raw = payload.get(key)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        affinity = _num("affinity")
        familiarity = _num("familiarity")
        if affinity is None and familiarity is None:
            return error_response("没有收到要改的数值")
        touched = self.engine.set_relation_values(uid, affinity, familiarity)
        if not touched:
            return error_response("这个人还没聊过 —— 等他发一条消息，面板里出现之后再改")
        try:
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 保存好感度/熟悉度失败：{exc}")
        logger.info(
            f"[ai_mind] 手动设定 {uid}：好感度 {affinity} / 熟悉度 {familiarity}"
            f"（{touched} 条关系）"
        )
        result = self._relationship_payload()
        result["saved"] = True
        return json_response(result)

    async def _api_relationship(self) -> Any:
        return json_response(self._relationship_payload())

    async def _api_relationship_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params

        emotion = self.settings.emotion
        if "special_users" in payload:
            users = _split_keywords(payload.get("special_users"))
        else:
            users = list(emotion.special_users)
        for item in _split_keywords(payload.get("add")):
            if item not in users:
                users.append(item)
        drop = set(_split_keywords(payload.get("remove")))
        if drop:
            users = [item for item in users if item not in drop]

        label = payload.get("special_label")
        label = str(label).strip() if label is not None else str(emotion.special_label or "")
        label = label or "特别的人"

        touched = any(
            key in payload
            for key in ("special_users", "add", "remove", "special_label",
                        "inject_identity", "identity_min_messages", "group_discretion")
        )
        if not touched:
            return error_response("没有收到任何要保存的内容")

        overrides = dict(self._relationship_data)
        overrides["special_users"] = users
        overrides["special_label"] = label
        if "inject_identity" in payload:
            overrides["inject_identity"] = bool(payload.get("inject_identity"))
        if "group_discretion" in payload:
            overrides["group_discretion"] = bool(payload.get("group_discretion"))
        if "identity_min_messages" in payload:
            try:
                overrides["identity_min_messages"] = max(
                    1, int(float(payload.get("identity_min_messages") or 3))
                )
            except (TypeError, ValueError):
                pass
        self._relationship_data = overrides
        if not self._save_relationship_overrides(overrides):
            return error_response("写入专属用户配置失败，详细原因见 AstrBot 日志")
        self._reload_settings()
        logger.info(f"[ai_mind] 专属用户已更新：{self._relationship_describe()}")
        result = self._relationship_payload()
        result["saved"] = True
        return json_response(result)

    async def _api_relationship_reset(self) -> Any:
        self._relationship_data = {}
        path = self._relationship_file()
        try:
            if path.is_file():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除专属用户覆盖配置失败：{exc}")
        self._reload_settings()
        result = self._relationship_payload()
        result["reset"] = True
        return json_response(result)

    async def _api_presets(self) -> Any:
        return json_response(self._preset_payload())

    async def _api_preset_save(self) -> Any:
        params = await self._params()
        name = str(params.get("name") or "").strip()
        if not name:
            return error_response("先给预设起个名字", status_code=400)
        data = {
            "label": params.get("label") or name,
            "baseline": {
                "p": params.get("p", 0.0),
                "a": params.get("a", 0.0),
                "d": params.get("d", 0.0),
            },
            "styles": params.get("styles") or {},
            "style_note": params.get("style_note") or "",
            "rules": params.get("rules") or [],
            "emotion_half_life": params.get("emotion_half_life"),
            "mood_half_life": params.get("mood_half_life"),
            "mood_coupling": params.get("mood_coupling"),
            "affinity_initial": params.get("affinity_initial"),
        }
        ok, message = save_custom_preset(self.data_dir, name, data)
        if not ok:
            return error_response(message, status_code=400)
        return json_response({"ok": True, "name": name})

    async def _api_preset_delete(self) -> Any:
        params = await self._params()
        name = str(params.get("name") or "").strip()
        if not delete_custom_preset(self.data_dir, name):
            return error_response("删不掉：内置预设不能删，或者这个名字不存在", status_code=400)
        return json_response({"ok": True})

    async def _api_preset_activate(self) -> Any:
        params = await self._params()
        name = str(params.get("name") or "").strip()
        if name not in all_presets(self.data_dir):
            return error_response("没有这个预设", status_code=400)
        try:
            persona_cfg = self.config.setdefault("emotion", {}).setdefault("persona", {})
            persona_cfg["preset"] = name
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
        except Exception as exc:  # noqa: BLE001
            return error_response("写配置失败：" + str(exc), status_code=500)
        # 立刻生效，不用重启
        self.settings = MindSettings.from_config(self.config, self.data_dir)
        self.engine.reconfigure(self.settings.emotion, get_preset(name, self.data_dir))
        return json_response({"ok": True, "active": name})

    async def _api_preset_import(self) -> Any:
        params = await self._params()
        raw = params.get("json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return error_response("这段 JSON 解析不了", status_code=400)
        if not isinstance(raw, dict):
            return error_response("内容格式不对", status_code=400)
        name = str(params.get("name") or raw.get("key") or "").strip()
        ok, message = save_custom_preset(self.data_dir, name, raw)
        if not ok:
            return error_response(message, status_code=400)
        return json_response({"ok": True, "name": message})
    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("forgetme", alias={"忘我", "删除我的数据"})
    async def cmd_forget_me(self, event: AstrMessageEvent):
        """把你自己的数据全删掉（记忆、关系、表达示例、情绪命中记录）。"""
        yield event.plain_result(await self._forget_me_text(event))

    async def _forget_me_text(self, event: AstrMessageEvent) -> str:
        if not self.settings.privacy.allow_self_delete:
            return "这个插件不允许自己删数据，找管理员。"
        uid = str(event.get_sender_id() or "")
        if not uid:
            return "认不出你是谁，删不了。"
        include_group = bool(self.settings.privacy.self_delete_group_memories)
        removed = self._forget_user(uid, include_group=include_group)
        total = sum(int(value) for value in removed.values())
        if not total:
            return "我这儿本来就没有你的东西。"
        return (
            "删干净了。"
            + chr(10)
            + "记忆 %d 条 · 关系 %d 处 · 表达示例 %d 条 · 情绪记录 %d 条"
            % (removed["memories"], removed["relations"], removed["styles"],
               removed["lexicon_hits"])
        )

    @filter.command("privacy", alias={"隐私"})
    async def cmd_privacy(self, event: AstrMessageEvent):
        """看你自己留下了什么；管理员可以管拒绝名单。"""
        text = (event.message_str or "").strip()
        parts = [item for item in text.split() if item]
        uid = str(event.get_sender_id() or "")
        if len(parts) >= 3 and parts[1] in {"拒绝", "deny", "允许", "allow"}:
            if not self._is_admin(event):
                yield event.plain_result("这个是管理员的事。")
                return
            target = parts[2]
            add = parts[1] in {"拒绝", "deny"}
            current = list(self.settings.privacy.denied_users)
            if add and target not in current:
                current.append(target)
            if not add:
                current = [item for item in current if item != target]
            merged = dict(self._privacy_data)
            merged["denied_users"] = current
            self._privacy_data = merged
            if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
                yield event.plain_result("没写进去，看日志。")
                return
            self._reload_from_config()
            yield event.plain_result(
                ("已加入拒绝名单：" if add else "已移出拒绝名单：")
                + target
                + "（共 %d 人）" % len(current)
            )
            return
        summary = self._privacy_summary(uid)
        lines = [
            "你在我这儿留下的是：",
            "- 记忆 %d 条" % summary["memories"],
            "- 关系 %d 处" % summary["relations"],
            "- 表达示例 %d 条" % summary["styles"],
            "- 情绪记录 %d 条" % summary["lexicon_hits"],
        ]
        if self._is_denied(uid):
            lines.append("你目前在拒绝名单里，我不会记你。")
        lines.append("想全删掉就发 /忘我。")
        yield event.plain_result(chr(10).join(lines))

    @filter.command("mood", alias={"情绪", "心情"})
    async def cmd_mood(self, event: AstrMessageEvent):
        """查看 / 管理情绪状态。"""
        if not self.settings.commands_enabled:
            yield event.plain_result("指令已被管理员关闭。")
            return
        try:
            text = self._mood_text(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 情绪指令出错：{exc}", exc_info=True)
            text = "情绪系统刚刚打了个结，稍后再试试吧。"
        yield event.plain_result(text)

    @filter.command("affinity", alias={"好感度", "好感"})
    async def cmd_affinity(self, event: AstrMessageEvent):
        """只看你和她的关系。"""
        if not self.settings.commands_enabled:
            yield event.plain_result("指令已被管理员关闭。")
            return
        try:
            now = time.time()
            self.engine.load()
            session = self.engine.resolve(event.unified_msg_origin, now)
            relation = self.engine.relation_of(
                session, str(event.get_sender_id() or ""), event.get_sender_name() or "", now
            )
            text = relation_panel(self.engine.snapshot(session, relation, now), self.settings.emotion)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 好感度指令出错：{exc}", exc_info=True)
            text = "查不到呢，稍后再试试吧。"
        yield event.plain_result(text)

    @filter.command("memory", alias={"记忆", "回忆"})
    async def cmd_memory(self, event: AstrMessageEvent):
        """查看 / 管理长期记忆。"""
        if not self.settings.commands_enabled:
            yield event.plain_result("指令已被管理员关闭。")
            return
        try:
            text = await self._memory_text(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记忆指令出错：{exc}", exc_info=True)
            text = "记忆库刚刚卡了一下，稍后再试试。"
        yield event.plain_result(text)

    def _mood_text(self, event: AstrMessageEvent) -> str:
        now = time.time()
        session = self.engine.resolve(event.unified_msg_origin, now)
        relation = self.engine.relation_of(
            session, str(event.get_sender_id() or ""), event.get_sender_name() or "", now
        )
        snapshot = self.engine.snapshot(session, relation, now)
        tokens = (event.message_str or "").strip().split()
        args = tokens[1:] if len(tokens) > 1 else []
        sub = args[0].lower() if args else ""

        if sub in {"help", "帮助", "?", "？"}:
            return help_panel()
        if sub in {"history", "历史", "记录"}:
            return history_panel(snapshot)
        if sub in {"rank", "排行", "排名", "榜单"}:
            return rank_panel(self.engine.rank(session, 10), self.settings.emotion)
        if sub in {"detail", "详细", "调试"}:
            return detail_panel(snapshot, session.key, self.engine.session_count)
        if sub in {"freeze", "冻结"}:
            self._frozen.add(session.key)
            return "🧊 已冻结这个会话的情绪：之后聊天不再自动改变它，曲线上也不会新增点。"
        if sub in {"unfreeze", "解冻"}:
            self._frozen.discard(session.key)
            return "🔥 已解冻，情绪恢复自动演化。"
        if sub in {"reset", "重置"}:
            if not self._is_admin(event):
                return "🚫 这个指令只有管理员能用。"
            self.engine.reset(session, now, keep_relations=True)
            self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
            self._schedule_flush()
            return "🫧 好了，情绪和心境都归零了。"
        if sub in {"set", "设定", "设置"}:
            if not self._is_admin(event):
                return "🚫 这个指令只有管理员能用。"
            numbers = args[1:4]
            if len(numbers) < 3:
                return "用法：/情绪 设定 <愉悦> <唤醒> <掌控>，例如 /情绪 设定 0.6 -0.4 0.2"
            try:
                pad = PAD.of(float(numbers[0]), float(numbers[1]), float(numbers[2]))
            except (TypeError, ValueError):
                return "🚫 三个参数都要是 -1 ~ 1 之间的数字。"
            self.engine.set_emotion(session, pad, now)
            self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
            self._schedule_flush()
            return f"✅ 已手动设定此刻情绪：{pad.pretty()}"
        return mood_panel(snapshot, self.settings.emotion)

    async def _memory_text(self, event: AstrMessageEvent) -> str:
        session_id = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        tokens = (event.message_str or "").strip().split(maxsplit=2)
        sub = tokens[1].strip().lower() if len(tokens) > 1 else ""
        rest = tokens[2].strip() if len(tokens) > 2 else ""
        now = time.time()

        if sub in {"帮助", "help", "?", "？"}:
            return memory_help_panel()
        if sub in {"搜索", "search", "查", "找"}:
            if not rest:
                return "用法：/记忆 搜索 <关键词>"
            return search_panel(self._search(rest, session_id, sender_id), rest)
        if sub in {"记住", "remember", "记"}:
            if not rest:
                return "用法：/记忆 记住 <内容>"
            return self._manual_remember(rest, session_id, sender_id)
        if sub in {"忘记", "forget", "删", "删除"}:
            if not rest.isdigit():
                return "用法：/记忆 忘记 <编号>"
            return self._forget(int(rest), session_id, sender_id)
        if sub in {"清空", "clear", "重置"}:
            if self.settings.memory.admin_only_clear and not self._is_admin(event):
                return "🚫 清空记忆只有管理员能用。"
            if rest in {"全部", "all", "所有"}:
                return clear_panel(self.store.clear(everything=True), "整个记忆库")
            removed = self.store.clear(session_id=session_id) + self.store.clear(owner_id=sender_id)
            return clear_panel(removed, "本会话与你相关")

        result = self.retriever.retrieve(
            self.store, query=event.message_str or "", session_id=session_id, sender_id=sender_id, now=now
        )
        return memory_main_panel(
            store=self.store, result=result, settings=self.settings.memory, session_id=session_id, now=now
        )

    def _search(self, keyword: str, session_id: str, sender_id: str) -> list[Memory]:
        return self.retriever.search(
            self.store, keyword=keyword, session_id=session_id, sender_id=sender_id
        )

    def _manual_remember(self, content: str, session_id: str, sender_id: str) -> str:
        content = content.strip()
        if len(content) < 2:
            return "内容太短了，写清楚一点吧。"
        reasons = detect_sensitive(content) if self.settings.memory.scan_sensitive else []
        sensitive = bool(reasons)
        scope = decide_scope(
            session_id=session_id,
            sensitive=sensitive,
            group_scope=self.settings.memory.group_scope,
            private_scope=self.settings.memory.private_scope,
        )
        memory = Memory(
            content=content,
            scope=scope,
            session_id=session_id if scope == "session" else "",
            owner_id=sender_id if scope == "user" else "",
            importance=1.0,
            pinned=True,
            sensitive=sensitive,
            sensitive_reasons=tuple(reasons),
            source="manual",
            created_at=time.time(),
            updated_at=time.time(),
        )
        memory.id = self.store.add(memory)
        if not memory.id:
            return "写入记忆库失败了，看看日志吧。"
        return remember_panel(memory, sensitive)

    def _forget(self, memory_id: int, session_id: str, sender_id: str) -> str:
        memory = self.store.get(memory_id)
        if memory is None:
            return forget_panel(None, str(memory_id), False)
        if not self._can_manage(memory, session_id, sender_id):
            return "🚫 这条记忆不属于你，删不了。"
        return forget_panel(memory, str(memory_id), self.store.delete(memory_id))

    @staticmethod
    def _can_manage(memory: Memory, session_id: str, sender_id: str) -> bool:
        if memory.owner_id and str(memory.owner_id) == str(sender_id):
            return True
        if memory.session_id and memory.session_id == session_id:
            return True
        return False

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        stripped = (text or "").strip()
        return bool(stripped) and stripped.startswith(COMMAND_PREFIXES)

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                pass
        try:
            return str(getattr(event, "role", "") or "") == "admin"
        except Exception:  # noqa: BLE001
            return False

