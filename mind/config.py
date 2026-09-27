"""合并后的配置解析。

原来的两个插件各自读自己那一坨配置；合并成一个插件以后，
配置按职责重新分块：

```
enabled            总开关
emotion.*          情绪：气质、动力学、关系、注入、安全
memory.*           记忆：采集、抽取、检索、向量、隐私
panel.*            面板：曲线采样与展示
commands.*         指令开关
advanced.*         共享的维护参数
```

两个子模块各自的 Settings 类保持原样不动 —— 它们已经被测试覆盖过，
这里只负责把配置切片喂给它们。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .emotion.engine import Settings as EmotionSettings
from .memory.config import Settings as MemorySettings
from .memory.config import cfg_get
from .guard import DEFAULT_TOOL_BLACKLIST
from .debounce import DebounceSettings


def _f(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if result != result:
        result = default
    return max(low, min(high, result))


def _i(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


def _b(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def _str_list(value: Any) -> list[str]:
    """把配置里的列表/逗号串洗成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", "\n").split("\n") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


#: 去 AI 味的三种模式
HUMANIZE_MODES = ("off", "check", "rewrite")
HUMANIZE_MODE_LABELS = {
    "off": "不做",
    "check": "只体检（记录分数，不改）",
    "rewrite": "打回重写（不达标就让它重说）",
}


@dataclass
class ImagesSettings:
    """关键词配图：说到某些话，直接甩一张图。"""

    enabled: bool = True
    #: replace = 只发图、不再走 LLM；before = 先发图、再正常回复
    mode: str = "replace"
    #: 模糊匹配的阈值（字符集合 Jaccard）
    fuzzy_threshold: float = 0.6
    #: 同一会话里同一个触发词的冷却时间，防止刷屏
    cooldown_seconds: float = 30.0
    #: 单张图片上传上限
    max_bytes: int = 8 * 1024 * 1024
    #: 面板预览的最大体积，超过就只给元数据
    preview_max_bytes: int = 512 * 1024
    #: 指令消息（以 / 等开头的）不参与配图匹配
    skip_commands: bool = True
    #: 只发图时，是否仍然结算情绪（她还是会因为这句话变心情）
    react_emotion: bool = True
    #: 允许 AI 自己在回复里用 <pic>关键词</pic> 点图发出去
    ai_send_enabled: bool = True
    #: 一条回复里最多让她发几张
    ai_send_max: int = 2
    #: 可选：发图时附带的一句文字
    reply_text: str = ""

    @staticmethod
    def from_config(config: Mapping[str, Any] | None) -> "ImagesSettings":
        return ImagesSettings(
            enabled=_b(cfg_get(config, "images.enabled", True), True),
            mode=str(cfg_get(config, "images.mode", "replace") or "replace").strip().lower(),
            fuzzy_threshold=_f(cfg_get(config, "images.fuzzy_threshold", 0.6), 0.6, 0.1, 1.0),
            cooldown_seconds=_f(cfg_get(config, "images.cooldown_seconds", 30), 30.0, 0.0, 86400.0),
            max_bytes=_i(cfg_get(config, "images.max_bytes", 8 * 1024 * 1024), 8 * 1024 * 1024, 65536, 200 * 1024 * 1024),
            preview_max_bytes=_i(cfg_get(config, "images.preview_max_bytes", 512 * 1024), 512 * 1024, 4096, 50 * 1024 * 1024),
            skip_commands=_b(cfg_get(config, "images.skip_commands", True), True),
            react_emotion=_b(cfg_get(config, "images.react_emotion", True), True),
            ai_send_enabled=_b(cfg_get(config, "images.ai_send.enabled", True), True),
            ai_send_max=_i(cfg_get(config, "images.ai_send.max_per_message", 2), 2, 1, 9),
            reply_text=str(cfg_get(config, "images.reply_text", "") or ""),
        )


