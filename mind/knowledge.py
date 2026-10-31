
"""知识树：按会话激活的树形知识库。

移植自 NeoHotaru/dsh-knowledge-canvas 的骨架（MIT）：
**树形知识 + 按会话激活 + 节点图 + 模型改件的待审标记。**

和「长期记忆」的区别：记忆是聊天里捞出来的碎片，知识树是**你自己整理的结构**
（比如「本群规矩」「插件怎么用」），挂在树上、可以整棵激活给某个会话，
模型改了什么会先进待审队列，你批准了才生效 —— 上游那套「review markers」就是这个。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 一棵树最多多少节点（防止模型把树写爆）
MAX_NODES = 400
#: 注入时最多带多少字（超了就只给标题，省 token）
MAX_RENDER_CHARS = 2400
#: 单条内容截断
MAX_CONTENT = 600


def _now() -> float:
    return time.time()


def _clean(text: Any, limit: int = MAX_CONTENT) -> str:
    return str(text or "").strip()[:limit]


@dataclass
class Node:
    id: str
    parent: str = ""
    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    updated: float = 0.0
    origin: str = "user"       # user | model

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "parent": self.parent, "title": self.title,
            "content": self.content, "tags": list(self.tags),
            "updated": self.updated, "origin": self.origin,
        }

    @staticmethod
    def from_dict(data: Any) -> "Node":
        raw = data if isinstance(data, dict) else {}
        tags = raw.get("tags")
        return Node(
            id=str(raw.get("id") or ""),
            parent=str(raw.get("parent") or ""),
            title=_clean(raw.get("title"), 80),
            content=_clean(raw.get("content")),
            tags=[str(tag) for tag in (tags if isinstance(tags, list) else [])][:8],
            updated=float(raw.get("updated") or 0.0),
            origin=str(raw.get("origin") or "user"),
        )


@dataclass
class Tree:
    id: str
    name: str = ""
    nodes: list[Node] = field(default_factory=list)
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "nodes": [node.to_dict() for node in self.nodes],
            "created": self.created, "updated": self.updated,
        }

    @staticmethod
    def from_dict(data: Any) -> "Tree":
        raw = data if isinstance(data, dict) else {}
        nodes = raw.get("nodes")
        return Tree(
            id=str(raw.get("id") or ""),
            name=_clean(raw.get("name"), 60),
            nodes=[Node.from_dict(item) for item in (nodes if isinstance(nodes, list) else [])],
            created=float(raw.get("created") or 0.0),
            updated=float(raw.get("updated") or 0.0),
        )

    def children(self, parent: str) -> list[Node]:
        return [node for node in self.nodes if node.parent == parent]

    def get(self, node_id: str) -> Node | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None


@dataclass
class Edit:
    """模型提出的一条改动（等你批准）。"""

    id: str
    tree_id: str
    node_id: str
    title: str = ""
    content: str = ""
    note: str = ""
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tree_id": self.tree_id, "node_id": self.node_id,
            "title": self.title, "content": self.content,
            "note": self.note, "created": self.created,
        }

    @staticmethod
    def from_dict(data: Any) -> "Edit":
        raw = data if isinstance(data, dict) else {}
        return Edit(
            id=str(raw.get("id") or ""),
            tree_id=str(raw.get("tree_id") or ""),
            node_id=str(raw.get("node_id") or ""),
            title=_clean(raw.get("title"), 80),
            content=_clean(raw.get("content")),
            note=_clean(raw.get("note"), 200),
            created=float(raw.get("created") or 0.0),
        )


@dataclass
class KnowledgeBase:
    trees: list[Tree] = field(default_factory=list)
    #: 会话 → 激活的树 id
    active: dict[str, list[str]] = field(default_factory=dict)
    pending: list[Edit] = field(default_factory=list)

    # -- 查询 -------------------------------------------------------------
    def tree(self, tree_id: str) -> Tree | None:
        for item in self.trees:
            if item.id == tree_id:
                return item
        return None

    def by_name(self, name: str) -> Tree | None:
        wanted = _clean(name, 60).lower()
        for item in self.trees:
            if item.name.lower() == wanted or item.id == wanted:
                return item
        return None

    def active_trees(self, session: str) -> list[Tree]:
        rows: list[Tree] = []
        for tree_id in self.active.get(str(session or ""), []):
            found = self.tree(tree_id)
            if found is not None:
                rows.append(found)
        return rows

    # -- 改 ---------------------------------------------------------------
    def _next_tree_id(self) -> str:
        existing = {tree.id for tree in self.trees}
        index = len(self.trees) + 1
        while ("t%d" % index) in existing:
            index += 1
        return "t%d" % index

    def _next_edit_id(self) -> str:
        existing = {edit.id for edit in self.pending}
        index = len(self.pending) + 1
        while ("e%d" % index) in existing:
            index += 1
        return "e%d" % index

    def add_tree(self, name: str, tree_id: str = "", with_root: str = "") -> Tree:
        stamp = _now()
        tree = Tree(
            id=tree_id or self._next_tree_id(),
            name=_clean(name, 60) or "新知识树",
            created=stamp,
            updated=stamp,
        )
        if with_root:
            tree.nodes.append(Node(id=_node_id(tree), title=_clean(with_root, 80),
                                   content="", updated=stamp))
        self.trees.append(tree)
        return tree

    def add_node(self, tree: Tree, title: str, content: str = "",
                 parent: str = "", tags: Iterable[str] | None = None,
                 origin: str = "user") -> Node | None:
        if tree is None or len(tree.nodes) >= MAX_NODES:
            return None
        node = Node(
            id=_node_id(tree), parent=str(parent or ""),
            title=_clean(title, 80), content=_clean(content),
            tags=[str(tag) for tag in (tags or [])][:8],
            updated=_now(), origin=origin,
        )
        tree.nodes.append(node)
        tree.updated = node.updated
        return node

    def propose(self, tree_id: str, node_id: str, title: str = "",
                content: str = "", note: str = "") -> Edit:
        edit = Edit(
            id=self._next_edit_id(),
            tree_id=str(tree_id), node_id=str(node_id),
            title=_clean(title, 80), content=_clean(content),
            note=_clean(note, 200), created=_now(),
        )
        self.pending.append(edit)
        return edit

    def approve(self, edit_id: str) -> bool:
        for index, edit in enumerate(self.pending):
            if edit.id != str(edit_id):
                continue
            tree = self.tree(edit.tree_id)
            if tree is None:
                self.pending.pop(index)
                return False
            node = tree.get(edit.node_id)
            if node is None:
                self.pending.pop(index)
                return False
            if edit.title:
                node.title = edit.title
            if edit.content:
                node.content = edit.content
            node.updated = _now()
            node.origin = "model"
            tree.updated = node.updated
            self.pending.pop(index)
            return True
        return False

    def reject(self, edit_id: str) -> bool:
        for index, edit in enumerate(self.pending):
            if edit.id == str(edit_id):
                self.pending.pop(index)
                return True
        return False

    def activate(self, session: str, tree_id: str) -> bool:
        key = str(session or "")
        tree = self.tree(tree_id)
        if not key or tree is None:
            return False
        rows = self.active.setdefault(key, [])
        if tree.id in rows:
            return False
        rows.append(tree.id)
        return True

    def deactivate(self, session: str, tree_id: str) -> bool:
        key = str(session or "")
        rows = self.active.get(key) or []
        if str(tree_id) in rows:
            rows.remove(str(tree_id))
            return True
        return False

    # -- 序列化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "trees": [tree.to_dict() for tree in self.trees],
            "active": {key: list(value) for key, value in self.active.items()},
            "pending": [edit.to_dict() for edit in self.pending],
        }

    @staticmethod
    def from_dict(data: Any) -> "KnowledgeBase":
        raw = data if isinstance(data, dict) else {}
        trees = raw.get("trees")
        active = raw.get("active")
        pending = raw.get("pending")
        kb = KnowledgeBase(
            trees=[Tree.from_dict(item) for item in (trees if isinstance(trees, list) else [])],
            active={
                str(key): [str(x) for x in value if x]
                for key, value in (active.items() if isinstance(active, dict) else [])
                if isinstance(value, list)
            },
            pending=[Edit.from_dict(item) for item in (pending if isinstance(pending, list) else [])],
        )
        return kb


def _node_id(tree: Tree) -> str:
    return "n%d" % (len(tree.nodes) + 1)


def load(path: Path | str) -> KnowledgeBase:
    try:
        return KnowledgeBase.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return KnowledgeBase()


def save(path: Path | str, kb: KnowledgeBase) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(kb.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        return True
    except (OSError, ValueError):
        return False


def render_tree(tree: Tree, max_chars: int = MAX_RENDER_CHARS) -> str:
    """把一棵树渲染成缩进大纲（有内容的节点才带正文）。"""
    if tree is None or not tree.nodes:
        return ""
    lines: list[str] = ["【%s】" % tree.name]
    used = len(lines[0])

    def walk(parent: str, depth: int) -> None:
        nonlocal used
        for node in tree.children(parent):
            indent = "  " * depth
            head = "%s- %s" % (indent, node.title or node.id)
            body = ""
            if node.content and used + len(head) + len(node.content) <= max_chars:
                body = "：" + node.content
            line = head + body
            if used + len(line) > max_chars:
                return
            lines.append(line)
            used += len(line)
            walk(node.id, depth + 1)

    walk("", 0)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def render_active(kb: KnowledgeBase, session: str,
                  max_chars: int = MAX_RENDER_CHARS) -> str:
    """某个会话激活的知识，拼成一段注入文本。没有就返回空串。"""
    parts: list[str] = []
    used = 0
    for tree in kb.active_trees(session):
        block = render_tree(tree, max(200, max_chars - used))
        if not block:
            continue
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return ""
    return (
        "<knowledge>\n"
        "这些是已经整理好的知识（既定事实，不要复述成「我记得你说过」，"
        "也不要重新整理；需要补充时先提改动，等批准）：\n"
        + "\n\n".join(parts)
        + "\n</knowledge>"
    )


def find_nodes(kb: KnowledgeBase, keyword: str) -> list[tuple[Tree, Node]]:
    wanted = str(keyword or "").strip().lower()
    if not wanted:
        return []
    hits: list[tuple[Tree, Node]] = []
    for tree in kb.trees:
        for node in tree.nodes:
            haystack = (node.title + " " + node.content + " " + " ".join(node.tags)).lower()
            if wanted in haystack:
                hits.append((tree, node))
    return hits


def summarize(kb: KnowledgeBase) -> dict[str, Any]:
    return {
        "trees": len(kb.trees),
        "nodes": sum(len(tree.nodes) for tree in kb.trees),
        "pending": len(kb.pending),
        "sessions": len([key for key, value in kb.active.items() if value]),
    }


__all__ = [
    "MAX_NODES", "MAX_CONTENT", "MAX_RENDER_CHARS",
    "Node", "Tree", "Edit", "KnowledgeBase",
    "load", "save", "render_tree", "render_active", "find_nodes", "summarize",
]

