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
import random
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
        self.store = MemoryStore(self.data_dir / "mind.db", logger)
        self.samples = SampleStore(self.store.connection, logger)
        self.lexicons = LexiconStore(self.store.connection, logger)
        self.humanize_log = HumanizeStore(self.store.connection, logger)
        self.styles = StyleStore(self.store.connection, logger)
        self.images = ImageStore(self.store.connection, self.data_dir, logger)
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
        self._sample_broken = False
        self._sample_warned = False
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
        logger.info(f"[ai_mind] {splitter_engine.describe(self.splitter)}")
        logger.info(f"[ai_mind] {debounce_engine.describe(self.settings.debounce)}")
        try:
            from .mind.emotion import lexicon as _lexicon
        except ImportError:  # pragma: no cover
            from mind.emotion import lexicon as _lexicon
        logger.info(f"[ai_mind] {_lexicon.describe_lexicon()}")
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
        self.store.close()

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
                if self.engine.dirty:
                    await asyncio.to_thread(self.engine.flush, now)
                await self.flush_memories()
                await self.embed_pending()
                self.store.prune(
                    max_memories=self.settings.memory.max_memories,
                    importance_floor=self.settings.memory.prune_importance_floor,
                )
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
        if not should_sample(previous, pad, self.settings.panel.sample_epsilon, force=force):
            return
        self.samples.append(session_key, now, pad, kind)
        self._last_sample[session_key] = self.samples.latest(session_key)
        if self.samples.conn is None:
            # 曲线表没建起来：别静默失败，否则面板上就是一片空白却查不出原因
            self._sample_broken = True
            if not self._sample_warned:
                self._sample_warned = True
                logger.warning("[ai_mind] 曲线采样表不可用，面板上的情绪曲线会退化")

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
            if self.settings.debug_log:
                logger.info(f"[ai_mind] 记忆抽取：新增 {added} / 合并 {merged} / 跳过 {skipped}")
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
        if self._mark_seen(dedup_key) and not frozen:
            gap = max(0.0, now - session.last_interaction)
            self.engine.apply_idle(session, now)
            self.engine.touch(session, relation, now)
            session.last_gap = gap
            stimulus = self.engine.appraiser.appraise(
                event.message_str or "", relation=relation, is_special=relation.special
            )
            if stimulus is not None:
                if stimulus.source == "crisis" and not self.settings.emotion.crisis_enabled:
                    stimulus = None
                else:
                    self.engine.apply_stimulus(session, stimulus, relation, now)
                    hits = getattr(stimulus, "lexicon_hits", ()) or ()
                    if hits:
                        self._record_lexicon_hits(
                            session_key, uid, hits, getattr(stimulus, "emotion", "")
                        )
                    self._maybe_sample(
                        session_key, session.emotion,
                        "crisis" if stimulus.source == "crisis" else "stimulus", now, force=True,
                    )
            self._maybe_sample(session_key, session.emotion, "stimulus", now)
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
            self._mark_llm(event)

            # 每轮都会变的内容攒在这里，最后统一贴到请求尾部（见 _cache_or_place）
            cache_tail: list[str] = []

            # ---------- 情绪 ----------
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
                try:
                    kept = self._filter_denied_memories(result.items)
                    if len(kept) != len(result.items):
                        result.items = kept
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_mind] 过滤拒绝名单的记忆失败：{exc}")
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

            # ---------- 工具守卫（请求级） ----------
            self._apply_tool_guard(event, req)

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

            # ---------- 把每轮都变的内容贴到请求最后 ----------
            # 放这里而不是开头，是为了让前缀（persona + 历史）保持稳定，
            # 前缀缓存才命得中。
            if cache_tail:
                joined = "\n\n".join(cache_tail)
                if not self._append_content_part(req, joined):
                    # 这个 AstrBot 版本不支持附加到用户消息：退回受信任位。
                    # 宁可少省点 token，也不能把情绪和记忆弄丢。
                    self._place_trusted(req, joined)
        except Exception as exc:  # noqa: BLE001 - 钩子里的异常绝不能打断对话
            logger.error(f"[ai_mind] 注入失败：{exc}", exc_info=True)

    def _prompt(self, key: str, fallback: str = "") -> str:
        """取一段可配置的提示词；仓库出问题就用代码里的默认值。"""
        try:
            text = self.prompts.get(key)
        except Exception:  # noqa: BLE001
            text = ""
        return (text or "").strip() or fallback

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
    def _place_trusted(req: ProviderRequest, block: str) -> bool:
        """塞进「系统级受信任位置」。

        AstrBot 的 ProviderRequest.contexts 是 OpenAI 格式的上下文列表，
        会被放在 System Prompt 之后、用户消息之前 —— 这正是身份信息该待的地方。
        拿不到 contexts 就退回系统提示词，绝不会退进用户消息。
        """
        if not block:
            return False
        contexts = getattr(req, 'contexts', None)
        if isinstance(contexts, list):
            try:
                contexts.append({'role': 'system', 'content': block})
                return True
            except Exception:  # noqa: BLE001
                pass
        req.system_prompt = ((req.system_prompt or '') + chr(10) + chr(10) + block).strip()
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
            return  # 只有一段就别折腾了，原样交回框架

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
        if self.settings.debug_log:
            preview_text = " ｜ ".join(
                value[:20] for value in (splitter_engine.segment_text(seg) for seg in segments)
            )
            logger.info(f"[ai_mind] 分段发送 {sent} 段（{session_key}）：{preview_text}")
    # ------------------------------------------------------------------
    # 关键词配图
    # ------------------------------------------------------------------
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
        return await handler()

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
            affected = self.store.delete_many(ids)
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
        return json_response({"ok": bool(self.store.delete(memory_id))})

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
            if not result.items:
                return "关于他，我这儿什么都没记着。"
            self.store.touch(result.ids())
            return "我记得这些：" + chr(10) + chr(10).join(
                "- " + item.content for item in result.items[:8]
            )
        except Exception as exc:  # noqa: BLE001 - 工具报错不能把对话搞崩
            logger.warning(f"[ai_mind] 回忆工具失败：{exc}")
            return "想不起来了（内部出错了）。"

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
        if "mode" in payload:
            mode = str(payload.get("mode") or "").strip().lower()
            if mode in ("simple", "expert"):
                current["mode"] = mode
                self._save_simple_overrides("panel_mode.json", current, "面板模式")
        return json_response({"mode": str(current.get("mode") or "simple")})

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

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_guard_message(self, event: AstrMessageEvent):
        """消息级鉴权：非主人的注入 / 冒充 / 索取密钥，拦在 LLM 之前。"""
        if not self.settings.enabled:
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
        report = guard_engine.scrub_reply(text)
        if not report.leaked:
            return
        self._guard_leaks += 1
        logger.warning(
            f"[ai_mind] 回复里出现了内部内容 {report.markers + report.phrases}，已清理"
        )
        cleaned = report.cleaned
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