@dataclass
class PanelSettings:
    """心智面板（WebUI 页面）的参数。"""

    #: 默认展示最近多少小时的曲线
    curve_hours: float = 72.0
    #: 每个会话最多保留多少个采样点（超出丢最旧的）
    curve_max_points: int = 3000
    #: 没有情绪事件时，也会按这个间隔打一个心跳点，让衰减过程画得出来
    heartbeat_seconds: float = 300.0
    #: 变化小于这个幅度就不采样，避免曲线被噪声填满
    sample_epsilon: float = 0.004
    #: 每个会话最多返回多少个事件点（防止图表卡死）
    max_points_returned: int = 1500
    #: 是否允许非管理员读写面板数据
    public_access: bool = False
    #: 面板背景图（每次打开随机一张虚化图 + 毛玻璃卡片）
    background: bool = True

    @staticmethod
    def from_config(config: Mapping[str, Any] | None) -> "PanelSettings":
        return PanelSettings(
            curve_hours=_f(cfg_get(config, "panel.curve_hours", 72), 72.0, 1.0, 24 * 365.0),
            curve_max_points=_i(cfg_get(config, "panel.curve_max_points", 3000), 3000, 100, 200000),
            heartbeat_seconds=_f(cfg_get(config, "panel.heartbeat_seconds", 300), 300.0, 30.0, 86400.0),
            sample_epsilon=_f(cfg_get(config, "panel.sample_epsilon", 0.004), 0.004, 0.0, 0.2),
            max_points_returned=_i(cfg_get(config, "panel.max_points_returned", 1500), 1500, 50, 20000),
            public_access=_b(cfg_get(config, "panel.public_access", False), False),
            background=_b(cfg_get(config, "panel.background", True), True),
        )


