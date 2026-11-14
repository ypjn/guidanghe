"""
归档盒 V0.5 — 执行器、撤销器、中断恢复

核心能力：
1. execute_plan(cancel_event=...) — 逐项执行，每步后检查取消
2. undo_latest_batch() — 撤销最近批次（V0.3/V0.4 已有）
3. recover_batch() — 中断恢复：核对 planned 动作的磁盘实际状态

取消机制（经验 #1976249 教训）：
  后台线程只做计算，主循环用 cancel_event 轮询检查。
  取消在当前文件操作结束后生效（不中断正在进行的 os.rename）。

中断恢复核心核对逻辑：
  对 status=planned 的动作，有三种可能：
  A) src exists + target not exists      → 还没动，保持 planned
  B) src not exists + target exists + sha match → 动了但日志没写 → 补记为 moved
  C) 其他组合                           → 状态不明确 → paused（暂停人工处理）
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from planner import OrganizePlan, PlanItem
from storage import (
    Storage, BatchRecord, ActionRecord,
    STATUS_PLANNED, STATUS_MOVED, STATUS_FAILED, STATUS_SKIPPED,
    STATUS_CANCELLED, STATUS_UNDONE, STATUS_CONFLICT, STATUS_PAUSED,
    BATCH_PLANNING, BATCH_EXECUTING, BATCH_DONE, BATCH_PARTIAL,
    BATCH_CANCELLED, BATCH_CANCELLED_CLEAN, BATCH_RECOVERED_PARTIAL,
    BATCH_UNDONE, BATCH_PARTIAL_UNDONE, BATCH_PARTIAL_FAILED_UNDONE,
    BATCH_PENDING_STATUSES,
)
from utils import sha256_file, win_long_path, strip_long_prefix

# 执行/撤销期间把状态写库的缓冲大小：同一批次批量写入，避免每项两次 commit。
_DB_FLUSH_EVERY = 20


# ======================== 结果数据类 ========================

@dataclass
class ExecuteResult:
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0
    paused: int = 0
    errors: List[str] = field(default_factory=list)
    batch_id: int = 0
    was_cancelled: bool = False

    @property
    def total(self) -> int:
        return self.moved + self.skipped + self.failed + self.cancelled + self.paused

    def summary(self) -> str:
        parts = [f"成功 {self.moved}"]
        if self.skipped: parts.append(f"跳过 {self.skipped}")
        if self.failed: parts.append(f"失败 {self.failed}")
        if self.cancelled: parts.append(f"取消剩余 {self.cancelled}")
        if self.paused: parts.append(f"暂停 {self.paused}")
        return "/".join(parts)


@dataclass
class UndoResult:
    restored: int = 0
    conflict: int = 0
    errors: int = 0
    errors_list: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    batch_id: int = 0
    # S1-02/S1-03：批次状态未确定（planning/executing/planned/paused）时拒绝撤销
    refused: bool = False
    # 用户中途停止：已恢复的留下，未处理的仍是 moved，批次不能标成 undone
    was_cancelled: bool = False
    remaining: int = 0

    @property
    def is_complete(self) -> bool:
        # 完整撤销必须没有拒绝、没有中途停止、没有冲突/错误，并且确实有恢复。
        # 恢复 0 项、或只恢复了一部分就停下，都不能叫完整成功。
        return (not self.refused and not self.was_cancelled
                and self.restored > 0 and self.remaining == 0
                and self.conflict == 0 and self.errors == 0)

    def summary(self) -> str:
        if self.refused:
            return "撤销未执行（已拒绝）"
        if self.was_cancelled:
            total = self.restored + self.remaining
            return (f"已停止：恢复 {self.restored}/{total}，"
                    f"剩余 {self.remaining} 项可以继续撤销")
        if self.is_complete:
            return f"完整撤销：恢复 {self.restored} 项"
        if self.restored == 0 and self.conflict == 0 and self.errors == 0:
            return "未恢复任何文件"
        return f"部分撤销：恢复 {self.restored} / 冲突 {self.conflict} / 错误 {self.errors}"


@dataclass
class RecoverResult:
    recovered_moved: int = 0     # 核对后确认已移动成功 → 补记 moved
    still_planned: int = 0       # 还没动，保持 planned
    paused: int = 0              # 状态不明确，暂停人工处理
    batch_id: int = 0
    batch_status_after: str = ""
    report: List[str] = field(default_factory=list)

    def summary(self) -> str:
        labels = {
            BATCH_PLANNING: "计划中", BATCH_EXECUTING: "执行中",
            BATCH_DONE: "已完成", BATCH_PARTIAL: "部分完成",
            BATCH_CANCELLED: "已取消（有已完成项）",
            BATCH_CANCELLED_CLEAN: "已取消（未移动）",
            BATCH_RECOVERED_PARTIAL: "恢复后仍未确定",
            BATCH_UNDONE: "已撤销",
            BATCH_PARTIAL_UNDONE: "部分撤销",
            BATCH_PARTIAL_FAILED_UNDONE: "部分撤销（有失败）",
        }
        parts = [f"核对批次 #{self.batch_id}"]
        if self.recovered_moved: parts.append(f"补记已移动 {self.recovered_moved}")
        if self.still_planned: parts.append(f"仍未执行 {self.still_planned}")
        if self.paused: parts.append(f"暂停待人工确认 {self.paused}")
        parts.append(
            "批次状态 → " + labels.get(self.batch_status_after,
                                        self.batch_status_after or "未知"))
        return " | ".join(parts)


# ======================== 执行器 ========================

def execute_plan(
    plan: OrganizePlan,
    storage: Storage,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ExecuteResult:
    """
    执行固定整理计划。支持取消（cancel_event）。

    前置条件：存在被中断（planning/executing）的批次时拒绝开始，
    必须先恢复或撤销该批次，避免产生重叠批次导致旧批次无法撤销。

    流程：
    1. 创建批次记录（BATCH_PLANNING）
    2. 为每个文件写执行意图（status=planned）
    3. 批次状态改为 BATCH_EXECUTING
    4. 逐项核验 → 创建目录 → 移动 → 更新状态 → 检查取消
    5. 批次状态改为 done/partial/cancelled
    """
    result = ExecuteResult()
    root = os.path.abspath(plan.root)

    blocked = interrupted_batch_id(storage)
    if blocked is not None:
        result.errors.append(
            f"存在被中断的批次 #{blocked}，请先恢复或撤销该批次后再开始新的整理"
        )
        return result

    selected_items = [i for i in plan.items if i.selected]
    result.batch_id = storage.create_batch(plan, f"待执行 {len(selected_items)} 项")

    # 步骤 2：先持久化所有执行意图
    # S1-02：意图写库失败时立刻中止——批次留在 planning（未完成），
    # 之后会被「核对恢复」流程接手，绝不能在记录不全的情况下继续移动文件。
    # S3 / F-20：整批一次性写入（单次 commit），不再每个动作单独 commit。
    sha_by_index: List[str] = []
    for item in selected_items:
        src_path = os.path.join(root, item.file.rel_path)
        sha_by_index.append(item.source_sha256 or sha256_file(src_path) or "")
    try:
        storage.create_actions_bulk(result.batch_id, selected_items, sha_by_index)
    except Exception as e:
        result.errors.append(
            f"写入执行意图失败（共 {len(selected_items)} 项），已中止整理：{e}")
        try:
            storage.update_batch_status(result.batch_id, BATCH_PLANNING)
            storage.conn.commit()
        except Exception:
            pass
        return result
    storage.update_batch_status(result.batch_id, BATCH_EXECUTING)

    # 步骤 3-4：逐项执行
    # S3 / F-20：状态更新按批次缓冲写入（每 _DB_FLUSH_EVERY 项一次 commit），
    # 不再每项一次全表 get_actions + 两次 commit。意图已在移动前落库，
    # 即使缓冲中的结果丢失，中断恢复也能按磁盘状态 + SHA 核对回来。
    created_dirs: set[str] = set()
    pending_updates: List[tuple] = []
    # S1-02：单项写库失败不能让异常穿透整个执行循环——记录下来，
    # 循环结束后把批次明确标成未完成并提交。
    db_write_failed = False

    def _flush(force: bool = False) -> None:
        nonlocal db_write_failed
        if not pending_updates:
            return
        if not force and len(pending_updates) < _DB_FLUSH_EVERY:
            return
        try:
            storage.update_actions_bulk(result.batch_id, pending_updates)
            pending_updates.clear()
        except Exception as e:
            db_write_failed = True
            result.errors.append(f"写入整理记录失败（{len(pending_updates)} 项）：{e}")
            pending_updates.clear()

    def _record(action_idx: int, status: str, error: str = "") -> None:
        pending_updates.append((action_idx, status, error))
        _flush()

    for idx, item in enumerate(selected_items):
        # ---- 取消检查（在每个文件操作前/后）----
        if cancel_event and cancel_event.is_set():
            # 把剩余所有 planned 动作标记为 cancelled
            remaining = selected_items[idx:]
            for remaining_idx, _ in enumerate(remaining):
                real_idx = idx + remaining_idx
                pending_updates.append((real_idx, STATUS_CANCELLED, "用户取消"))
                result.cancelled += 1
            result.was_cancelled = True
            break

        src_path = os.path.join(root, item.file.rel_path)
        target_path = os.path.join(root, item.target_rel_path)
        target_dir = os.path.dirname(target_path)

        # ---- 边界校验：目标真实路径必须留在整理空间内 ----
        # 防止 _已整理 等目录是指向空间外的符号链接，导致文件被移到空间外。
        if not _is_within_root(target_path, root):
            _record(idx, STATUS_FAILED,
                    error="目标路径越出整理空间（符号链接逃逸）")
            result.failed += 1
            result.errors.append(
                f"失败（目标越出整理空间）：{item.file.rel_path} → "
                f"{item.target_rel_path}")
            continue

        if progress_cb:
            progress_cb(idx + 1, len(selected_items), item.file.rel_path)

        # ---- 核验 ----
        if not os.path.isfile(win_long_path(src_path)):
            _record(idx, STATUS_SKIPPED, error="源文件已不存在")
            result.skipped += 1
            result.errors.append(f"跳过（源文件已不存在）：{item.file.rel_path}")
            continue

        stored_sha = sha_by_index[idx]
        current_sha = sha256_file(src_path)
        if current_sha is None:
            # S3 / F-10：读不出来就是"读失败"，不能说成"内容已变化"。
            reason = ("无法读取源文件内容（权限/占用），未移动"
                      if stored_sha else "源文件无法读取，未移动")
            _record(idx, STATUS_SKIPPED, error=reason)
            result.skipped += 1
            result.errors.append(f"跳过（{reason}）：{item.file.rel_path}")
            continue
        if stored_sha and stored_sha != current_sha:
            _record(idx, STATUS_SKIPPED, error="源文件内容已变化")
            result.skipped += 1
            result.errors.append(f"跳过（源文件内容已变化）：{item.file.rel_path}")
            continue

        if os.path.exists(win_long_path(target_path)):
            _record(idx, STATUS_SKIPPED, error="目标路径已被占用")
            result.skipped += 1
            result.errors.append(
                f"跳过（目标路径已被占用）：{item.file.rel_path} → "
                f"{item.target_rel_path}")
            continue

        # ---- 创建目标目录（记录每一级新建目录，撤销时才能清干净）----
        if not os.path.isdir(win_long_path(target_dir)):
            try:
                for rel_dir in _makedirs_tracked(root, target_dir):
                    if rel_dir not in created_dirs:
                        storage.record_created_dir(result.batch_id, rel_dir)
                        created_dirs.add(rel_dir)
            except OSError as e:
                _record(idx, STATUS_FAILED, error=f"创建目录失败：{e}")
                result.failed += 1
                result.errors.append(
                    f"失败（创建目标目录）：{item.file.rel_path} → "
                    f"{item.target_rel_path}：{e}")
                continue

        # ---- 移动（不覆盖：复制到临时名 → 独占创建目标 → 原子替换 → 删源）----
        # 不直接用 os.rename：macOS 上 os.rename 在目标已存在时会静默覆盖，
        # 「exists 检查 → os.rename」之间存在 TOCTOU 窗口。_copy_then_replace
        # 用 O_CREAT|O_EXCL 独占创建目标，目标已存在时返回冲突，绝不覆盖。
        moved_ok, move_err, is_conflict = _copy_then_replace(
            src_path, target_path)
        if not moved_ok:
            if is_conflict:
                _record(idx, STATUS_SKIPPED, error=f"未移动：{move_err}")
                result.skipped += 1
            else:
                _record(idx, STATUS_FAILED, error=f"移动失败：{move_err}")
                result.failed += 1
            result.errors.append(
                f"移动失败（保留源文件）：{item.file.rel_path} → {move_err}")
            continue
        _record(idx, STATUS_MOVED)
        result.moved += 1

    _flush(force=True)

    # ---- 批次最终状态 ----
    # S1-02：写库失败过的批次必须标成未完成并提交，
    # 不允许以 done 收尾导致撤销/核对恢复流程被跳过。
    _finalize_batch(storage, result.batch_id, result,
                    force_unfinished=db_write_failed)
    return result


# ======================== 撤销器 ========================

def undo_latest_batch(
    storage: Storage,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> UndoResult:
    """撤销最近批次。支持 conflict 状态重试（V0.4）。

    S1-02/S1-03：撤销不能再报假成功——
    - 批次还在 planning/executing，或仍有 planned/paused 条目时，拒绝撤销
      （这些项的实际状态未确定，需要先「核对恢复」）；
    - 「完整撤销」要求确实恢复了至少 1 项，且没有冲突/错误；
    - 恢复 0 项时不把批次标成 undone，不算完整成功。
    """
    result = UndoResult()
    batch = storage.get_undoable_batch()
    if batch is None:
        result.errors_list.append("没有可撤销的批次")
        return result

    result.batch_id = batch.batch_id
    # S2 F-06：历史库就在当前整理空间内。目录改名/移动后批次里保存的旧
    # root 已经不存在，撤销必须按当前根目录解析文件，不能静默失效。
    root = os.path.abspath(storage.display_root)

    # ---- 拒绝条件 1：批次本身状态未确定（还在 planning/executing）----
    if batch.status in (BATCH_PLANNING, BATCH_EXECUTING):
        result.refused = True
        result.errors_list.append(
            f"批次 #{batch.batch_id} 的状态还是「{batch.status}」"
            "（执行过程未确认结束），撤销被拒绝。\n"
            "请先通过「核对恢复」确认每项的真实状态，再决定是否撤销。")
        return result

    actions = storage.get_actions(batch.batch_id)

    # ---- 拒绝条件 2：仍有 planned / paused 条目（单项状态未确定）----
    unfinished = [a for a in actions
                  if a.status in (STATUS_PLANNED, STATUS_PAUSED)]
    if unfinished:
        result.refused = True
        result.errors_list.append(
            f"批次 #{batch.batch_id} 还有 {len(unfinished)} 个状态未确定的条目"
            "（planned/paused），撤销被拒绝：\n"
            "  " + "\n  ".join(a.source_rel for a in unfinished[:10])
            + ("…" if len(unfinished) > 10 else "")
            + "\n请先「核对恢复」或人工处理这些条目，再执行撤销。")
        return result

    db_write_failed = False
    recoverables = [a for a in actions if a.status in (STATUS_MOVED, STATUS_CONFLICT)]
    pending_updates: List[tuple] = []

    def _flush_undo(force: bool = False) -> None:
        nonlocal db_write_failed
        if not pending_updates:
            return
        if not force and len(pending_updates) < _DB_FLUSH_EVERY:
            return
        try:
            storage.update_actions_bulk(batch.batch_id, pending_updates)
            pending_updates.clear()
        except Exception as e:
            db_write_failed = True
            result.errors_list.append(f"写库失败（{len(pending_updates)} 项）：{e}")
            pending_updates.clear()

    def _mark_conflict(action, message: str) -> None:
        pending_updates.append((action.action_index, STATUS_CONFLICT, message))
        result.conflict += 1
        result.conflicts.append(f"{action.source_rel}：{message}")

    for idx, action in enumerate(recoverables):
        # S3 / guia F-06：撤销可协作取消（当前文件操作完成后停止）
        if cancel_event is not None and cancel_event.is_set():
            result.was_cancelled = True
            result.remaining = len(recoverables) - idx
            result.errors_list.append(
                f"撤销已停止：已恢复 {result.restored} 项，"
                f"剩余 {result.remaining} 项仍在整理目录，可以继续撤销。")
            break
        target_abs = os.path.join(root, action.target_rel)
        source_abs = os.path.join(root, action.source_rel)

        if progress_cb:
            progress_cb(idx + 1, len(recoverables), action.source_rel)

        if not os.path.isfile(win_long_path(target_abs)):
            _mark_conflict(action, "目标文件已消失，无法恢复（需人工核对）")
            _flush_undo()
            continue

        # S3 / F-10/F-11：读失败 ≠ 内容被修改。没有记录指纹时按路径恢复，
        # 但把情况写进说明，绝不谎报"内容已被修改"。
        nd_note = ""
        if not action.source_sha256:
            nd_note = "原记录没有内容指纹，按路径恢复"
        current_sha = sha256_file(target_abs)
        if current_sha is None:
            _mark_conflict(
                action, "目标文件无法读取（权限/占用），不能核对内容；"
                        "请关闭占用它的程序后重试，或确认后标记已人工处理")
            _flush_undo()
            continue
        if action.source_sha256 and current_sha != action.source_sha256:
            _mark_conflict(action, "目标内容已变化（与记录的内容指纹不一致）")
            _flush_undo()
            continue

        if os.path.exists(win_long_path(source_abs)):
            _mark_conflict(action, "原路径已被占用，不能恢复")
            _flush_undo()
            continue

        try:
            source_dir = os.path.dirname(source_abs)
            if source_dir:
                for rel_dir in _makedirs_tracked(root, source_dir):
                    # 撤销时重建的父目录同样登记，失败时由用户自行清理
                    if rel_dir not in batch.created_dirs:
                        try:
                            storage.record_created_dir(batch.batch_id, rel_dir)
                        except Exception:
                            pass
                        batch.created_dirs.append(rel_dir)
        except OSError as e:
            pending_updates.append(
                (action.action_index, STATUS_CONFLICT,
                 f"恢复失败（创建父目录）：{e}"))
            result.errors += 1
            result.errors_list.append(
                f"恢复失败：{action.source_rel} ← 创建父目录：{e}")
            _flush_undo()
            continue

        # 不覆盖的落盘：与执行器同一套。原路径已被占用时返回冲突，
        # 绝不覆盖原路径上新出现的文件。
        moved_ok, move_err, is_conflict = _copy_then_replace(
            target_abs, source_abs)
        if not moved_ok:
            pending_updates.append(
                (action.action_index, STATUS_CONFLICT,
                 f"恢复失败：{move_err}"))
            if is_conflict:
                result.conflict += 1
                result.conflicts.append(
                    f"{action.source_rel}：{move_err}")
            else:
                result.errors += 1
            result.errors_list.append(
                f"恢复失败：{action.source_rel} ← {move_err}")
            _flush_undo()
            continue
        pending_updates.append(
            (action.action_index, STATUS_UNDONE, nd_note))
        if nd_note:
            result.notes.append(f"{action.source_rel}：{nd_note}")
        result.restored += 1
        _flush_undo()

    _flush_undo(force=True)

    # 清理本批次创建且当前为空的目录（含父级，直到整理空间根）
    _cleanup_empty_dirs(root, batch.created_dirs)

    # ---- 批次最终状态（不得假成功，中途停止也不能写成 undone）----
    if db_write_failed:
        # 写库失败的批次保持原状态（未撤销），不标记 undone
        pass
    elif result.was_cancelled:
        # 停下时已恢复的保持 undone，没处理的保持 moved。
        # 批次用 partial_undone，下次「撤销」还能继续，不写完成时间。
        if result.restored > 0 or result.conflict > 0 or result.errors > 0:
            storage.update_batch_status(
                batch.batch_id, BATCH_PARTIAL_UNDONE, finished_at=False)
    elif result.conflict == 0 and result.errors == 0 and result.restored > 0:
        storage.update_batch_status(batch.batch_id, BATCH_UNDONE, finished_at=True)
    elif result.restored == 0 and result.conflict == 0 and result.errors == 0:
        # 没有任何项被恢复：不标记 undone、不算完整成功，批次保持原状态
        result.errors_list.append(
            "没有恢复任何文件（可能已全部撤销过，或本批次没有可恢复的移动项），"
            "批次保持原状态，未标记为已撤销。")
    elif result.conflict > 0 or result.errors > 0:
        storage.update_batch_status(batch.batch_id, BATCH_PARTIAL_FAILED_UNDONE,
                                    finished_at=True)

    storage.conn.execute(
        "UPDATE batches SET summary = ? WHERE id = ?",
        (f"撤销 恢复 {result.restored} / 冲突 {result.conflict} / 错误 {result.errors}",
         batch.batch_id),
    )
    storage.conn.commit()

    return result


# ======================== 中断恢复 ========================

def recover_batch(storage: Storage, batch_id: Optional[int] = None,
                  progress_cb: Optional[Callable[[int, int, str], None]] = None,
                  cancel_event: Optional[threading.Event] = None) -> RecoverResult:
    """
    核对未完成批次的磁盘实际状态，决定每个 planned 动作的真实情况。

    V1.2 第二轮：可选 progress_cb(checked, total, current_path)——
    后台核对恢复线程用它汇报进度（按“待核对动作数”统计口径），
    原有不传回调的调用方式完全兼容。

    核心核对（每个 status=planned 的动作）：
      A) src exists + target not exists
         → 动作还没执行，保持 planned（下次启动会继续）

      B) src not exists + target exists + sha(target) == source_sha256
         → 移动成功但状态没写进去（进程在 os.rename 后、update_action 前被杀）
         → 补记为 moved

      C) src not exists + target exists + sha(target) != source_sha256
         → 磁盘变化不可信，暂停人工处理

      D) src not exists + target not exists
         → 什么情况都有可能（被其他程序删了、移动了之后又被移走、……）
         → 暂停人工处理

      E) src exists + target exists
         → 磁盘状态和 planned 预期不符（target 不该存在）
         → 暂停人工处理

      F) src exists + target exists + sha(target) == source_sha256
         → 可能是跨文件系统场景先 copy 成功，os.rename 失败 → 已复制，
           但原文件还在（没删干净）。这种情况直接补记 moved 会导致
           两个副本存在。标记 skipped 可能更安全——两个文件都保留。
         → 实际更保守：paused

    对 status=cancelled 的 planned 动作：同样核对，发现已移动则补记 moved。
    """
    result = RecoverResult()

    # 找到批次
    if batch_id is None:
        # 找最近一个执行中/部分完成的批次
        cur = storage.conn.execute(
            "SELECT id FROM batches WHERE status IN (?, ?, ?, ?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (BATCH_PLANNING, BATCH_EXECUTING,
             BATCH_PARTIAL, BATCH_CANCELLED, BATCH_RECOVERED_PARTIAL),
        )
        row = cur.fetchone()
        if not row:
            cur = storage.conn.execute(
                "SELECT id FROM batches ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                result.report.append("没有任何批次记录")
                return result
        batch_id = row[0]

    batch = storage.get_batch(batch_id)
    if batch is None:
        result.report.append(f"批次 #{batch_id} 不存在")
        return result

    result.batch_id = batch_id
    # 目录改名/移动后，按当前根目录核对文件（批次里的旧 root 仅作记录）
    root = os.path.abspath(storage.display_root)

    # 只处理 status ∈ {planned, cancelled} 的动作
    # 进度统计口径：待核对（planned/cancelled）动作数
    all_actions = storage.get_actions(batch_id)
    to_check = [a for a in all_actions
                if a.status in (STATUS_PLANNED, STATUS_CANCELLED)]
    total_to_check = len(to_check)
    checked = 0

    for action in to_check:
        # S3 / guia F-06：核对恢复可协作取消
        if cancel_event is not None and cancel_event.is_set():
            result.report.append("核对已取消（未处理的项目保持原状态）")
            break
        if progress_cb is not None:
            try:
                progress_cb(checked, total_to_check,
                            os.path.join(root, action.source_rel))
            except Exception:
                pass   # 进度回调异常不影响核对本身
        checked += 1

        src_abs = os.path.join(root, action.source_rel)
        tgt_abs = os.path.join(root, action.target_rel)

        src_exists = os.path.isfile(win_long_path(src_abs))
        tgt_exists = os.path.isfile(win_long_path(tgt_abs))
        tgt_sha = sha256_file(tgt_abs) if tgt_exists else None
        tgt_sha_matches = (tgt_sha == action.source_sha256) and (action.source_sha256 != "")
        # S3 / F-10：旧记录没有内容指纹时，用大小做保守核对（并在说明里标注）
        size_matches = False
        if tgt_exists and not action.source_sha256:
            try:
                size_matches = (os.path.getsize(win_long_path(tgt_abs))
                                == int(action.source_size or 0))
            except OSError:
                size_matches = False
        can_confirm_moved = tgt_sha_matches or (
            tgt_exists and not action.source_sha256 and size_matches)

        case_desc = ""

        if src_exists and not tgt_exists:
            # 情况 A：还没执行 → 保持 planned
            case_desc = f"A:src在+target不在 → 保持{action.status}"
            result.still_planned += 1
            # 不改 action 的 status

        elif not src_exists and tgt_exists and can_confirm_moved:
            # 情况 B：移动成功但状态没写 → 补记 moved
            if tgt_sha_matches:
                case_desc = "B:src消失+target在+sha匹配 → 补记moved"
                note = "[恢复补记] 进程中断后核对"
            else:
                case_desc = "B:src消失+target在+大小一致（无指纹）→ 补记moved"
                note = "[恢复补记] 进程中断后核对（原记录无内容指纹，仅按大小核对）"
            storage.update_action(batch_id, action.action_index, STATUS_MOVED,
                                  error=note)
            result.recovered_moved += 1

        elif not src_exists and tgt_exists and tgt_sha is None:
            # 目标存在但读不出来：不能说成"内容不符"
            case_desc = "C:src消失+target无法读取 → paused"
            storage.update_action(
                batch_id, action.action_index, STATUS_PAUSED,
                error="[恢复] 目标文件无法读取（权限/占用），需人工确认")
            result.paused += 1

        elif not src_exists and tgt_exists and not can_confirm_moved:
            # 情况 C
            case_desc = "C:src消失+target在+内容不符 → paused"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error=f"[恢复] target SHA={tgt_sha[:10] if tgt_sha else 'none'} ≠ 预期")
            result.paused += 1

        elif not src_exists and not tgt_exists:
            # 情况 D
            case_desc = f"D:src消失+target消失 → paused"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error="[恢复] src 和 target 都不存在")
            result.paused += 1

        elif src_exists and tgt_exists:
            # 情况 E：src 在、target 也在
            # 如果 target SHA 匹配，可能是 copy+del 没删干净。保守标记 paused。
            if tgt_sha_matches:
                case_desc = f"F:src在+target在+sha匹配 → paused (可能 copy 未删干净)"
                storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                      error="[恢复] src 和 target 同时存在，可能跨盘 copy 未删")
            else:
                case_desc = f"E:src在+target在+sha不匹配 → paused"
                storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                      error="[恢复] src 和 target 同时存在且 target 内容不符")
            result.paused += 1

        else:
            case_desc = f"???:无法分类"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error="[恢复] 无法分类的磁盘状态")
            result.paused += 1

        line = f"  [{action.status}] {action.source_rel} → {action.target_rel} | {case_desc}"
        result.report.append(line)

    # 更新批次最终状态
    actions_now = storage.get_actions(batch_id)
    moved = sum(1 for a in actions_now if a.status == STATUS_MOVED)
    failed = sum(1 for a in actions_now if a.status == STATUS_FAILED)
    skipped = sum(1 for a in actions_now if a.status == STATUS_SKIPPED)
    cancelled = sum(1 for a in actions_now if a.status == STATUS_CANCELLED)
    paused = sum(1 for a in actions_now if a.status == STATUS_PAUSED)
    planned_still = sum(1 for a in actions_now if a.status == STATUS_PLANNED)

    if paused > 0:
        final_status = BATCH_RECOVERED_PARTIAL
    elif planned_still > 0 and moved > 0:
        # 有已移动的，也有还没执行的 —— 恢复后状态不完整
        final_status = BATCH_RECOVERED_PARTIAL
    elif planned_still > 0 and moved == 0:
        # 全是 planned —— 从未真正执行过
        final_status = BATCH_CANCELLED_CLEAN
    elif moved > 0:
        final_status = BATCH_PARTIAL if (failed or skipped or cancelled) else BATCH_DONE
    elif cancelled > 0 and moved == 0:
        final_status = BATCH_CANCELLED_CLEAN
    elif cancelled > 0:
        final_status = BATCH_CANCELLED
    elif failed or skipped:
        final_status = BATCH_PARTIAL
    else:
        final_status = BATCH_CANCELLED_CLEAN

    # 仍有 planned/paused 的批次没有结束，不写 finished_at，避免和后续执行交错
    storage.update_batch_status(
        batch_id, final_status, finished_at=_batch_is_finished(final_status))
    result.batch_status_after = final_status

    storage.conn.execute(
        "UPDATE batches SET summary = ? WHERE id = ?",
        (f"[恢复] moved={moved} failed={failed} skipped={skipped} "
         f"cancelled={cancelled} paused={paused}",
         batch_id),
    )
    storage.conn.commit()

    return result


# ======================== 辅助 ========================

def _is_within_root(path: str, root: str) -> bool:
    """解析符号链接后，path 的真实路径是否仍在 root 的真实路径之内。

    用于拦截「_已整理 等目录是指向空间外的符号链接」导致文件/目录逃逸。
    用户选定的整理空间本身若是符号链接，以 realpath(root) 为基准，
    因此仍允许在该空间内整理；只拦截从空间内部逃到空间外的路径。
    """
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    root_key = os.path.normcase(real_root)
    path_key = os.path.normcase(real_path)
    return path_key == root_key or path_key.startswith(root_key + os.sep)


def _makedirs_tracked(root: str, target_dir: str) -> List[str]:
    """逐级创建目录，返回本次新建的每一级相对路径（父 → 子）。

    S3 / F-19：只记录叶子目录会让撤销后残留 `_已整理` 等父目录。
    """
    # 边界：目标目录的真实路径必须留在整理空间内，防止符号链接逃逸到空间外建目录。
    if not _is_within_root(target_dir, root):
        raise OSError(f"目标目录越出整理空间（符号链接逃逸）：{target_dir}")
    root_abs = os.path.abspath(root)
    target_abs = os.path.abspath(target_dir)
    missing: List[str] = []
    cur = target_abs
    root_key = os.path.normcase(root_abs)
    while True:
        cur_key = os.path.normcase(cur)
        if cur_key == root_key or not cur_key.startswith(root_key + os.sep):
            break
        if os.path.isdir(win_long_path(cur)):
            break
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    created: List[str] = []
    for directory in reversed(missing):
        os.makedirs(win_long_path(directory), exist_ok=True)
        created.append(os.path.relpath(directory, root_abs))
    return created


def _cleanup_empty_dirs(root: str, rel_dirs) -> None:
    """删除本批次创建、当前为空的目录，并向上回溯父目录直到根。"""
    root_abs = os.path.abspath(root)
    root_key = os.path.normcase(root_abs)
    candidates = set()
    for rel_dir in rel_dirs:
        candidates.add(os.path.abspath(os.path.join(root_abs, rel_dir)))
    # 深的先删
    for abs_dir in sorted(candidates, key=lambda p: p.count(os.sep), reverse=True):
        cur = abs_dir
        while True:
            cur_key = os.path.normcase(cur)
            if cur_key == root_key or not cur_key.startswith(root_key + os.sep):
                break
            if not os.path.isdir(win_long_path(cur)):
                break
            try:
                if os.listdir(win_long_path(cur)):
                    break
                os.rmdir(win_long_path(cur))
            except OSError:
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent


def acknowledge_batch_handled(storage: Storage, batch_id: int) -> None:
    """S3 / F-24：冲突批次的出口——用户确认已人工处理后不再反复询问。"""
    storage.acknowledge_conflict_batch(batch_id)


def _batch_is_finished(status: str) -> bool:
    """只有这次操作已经收束的批次才写完成时间。

    planning / executing / recovered_partial / partial_undone 还可能继续，
    写上完成时间会让下一次整理和旧批次分不清先后。
    """
    return status in (
        BATCH_DONE, BATCH_UNDONE, BATCH_PARTIAL, BATCH_CANCELLED,
        BATCH_CANCELLED_CLEAN, BATCH_PARTIAL_FAILED_UNDONE,
    )


def _copy_then_replace(src_path: str, dst_path: str) -> tuple[bool, str, bool]:
    """S1-04：os.rename 失败后的"不覆盖"移动兜底，替代 shutil.move。

    shutil.move 在目标已存在时会静默覆盖用户文件。这里改为：
      1) 复制源文件到目标同目录下的临时名（copy2 保留元数据）；
      2) 再次确认目标不存在——存在则记冲突：不覆盖、不删源、清掉临时副本；
      3) os.replace 原子替换（临时名与目标同目录，同文件系统，原子有效）；
      4) 删除源文件；删除失败则回滚目标，保留源，报错（宁可没移动，
         也不留两份或丢文件）。

    返回 (是否成功, 错误说明, 是否为"目标已存在"冲突)。
    """
    tmp_path = dst_path + ".archivetmp"
    counter = 0
    while os.path.exists(win_long_path(tmp_path)):
        counter += 1
        tmp_path = f"{dst_path}.archivetmp{counter}"
    try:
        shutil.copy2(win_long_path(src_path), win_long_path(tmp_path))
    except OSError as e:
        return False, f"复制到临时文件失败：{e}", False

    dst_long = win_long_path(dst_path)
    # 先独占创建目标名，挡住「检查时不存在、替换前被别人建出来」的覆盖窗口。
    # 创建出来的空文件马上被同目录的临时副本原子替换。
    try:
        exclusive = os.open(dst_long, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, "目标路径已存在，为避免覆盖未移动", True
    except OSError as e:
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, f"无法独占创建目标：{e}", False
    else:
        os.close(exclusive)

    try:
        os.replace(win_long_path(tmp_path), dst_long)
    except OSError as e:
        try:
            os.unlink(dst_long)
        except OSError:
            pass
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, f"原子替换目标失败：{e}", False

    try:
        os.unlink(win_long_path(src_path))
    except OSError as e:
        # 源删除失败：回滚目标，保留源。回滚也失败时两份都还在，必须照实说明。
        try:
            os.unlink(dst_long)
        except OSError as rollback_err:
            return False, (
                f"删除源失败，回滚目标也失败。源文件仍在：{src_path}；"
                f"目标副本仍在：{dst_path}。"
                f"（删除源：{e}；删除副本：{rollback_err}）"
            ), False
        return False, f"已复制但删除源失败（已回滚目标副本，源文件保留）：{e}", False
    return True, "", False


def _finalize_batch(storage: Storage, batch_id: int, result: ExecuteResult,
                    force_unfinished: bool = False):
    """根据执行结果更新批次最终状态。

    S1-02：force_unfinished=True（执行期间发生过写库失败）时，
    批次必须落在"未完成"状态并提交，绝不能标成 done。
    """
    if force_unfinished:
        final_status = BATCH_CANCELLED if result.was_cancelled else BATCH_PARTIAL
    elif result.was_cancelled:
        if result.moved == 0 and result.skipped == 0 and result.failed == 0:
            final_status = BATCH_CANCELLED_CLEAN
        else:
            final_status = BATCH_CANCELLED
    elif result.failed == 0 and result.skipped == 0 and result.cancelled == 0:
        final_status = BATCH_DONE
    else:
        final_status = BATCH_PARTIAL

    try:
        storage.update_batch_status(batch_id, final_status, finished_at=True)
        storage.conn.execute(
            "UPDATE batches SET summary = ? WHERE id = ?",
            (f"移动 {result.moved} / 跳过 {result.skipped} / 失败 {result.failed}"
             f" / 取消 {result.cancelled}"
             + ("（含写库失败，批次未完成）" if force_unfinished else ""),
             batch_id),
        )
        storage.conn.commit()
    except Exception as e:
        # 连最终状态都写不进去：尽力直接提交状态，至少不再静默吞掉
        try:
            storage.conn.execute(
                "UPDATE batches SET status = ? WHERE id = ?",
                (BATCH_PARTIAL, batch_id),
            )
            storage.conn.commit()
        except Exception:
            pass
        result.errors.append(f"标记批次最终状态失败：{e}")


def undo_is_available(storage: Storage) -> bool:
    """是否存在可撤销的最近批次（done 批次仍可手动撤销）。"""
    return storage.get_undoable_batch() is not None


def has_unrecovered_batch(storage: Storage) -> bool:
    """是否存在需要中断恢复的批次（executing/planning/partial/cancelled）。"""
    cur = storage.conn.execute(
        "SELECT COUNT(*) FROM batches WHERE status IN (?, ?, ?, ?)",
        (BATCH_PLANNING, BATCH_EXECUTING, BATCH_PARTIAL, BATCH_CANCELLED),
    )
    return cur.fetchone()[0] > 0


def interrupted_batch_id(storage: Storage) -> Optional[int]:
    """返回仍在执行中（上次运行被中断）的批次 ID；没有则 None。"""
    cur = storage.conn.execute(
        "SELECT id FROM batches WHERE status IN (?, ?) ORDER BY id DESC LIMIT 1",
        (BATCH_PLANNING, BATCH_EXECUTING),
    )
    row = cur.fetchone()
    return row[0] if row else None
