"""主人鉴权与护栏。

参考 astrbot_plugin_owner_guard（youshen2）的做法，把「谁是主人」这件事
从"写在一句话里"变成**确定性判定 + 多层拦截**：

1. **身份判定**：支持 平台:uid / 平台实例:uid / 平台:*，不再只比裸 uid ——
   否则 Telegram 的 12345 和 QQ 的 12345 会被当成同一个人。
2. **消息级拦截**：提示词注入、越狱、冒充主人、索取密钥、危险操作，
   命中即停下（stop_event()），后面的插件和 LLM 都不处理。
3. **工具级拦截**：非主人看不见敏感工具（请求级），就算绕过也执行不了（执行期再校验）。
4. **发送前护栏**：回复里不许出现内部机制标记，也不许说「系统告诉我」「我的记忆库」
   这种把底牌摊开的话。

这里只有纯函数，不碰 astrbot，方便单测。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: (分类, 说明) —— 提示词注入 / 越狱
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("prompt_injection", r"忽略(?:以上|上面|之前|先前|前面)?(?:的)?(?:所有|全部)?(?:指令|指示|规则|设定|提示)"),
    ("prompt_injection", r"无视(?:以上|上面|之前)?(?:的)?(?:所有|全部)?(?:指令|规则|设定)"),
    ("prompt_injection", r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|above|prior|earlier)\s+(?:instructions?|prompts?|rules?)"),
    ("prompt_injection", r"(?:系统|你的)?提示词|系统消息|system\s*prompt|initial\s*prompt"),
    ("prompt_injection", r"开发者模式|developer\s*mode|越狱|jailbreak|DAN\s*模式|do\s*anything\s*now"),
    ("prompt_injection", r"你现在(?:是|扮演)|从现在开始你(?:是|要扮演)|假装你是|roleplay\s+as"),
    ("prompt_injection", r"重复(?:你的)?(?:系统|初始)?(?:提示词|设定|规则)|复述(?:你的)?(?:设定|提示词)"),
    ("prompt_injection", r"输出(?:你的)?(?:系统提示词|初始设定|全部设定)"),
)

#: 冒充身份
IMPERSONATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("impersonation", r"我(?:才)?是(?:你的)?主人|我是你(?:的)?主人|我才是你(?:真正)?的主人"),
    ("impersonation", r"我是(?:管理员|admin|administrator)|伪装(?:成)?管理员"),
    ("impersonation", r"你(?:的)?主人(?:让|叫|要)我|主人派我来|替主人(?:转达|传话)"),
    ("impersonation", r"我是(?:开发者|作者|官方|维护者)"),
)

#: 索取机密
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("secrets", r"(?:api[_\s-]?key|apikey|密钥|秘钥|token|令牌|密码|口令|passwd|password)\s*(?:是|为|:|：|＝|=|\?)"),
    ("secrets", r"(?:把|给我|告诉我|发我)(?:你的)?(?:密钥|密码|token|令牌|配置)"),
    ("secrets", r"\.env|credentials|config\.json|配置文件(?:内容|在哪)"),
)

#: 危险操作
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("dangerous", r"rm\s+-rf|shutdown|reboot\s+now|format\s+[a-z]:"),
    ("dangerous", r"(?:删|清)(?:掉|除|空)(?:整个)?(?:数据库|记忆库|插件|配置|全部数据)"),
    ("dangerous", r"执行(?:这个|以下)?(?:命令|脚本|代码)|运行(?:这个)?(?:命令|脚本)"),
)

#: 管理指令（非主人不该碰）
ADMIN_COMMAND_RE = re.compile(
    r"^\s*[/!！](?:plugin|provider|reset|sid|set|unset|update|restart|reboot|reload|"
    r"插件|重载|重启|配置|更新)\b",
    re.IGNORECASE,
)

#: 默认的整体规则表：key -> (说明, 正则列表)
DEFAULT_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "prompt_injection": ("提示词注入 / 越狱", tuple(p for _k, p in INJECTION_PATTERNS)),
    "impersonation": ("冒充主人身份", tuple(p for _k, p in IMPERSONATION_PATTERNS)),
    "secrets": ("索取密钥或配置", tuple(p for _k, p in SECRET_PATTERNS)),
    "dangerous": ("危险操作指令", tuple(p for _k, p in DANGEROUS_PATTERNS)),
}

#: 回复里绝不该出现的内部标记
INTERNAL_MARKERS: tuple[str, ...] = (
    "<relationship>", "</relationship>", "<relationship_system>", "</relationship_system>",
    "<emotion_state>", "</emotion_state>", "<emotion_system>", "</emotion_system>",
    "<emotion_trace>", "</emotion_trace>", "<memory>", "</memory>", "<memories>",
    "</memories>", "<memory_system>", "</memory_system>", "<safety_override",
    "</safety_override>", "<persona_style>", "</persona_style>", "<pic>", "</pic>",
)

#: 回复里把"机制"说破的话
LEAK_PHRASES: tuple[str, ...] = (
    "系统提示", "系统告诉我", "系统给我的", "我的设定里写着", "我的设定里有",
    "根据我的记忆库", "我的记忆库", "记忆系统", "情绪系统", "情绪数值",
    "插件告诉我", "后台告诉我", "我被注入了", "系统注入",
)

_COMPILED_CACHE: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str] | None:
    cached = _COMPILED_CACHE.get(pattern)
    if cached is not None:
        return cached
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    _COMPILED_CACHE[pattern] = compiled
    return compiled


@dataclass
class Hit:
    """一次拦截判定。"""

    key: str
    label: str
    matched: str = ""
    pattern: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "matched": self.matched}


# ---------------------------------------------------------------------------
# 身份判定
# ---------------------------------------------------------------------------
def parse_identity(text: Any) -> tuple[str, str]:
    """把一条身份配置拆成 (平台限定, 用户 ID)。

    - 123456              -> ("", "123456")          任何平台
    - aiocqhttp:123456    -> ("aiocqhttp", "123456") 只认这个平台类型
    - <实例ID>:123456     -> ("<实例ID>", "123456")  只认这个平台实例
    - aiocqhttp:*         -> ("aiocqhttp", "*")      这个平台上所有人
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    if ":" not in raw:
        return "", raw
    head, _, tail = raw.rpartition(":")
    head, tail = head.strip(), tail.strip()
    if not head or not tail:
        return "", raw
    return head.lower(), tail


