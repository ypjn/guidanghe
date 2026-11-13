"""
归档盒 V1.7 — 待整理清单模型（纯逻辑，无 Qt 依赖）

职责：
- 记录用户拖入/选择的文件与目录；
- 按「一份计划属于一个整理根目录」的规则判断来源归属；
- 去重与父子包含合并；
- 把清单解析为当前扫描结果中的文件范围，供计划使用。

边界：
- 只登记范围，不移动文件、不写历史；
- 不自动创建不存在的目录；
- 不把共同父目录静默设为新根；
- 所有纳入的文件必须来自既有扫描结果（因此不会绕过保护/排除机制）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


def normalize_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or "")))


def is_within(parent: str, child: str) -> bool:
    parent = normalize_path(parent)
    child = normalize_path(child)
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def rel_of(root: str, path: str) -> str:
    rel = os.path.relpath(normalize_path(path), normalize_path(root))
    return "" if rel == "." else rel


@dataclass
class IntakeItem:
    path: str
    kind: str          # dir / file
    added_at: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind,
                "added_at": self.added_at}

    @staticmethod
    def from_dict(data: dict) -> "IntakeItem":
        return IntakeItem(path=str(data.get("path", "")),
                          kind=str(data.get("kind", "")),
                          added_at=str(data.get("added_at", "")))


@dataclass
class AddResult:
    status: str        # added / duplicate / covered / coalesced / needs_root /
                       # outside / invalid
    message: str = ""
    item: Optional[IntakeItem] = None
    candidate_root: str = ""


@dataclass
class ItemResolution:
    path: str
    kind: str
    included: int = 0
    reason: str = ""
    ok: bool = True


@dataclass
class ScopeResolution:
    root: str
    included: set = field(default_factory=set)
    details: List[ItemResolution] = field(default_factory=list)
    unsupported: int = 0
    not_scanned: int = 0
    protected: int = 0

    @property
    def included_count(self) -> int:
        return len(self.included)

    def summary(self) -> str:
        parts = [f"清单 {len(self.details)} 项 → 纳入 {self.included_count} 个文件"]
        if self.protected:
            parts.append(f"受保护/被排除 {self.protected} 项")
        if self.not_scanned:
            parts.append(f"未进入扫描 {self.not_scanned} 项")
        if self.unsupported:
            parts.append(f"无效来源 {self.unsupported} 项")
        return "；".join(parts)


class IntakeList:
    def __init__(self, items: Optional[List[IntakeItem]] = None):
        self.items: List[IntakeItem] = list(items or [])

    # ---- 基本操作 ----
    def clear(self):
        self.items.clear()

    def remove(self, path: str) -> bool:
        target = normalize_path(path)
        before = len(self.items)
        self.items = [i for i in self.items if i.path != target]
        return len(self.items) != before

    def __len__(self):
        return len(self.items)

    def paths(self) -> List[str]:
        return [i.path for i in self.items]

    def to_payload(self) -> List[dict]:
        return [i.to_dict() for i in self.items]

    @staticmethod
    def from_payload(rows) -> "IntakeList":
        items = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = IntakeItem.from_dict(row)
            if item.path and item.kind in ("dir", "file"):
                items.append(item)
        return IntakeList(items)

    # ---- 加入 ----
    def add(self, path: str, root: Optional[str]) -> AddResult:
        raw = os.path.abspath(os.path.expanduser(path or ""))
        if not raw or not os.path.exists(raw):
            return AddResult("invalid", f"来源不存在或不可访问：{path}")
        p = normalize_path(raw)
        kind = "dir" if os.path.isdir(p) else "file"

        if not root:
            if kind == "dir":
                return AddResult(
                    "needs_root",
                    "尚未选择整理空间。可将该目录设为整理空间后再加入。",
                    candidate_root=p)
            parent = normalize_path(os.path.dirname(p))
            # 只提出候选，不在这里改当前空间。是否采用由界面明确确认。
            if parent and parent != p and os.path.isdir(parent):
                return AddResult(
                    "needs_root",
                    "尚未选择整理空间。可将该文件所在目录设为整理空间后再加入。",
                    candidate_root=parent)
            return AddResult(
                "needs_root",
                "尚未选择整理空间。请先选择空间，再加入文件。")

        root_n = normalize_path(root)
        if not is_within(root_n, p):
            return AddResult(
                "outside",
                f"来源不在当前整理空间内：{p}\n"
                f"当前空间：{root_n}\n"
                "不会自动把共同父目录设为新根；请切换到对应空间后再加入。")

        # 完全重复
        for it in self.items:
            if it.path == p:
                return AddResult("duplicate", f"已在清单中：{p}")

        # 已被现有目录覆盖
        for it in self.items:
            if it.kind == "dir" and it.path != p and is_within(it.path, p):
                return AddResult(
                    "covered",
                    f"已包含在目录「{it.path}」中，无需重复加入。")

        # 新目录覆盖已有条目：合并为父目录
        removed = 0
        if kind == "dir":
            kept = []
            for it in self.items:
                if is_within(p, it.path):
                    removed += 1
                    continue
                kept.append(it)
            self.items = kept

        item = IntakeItem(path=p, kind=kind,
                          added_at=datetime.now().isoformat(timespec="seconds"))
        self.items.append(item)
        if removed:
            return AddResult(
                "coalesced",
                f"已加入目录「{p}」，并合并其下已存在的 {removed} 项。", item)
        return AddResult("added", f"已加入：{p}", item)

    # ---- 解析到扫描结果 ----
    def resolve(self, root: str, scan_result) -> ScopeResolution:
        root_n = normalize_path(root)
        res = ScopeResolution(root=root_n)
        if scan_result is None:
            for it in self.items:
                res.details.append(ItemResolution(
                    it.path, it.kind, 0, "当前没有可用扫描结果", False))
                res.unsupported += 1
            return res

        files = getattr(scan_result, "files", []) or []
        rel_set = {getattr(f, "rel_path", "") for f in files}
        protected_roots = [normalize_path(p) for p in
                           getattr(scan_result, "protected_roots", {}) or {}]

        for it in self.items:
            item = ItemResolution(it.path, it.kind)
            if not os.path.exists(it.path):
                item.ok = False
                item.reason = "来源已不存在"
                res.unsupported += 1
                res.details.append(item)
                continue
            if not is_within(root_n, it.path):
                item.ok = False
                item.reason = "来源不在当前空间内"
                res.unsupported += 1
                res.details.append(item)
                continue

            rel = rel_of(root_n, it.path)
            if it.kind == "file":
                if rel in rel_set:
                    item.included = 1
                    res.included.add(rel)
                else:
                    item.ok = False
                    item.reason = "未进入扫描（可能受保护、被排除或已变化）"
                    res.not_scanned += 1
            else:
                prefix = rel + os.sep if rel else ""
                hit = [r for r in rel_set
                       if (prefix and r.startswith(prefix)) or
                       (not prefix and r)]
                if hit:
                    item.included = len(hit)
                    res.included.update(hit)
                else:
                    is_protected = any(
                        is_within(pr, it.path) for pr in protected_roots)
                    item.ok = False
                    if is_protected:
                        item.reason = "受保护目录，未纳入扫描"
                        res.protected += 1
                    else:
                        item.reason = "没有可纳入的文件（空目录或被排除）"
                        res.not_scanned += 1
            res.details.append(item)
        return res

    def describe(self, root: str, scan_result) -> str:
        return self.resolve(root, scan_result).summary()
