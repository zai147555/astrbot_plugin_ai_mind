"""情绪状态的数据模型与动力学。

设计要点
--------
1. 用 **PAD** 三维连续量描述情绪（Pleasure 愉悦 / Arousal 唤醒 / Dominance 掌控），
   比离散标签更能表达"又生气又有点想他"这类混合状态。
2. 情绪分两层：emotion（即时情绪，衰减快）+ mood（心境，衰减慢），
   两者都围绕一个长期固定的 baseline（气质基线）做指数衰减。
3. 所有衰减都按"时间差"惰性计算，不依赖定时器：
   插件重启、进程卡顿、长时间挂机都不会让情绪算错。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """把数值夹进 [low, high]。"""
    if value < low:
        return low
    if value > high:
        return high
    return value


def to_float(value: Any, default: float = 0.0) -> float:
    """宽容地把任意输入转成有限浮点数。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def now_ts() -> float:
    return time.time()


def decay_scalar(current: float, target: float, half_life: float, dt: float) -> float:
    """指数衰减：每经过 half_life 秒，与 target 的距离减半。

        v(t) = target + (v0 - target) * 0.5 ** (dt / half_life)
    """
    if dt <= 0 or half_life <= 0:
        return current
    return target + (current - target) * (0.5 ** (dt / half_life))


