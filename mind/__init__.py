"""AI 心智内核：情绪 + 长期记忆。

两个子包各自独立、各自被测试覆盖：

- `emotion/` —— PAD 三维情绪模型、衰减动力学、傲娇预设、危机拦截
- `memory/`  —— 长期记忆抽取、三路检索、隐私闸门

本层补充合并后才有的东西：

- `config`   —— 把一份配置切成两块喂给它们
- `samples`  —— 情绪曲线的时间序列存储（SQLite）
- `panel`    —— WebUI 面板要用的数据组装
"""

from .prompts import BLOCK_BY_KEY, BLOCKS, PromptStore, render_prompt
from .config import (
    GuardSettings,
    PrivacySettings,
    StyleSettings,
    HUMANIZE_MODE_LABELS,
    HUMANIZE_MODES,
    HumanizeSettings,
    ImagesSettings,
    MindSettings,
    PanelSettings,
    cfg_get,
)
from .emotion.engine import Settings  # 情绪设置（合并后仍叫 Settings，便于复用）
from .panel import (
    build_curve,
    describe_session,
    emotion_payload,
    is_group,
    memory_payload,
    memory_to_dict,
    session_entries,
    theory_payload,
)
from .images import (  # noqa: F401
    MODE_CONTAINS,
    MODE_EXACT,
    MODE_FUZZY,
    MODE_LABELS,
    MODES,
    Cooldown,
    ImageAsset,
    ImageStore,
    ImageTrigger,
    IMAGES_MARKER,
    PIC_TAG_RE,
    char_jaccard,
    extract_pic_tags,
    has_pic_tag,
    render_available_images,
    strip_pic_tags,
    guess_extension,
    human_size,
    match_score,
)
from . import graph  # noqa: F401
from . import guard  # noqa: F401
from . import prompts  # noqa: F401
from . import style  # noqa: F401
from .style import STATUS_APPROVED, STATUS_PENDING, StyleExample, StyleStore
from .samples import (
    HumanizeEntry,
    HumanizeStore,
    LexiconHit,
    LexiconStore,
    Sample,
    SampleStore,
    should_sample,
)

# 情绪内核
from .emotion import (  # noqa: F401
    CRISIS_BLOCK,
    PROTOTYPES,
    all_presets,
    delete_custom_preset,
    get_preset,
    load_custom_presets,
    preset_from_dict,
    preset_to_dict,
    save_custom_preset,
    EMOTION_TAG_RE,
    PAD,
    PRESETS,
    RULES_BLOCK as EMOTION_RULES_BLOCK,
    RULES_MARKER as EMOTION_RULES_MARKER,
    EmotionEngine,
    SessionMood,
    Snapshot,
    classify,
    detect_crisis,
    heuristic_self_emotion,
    parse_custom_rule,
    resolve_prototype,
)
from .emotion.display import history_panel, mood_panel, rank_panel, relation_panel  # noqa: F401
from .emotion.prompt import build_injection as build_emotion_injection  # noqa: F401
from .emotion.prompt import (  # noqa: F401
    RELATION_MARKER,
    RELATION_RULES_BLOCK,
    render_relationship_block,
)

# 记忆内核
from .memory import (  # noqa: F401
    ExtractedItem,
    KINDS,
    KIND_LABELS,
    Memory,
    MemoryExtractor,
    MemoryStore,
    RetrievalResult,
    Retriever,
    RULES_BLOCK as MEMORY_RULES_BLOCK,
    RULES_MARKER as MEMORY_RULES_MARKER,
    Turn,
    allows,
    decide_scope,
    detect_sensitive,
    format_existing_for_prompt,
    parse_extraction,
    render_memory_block,
)
from .memory.display import (  # noqa: F401
    clear_panel,
    forget_panel,
    help_panel as memory_help_panel,
    main_panel as memory_main_panel,
    remember_panel,
    search_panel,
)
from .memory.privacy import is_group_session  # noqa: F401

__all__ = [
    "EMOTION_RULES_BLOCK",
    "PROTOTYPES",
    "all_presets",
    "delete_custom_preset",
    "get_preset",
    "load_custom_presets",
    "preset_from_dict",
    "preset_to_dict",
    "save_custom_preset",
    "EMOTION_RULES_MARKER",
    "MEMORY_RULES_BLOCK",
    "MEMORY_RULES_MARKER",
    "MODE_CONTAINS",
    "MODE_EXACT",
    "MODE_FUZZY",
    "MODE_LABELS",
    "MODES",
    "Cooldown",
    "ImageAsset",
    "ImageStore",
    "ImageTrigger",
    "ImagesSettings",
    "IMAGES_MARKER",
    "PIC_TAG_RE",
    "extract_pic_tags",
    "has_pic_tag",
    "render_available_images",
    "strip_pic_tags",
    "HUMANIZE_MODE_LABELS",
    "HUMANIZE_MODES",
    "HumanizeEntry",
    "HumanizeSettings",
    "BLOCKS",
    "BLOCK_BY_KEY",
    "GuardSettings",
    "PrivacySettings",
    "StyleExample",
    "StyleSettings",
    "StyleStore",
    "PromptStore",
    "render_prompt",
    "HumanizeStore",
    "KINDS",
    "KIND_LABELS",
    "MindSettings",
    "char_jaccard",
    "guess_extension",
    "human_size",
    "match_score",
    "Settings",
    "cfg_get",
    "PanelSettings",
    "Sample",
    "SampleStore",
    "build_curve",
    "build_emotion_injection",
    "RELATION_MARKER",
    "RELATION_RULES_BLOCK",
    "render_relationship_block",
    "describe_session",
    "emotion_payload",
    "memory_payload",
    "memory_to_dict",
    "session_entries",
    "should_sample",
    "LexiconHit",
    "LexiconStore",
    "theory_payload",
    "graph",
    "guard",
]