@dataclass
class HumanizeSettings:
    """去 AI 味：发送前的一道阀门。"""

    enabled: bool = True
    #: off 不做；check 只体检记录；rewrite 不达标就打回重写
    mode: str = "check"
    #: 低于这个分数就打回（check 模式下只记录）
    threshold: float = 60.0
    #: 最多重写几轮
    max_rounds: int = 2
    #: 短于这个字数不体检（「嗯。」本来就没法像机器）
    min_length: int = 12
    #: both / private / group
    scope: str = "both"
    #: 只处理 LLM 的回复（指令回执不动）
    llm_only: bool = True
    #: 用哪个模型重写（留空 = 当前对话模型）
    provider_id: str = ""
    timeout: float = 20.0
    #: 改完分更低就保留原来那版
    keep_best: bool = True
    #: 额外要拦的词
    extra_ai_words: list[str] = field(default_factory=list)
    extra_templates: list[str] = field(default_factory=list)
    #: 白名单：这些词不算 AI 味
    allow_words: list[str] = field(default_factory=list)

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "HumanizeSettings":
        raw = dict(cfg_get(config, "humanize", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        mode = str(raw.get("mode") or "check").strip().lower()
        if mode not in HUMANIZE_MODES:
            mode = "check"
        scope = str(raw.get("scope") or "both").strip().lower()
        if scope not in ("both", "private", "group"):
            scope = "both"
        return HumanizeSettings(
            enabled=_b(raw.get("enabled"), True),
            mode=mode,
            threshold=_f(raw.get("threshold"), 60.0, 0.0, 100.0),
            max_rounds=_i(raw.get("max_rounds"), 2, 1, 5),
            min_length=_i(raw.get("min_length"), 12, 2, 200),
            scope=scope,
            llm_only=_b(raw.get("llm_only"), True),
            provider_id=str(raw.get("provider_id") or "").strip(),
            timeout=_f(raw.get("timeout"), 20.0, 5.0, 120.0),
            keep_best=_b(raw.get("keep_best"), True),
            extra_ai_words=_str_list(raw.get("extra_ai_words")),
            extra_templates=_str_list(raw.get("extra_templates")),
            allow_words=_str_list(raw.get("allow_words")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "threshold": float(self.threshold),
            "max_rounds": int(self.max_rounds),
            "min_length": int(self.min_length),
            "scope": self.scope,
            "llm_only": bool(self.llm_only),
            "provider_id": self.provider_id,
            "timeout": float(self.timeout),
            "keep_best": bool(self.keep_best),
            "extra_ai_words": list(self.extra_ai_words),
            "extra_templates": list(self.extra_templates),
            "allow_words": list(self.allow_words),
        }


@dataclass
class GuardSettings:
    """主人鉴权与护栏。"""

    enabled: bool = True
    #: 除了「专属用户」，额外认这些身份（支持 平台:uid / 平台实例:uid / 平台:*）
    extra_masters: list[str] = field(default_factory=list)
    #: AstrBot 全局管理员等同主人
    trust_admins: bool = True
    #: 消息级拦截：注入 / 冒充 / 索取密钥 / 危险操作
    block_sensitive: bool = True
    #: 连普通指令也不让非主人用
    block_all_commands: bool = False
    #: 拦截后回一句（留空就静默不理）
    reply: str = "这个我不能帮你做。"
    #: 工具调用拦截：默认只拦高危工具（见 guard.DEFAULT_TOOL_BLACKLIST）
    block_tools: bool = True
    tool_mode: str = "blacklist"
    kept_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOL_BLACKLIST))
    #: 发送前护栏：把内部标记和"说破机制"的话清掉
    scrub_reply: bool = True
    #: 额外要拦的正则
    extra_patterns: list[str] = field(default_factory=list)
    #: 白名单：命中里包含这些词就不拦
    allow_patterns: list[str] = field(default_factory=list)

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "GuardSettings":
        raw = dict(cfg_get(config, "guard", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        mode = str(raw.get("tool_mode") or "blacklist").strip().lower()
        if mode not in ("whitelist", "blacklist"):
            mode = "blacklist"
        kept = _str_list(raw.get("kept_tools"))
        if not kept:
            # 名单空着 = 没配过（或者是从老版本升上来的）。
            # 白名单为空会让非主人一个工具都用不了（连「检查插件状态」都被摘），
            # 黑名单为空又等于完全不拦 —— 两种都不是用户想要的，
            # 一律回落到默认高危名单。真想完全不拦，请关掉 block_tools。
            mode = "blacklist"
            kept = list(DEFAULT_TOOL_BLACKLIST)
        return GuardSettings(
            enabled=_b(raw.get("enabled"), True),
            extra_masters=_str_list(raw.get("extra_masters")),
            trust_admins=_b(raw.get("trust_admins"), True),
            block_sensitive=_b(raw.get("block_sensitive"), True),
            block_all_commands=_b(raw.get("block_all_commands"), False),
            reply=str(raw.get("reply") if raw.get("reply") is not None else "这个我不能帮你做。"),
            block_tools=_b(raw.get("block_tools"), True),
            tool_mode=mode,
            kept_tools=kept,
            scrub_reply=_b(raw.get("scrub_reply"), True),
            extra_patterns=_str_list(raw.get("extra_patterns")),
            allow_patterns=_str_list(raw.get("allow_patterns")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "extra_masters": list(self.extra_masters),
            "trust_admins": bool(self.trust_admins),
            "block_sensitive": bool(self.block_sensitive),
            "block_all_commands": bool(self.block_all_commands),
            "reply": self.reply,
            "block_tools": bool(self.block_tools),
            "tool_mode": self.tool_mode,
            "kept_tools": list(self.kept_tools),
            "scrub_reply": bool(self.scrub_reply),
            "extra_patterns": list(self.extra_patterns),
            "allow_patterns": list(self.allow_patterns),
        }


@dataclass
class StyleSettings:
    """表达示例：学她以前怎么说话，再拿出来当参考。"""

    enabled: bool = True
    #: 学来的东西默认先待审；打开就自动批准（风险自负）
    auto_approve: bool = False
    #: 去 AI 味的分数门槛：低于这个分不值得学
    min_score: float = 78.0
    min_chars: int = 8
    max_chars: int = 120
    #: 每轮注入几条
    inject_count: int = 3
    #: both / private / group
    scope: str = "both"
    llm_only: bool = True
    #: 过多久自动清掉没被批准的（天，0 = 不清）
    pending_ttl_days: float = 30.0

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "StyleSettings":
        raw = dict(cfg_get(config, "style", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        scope = str(raw.get("scope") or "both").strip().lower()
        if scope not in ("both", "private", "group"):
            scope = "both"
        return StyleSettings(
            enabled=_b(raw.get("enabled"), True),
            auto_approve=_b(raw.get("auto_approve"), False),
            min_score=_f(raw.get("min_score"), 78.0, 0.0, 100.0),
            min_chars=_i(raw.get("min_chars"), 8, 2, 200),
            max_chars=_i(raw.get("max_chars"), 120, 10, 2000),
            inject_count=_i(raw.get("inject_count"), 3, 0, 8),
            scope=scope,
            llm_only=_b(raw.get("llm_only"), True),
            pending_ttl_days=_f(raw.get("pending_ttl_days"), 30.0, 0.0, 3650.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "auto_approve": bool(self.auto_approve),
            "min_score": float(self.min_score),
            "min_chars": int(self.min_chars),
            "max_chars": int(self.max_chars),
            "inject_count": int(self.inject_count),
            "scope": self.scope,
            "llm_only": bool(self.llm_only),
            "pending_ttl_days": float(self.pending_ttl_days),
        }


@dataclass
class PrivacySettings:
    """隐私与合规：谁能被记住、谁能把数据删掉。"""

    #: 拒绝名单：这些人既不采集也不注入（支持 平台:uid 写法）
    denied_users: list[str] = field(default_factory=list)
    #: 允许本人通过指令删掉自己的全部数据
    allow_self_delete: bool = True
    #: 删自己的时候，要不要连"在同一个会话里学到的别的东西"一起删（默认不删）
    self_delete_group_memories: bool = False
    #: 同意门：会话里没同意过就不采集
    consent_required: bool = False
    #: 允许管理员导出某个人的档案
    allow_admin_export: bool = True

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "PrivacySettings":
        raw = dict(cfg_get(config, "privacy", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        return PrivacySettings(
            denied_users=_str_list(raw.get("denied_users")),
            allow_self_delete=_b(raw.get("allow_self_delete"), True),
            self_delete_group_memories=_b(raw.get("self_delete_group_memories"), False),
            consent_required=_b(raw.get("consent_required"), False),
            allow_admin_export=_b(raw.get("allow_admin_export"), True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "denied_users": list(self.denied_users),
            "allow_self_delete": bool(self.allow_self_delete),
            "self_delete_group_memories": bool(self.self_delete_group_memories),
            "consent_required": bool(self.consent_required),
            "allow_admin_export": bool(self.allow_admin_export),
        }


@dataclass
class MindSettings:
    """整个插件的配置总成。"""

    enabled: bool = True
    commands_enabled: bool = True
    admin_only_mutations: bool = True
    debug_log: bool = False
    #: 省 token：把每轮都变的内容贴到请求最后，让前缀缓存能命中
    cache_friendly: bool = True
    maintenance_interval: float = 20.0

    emotion: EmotionSettings = None  # type: ignore[assignment]
    memory: MemorySettings = None    # type: ignore[assignment]
    panel: PanelSettings = None      # type: ignore[assignment]
    images: ImagesSettings = None    # type: ignore[assignment]
    humanize: HumanizeSettings = None  # type: ignore[assignment]
    guard: GuardSettings = None  # type: ignore[assignment]
    style: StyleSettings = None  # type: ignore[assignment]
    privacy: PrivacySettings = None  # type: ignore[assignment]
    debounce: DebounceSettings = None  # type: ignore[assignment]

    @staticmethod
    def from_config(
        config: Mapping[str, Any] | None,
        preset_dir: Any = None,
        relationship: Mapping[str, Any] | None = None,
        humanize: Mapping[str, Any] | None = None,
        guard: Mapping[str, Any] | None = None,
        style: Mapping[str, Any] | None = None,
        privacy: Mapping[str, Any] | None = None,
        debounce: Mapping[str, Any] | None = None,
    ) -> "MindSettings":
        emotion_raw = dict(cfg_get(config, "emotion", {}) or {})
        # 面板上填的"专属用户 / 称呼"盖在配置之上：不懂 JSON 的人也能认人
        if relationship:
            rel = dict(emotion_raw.get("relationship") or {})
            rel.update({k: v for k, v in relationship.items() if v is not None})
            emotion_raw["relationship"] = rel
        memory_raw = dict(cfg_get(config, "memory", {}) or {})
        # 子配置里没有 enabled 时默认开启，总开关在顶层
        emotion_raw.setdefault("enabled", True)
        memory_raw.setdefault("enabled", True)

        return MindSettings(
            enabled=_b(cfg_get(config, "enabled", True), True),
            commands_enabled=_b(cfg_get(config, "commands.enabled", True), True),
            admin_only_mutations=_b(cfg_get(config, "commands.admin_only_mutations", True), True),
            debug_log=_b(cfg_get(config, "advanced.debug_log", False), False),
            cache_friendly=_b(cfg_get(config, "advanced.cache_friendly", True), True),
            maintenance_interval=_f(
                cfg_get(config, "advanced.maintenance_interval", 20), 20.0, 5.0, 600.0
            ),
            emotion=EmotionSettings.from_config(emotion_raw, preset_dir),
            memory=MemorySettings.from_config(memory_raw),
            panel=PanelSettings.from_config(config),
            images=ImagesSettings.from_config(config),
            humanize=HumanizeSettings.from_config(config, humanize),
            guard=GuardSettings.from_config(config, guard),
            style=StyleSettings.from_config(config, style),
            privacy=PrivacySettings.from_config(config, privacy),
            debounce=DebounceSettings.from_config(config, debounce),
        )


__all__ = [
    "HUMANIZE_MODE_LABELS",
    "HUMANIZE_MODES",
    "GuardSettings",
    "DebounceSettings",
    "PrivacySettings",
    "StyleSettings",
    "HumanizeSettings",
    "ImagesSettings",
    "MindSettings",
    "PanelSettings",
    "cfg_get",
]

