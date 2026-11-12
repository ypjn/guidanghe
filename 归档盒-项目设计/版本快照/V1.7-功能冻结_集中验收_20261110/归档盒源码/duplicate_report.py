"""
归档盒 V1.7 — 重复文件核对报告（纯逻辑，无 Qt 依赖）

范围：当前整理空间、当前扫描纳入的文件。
- 内容重复：SHA-256 已核对且一致，组内至少 2 份；
- 同名不同内容：名称相同、全部成员已核对且存在不同指纹；
- 待核对/核对失败：缺失指纹、读取失败、文件消失等，**不作为重复证据**。

本轮只查看与核对：不删除、不合并、不移动，也不改变计划勾选。
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from utils import sha256_file

STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"

STATUS_LABELS = {
    STATUS_VERIFIED: "已核对",
    STATUS_FAILED: "核对失败",
    STATUS_PENDING: "待核对",
}

KIND_CONTENT = "content"
KIND_SAME_NAME_DIFF = "same_name_diff"
KIND_SAME_NAME_PENDING = "same_name_pending"
KIND_UNVERIFIED = "unverified"

KIND_LABELS = {
    KIND_CONTENT: "内容重复",
    KIND_SAME_NAME_DIFF: "同名不同内容",
    KIND_SAME_NAME_PENDING: "同名（待核对）",
    KIND_UNVERIFIED: "待核对 / 核对失败",
}

REPORT_FORMAT = "archive-box-duplicate-report"
REPORT_VERSION = 1


@dataclass
class DupEntry:
    rel_path: str
    abs_path: str
    filename: str
    size: int
    size_display: str
    expected_mtime_ns: int = 0
    sha256: str = ""
    status: str = STATUS_PENDING
    reason: str = ""
    target_rel_path: str = ""
    group_id: str = ""
    group_size: int = 0
    rechecked: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def fingerprint_short(self) -> str:
        return (self.sha256[:12] + "…") if self.sha256 else "—"

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.abs_path)


@dataclass
class DupGroup:
    kind: str
    group_id: str
    label: str
    entries: List[DupEntry] = field(default_factory=list)
    note: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass
class DuplicateReport:
    root: str
    generated_at: str
    scope_note: str
    source: str                  # plan / scan
    total_files: int = 0
    verified: int = 0
    failed: int = 0
    pending: int = 0
    content_groups: int = 0
    content_files: int = 0
    same_name_groups: int = 0
    same_name_files: int = 0
    groups: List[DupGroup] = field(default_factory=list)

    @property
    def group_count_text(self) -> str:
        return (f"内容重复 {self.content_groups} 组 / {self.content_files} 份；"
                f"同名不同内容 {self.same_name_groups} 组 / "
                f"{self.same_name_files} 份")

    @property
    def status_text(self) -> str:
        parts = [f"已核对 {self.verified}"]
        if self.failed:
            parts.append(f"核对失败 {self.failed}")
        if self.pending:
            parts.append(f"待核对 {self.pending}")
        return "，".join(parts)


def entries_from_plan(plan, scan_result=None) -> List[DupEntry]:
    """从有效计划或扫描结果构造待核对条目（不读取文件内容）。"""
    entries: List[DupEntry] = []
    if plan is not None and getattr(plan, "items", None):
        computed = bool(getattr(plan, "duplicates_computed", False))
        for item in plan.items:
            f = item.file
            entries.append(DupEntry(
                rel_path=f.rel_path,
                abs_path=getattr(f, "abs_path", os.path.join(plan.root, f.rel_path)),
                filename=f.filename,
                size=int(f.size),
                size_display=f.size_display,
                expected_mtime_ns=int(getattr(f, "mtime_ns", 0) or 0),
                sha256=(item.source_sha256 or "") if computed else "",
                status=STATUS_VERIFIED if (computed and item.source_sha256)
                else STATUS_PENDING,
                target_rel_path=getattr(item, "target_rel_path", ""),
            ))
        return entries
    if scan_result is not None:
        for f in getattr(scan_result, "files", []) or []:
            entries.append(DupEntry(
                rel_path=f.rel_path,
                abs_path=f.abs_path,
                filename=f.filename,
                size=int(f.size),
                size_display=f.size_display,
                expected_mtime_ns=int(getattr(f, "mtime_ns", 0) or 0),
                status=STATUS_PENDING,
            ))
    return entries


def verify_entries(entries: List[DupEntry], progress_cb=None,
                   cancel_event=None) -> bool:
    """核对缺失或可能过期的指纹。

    - 已有指纹且文件大小未变：直接复用，不重读内容；
    - 文件大小变化、缺失指纹：重新计算 SHA-256；
    - 文件消失/不可读：标记失败，不作为重复证据。

    返回 False 表示被取消。
    """
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        if cancel_event is not None and cancel_event.is_set():
            return False
        try:
            st = os.stat(entry.abs_path)
        except OSError as e:
            entry.status = STATUS_FAILED
            entry.sha256 = ""
            entry.reason = f"文件不存在或无法访问：{e}"
            if progress_cb:
                progress_cb(index, total, entry.rel_path)
            continue
        if not os.path.isfile(entry.abs_path):
            entry.status = STATUS_FAILED
            entry.sha256 = ""
            entry.reason = "路径已不是普通文件"
            if progress_cb:
                progress_cb(index, total, entry.rel_path)
            continue
        mtime_ns = int(getattr(st, "st_mtime_ns", 0) or 0)
        mtime_changed = bool(entry.expected_mtime_ns) and \
            mtime_ns != entry.expected_mtime_ns
        if st.st_size != entry.size or mtime_changed:
            entry.rechecked = True
            entry.reason = "文件已变化，已重新核对"
            entry.size = int(st.st_size)
            entry.size_display = _size_display(entry.size)
            entry.expected_mtime_ns = mtime_ns
            entry.sha256 = ""
            entry.status = STATUS_PENDING
        if entry.sha256 and entry.status == STATUS_VERIFIED:
            if progress_cb:
                progress_cb(index, total, entry.rel_path)
            continue
        digest = sha256_file(entry.abs_path)
        if not digest:
            entry.status = STATUS_FAILED
            entry.sha256 = ""
            entry.reason = entry.reason or "无法读取文件内容"
        else:
            entry.status = STATUS_VERIFIED
            entry.sha256 = digest
            entry.expected_mtime_ns = mtime_ns
            if entry.rechecked:
                entry.reason = "文件已变化，指纹已重新核对"
        if progress_cb:
            progress_cb(index, total, entry.rel_path)
    return True


def _size_display(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def assemble_report(root: str, entries: List[DupEntry], *,
                    scope_note: str, source: str,
                    generated_at: str = "") -> DuplicateReport:
    report = DuplicateReport(
        root=os.path.abspath(root),
        generated_at=generated_at or datetime.now().isoformat(timespec="seconds"),
        scope_note=scope_note, source=source,
        total_files=len(entries),
    )
    report.verified = sum(1 for e in entries if e.status == STATUS_VERIFIED)
    report.failed = sum(1 for e in entries if e.status == STATUS_FAILED)
    report.pending = sum(1 for e in entries if e.status == STATUS_PENDING)

    for e in entries:
        e.group_id = ""
        e.group_size = 0

    # ---- 内容重复：已核对且 SHA 一致 ----
    by_sha = {}
    for e in entries:
        if e.status == STATUS_VERIFIED and e.sha256:
            by_sha.setdefault(e.sha256, []).append(e)
    content_groups = []
    for digest, group in sorted(by_sha.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(group) < 2:
            continue
        gid = f"C{len(content_groups) + 1}"
        group.sort(key=lambda x: x.rel_path)
        for e in group:
            e.group_id = gid
            e.group_size = len(group)
        content_groups.append(DupGroup(
            kind=KIND_CONTENT, group_id=gid,
            label=f"内容重复 #{len(content_groups) + 1}（{len(group)} 份）",
            entries=group,
            note=f"SHA-256 一致：{digest[:16]}…"))
    report.groups.extend(content_groups)
    report.content_groups = len(content_groups)
    report.content_files = sum(len(g.entries) for g in content_groups)

    # ---- 同名：区分“已核对且内容不同”和“等待核对” ----
    by_name = {}
    for e in entries:
        by_name.setdefault(e.filename, []).append(e)
    same_groups = []
    for name, group in sorted(by_name.items()):
        if len(group) < 2:
            continue
        verified = [e for e in group if e.status == STATUS_VERIFIED and e.sha256]
        distinct = {e.sha256 for e in verified}
        if len(verified) == len(group) and len(distinct) >= 2:
            group.sort(key=lambda x: x.rel_path)
            same_groups.append(DupGroup(
                kind=KIND_SAME_NAME_DIFF, group_id=f"N{len(same_groups) + 1}",
                label=f"同名不同内容 #{len(same_groups) + 1}（{len(group)} 份）",
                entries=group,
                note=f"名称相同，但已核对出 {len(distinct)} 种不同内容"))
        elif len(verified) < len(group):
            group.sort(key=lambda x: x.rel_path)
            same_groups.append(DupGroup(
                kind=KIND_SAME_NAME_PENDING,
                group_id=f"N{len(same_groups) + 1}",
                label=f"同名（待核对） #{len(same_groups) + 1}（{len(group)} 份）",
                entries=group,
                note="存在未完成核对或核对失败的成员，不能判断内容是否相同"))
    report.groups.extend(same_groups)
    report.same_name_groups = len(same_groups)
    report.same_name_files = sum(len(g.entries) for g in same_groups)

    # ---- 其余未核对/失败条目单独展示 ----
    grouped_ids = set()
    for g in same_groups:
        grouped_ids.update(id(e) for e in g.entries)
    leftovers = [e for e in entries
                 if e.status != STATUS_VERIFIED and id(e) not in grouped_ids]
    if leftovers:
        leftovers.sort(key=lambda x: x.rel_path)
        report.groups.append(DupGroup(
            kind=KIND_UNVERIFIED, group_id="U1",
            label=f"待核对 / 核对失败（{len(leftovers)} 份）",
            entries=leftovers,
            note="这些条目没有有效指纹，不能作为内容相同或不同的证据"))
    return report


def export_payload(report: DuplicateReport) -> dict:
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "root": report.root,
        "scope": report.scope_note,
        "source": report.source,
        "generated_at": report.generated_at,
        "counts": {
            "total_files": report.total_files,
            "verified": report.verified,
            "failed": report.failed,
            "pending": report.pending,
            "content_groups": report.content_groups,
            "content_files": report.content_files,
            "same_name_groups": report.same_name_groups,
            "same_name_files": report.same_name_files,
        },
        "groups": [
            {
                "kind": g.kind,
                "kind_label": g.kind_label,
                "group_id": g.group_id,
                "label": g.label,
                "note": g.note,
                "count": g.count,
                "entries": [
                    {
                        "filename": e.filename,
                        "rel_path": e.rel_path,
                        "abs_path": e.abs_path,
                        "size": e.size,
                        "size_display": e.size_display,
                        "status": e.status,
                        "status_label": e.status_label,
                        "reason": e.reason,
                        "sha256": e.sha256,
                        "target_rel_path": e.target_rel_path,
                    }
                    for e in g.entries
                ],
            }
            for g in report.groups
        ],
    }


def export_csv_text(report: DuplicateReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "组类型", "组标识", "组内份数", "文件", "源相对路径", "完整路径",
        "大小(字节)", "大小", "核对状态", "说明", "SHA-256", "计划目标"])
    for g in report.groups:
        for e in g.entries:
            writer.writerow([
                g.kind_label, g.group_id, g.count, e.filename, e.rel_path,
                e.abs_path, e.size, e.size_display, e.status_label,
                e.reason, e.sha256, e.target_rel_path])
    return buf.getvalue()


def export_json_text(report: DuplicateReport) -> str:
    return json.dumps(export_payload(report), ensure_ascii=False, indent=2)
