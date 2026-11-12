"""
归档盒 V1.3 — 规则试运行 / 影响预览

职责：在保存或执行前，用编辑草稿与当前生效配置分别跑一遍分类，
显示命中统计、未匹配数量、分类或目标位置变化及代表样本。

安全边界：
- 零文件操作副作用：不移动、不创建分类目录、不写操作历史；
- 不计算 SHA-256（不读取文件内容），重复信息明确标注「未计算」；
- 分类与目标路径构造复用 planner.classify_entries / build_plan，
  与正式计划是同一套逻辑，不维护第二份算法；
- 后台线程携带任务标识 seq 与根目录 root，回调按两者校验来源。
"""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from classification_config import (
    ClassificationConfig, config_to_dict,
)
from planner import (
    OrganizePlan, build_plan, MODE_BY_DIRECTION_SEMESTER,
)
from scanner import ScanResult


DUPLICATE_NOTE = "试运行不读取文件内容：未计算 SHA-256，内容重复信息未核验。"


@dataclass
class RuleHit:
    key: str
    label: str
    count: int


@dataclass
class ImpactItem:
    rel_path: str
    old_reason: str
    new_reason: str
    old_target: str
    new_target: str


@dataclass
class ImpactResult:
    root: str
    mode: str
    sample_total: int
    hits: List[RuleHit] = field(default_factory=list)
    unmatched: int = 0
    changed: List[ImpactItem] = field(default_factory=list)
    unchanged: int = 0
    representatives: List[ImpactItem] = field(default_factory=list)
    config_changed: bool = False
    duplicate_note: str = DUPLICATE_NOTE

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    def summary_lines(self) -> List[str]:
        lines = [
            f"数据范围：{self.root}",
            f"样本数量：{self.sample_total} 个文件（当前扫描结果，未重新读取文件内容）",
            f"配置是否变化：{'是' if self.config_changed else '否（与当前生效配置一致）'}",
            f"相比当前配置，分类或目标位置变化：{self.changed_count} 个；"
            f"未变化：{self.unchanged} 个",
            f"未匹配（将进入待定）：{self.unmatched} 个",
            self.duplicate_note,
        ]
        return lines


def _plan_items(scan_result: ScanResult, config: ClassificationConfig,
                mode: str, rules, cancel_event=None, progress_cb=None):
    plan: OrganizePlan = build_plan(
        scan_result, mode, rules, config=config, compute_duplicates=False,
        cancel_event=cancel_event, progress_cb=progress_cb,
    )
    return plan


def preview_impact(
    scan_result: ScanResult,
    draft: ClassificationConfig,
    base: ClassificationConfig,
    mode: str = MODE_BY_DIRECTION_SEMESTER,
    rules=None,
    cancel_event=None,
    progress_cb=None,
) -> ImpactResult:
    """计算草稿配置相对当前生效配置的影响（纯计算、零副作用）。"""
    rules = rules or []
    total = len(scan_result.files)

    def _draft_progress(cur, _total, path):
        if progress_cb:
            # 草稿分类占前一半进度
            progress_cb(cur, total * 2, path)

    draft_plan = _plan_items(
        scan_result, draft, mode, rules,
        cancel_event=cancel_event, progress_cb=_draft_progress)
    if cancel_event and cancel_event.is_set():
        return ImpactResult(root=scan_result.root, mode=mode, sample_total=total)

    def _base_progress(cur, _total, path):
        if progress_cb:
            progress_cb(total + cur, total * 2, path)

    base_plan = _plan_items(
        scan_result, base, mode, rules,
        cancel_event=cancel_event, progress_cb=_base_progress)

    # 命中统计（按稳定 rule_key 分组，标签用人类可读的分类依据）
    labels: "OrderedDict[str, str]" = OrderedDict()
    counts: Counter = Counter()
    unmatched = 0
    for item in draft_plan.items:
        counts[item.rule_key] += 1
        labels.setdefault(item.rule_key, item.rule_applied)
        if item.direction == "待定":
            unmatched += 1
    hits = [
        RuleHit(key=k, label=labels[k], count=n)
        for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], labels[kv[0]]))
    ]

    # 逐项对比（同一扫描输入、同一排序，按下标一一对应）
    base_by_rel = {i.file.rel_path: i for i in base_plan.items}
    changed: List[ImpactItem] = []
    unchanged = 0
    for new_item in draft_plan.items:
        old_item = base_by_rel.get(new_item.file.rel_path)
        if old_item is None:
            continue
        if (old_item.target_rel_path != new_item.target_rel_path
                or old_item.rule_key != new_item.rule_key
                or old_item.rule_applied != new_item.rule_applied):
            changed.append(ImpactItem(
                rel_path=new_item.file.rel_path,
                old_reason=old_item.rule_applied,
                new_reason=new_item.rule_applied,
                old_target=old_item.target_rel_path,
                new_target=new_item.target_rel_path,
            ))
        else:
            unchanged += 1

    reps = changed[:10] if changed else [
        ImpactItem(rel_path=i.file.rel_path, old_reason=i.rule_applied,
                   new_reason=i.rule_applied,
                   old_target=i.target_rel_path, new_target=i.target_rel_path)
        for i in draft_plan.items[:5]
    ]

    return ImpactResult(
        root=scan_result.root, mode=mode, sample_total=len(draft_plan.items),
        hits=hits, unmatched=unmatched, changed=changed, unchanged=unchanged,
        representatives=reps,
        config_changed=(config_to_dict(draft) != config_to_dict(base)),
    )


class PreviewWorker(QThread):
    """后台试运行线程：结果/进度都带 seq + root，供界面双重校验。"""

    progress = Signal(int, str, int, int, str)   # seq, root, cur, total, path
    finished_ok = Signal(int, str, object)       # seq, root, ImpactResult
    failed = Signal(int, str, str)               # seq, root, error
    cancelled = Signal(int, str)                 # seq, root

    def __init__(self, seq: int, root: str, scan_result: ScanResult,
                 draft: ClassificationConfig, base: ClassificationConfig,
                 mode: str, rules=None, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.root = root
        self.scan_result = scan_result
        self.draft = draft
        self.base = base
        self.mode = mode
        self.rules = rules or []
        self.cancel_event = threading.Event()
        self._last_emit = -1

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            total = max(1, len(self.scan_result.files) * 2)

            def _progress(cur, _total, path):
                # 节流：最多约 100 次进度信号
                step = max(1, total // 100)
                if cur - self._last_emit >= step or cur >= total:
                    self._last_emit = cur
                    self.progress.emit(self.seq, self.root, cur, total, path)

            result = preview_impact(
                self.scan_result, self.draft, self.base, mode=self.mode,
                rules=self.rules, cancel_event=self.cancel_event,
                progress_cb=_progress,
            )
            if self.cancel_event.is_set():
                self.cancelled.emit(self.seq, self.root)
                return
            self.finished_ok.emit(self.seq, self.root, result)
        except Exception as e:
            self.failed.emit(self.seq, self.root, str(e))
