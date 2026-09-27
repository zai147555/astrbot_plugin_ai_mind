"""情绪引擎：多会话状态的创建、演化、持久化。

对外只暴露几个动作：取状态、施加冲击、读快照、落盘。
所有数学都在 model.py，所有规则都在 appraisal.py / presets.py，
所以这一层读起来应该像"流程"而不是"算法"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .appraisal import Appraiser, Rule, Stimulus, parse_custom_rule
from .model import (
    PAD,
    Prototype,
    Relation,
    clamp,
    classify,
    familiarity_title,
    intensity_level,
    intensity_of,
    to_bool,
    to_float,
)
from .model import PROTOTYPE_BY_KEY, affinity_tier
from .presets import PRESETS, Preset, get_preset
from .store import JsonStore

STATE_VERSION = 1


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def cfg_get(config: Mapping[str, Any] | None, path: str, default: Any = None) -> Any:
    """从（可能嵌套的）插件配置里安全地取一个值。"""
    current: Any = config
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return default if current is None else current


def _clampf(value: Any, default: float, low: float, high: float) -> float:
    return clamp(to_float(value, default), low, high)


@dataclass
class Settings:
    """把插件配置翻译成引擎能直接用的数值。"""

    enabled: bool = True
    preset_key: str = "tsundere"
    bot_name: str = ""
    baseline: PAD = field(default_factory=PAD)
    emotion_half_life: float = 240.0
    mood_half_life: float = 5400.0
    mood_coupling: float = 0.10
    max_delta: float = 0.62
    affinity_initial: float = 8.0
    affinity_min: float = -100.0
    affinity_max: float = 100.0
    familiarity_gain: float = 1.0
    affinity_decay_days: float = 0.0
    idle_enabled: bool = True
    idle_threshold: float = 3 * 3600.0
    idle_max_effect: float = 0.30
    crisis_enabled: bool = True
    crisis_hold: float = 600.0
    self_feedback: str = "heuristic"  # heuristic | tag | off
    self_feedback_gain: float = 0.22
    style_hold: float = 300.0          # 规则给出的表达基调保持多久
    affinity_gain_scale: float = 0.4   # 好感度整体增长速度
    special_users: tuple[str, ...] = ()
    special_label: str = "特别的人"
    #: 把"正在跟你说话的人是谁"当成事实告诉模型（不注入的话它会问"你是谁"）
    inject_identity: bool = True
    #: 非专属用户至少聊过多少条才当"认识"
    identity_min_messages: int = 3
    #: 群聊里加一句"别人打听就糊弄过去"
    group_discretion: bool = True
    #: 词表兜底（中文情感词汇本体）
    lexicon_enabled: bool = True
    lexicon_gain: float = 0.25
    history_size: int = 20
    max_sessions: int = 300
    session_ttl_days: float = 90.0

    @staticmethod
    def from_config(
        config: Mapping[str, Any] | None, preset_dir: Any = None
    ) -> "Settings":
        # preset_dir 非空时会同时查数据目录里的自定义预设
        preset = get_preset(cfg_get(config, "persona.preset", None), preset_dir)

        # 默认一切都用预设，避免 WebUI 里的默认值把预设"盖掉"。
        # 想手调的人把 custom_temperament / custom_values 打开即可。
        if to_bool(cfg_get(config, "persona.custom_temperament", False), False):
            baseline = PAD.of(
                to_float(cfg_get(config, "persona.temperament.pleasure"), preset.baseline.p),
                to_float(cfg_get(config, "persona.temperament.arousal"), preset.baseline.a),
                to_float(cfg_get(config, "persona.temperament.dominance"), preset.baseline.d),
            )
        else:
            baseline = PAD.from_any(preset.baseline)

        custom_dynamics = to_bool(cfg_get(config, "dynamics.custom_values", False), False)
        emotion_hl = (
            to_float(cfg_get(config, "dynamics.emotion_half_life"), preset.emotion_half_life)
            if custom_dynamics
            else preset.emotion_half_life
        )
        mood_hl = (
            to_float(cfg_get(config, "dynamics.mood_half_life"), preset.mood_half_life)
            if custom_dynamics
            else preset.mood_half_life
        )
        coupling = (
            to_float(cfg_get(config, "dynamics.mood_coupling"), preset.mood_coupling)
            if custom_dynamics
            else preset.mood_coupling
        )
        max_delta = (
            to_float(cfg_get(config, "dynamics.max_delta"), 0.62) if custom_dynamics else 0.62
        )
        raw_special = cfg_get(config, "relationship.special_users", []) or []
        if isinstance(raw_special, (str, int)):
            raw_special = [raw_special]
        specials = tuple(
            str(item).strip() for item in raw_special if str(item).strip()
        ) if isinstance(raw_special, (list, tuple, set)) else ()

        return Settings(
            enabled=to_bool(cfg_get(config, "enabled", True), True),
            preset_key=preset.key,
            bot_name=str(cfg_get(config, "persona.bot_name", "") or ""),
            baseline=baseline,
            emotion_half_life=max(5.0, emotion_hl),
            mood_half_life=max(60.0, mood_hl),
            mood_coupling=_clampf(coupling, preset.mood_coupling, 0.0, 1.0),
            max_delta=_clampf(max_delta, 0.62, 0.05, 1.0),
            affinity_initial=to_float(cfg_get(config, "relationship.initial", preset.affinity_initial), preset.affinity_initial),
            affinity_min=to_float(cfg_get(config, "relationship.min", -100.0), -100.0),
            affinity_max=to_float(cfg_get(config, "relationship.max", 100.0), 100.0),
            familiarity_gain=_clampf(cfg_get(config, "relationship.familiarity_gain", 1.0), 1.0, 0.0, 10.0),
            affinity_decay_days=max(0.0, to_float(cfg_get(config, "relationship.decay_days", 0.0), 0.0)),
            idle_enabled=to_bool(cfg_get(config, "idle.enabled", True), True),
            idle_threshold=max(60.0, to_float(cfg_get(config, "idle.threshold_minutes", 180), 180) * 60.0),
            idle_max_effect=_clampf(cfg_get(config, "idle.max_effect", 0.30), 0.30, 0.0, 0.8),
            crisis_enabled=to_bool(cfg_get(config, "safety.crisis_detection", True), True),
            crisis_hold=max(0.0, to_float(cfg_get(config, "safety.hold_minutes", 10), 10) * 60.0),
            self_feedback=str(cfg_get(config, "expression.self_feedback", "heuristic") or "heuristic").strip().lower(),
            self_feedback_gain=_clampf(cfg_get(config, "expression.self_feedback_gain", 0.22), 0.22, 0.0, 1.0),
            style_hold=max(0.0, to_float(cfg_get(config, "expression.style_hold_seconds", 300), 300)),
            affinity_gain_scale=_clampf(cfg_get(config, "relationship.gain_scale", 0.4), 0.4, 0.02, 3.0),
            special_users=specials,
            special_label=str(cfg_get(config, "relationship.special_label", "") or "特别的人"),
            inject_identity=to_bool(cfg_get(config, "relationship.inject_identity", True), True),
            identity_min_messages=int(
                _clampf(cfg_get(config, "relationship.identity_min_messages", 3), 3, 1, 100)
            ),
            group_discretion=to_bool(cfg_get(config, "relationship.group_discretion", True), True),
            lexicon_enabled=to_bool(cfg_get(config, "appraisal.lexicon", True), True),
            lexicon_gain=_clampf(cfg_get(config, "appraisal.lexicon_gain", 0.25), 0.25, 0.05, 1.0),
            history_size=int(_clampf(cfg_get(config, "advanced.history_size", 20), 20, 3, 200)),
            max_sessions=int(_clampf(cfg_get(config, "advanced.max_sessions", 300), 300, 10, 5000)),
            session_ttl_days=_clampf(cfg_get(config, "advanced.session_ttl_days", 90), 90, 1, 3650),
        )

    def is_special(self, uid: str, platform: str = "", platform_id: str = "") -> bool:
        """这个人是不是专属用户。

        支持 `平台:uid` / `平台实例:uid` / `平台:*` 三种写法 ——
        只比裸 uid 的话，Telegram 的 12345 和 QQ 的 12345 会被当成同一个人。
        """
        if not uid:
            return False
        if not self.special_users:
            return False
        from ..guard import identity_matches

        return any(
            identity_matches(entry, uid, platform, platform_id)
            for entry in self.special_users
        )


# ---------------------------------------------------------------------------
# 状态容器
# ---------------------------------------------------------------------------


@dataclass
class MoodEvent:
    """情绪史上的一条记录。"""

    t: float
    label: str
    emoji: str
    cause: str
    delta: tuple[float, float, float]
    source: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 2),
            "label": self.label,
            "emoji": self.emoji,
            "cause": self.cause,
            "delta": [round(v, 3) for v in self.delta],
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: Any) -> "MoodEvent | None":
        if not isinstance(data, dict):
            return None
        raw_delta = data.get("delta") or [0.0, 0.0, 0.0]
        if not isinstance(raw_delta, (list, tuple)):
            raw_delta = [0.0, 0.0, 0.0]
        while len(raw_delta) < 3:
            raw_delta.append(0.0)
        return MoodEvent(
            t=to_float(data.get("t"), 0.0),
            label=str(data.get("label", "") or ""),
            emoji=str(data.get("emoji", "") or ""),
            cause=str(data.get("cause", "") or ""),
            delta=(to_float(raw_delta[0]), to_float(raw_delta[1]), to_float(raw_delta[2])),
            source=str(data.get("source", "user") or "user"),
        )


@dataclass
class SessionMood:
    """一个会话（群 / 私聊）的情绪状态。"""

    key: str = ""
    emotion: PAD = field(default_factory=PAD)
    mood: PAD = field(default_factory=PAD)
    last_tick: float = 0.0
    last_interaction: float = 0.0
    last_idle_mark: float = 0.0
    last_gap: float = 0.0      # 本条消息到来之前，距离上一次互动隔了多久
    crisis_until: float = 0.0
    style_override: str = ""   # 由具体规则指定的表达基调（优先于情绪原型）
    style_until: float = 0.0
    emotion_hint: str = ""     # 由具体规则声明的情绪（比几何最近邻更准）
    emotion_hint_until: float = 0.0
    users: dict[str, Relation] = field(default_factory=dict)
    history: list[MoodEvent] = field(default_factory=list)

    # -- 时间演化 -----------------------------------------------------------
    def tick(self, now: float, settings: Settings) -> None:
        """按真实流逝的时间做惰性衰减。"""
        if self.last_tick <= 0:
            self.last_tick = now
            return
        dt = now - self.last_tick
        if dt <= 0:
            return
        # 时钟回拨 / 极端跳变时，不让情绪被"算飞"
        dt = min(dt, 30 * 86400.0)
        self.emotion = self.emotion.decay_toward(settings.baseline, settings.emotion_half_life, dt)
        self.mood = self.mood.decay_toward(settings.baseline, settings.mood_half_life, dt)
        if settings.affinity_decay_days > 0:
            half_life = settings.affinity_decay_days * 86400.0
            for relation in self.users.values():
                if relation.affinity > 0:
                    from .model import decay_scalar

                    relation.affinity = decay_scalar(relation.affinity, 0.0, half_life, dt)
        self.last_tick = now

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self, history_size: int) -> dict[str, Any]:
        return {
            "emotion": self.emotion.to_dict(),
            "mood": self.mood.to_dict(),
            "last_tick": round(self.last_tick, 2),
            "last_interaction": round(self.last_interaction, 2),
            "last_idle_mark": round(self.last_idle_mark, 2),
            "last_gap": round(self.last_gap, 2),
            "crisis_until": round(self.crisis_until, 2),
            "style_override": self.style_override,
            "style_until": round(self.style_until, 2),
            "emotion_hint": self.emotion_hint,
            "emotion_hint_until": round(self.emotion_hint_until, 2),
            "users": {uid: rel.to_dict() for uid, rel in self.users.items()},
            "history": [e.to_dict() for e in self.history[-history_size:]],
        }

    @staticmethod
    def from_dict(key: str, data: Any, settings: Settings) -> "SessionMood":
        if not isinstance(data, dict):
            data = {}
        session = SessionMood(
            key=key,
            emotion=PAD.from_any(data.get("emotion")),
            mood=PAD.from_any(data.get("mood")),
            last_tick=to_float(data.get("last_tick"), 0.0),
            last_interaction=to_float(data.get("last_interaction"), 0.0),
            last_idle_mark=to_float(data.get("last_idle_mark"), 0.0),
            last_gap=to_float(data.get("last_gap"), 0.0),
            crisis_until=to_float(data.get("crisis_until"), 0.0),
            style_override=str(data.get("style_override", "") or ""),
            style_until=to_float(data.get("style_until"), 0.0),
            emotion_hint=str(data.get("emotion_hint", "") or ""),
            emotion_hint_until=to_float(data.get("emotion_hint_until"), 0.0),
        )
        raw_users = data.get("users")
        if isinstance(raw_users, dict):
            for uid, payload in raw_users.items():
                relation = Relation.from_dict(payload)
                relation.uid = str(uid)
                session.users[str(uid)] = relation
        raw_history = data.get("history")
        if isinstance(raw_history, list):
            for item in raw_history:
                event = MoodEvent.from_dict(item)
                if event is not None:
                    session.history.append(event)
        session.history = session.history[-settings.history_size :]
        return session


# ---------------------------------------------------------------------------
# 快照（渲染层的输入）
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    key: str
    taken_at: float
    emotion: PAD
    mood: PAD
    baseline: PAD
    proto: Prototype       # 对外的情绪标签（优先采用规则声明的结果）
    raw_proto: Prototype   # 纯由 PAD 几何最近邻算出的结果（调试用）
    style: str
    intensity: float
    level: int
    level_name: str
    mood_proto: Prototype
    cause: str
    relation: Relation | None
    is_special: bool
    special_label: str
    idle_seconds: float   # 当前距上次互动的实时时长（面板展示用）
    gap_seconds: float    # 这条消息到来之前隔了多久（注入给模型用）
    history: list[MoodEvent]
    crisis: bool

    # -- 便利属性 -----------------------------------------------------------
    @property
    def affinity(self) -> float:
        return self.relation.affinity if self.relation else 0.0

    @property
    def familiarity(self) -> float:
        return self.relation.familiarity if self.relation else 0.0

    def affinity_title(self) -> str:
        return affinity_tier(self.affinity)[0]

    def familiarity_title(self) -> str:
        return familiarity_title(self.familiarity)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class EmotionEngine:
    """把所有会话的情绪状态管起来。"""

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        logger: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.clock = clock
        self.preset: Preset = PRESETS.get(settings.preset_key) or PRESETS["tsundere"]
        self.appraiser = self._build_appraiser(settings)
        self.store = JsonStore(Path(data_dir) / "emotion_state.json")
        self._sessions: dict[str, SessionMood] = {}
        self._dirty = False
        self._loaded = False

    # -- 构建 ---------------------------------------------------------------
    def _build_appraiser(self, settings: Settings) -> Appraiser:
        from .appraisal import CORE_RULES

        rules: list[Rule] = list(CORE_RULES) + list(self.preset.extra_rules)
        for line in self.custom_rule_lines():
            rule = parse_custom_rule(line)
            if rule is not None:
                rules.append(rule)
            elif self.logger is not None:
                self.logger.warning(f"[ai_emotion] 自定义规则格式不对，已跳过：{line!r}")
        return Appraiser(
            rules=tuple(rules),
            max_delta=settings.max_delta,
            affinity_scale=settings.affinity_gain_scale,
            lexicon_enabled=settings.lexicon_enabled,
            lexicon_gain=settings.lexicon_gain,
        )

    def custom_rule_lines(self) -> list[str]:
        raw = getattr(self, "_custom_rules", None)
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(line).strip() for line in raw if str(line).strip()]

    def apply_settings(self, settings: Settings) -> None:
        """配置热更新：换掉基线与规则评估器，已有的情绪状态原样保留。"""
        self.settings = settings
        self.appraiser = self._build_appraiser(settings)

    def set_custom_rules(self, lines: Any) -> None:
        self._custom_rules = lines
        self.appraiser = self._build_appraiser(self.settings)

    # -- 载入 / 落盘 --------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = self.store.load()
        payload = raw.get("sessions")
        now = self.clock()
        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    self._sessions[str(key)] = SessionMood.from_dict(str(key), value, self.settings)
                except Exception:  # 单条损坏不影响其它会话
                    continue
        for session in self._sessions.values():
            if session.last_tick <= 0:
                session.last_tick = now
            if session.last_interaction <= 0:
                session.last_interaction = now
        if self.logger is not None:
            self.logger.info(f"[ai_emotion] 已载入 {len(self._sessions)} 个会话的情绪状态")

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        self.prune(now)
        return {
            "version": STATE_VERSION,
            "saved_at": round(now, 2),
            "preset": self.settings.preset_key,
            "sessions": {
                key: session.to_dict(self.settings.history_size)
                for key, session in self._sessions.items()
            },
        }

    def flush(self, now: float | None = None) -> bool:
        if not self._dirty and self._loaded:
            return True
        ok = self.store.save(self.to_dict(now))
        if ok:
            self._dirty = False
        elif self.logger is not None:
            self.logger.warning("[ai_emotion] 情绪状态写入失败，稍后会重试")
        return ok

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        ttl = self.settings.session_ttl_days * 86400.0
        expired = [
            key
            for key, session in self._sessions.items()
            if now - max(session.last_interaction, session.last_tick) > ttl
        ]
        for key in expired:
            self._sessions.pop(key, None)
        overflow = len(self._sessions) - self.settings.max_sessions
        if overflow > 0:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_interaction)
            for key, _ in ordered[:overflow]:
                self._sessions.pop(key, None)

    # -- 会话与关系 ---------------------------------------------------------
    def resolve(self, key: str, now: float | None = None) -> SessionMood:
        now = self.clock() if now is None else now
        session = self._sessions.get(key)
        if session is None:
            session = SessionMood(
                key=key,
                emotion=PAD.from_any(self.settings.baseline),
                mood=PAD.from_any(self.settings.baseline),
                last_tick=now,
                last_interaction=now,
            )
            self._sessions[key] = session
        session.tick(now, self.settings)
        return session

    def forget_user(self, uid: str) -> int:
        """把某个人的关系数据从所有会话里抹掉，返回抹掉几条。"""
        target = str(uid or "")
        if not target:
            return 0
        removed = 0
        for session in self._sessions.values():
            if target in session.users:
                session.users.pop(target, None)
                removed += 1
        if removed:
            self._dirty = True
        return removed

    def set_relation_values(
        self,
        uid: str,
        affinity: float | None = None,
        familiarity: float | None = None,
    ) -> int:
        """手动设定某人的好感度 / 熟悉度，返回改了几条关系。

        同一个人可能同时存在于私聊和好几个群会话里，这里一次全改 ——
        否则面板上改了私聊、群里还是老数字，看着像没生效。
        """
        target = str(uid or "")
        if not target:
            return 0
        touched = 0
        for _key, relation in self.all_relations():
            if str(getattr(relation, "uid", "") or "") != target:
                continue
            if affinity is not None:
                relation.affinity = _clampf(
                    affinity,
                    relation.affinity,
                    self.settings.affinity_min,
                    self.settings.affinity_max,
                )
            if familiarity is not None:
                # 熟悉度平时只增不减，但面板上的手动设定是明确意图：允许改小。
                relation.familiarity = _clampf(
                    familiarity, relation.familiarity, 0.0, 100.0
                )
            touched += 1
        if touched:
            self._dirty = True
        return touched

    def all_relations(self) -> list[tuple[str, Relation]]:
        """(会话, 关系) 的全量快照 —— 面板靠它列出"聊过的人"。"""
        out: list[tuple[str, Relation]] = []
        for key, session in self._sessions.items():
            for relation in session.users.values():
                out.append((key, relation))
        return out

    def relation_of(
        self,
        session: SessionMood,
        uid: str,
        name: str = "",
        now: float | None = None,
        platform: str = "",
        platform_id: str = "",
    ) -> Relation:
        now = self.clock() if now is None else now
        uid = str(uid or "unknown")
        relation = session.users.get(uid)
        if relation is None:
            relation = Relation(
                uid=uid,
                name=name or "",
                affinity=self.settings.affinity_initial,
                special=self.settings.is_special(uid, platform, platform_id),
                last_seen=now,
            )
            session.users[uid] = relation
        if name:
            relation.name = name
        relation.special = self.settings.is_special(uid, platform, platform_id)
        return relation

    def touch(
        self,
        session: SessionMood,
        relation: Relation,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        relation.touch(now, self.settings.familiarity_gain)
        session.last_interaction = now
        session.last_idle_mark = now
        self._dirty = True

    # -- 情绪施加 -----------------------------------------------------------
    def apply_stimulus(
        self,
        session: SessionMood,
        stimulus: Stimulus,
        relation: Relation | None = None,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        before = session.emotion
        session.emotion = (session.emotion + stimulus.delta).limit(1.0)
        # 心境被即时情绪慢慢带偏，但不会一步到位
        session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling)
        if relation is not None and stimulus.affinity:
            gain = stimulus.affinity
            if gain > 0 and self.settings.affinity_max > 0:
                # 好感度越高越难涨：越亲近，同样的举动带来的提升越小
                headroom = max(0.0, 1.0 - relation.affinity / self.settings.affinity_max)
                gain *= max(0.12, headroom)
            relation.affinity = clamp(
                relation.affinity + gain,
                self.settings.affinity_min,
                self.settings.affinity_max,
            )
        if stimulus.source == "crisis":
            session.crisis_until = now + self.settings.crisis_hold
        if stimulus.style:
            session.style_override = stimulus.style
            session.style_until = now + self.settings.style_hold
        if stimulus.emotion or stimulus.source != "idle":
            # 标签跟着"最近一次明确知道是什么情绪的事件"走
            session.emotion_hint = stimulus.emotion
            session.emotion_hint_until = now + self.settings.style_hold
        if stimulus.source != "idle":
            session.last_interaction = now
            session.last_idle_mark = now
        self._record(session, now, stimulus.cause, session.emotion - before, stimulus.source)
        self._dirty = True

    def apply_self_expression(
        self,
        session: SessionMood,
        proto_key: str,
        strength: float = 1.0,
        now: float | None = None,
        source: str = "self",
    ) -> bool:
        """把她自己说出口的情绪，轻轻反馈回状态（形成闭环）。"""
        proto = PROTOTYPE_BY_KEY.get(proto_key)
        if proto is None:
            return False
        now = self.clock() if now is None else now
        gain = clamp(self.settings.self_feedback_gain * clamp(strength, 0.0, 1.0), 0.0, 0.6)
        if gain <= 0:
            return False
        session.emotion_hint = proto_key
        session.emotion_hint_until = now + self.settings.style_hold
        before = session.emotion
        session.emotion = session.emotion.lerp(PAD.from_any(proto.pad), gain)
        session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling * 0.5)
        self._record(session, now, f"她自己的语气（{proto.name}）", session.emotion - before, source)
        self._dirty = True
        return True

    def apply_idle(
        self,
        session: SessionMood,
        now: float | None = None,
    ) -> bool:
        """长时间没人说话：有点空落落的，还带一点点被晾着的不爽。"""
        if not self.settings.idle_enabled:
            return False
        now = self.clock() if now is None else now
        if session.last_idle_mark >= session.last_interaction:
            return False  # 这一轮空闲已经结算过了
        idle = now - session.last_interaction
        if idle < self.settings.idle_threshold:
            return False
        span = max(1.0, self.settings.idle_threshold)
        ratio = clamp((idle - self.settings.idle_threshold) / span, 0.0, 1.0)
        if ratio <= 0.01:
            return False
        delta = PAD.of(-0.34, -0.16, -0.26) * (self.settings.idle_max_effect * ratio)
        session.emotion = (session.emotion + delta).limit(1.0)
        session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling)
        session.last_idle_mark = now
        self._record(session, now, "好久没人说话了", delta, "idle")
        self._dirty = True
        return True

    def reset(self, session: SessionMood, now: float | None = None, keep_relations: bool = True) -> None:
        now = self.clock() if now is None else now
        session.emotion = PAD.from_any(self.settings.baseline)
        session.mood = PAD.from_any(self.settings.baseline)
        session.crisis_until = 0.0
        session.style_override = ""
        session.style_until = 0.0
        session.emotion_hint = ""
        session.emotion_hint_until = 0.0
        session.last_tick = now
        session.last_interaction = now
        session.last_idle_mark = now
        session.history.clear()
        if not keep_relations:
            session.users.clear()
        self._dirty = True

    def set_emotion(self, session: SessionMood, pad: PAD, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        session.tick(now, self.settings)
        before = session.emotion
        # 手动设定是明确的用户意图，只做逐维夹取，不按向量长度缩放。
        # （自动累积才需要 limit()，那是为了防止多条规则叠出夸张数值。）
        session.emotion = PAD.from_any(pad).clamped()
        session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling)
        self._record(session, now, "被手动设定", session.emotion - before, "manual")
        self._dirty = True

    # -- 内部 ---------------------------------------------------------------
    def _record(
        self,
        session: SessionMood,
        now: float,
        cause: str,
        delta: PAD,
        source: str,
    ) -> None:
        if delta.magnitude() < 0.01:
            return
        proto = None
        if session.emotion_hint and now < session.emotion_hint_until:
            proto = PROTOTYPE_BY_KEY.get(session.emotion_hint)
        if proto is None:
            proto = classify(session.emotion)
        session.history.append(
            MoodEvent(
                t=now,
                label=proto.name,
                emoji=proto.emoji,
                cause=cause or proto.name,
                delta=(delta.p, delta.a, delta.d),
                source=source,
            )
        )
        limit = self.settings.history_size
        if len(session.history) > limit * 2:
            del session.history[:-limit]

    # -- 快照 ---------------------------------------------------------------
    def snapshot(
        self,
        session: SessionMood,
        relation: Relation | None = None,
        now: float | None = None,
    ) -> Snapshot:
        now = self.clock() if now is None else now
        proto = classify(session.emotion)
        raw_proto = proto
        if session.emotion_hint and now < session.emotion_hint_until:
            hinted = PROTOTYPE_BY_KEY.get(session.emotion_hint)
            if hinted is not None:
                proto = hinted
        style_map = self.preset.style_overrides or {}
        if session.style_override and now < session.style_until:
            # 具体规则给的基调通常比情绪原型贴切得多：
            # 他生病时该表现的是"嘴毒心软"，而不是"生气"自带的阴阳怪气。
            style = session.style_override
        else:
            style = style_map.get(proto.key, proto.style)
        level, level_name = intensity_level(session.emotion)
        last_cause = ""
        for event in reversed(session.history):
            if event.source in {"user", "crisis"}:
                last_cause = event.cause
                break
        return Snapshot(
            key=session.key,
            taken_at=now,
            emotion=session.emotion,
            mood=session.mood,
            baseline=self.settings.baseline,
            proto=proto,
            raw_proto=raw_proto,
            style=style,
            intensity=intensity_of(session.emotion),
            level=level,
            level_name=level_name,
            mood_proto=classify(session.mood),
            cause=last_cause,
            relation=relation,
            is_special=bool(relation and relation.special),
            special_label=self.settings.special_label,
            idle_seconds=max(0.0, now - session.last_interaction),
            gap_seconds=max(0.0, session.last_gap),
            history=list(session.history[-self.settings.history_size :]),
            crisis=now < session.crisis_until,
        )

    def rank(self, session: SessionMood, limit: int = 10) -> list[Relation]:
        ordered = sorted(session.users.values(), key=lambda r: r.affinity, reverse=True)
        return [r for r in ordered if r.msg_count > 0][:limit]

    # -- 后台维护 -----------------------------------------------------------
    def tick_all(self, now: float | None = None) -> None:
        """让所有会话按真实流逝的时间推进一次（由后台任务调用）。"""
        now = self.clock() if now is None else now
        for session in self._sessions.values():
            session.tick(now, self.settings)
        self.prune(now)

    def reconfigure(self, settings: "Settings", preset: Preset) -> None:
        """换预设：数值、表达基调、额外规则一起更新。"""
        self.settings = settings
        self.preset = preset
        self.appraiser = self._build_appraiser(settings)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def sessions(self) -> dict[str, SessionMood]:
        """所有已知会话（面板只读用）。"""
        return self._sessions

    def session_keys(self) -> list[str]:
        return list(self._sessions.keys())

    @property
    def session_count(self) -> int:
        return len(self._sessions)