def humanize_seconds(seconds: float) -> str:
    """把秒数说成人话：3 分钟前 / 2 小时前 / 昨天。"""
    seconds = max(0.0, to_float(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# ---------------------------------------------------------------------------
# PAD 向量
# ---------------------------------------------------------------------------

#: 分类时各维度的权重。愉悦度最能决定"这是什么情绪"，唤醒与掌控次之。
DISTANCE_WEIGHTS = (1.0, 0.75, 0.55)


@dataclass
class PAD:
    """三维情绪向量，每一维取值范围都是 [-1, 1]。"""

    p: float = 0.0  # Pleasure  愉悦：不开心 <-> 开心
    a: float = 0.0  # Arousal   唤醒：昏昏欲睡 <-> 亢奋
    d: float = 0.0  # Dominance 掌控：被压制 <-> 占上风

    # -- 构造 ---------------------------------------------------------------
    @staticmethod
    def of(p: float = 0.0, a: float = 0.0, d: float = 0.0) -> "PAD":
        return PAD(to_float(p), to_float(a), to_float(d)).clamped()

    @staticmethod
    def from_any(value: Any) -> "PAD":
        """从 dict / list / tuple / PAD / None 构造，失败一律返回原点。"""
        if isinstance(value, PAD):
            return value.clamped()
        if isinstance(value, dict):
            return PAD.of(value.get("p"), value.get("a"), value.get("d"))
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return PAD.of(value[0], value[1], value[2])
        return PAD()

    # -- 运算 ---------------------------------------------------------------
    def clamped(self) -> "PAD":
        return PAD(clamp(self.p), clamp(self.a), clamp(self.d))

    def __add__(self, other: "PAD") -> "PAD":
        return PAD(self.p + other.p, self.a + other.a, self.d + other.d).clamped()

    def __sub__(self, other: "PAD") -> "PAD":
        return PAD(self.p - other.p, self.a - other.a, self.d - other.d).clamped()

    def __mul__(self, k: float) -> "PAD":
        return PAD(self.p * k, self.a * k, self.d * k).clamped()

    __rmul__ = __mul__

    def lerp(self, other: "PAD", t: float) -> "PAD":
        """向 other 线性靠拢（0 = 不动，1 = 完全变成 other）。"""
        t = clamp(t, 0.0, 1.0)
        return PAD(
            self.p + (other.p - self.p) * t,
            self.a + (other.a - self.a) * t,
            self.d + (other.d - self.d) * t,
        ).clamped()

    # -- 度量 ---------------------------------------------------------------
    def magnitude(self) -> float:
        """情绪强度（向量长度），范围 [0, sqrt(3)]。"""
        return math.sqrt(self.p * self.p + self.a * self.a + self.d * self.d)

    def distance_to(self, other: Any) -> float:
        o = PAD.from_any(other)
        wp, wa, wd = DISTANCE_WEIGHTS
        return math.sqrt(
            wp * (self.p - o.p) ** 2
            + wa * (self.a - o.a) ** 2
            + wd * (self.d - o.d) ** 2
        )

    def limit(self, max_magnitude: float) -> "PAD":
        """按比例缩放，使向量长度不超过 max_magnitude（保留方向）。"""
        mag = self.magnitude()
        if mag <= max_magnitude or mag <= 1e-9:
            return self.clamped()
        return (self * (max_magnitude / mag)).clamped()

    # -- 时间演化 -----------------------------------------------------------
    def decay_toward(self, target: Any, half_life: float, dt: float) -> "PAD":
        base = PAD.from_any(target)
        return PAD(
            decay_scalar(self.p, base.p, half_life, dt),
            decay_scalar(self.a, base.a, half_life, dt),
            decay_scalar(self.d, base.d, half_life, dt),
        ).clamped()

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, float]:
        return {"p": round(self.p, 4), "a": round(self.a, 4), "d": round(self.d, 4)}

    def pretty(self) -> str:
        return f"{self.p:+.2f} / {self.a:+.2f} / {self.d:+.2f}"


ORIGIN = PAD()


# ---------------------------------------------------------------------------
# 情绪原型：把连续的 PAD 映射成人能读懂的情绪词
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prototype:
    """一个情绪原型：PAD 空间里的锚点，附带"这种情绪该怎么说话"。"""

    key: str
    name: str
    emoji: str
    pad: tuple[float, float, float]
    style: str
    aliases: tuple[str, ...] = ()
    #: Plutchik 的 8 条基本情绪轴之一（见 PLUTCHIK）
    family: str = ""


#: Plutchik 情绪轮（1980）：8 条基本情绪轴，四条对立轴两两相对。
#: key -> (中文名, emoji, 对立家族)
PLUTCHIK: dict[str, tuple[str, str, str]] = {
    "joy": ("喜悦", "😊", "sadness"),
    "trust": ("信任", "🤝", "disgust"),
    "fear": ("恐惧", "😨", "anger"),
    "surprise": ("惊讶", "😲", "anticipation"),
    "sadness": ("悲伤", "😢", "joy"),
    "disgust": ("厌恶", "😖", "trust"),
    "anger": ("愤怒", "😠", "fear"),
    "anticipation": ("期待", "🤗", "surprise"),
}

#: 四条对立轴（画图用）
PLUTCHIK_AXES: tuple[tuple[str, str], ...] = (
    ("joy", "sadness"),
    ("trust", "disgust"),
    ("fear", "anger"),
    ("surprise", "anticipation"),
)

PROTOTYPES: tuple[Prototype, ...] = (
    Prototype("calm", "平静", "🙂", (0.0, 0.0, 0.0), "语气平和，淡淡的，不咸不淡就行"),
    Prototype("joy", "开心", "😊", (0.65, 0.45, 0.35), "语气轻快，愿意多聊两句，会主动接话", ("高兴", "愉快", "喜悦", "快乐", "开森"), family="joy"),
    Prototype("excited", "兴奋", "🤩", (0.70, 0.90, 0.50), "语速快、短句多、感叹号多，热情外放", ("激动", "亢奋", "上头"), family="joy"),
    Prototype("content", "满足", "😌", (0.50, -0.45, 0.35), "松弛慵懒，语气软下来", ("惬意", "安心", "舒服"), family="joy"),
    Prototype("affection", "亲昵", "🥰", (0.75, 0.15, 0.05), "想黏人，语气放软", ("喜欢", "爱意", "温柔", "甜蜜", "宠"), family="trust"),
    Prototype("proud", "得意", "😎", (0.55, 0.50, 0.80), "有点小骄傲，爱显摆", ("骄傲", "自信", "得瑟", "嚣张"), family="joy"),
    Prototype("curious", "好奇", "🤔", (0.20, 0.60, 0.05), "追问细节，问题变多", ("兴趣", "关注", "八卦"), family="anticipation"),
    Prototype("surprised", "惊讶", "😲", (0.10, 0.85, -0.35), "短促，反应快", ("震惊", "意外", "吃惊"), family="surprise"),
    Prototype("confused", "困惑", "😕", (-0.10, 0.40, -0.45), "迟疑，请求澄清", ("迷惑", "不解", "懵", "迷惑不解"), family="surprise"),
    Prototype("shy", "害羞", "😳", (0.50, 0.60, -0.50), "不好意思，想掩饰", ("羞涩", "脸红", "不好意思", "羞"), family="fear"),
    Prototype("bored", "无聊", "😐", (-0.20, -0.70, 0.00), "提不起劲，敷衍", ("没劲", "乏味", "闲"), family="disgust"),
    Prototype("tired", "疲惫", "😪", (-0.25, -0.75, -0.20), "有气无力，想结束对话", ("困", "累", "倦", "困倦"), family="disgust"),
    Prototype("lonely", "寂寞", "🌙", (-0.45, -0.25, -0.50), "有点空落落的，想被想起", ("想念", "孤单", "想他", "想你了"), family="sadness"),
    Prototype("down", "低落", "😔", (-0.55, -0.40, -0.35), "话变少，语气沉，但仍会回应", ("郁闷", "丧", "消沉", "不开心"), family="sadness"),
    Prototype("sad", "难过", "😢", (-0.80, -0.15, -0.60), "情绪外露，需要人陪", ("伤心", "悲伤", "哭"), family="sadness"),
    Prototype("hurt", "委屈", "🥺", (-0.60, 0.30, -0.75), "小声抗议，觉得自己没错", ("受伤", "被冤枉", "不甘"), family="sadness"),
    Prototype("angry", "生气", "😠", (-0.70, 0.80, 0.60), "语气冲，说短句，火药味重", ("愤怒", "火大", "气", "恼"), family="anger"),
    Prototype("annoyed", "烦躁", "😤", (-0.50, 0.65, 0.00), "不耐烦，回复变短、想催人", ("烦", "恼火", "嫌烦", "不耐烦"), family="anger"),
    Prototype("anxious", "担心", "😰", (-0.45, 0.70, -0.80), "反复确认，怕出事，容易道歉", ("不安", "焦虑", "担心", "慌", "心慌", "心疼"), family="fear"),
    Prototype("jealous", "吃醋", "😒", (-0.25, 0.55, -0.10), "阴阳怪气，旁敲侧击", ("醋意", "酸", "嫉妒"), family="anger"),
    Prototype("grateful", "感动", "🥹", (0.80, 0.40, -0.15), "真诚道谢，语气软下来", ("感谢", "触动", "暖心"), family="trust"),
    Prototype("expect", "期待", "✨", (0.45, 0.55, 0.10), "主动提议，问下次", ("盼望", "跃跃欲试"), family="anticipation"),
    Prototype("cold", "冷淡", "😶", (-0.35, -0.30, 0.30), "礼貌但疏离，回复极短", ("疏离", "冷战", "不想理"), family="disgust"),
    # ---- 更细的心情（3.0.0 新增）：都挑了跟上面不重叠的 PAD 区域，
    # classify 按距离自动挑最近的，所以加进来就会生效。
    Prototype("jealous", "吃醋", "😤", (-0.35, 0.55, -0.30), "话里带刺，非要问出个所以然", ("别人", "谁", "凭什么"), family="anger"),
    Prototype("smug", "得意", "😏", (0.55, 0.55, 0.60), "尾巴翘起来，主动邀功", ("厉害吧", "当然", "哼哼"), family="joy"),
    Prototype("helpless", "无奈", "😮‍💨", (-0.25, -0.15, -0.35), "叹口气，懒得争了", ("算了", "随你", "行吧"), family="sadness"),
    Prototype("nervous", "紧张", "😰", (-0.20, 0.75, -0.55), "说话变短、重复、试探", ("那个", "要不", "万一"), family="fear"),
    Prototype("looking_forward", "期待", "🤩", (0.40, 0.55, 0.10), "追问细节，开始安排", ("然后呢", "什么时候", "说好了"), family="anticipation"),
    Prototype("wronged", "委屈", "🥺", (-0.55, 0.30, -0.55), "声音变小，反复解释", ("我没有", "明明", "你听我说"), family="sadness"),
    Prototype("sleepy", "困倦", "😪", (0.05, -0.70, -0.25), "句子变短，开始敷衍", ("困", "睡了", "明天"), family="sadness"),
    Prototype("bored", "无聊", "🥱", (-0.15, -0.55, 0.05), "主动找事，开始挑刺", ("好无聊", "干嘛呢", "然后"), family="disgust"),
    Prototype("guilty", "心虚", "😳", (-0.20, 0.45, -0.40), "绕开话题，答非所问", ("没", "不是", "谁说的"), family="fear"),
    Prototype("moved", "感动", "🥹", (0.65, 0.40, -0.10), "嘴硬但语气软下来", ("谁要", "才不是", "谢谢"), family="trust"),
    Prototype("down", "失落", "😞", (-0.60, -0.30, -0.45), "回得少，不再追问", ("哦", "知道了", "没事"), family="sadness"),
)

PROTOTYPE_BY_KEY: dict[str, Prototype] = {p.key: p for p in PROTOTYPES}

#: 情绪别名 -> 原型 key（用于解析 <emotion>开心</emotion> 这类自表达标签）
PROTOTYPE_BY_ALIAS: dict[str, str] = {}
for _p in PROTOTYPES:
    PROTOTYPE_BY_ALIAS[_p.key] = _p.key
    PROTOTYPE_BY_ALIAS[_p.name] = _p.key
    for _alias in _p.aliases:
        PROTOTYPE_BY_ALIAS[_alias] = _p.key
del _p


def resolve_prototype(token: str) -> Prototype | None:
    """把任意情绪词解析成情绪原型。"""
    if not token:
        return None
    token = token.strip().strip("。.!！~～,， \t")
    if not token:
        return None
    key = PROTOTYPE_BY_ALIAS.get(token) or PROTOTYPE_BY_ALIAS.get(token.lower())
    if key:
        return PROTOTYPE_BY_KEY[key]
    # 退化匹配：包含关系（"有点开心" -> 开心）
    best: tuple[int, str] | None = None
    for alias, k in PROTOTYPE_BY_ALIAS.items():
        if len(alias) >= 2 and alias in token:
            if best is None or len(alias) > best[0]:
                best = (len(alias), k)
    return PROTOTYPE_BY_KEY[best[1]] if best else None


def classify(pad: PAD) -> Prototype:
    """找出离当前 PAD 最近的情绪原型。"""
    target = PAD.from_any(pad)
    best = PROTOTYPES[0]
    best_dist = float("inf")
    for proto in PROTOTYPES:
        dist = target.distance_to(PAD.from_any(proto.pad))
        if dist < best_dist:
            best, best_dist = proto, dist
    return best


#: 强度分级阈值（归一化后的向量长度）
INTENSITY_LEVELS: tuple[tuple[float, str], ...] = (
    (0.12, "几乎没有"),
    (0.30, "轻微"),
    (0.52, "明显"),
    (0.78, "强烈"),
    (9.99, "极强"),
)


def intensity_of(pad: PAD) -> float:
    """把向量长度归一化成 [0, 1] 的强度。"""
    return clamp(pad.magnitude() / 1.35, 0.0, 1.0)


def intensity_level(pad: PAD) -> tuple[int, str]:
    """返回 (1~5 的等级, 文字描述)。"""
    value = intensity_of(pad)
    for index, (threshold, label) in enumerate(INTENSITY_LEVELS, start=1):
        if value < threshold:
            return index, label
    return 5, INTENSITY_LEVELS[-1][1]


# ---------------------------------------------------------------------------
# 人际关系（好感度 / 熟悉度）
# ---------------------------------------------------------------------------

#: (下限, 称号, 图标) —— 好感度等级表，从高到低匹配
AFFINITY_TIERS: tuple[tuple[float, str, str], ...] = (
    (93.0, "特别的人", "💖"),
    (80.0, "挚友", "💛"),
    (65.0, "好友", "🤝"),
    (45.0, "朋友", "😊"),
    (25.0, "相识", "🙂"),
    (10.0, "眼熟", "👋"),
    (-999.0, "陌生人", "🌫"),
)


def affinity_tier(affinity: float) -> tuple[str, str]:
    """把好感度换算成称号与图标。"""
    for floor, title, icon in AFFINITY_TIERS:
        if affinity >= floor:
            return title, icon
    return "陌生人", "🌫"


def familiarity_title(familiarity: float) -> str:
    if familiarity >= 70:
        return "默契"
    if familiarity >= 45:
        return "熟络"
    if familiarity >= 20:
        return "熟悉"
    if familiarity >= 5:
        return "有点熟"
    return "生疏"


@dataclass
class Relation:
    """插件对某一个用户的长期关系记忆。"""

    uid: str = ""
    name: str = ""
    affinity: float = 5.0      # 好感度，-100 ~ 100
    familiarity: float = 0.0   # 熟悉度，0 ~ 100，只增不减
    msg_count: int = 0
    last_seen: float = 0.0
    special: bool = False      # 是否为"专属"用户

    def touch(self, now: float, familiarity_gain: float = 1.0) -> None:
        self.msg_count += 1
        self.last_seen = now
        self.familiarity = clamp(self.familiarity + familiarity_gain, 0.0, 100.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "affinity": round(self.affinity, 3),
            "familiarity": round(self.familiarity, 2),
            "msg_count": self.msg_count,
            "last_seen": round(self.last_seen, 2),
            "special": self.special,
        }

    @staticmethod
    def from_dict(data: Any) -> "Relation":
        if not isinstance(data, dict):
            return Relation()
        return Relation(
            uid=str(data.get("uid", "") or ""),
            name=str(data.get("name", "") or ""),
            affinity=to_float(data.get("affinity"), 5.0),
            familiarity=to_float(data.get("familiarity"), 0.0),
            msg_count=to_int(data.get("msg_count"), 0),
            last_seen=to_float(data.get("last_seen"), 0.0),
            special=to_bool(data.get("special"), False),
        )