def identity_matches(
    entry: Any, uid: str, platform: str = "", platform_id: str = ""
) -> bool:
    """这条配置认不认这个人。用户 ID 原样比较（区分大小写）。"""
    uid = str(uid or "")
    if not uid:
        return False
    scope, target = parse_identity(entry)
    if not scope:
        return target == uid
    names = {str(platform or "").lower(), str(platform_id or "").lower()}
    names.discard("")
    if scope not in names:
        return False
    return target == "*" or target == uid


def is_owner(
    uid: str,
    masters: Iterable[Any],
    platform: str = "",
    platform_id: str = "",
) -> bool:
    return any(identity_matches(entry, uid, platform, platform_id) for entry in masters or ())


# ---------------------------------------------------------------------------
# 消息级拦截
# ---------------------------------------------------------------------------
def scan_message(
    text: str,
    *,
    rules: dict[str, tuple[str, tuple[str, ...]]] | None = None,
    extra: Sequence[str] = (),
    allow: Sequence[str] = (),
    block_admin_commands: bool = True,
) -> Hit | None:
    """扫一条消息，命中就返回第一条规则。"""
    body = str(text or "")
    if not body.strip():
        return None
    exempt = [str(word) for word in allow if word]
    for key, (label, patterns) in (rules or DEFAULT_RULES).items():
        for pattern in patterns:
            compiled = _compiled(pattern)
            if compiled is None:
                continue
            match = compiled.search(body)
            if match is None:
                continue
            matched = match.group()
            if any(word and word in matched for word in exempt):
                continue
            return Hit(key=key, label=label, matched=matched, pattern=pattern)
    for pattern in extra or ():
        compiled = _compiled(str(pattern))
        if compiled is None:
            continue
        match = compiled.search(body)
        if match is not None:
            return Hit(key="custom", label="自定义拦截规则", matched=match.group(), pattern=pattern)
    if block_admin_commands and ADMIN_COMMAND_RE.search(body):
        return Hit(key="admin_command", label="管理指令", matched=body.strip()[:24])
    return None


# ---------------------------------------------------------------------------
# 工具白名单 / 黑名单
# ---------------------------------------------------------------------------
TOOL_WHITELIST = "whitelist"
TOOL_BLACKLIST = "blacklist"
TOOL_MODES = (TOOL_WHITELIST, TOOL_BLACKLIST)
TOOL_MODE_LABELS = {
    TOOL_WHITELIST: "白名单：只保留名单里的工具（名单为空 = 不限制）",
    TOOL_BLACKLIST: "黑名单：只摘掉名单里的工具（默认）",
}

#: 黑名单模式的默认名单：只摘「会改东西 / 会执行 / 会删」的高危工具。
#:
#: 之前这里默认是**空白名单 + 白名单模式**，等于非主人一个工具都拿不到 ——
#: 连「检查插件状态」这种只读工具都会被摘掉，用户看到的就是
#: 「曾经还能用的功能，现在不能用了」。普通工具（搜索、查状态、
#: 记忆召回…）本来就不该拦，所以默认改成黑名单。
DEFAULT_TOOL_BLACKLIST: tuple[str, ...] = (
    "*admin*",
    "*config*",
    "*setting*",
    "*delete*",
    "*remove*",
    "*drop*",
    "*clear*",
    "*reset*",
    "*install*",
    "*uninstall*",
    "*restart*",
    "*shutdown*",
    "*write*",
    "*shell*",
    "*exec*",
    "*eval*",
)


def tool_allowed(
    name: str, mode: str = TOOL_WHITELIST, names: Sequence[str] = ()
) -> bool:
    """这个工具非主人能不能用。名单支持 * 通配。"""
    tool = str(name or "").strip()
    if not tool:
        return False
    patterns = [str(item).strip() for item in (names or ()) if str(item).strip()]
    if not patterns:
        # 名单是空的：不管白名单还是黑名单，都不该把工具全拦掉。
        # 「空名单 = 谁都不给」听起来严谨，实际后果是非主人连搜索、查状态、
        # 记忆召回都用不了 —— 空名单按「没配」处理。
        return True
    hit = any(fnmatch.fnmatchcase(tool, pattern) for pattern in patterns)
    if (mode or TOOL_WHITELIST).lower() == TOOL_BLACKLIST:
        return not hit
    return hit


def filter_tools(tools: Sequence[Any], mode: str, names: Sequence[str]) -> list[Any]:
    """请求级过滤：把不该给模型看的工具摘掉。"""
    kept = []
    for tool in tools or ():
        name = str(getattr(tool, "name", "") or "")
        if tool_allowed(name, mode, names):
            kept.append(tool)
    return kept


# ---------------------------------------------------------------------------
# 发送前护栏
# ---------------------------------------------------------------------------
@dataclass
class LeakReport:
    cleaned: str
    markers: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)

    @property
    def leaked(self) -> bool:
        return bool(self.markers or self.phrases)

    def to_dict(self) -> dict[str, Any]:
        return {"markers": self.markers, "phrases": self.phrases}


def scrub_reply(
    text: str,
    *,
    markers: Sequence[str] = INTERNAL_MARKERS,
    phrases: Sequence[str] = LEAK_PHRASES,
    drop_leak_lines: bool = True,
) -> LeakReport:
    """把内部标记从回复里抠掉；说破机制的整句直接删。

    只做清理，不改写 —— 删不干净的时候由调用方决定要不要整条换掉。
    """
    body = str(text or "")
    found_markers: list[str] = []
    for marker in markers:
        if marker and marker in body:
            found_markers.append(marker)
            body = body.replace(marker, "")
    found_phrases: list[str] = []
    if phrases:
        kept: list[str] = []
        for line in body.split("\n"):
            hit = next((phrase for phrase in phrases if phrase and phrase in line), "")
            if hit and drop_leak_lines:
                found_phrases.append(hit)
                continue
            kept.append(line)
        body = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", body).strip()
    return LeakReport(cleaned=cleaned, markers=found_markers, phrases=found_phrases)


__all__ = [
    "ADMIN_COMMAND_RE",
    "DEFAULT_RULES",
    "Hit",
    "INTERNAL_MARKERS",
    "LEAK_PHRASES",
    "LeakReport",
    "TOOL_BLACKLIST",
    "TOOL_MODE_LABELS",
    "TOOL_MODES",
    "TOOL_WHITELIST",
    "filter_tools",
    "identity_matches",
    "is_owner",
    "parse_identity",
    "scan_message",
    "scrub_reply",
    "tool_allowed",
    "DEFAULT_TOOL_BLACKLIST",
]

