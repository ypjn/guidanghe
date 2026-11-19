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
    STATUS_RESTORING,
    BATCH_PLANNING, BATCH_EXECUTING, BATCH_DONE, BATCH_PARTIAL,
    BATCH_CANCELLED, BATCH_CANCELLED_CLEAN, BATCH_RECOVERED_PARTIAL,
    BATCH_UNDONE, BATCH_PARTIAL_UNDONE, BATCH_PARTIAL_FAILED_UNDONE,
    BATCH_PENDING_STATUSES,
)
from utils import sha256_file, win_long_path, strip_long_prefix

# 执行/撤销期间把状态写库的缓冲大小：同一批次批量写入，避免每项两次 commit。
_DB_FLUSH_EVERY = 20

_LOCK_FILENAME = "space.lock"


class SpaceBusyError(RuntimeError):
    """整理空间正被另一个归档盒进程执行/撤销/核对。"""


class _SpaceLock:
    """整理空间级跨进程互斥（.organizer/space.lock）。

    只包住实际改文件和写库的过程；锁被占用时立即失败，不等待。
    使用系统文件锁而不是“创建锁文件”：进程崩溃后不会留下永久残留锁。
    """

    def __init__(self, root: str):
        self.path = os.path.join(os.path.abspath(root), ".organizer",
                                 _LOCK_FILENAME)
        self._handle = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            self._lock(handle)
        except OSError as e:
            handle.close()
            raise SpaceBusyError(
                "另一个归档盒进程正在整理这个空间，请稍后再试") from e
        self._handle = handle
        return self

    @staticmethod
    def _lock(handle) -> None:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def __exit__(self, *_exc):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._unlock(handle)
        try:
            handle.close()
        except OSError:
            pass


def space_lock(root: str) -> _SpaceLock:
    return _SpaceLock(root)


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
    # 结果没写进库：文件可能已经移动，必须核对，不能报可撤销
    db_write_failed: bool = False

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
    # 恢复结果没写进库：文件已回原位但记录仍是 moved，必须核对，不能报完整成功
    db_write_failed: bool = False

    @property
    def is_complete(self) -> bool:
        # 完整撤销必须没有拒绝、没有中途停止、没有冲突/错误/写库失败，并且确实有恢复。
        return (not self.refused and not self.was_cancelled
                and not self.db_write_failed and not self.errors_list
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
    # 用户中途取消：没核对完的批次保持待核对，不写成干净取消
    was_cancelled: bool = False

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
    """执行固定整理计划。支持取消（cancel_event）。

    前置条件：存在被中断/待核对的批次时拒绝开始，避免产生重叠批次。
    整个过程持有整理空间级跨进程锁。
    """
    result = ExecuteResult()
    root = os.path.abspath(plan.root)
    try:
        with space_lock(root):
            _execute_plan_locked(plan, storage, result, progress_cb,
                                 cancel_event)
    except SpaceBusyError as e:
        result.errors.append(str(e))
    return result


def _execute_plan_locked(
    plan: OrganizePlan,
    storage: Storage,
    result: ExecuteResult,
    progress_cb,
    cancel_event,
) -> None:
    root = os.path.abspath(plan.root)

    blocked = interrupted_batch_id(storage)
    if blocked is not None:
        result.errors.append(
            f"存在被中断或待核对的批次 #{blocked}，请先核对恢复或撤销该批次后再开始新的整理"
        )
        return

    selected_items = [i for i in plan.items if i.selected]
    result.batch_id = storage.create_batch(plan, f"待执行 {len(selected_items)} 项")

    # 步骤 2：先持久化所有执行意图。
    # 预览时没有算出指纹的条目一律不执行（不能到执行时才悄悄补算，
    # 否则预览后被改过的文件仍会按旧计划移动）。
    # S3 / F-20：整批一次性写入（单次 commit），不再每个动作单独 commit。
    sha_by_index: List[str] = [item.source_sha256 or "" for item in selected_items]
    try:
        storage.create_actions_bulk(result.batch_id, selected_items, sha_by_index)
    except Exception as e:
        result.db_write_failed = True
        result.errors.append(
            f"写入执行意图失败（共 {len(selected_items)} 项），已中止整理：{e}")
        try:
            storage.update_batch_status(result.batch_id, BATCH_PLANNING)
            storage.conn.commit()
        except Exception:
            pass
        return
    storage.update_batch_status(result.batch_id, BATCH_EXECUTING)

    # 执行前再对计划路径做一次保护范围核对（预览后用户可能新增了保护）
    from protect_config import load_manual_protect
    from scanner import protected_path_reason
    try:
        manual_specs = [(p, "manual", "用户手动保护")
                        for p in load_manual_protect(root)]
    except Exception:
        manual_specs = []

    # 步骤 3-4：逐项执行
    created_dirs: set[str] = set()
    pending_updates: List[tuple] = []
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
            result.db_write_failed = True
            result.errors.append(
                f"写入整理记录失败（{len(pending_updates)} 项），已停止后续文件操作：{e}")
            pending_updates.clear()

    def _record(action_idx: int, status: str, error: str = "") -> None:
        # 每条结果立即落库：写库失败时才能在下一个文件操作前停下。
        pending_updates.append((action_idx, status, error))
        _flush(force=True)

    for idx, item in enumerate(selected_items):
        # 写库失败后不再移动任何后续文件（结果没落库，必须先核对）
        if db_write_failed:
            break
        # ---- 取消检查（在每个文件操作前/后）----
        if cancel_event and cancel_event.is_set():
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

        if progress_cb:
            progress_cb(idx + 1, len(selected_items), item.file.rel_path)

        guard = protected_path_reason(
            root, src_path, manual_specs, True)
        if guard:
            _record(idx, STATUS_SKIPPED, error=f"受保护范围：{guard}")
            result.skipped += 1
            result.errors.append(
                f"跳过（受保护范围：{guard}）：{item.file.rel_path}")
            continue

        # ---- 核验 ----
        if not os.path.isfile(win_long_path(src_path)):
            _record(idx, STATUS_SKIPPED, error="源文件已不存在")
            result.skipped += 1
            result.errors.append(f"跳过（源文件已不存在）：{item.file.rel_path}")
            continue

        stored_sha = sha_by_index[idx]
        if not stored_sha:
            # 没有指纹就不能内容核验，也不能当成“无需比对”直接移动。
            reason = "缺少内容指纹（预览不完整或读取失败），请重新生成预览后再整理"
            _record(idx, STATUS_SKIPPED, error=reason)
            result.skipped += 1
            result.errors.append(f"跳过（{reason}）：{item.file.rel_path}")
            continue
        current_sha = sha256_file(src_path)
        if current_sha is None:
            reason = "无法读取源文件内容（权限/占用），未移动"
            _record(idx, STATUS_SKIPPED, error=reason)
            result.skipped += 1
            result.errors.append(f"跳过（{reason}）：{item.file.rel_path}")
            continue
        if stored_sha != current_sha:
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

        # ---- 移动：目标已存在必须失败，绝不覆盖 ----
        moved_ok, move_err, is_conflict = _move_no_replace(src_path, target_path)
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
    _finalize_batch(storage, result.batch_id, result,
                    force_unfinished=db_write_failed)


# ======================== 撤销器 ========================

def undo_latest_batch(
    storage: Storage,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    batch_id: Optional[int] = None,
) -> UndoResult:
    """撤销一个批次（默认最近一个真正移动过文件的批次）。

    V1.7 安全复核：
    - 历史选中项、确认框、后台动作使用同一个 batch_id；
    - 仍需核对的批次不进入撤销，先引导核对；
    - 撤销前先写入可持久化的 restoring 意图，写库失败立刻停止文件操作；
    - 已核对的 planned 项留在原处，不因为旁边还有 planned 就拒绝整批；
    - is_complete 在存在写库失败或 errors_list 时必须为假。
    """
    result = UndoResult()
    root = os.path.abspath(storage.display_root)
    try:
        with space_lock(root):
            _undo_batch_locked(storage, result, progress_cb, cancel_event,
                               batch_id)
    except SpaceBusyError as e:
        result.refused = True
        result.errors_list.append(str(e))
    return result


def _undo_batch_locked(
    storage: Storage,
    result: UndoResult,
    progress_cb,
    cancel_event,
    batch_id: Optional[int],
) -> None:
    root = os.path.abspath(storage.display_root)

    # 仍需核对的批次不要进撤销（先核对，避免对状态不明的文件下手）
    needs_check = storage.get_batch_needing_check()
    if needs_check is not None and (
            batch_id is None or batch_id == needs_check.batch_id):
        result.batch_id = needs_check.batch_id
        result.refused = True
        result.errors_list.append(
            f"批次 #{needs_check.batch_id} 还有未核对或状态未确定的项目。\n"
            "请先「核对恢复」确认每项的真实状态，再执行撤销。")
        return

    batch = (storage.get_batch(batch_id) if batch_id is not None
             else storage.get_undoable_batch())
    if batch is None:
        result.errors_list.append("没有可撤销的批次")
        return

    result.batch_id = batch.batch_id

    # ---- 拒绝条件 1：批次本身状态未确定（还在 planning/executing）----
    if batch.status in (BATCH_PLANNING, BATCH_EXECUTING):
        result.refused = True
        result.errors_list.append(
            f"批次 #{batch.batch_id} 的状态还是「{batch.status}」"
            "（执行过程未确认结束），撤销被拒绝。\n"
            "请先通过「核对恢复」确认每项的真实状态，再决定是否撤销。")
        return

    actions = storage.get_actions(batch.batch_id)

    # ---- 拒绝条件 2：仍有未核对 planned / paused / restoring 条目 ----
    unfinished = [a for a in actions
                  if (a.status == STATUS_PLANNED and not a.verified)
                  or a.status in (STATUS_PAUSED, STATUS_RESTORING)]
    if unfinished:
        result.refused = True
        result.errors_list.append(
            f"批次 #{batch.batch_id} 还有 {len(unfinished)} 个状态未确定的条目"
            "（未核对 planned / paused / restoring），撤销被拒绝：\n"
            "  " + "\n  ".join(a.source_rel for a in unfinished[:10])
            + ("…" if len(unfinished) > 10 else "")
            + "\n请先「核对恢复」或人工处理这些条目，再执行撤销。")
        return

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
            result.db_write_failed = True
            result.errors_list.append(
                f"写库失败（{len(pending_updates)} 项），已停止后续文件操作：{e}")
            pending_updates.clear()

    def _mark_conflict(action, message: str) -> None:
        pending_updates.append((action.action_index, STATUS_CONFLICT, message))
        result.conflict += 1
        result.conflicts.append(f"{action.source_rel}：{message}")
        _flush_undo(force=True)

    for idx, action in enumerate(recoverables):
        if db_write_failed:
            result.remaining = len(recoverables) - idx
            break
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
            continue
        if action.source_sha256 and current_sha != action.source_sha256:
            _mark_conflict(action, "目标内容已变化（与记录的内容指纹不一致）")
            continue

        if os.path.exists(win_long_path(source_abs)):
            _mark_conflict(action, "原路径已被占用，不能恢复")
            continue

        # 先持久化撤销意图：进程若在移动后、结果落库前中断，
        # 重新打开后可以按路径和内容指纹把这次恢复补记回来。
        try:
            storage.update_action(batch.batch_id, action.action_index,
                                  STATUS_RESTORING, commit=True)
        except Exception as e:
            db_write_failed = True
            result.db_write_failed = True
            result.errors_list.append(
                f"写入撤销意图失败，已停止后续文件操作：{e}")
            result.remaining = len(recoverables) - idx
            break

        source_dir = os.path.dirname(source_abs)
        if source_dir:
            try:
                for rel_dir in _makedirs_tracked(root, source_dir):
                    # 撤销时重建的父目录同样登记
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
                    f"恢复失败：{action.source_rel} ← {e}")
                _flush_undo(force=True)
                continue

        moved_ok, move_err, is_conflict = _move_no_replace(target_abs, source_abs)
        if not moved_ok:
            if is_conflict:
                _mark_conflict(action, move_err)
            else:
                pending_updates.append(
                    (action.action_index, STATUS_CONFLICT,
                     f"恢复失败：{move_err}"))
                result.errors += 1
                result.errors_list.append(
                    f"恢复失败：{action.source_rel} ← {move_err}")
                _flush_undo(force=True)
            continue
        pending_updates.append(
            (action.action_index, STATUS_UNDONE, nd_note))
        if nd_note:
            result.notes.append(f"{action.source_rel}：{nd_note}")
        result.restored += 1
        _flush_undo(force=True)

    _flush_undo(force=True)

    # 清理本批次创建且当前为空的目录（只删本次登记为新建的目录）
    _cleanup_empty_dirs(root, batch.created_dirs)

    # ---- 批次最终状态（不得假成功，中途停止也不能写成 undone）----
    if db_write_failed:
        # 结果没写进库：批次保持原状态待核对，不标记 undone，不报完整成功
        result.errors_list.append(
            "撤销结果尚未写入记录，需要核对；不要再把本批次当作已完成撤销。")
    elif result.was_cancelled:
        # 停下时已恢复的保持 undone，没处理的保持 moved。
        if result.restored > 0 or result.conflict > 0 or result.errors > 0:
            storage.update_batch_status(
                batch.batch_id, BATCH_PARTIAL_UNDONE, finished_at=False)
    elif result.conflict == 0 and result.errors == 0 and result.restored > 0:
        # 已核对的 planned 项从未离开原处，不影响把本批次标记为已撤销
        storage.update_batch_status(batch.batch_id, BATCH_UNDONE, finished_at=True)
    elif result.restored == 0 and result.conflict == 0 and result.errors == 0:
        result.errors_list.append(
            "没有恢复任何文件（可能已全部撤销过，或本批次没有可恢复的移动项），"
            "批次保持原状态，未标记为已撤销。")
    elif result.conflict > 0 or result.errors > 0:
        storage.update_batch_status(batch.batch_id, BATCH_PARTIAL_FAILED_UNDONE,
                                    finished_at=True)

    try:
        storage.conn.execute(
            "UPDATE batches SET summary = ? WHERE id = ?",
            (f"撤销 恢复 {result.restored} / 冲突 {result.conflict} "
             f"/ 错误 {result.errors}"
             + ("（含写库失败，未确认）" if db_write_failed else ""),
             batch.batch_id),
        )
        storage.conn.commit()
    except Exception:
        pass


# ======================== 中断恢复 ========================

def recover_batch(storage: Storage, batch_id: Optional[int] = None,
                  progress_cb: Optional[Callable[[int, int, str], None]] = None,
                  cancel_event: Optional[threading.Event] = None) -> RecoverResult:
    """核对未完成批次的磁盘实际状态，补记或暂停。

    V1.7 安全复核：
    - 核对过程持有整理空间锁，和整理/撤销互斥；
    - planned、cancelled、paused、restoring 都要重新核对（paused 不能永久跳过）；
    - planned 项核对为“仍在原处”时写入 verified，和“没核对过”区分开；
    - 取消核对时批次保持待核对，绝不写成 cancelled_clean；
    - 只有全部核对完且没有任何已移动文件，才允许干净取消。
    """
    result = RecoverResult()
    root = os.path.abspath(storage.display_root)
    with space_lock(root):
        _recover_batch_locked(storage, result, batch_id, progress_cb,
                              cancel_event)
    return result


def _size_matches(path: str, expected: int) -> bool:
    try:
        return os.path.getsize(win_long_path(path)) == int(expected or 0)
    except OSError:
        return False


def _recover_batch_locked(storage: Storage, result: RecoverResult,
                          batch_id: Optional[int], progress_cb,
                          cancel_event) -> None:
    root = os.path.abspath(storage.display_root)

    if batch_id is None:
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
                return
        batch_id = row[0]

    batch = storage.get_batch(batch_id)
    if batch is None:
        result.report.append(f"批次 #{batch_id} 不存在")
        return

    result.batch_id = batch_id
    result.batch_status_after = batch.status

    # planned / cancelled 尚未执行；paused / restoring 需要重新核对
    all_actions = storage.get_actions(batch_id)
    to_check = [a for a in all_actions
                if a.status in (STATUS_PLANNED, STATUS_CANCELLED,
                                STATUS_PAUSED, STATUS_RESTORING)]
    total_to_check = len(to_check)
    checked = 0
    # 之前因“状态不明确”暂停、这次核对为“仍在原处且内容一致”的条目数。
    # 它们不再算暂停，也不让批次仅因此停留在 recovered_partial。
    reclassified_paused = 0

    for action in to_check:
        if cancel_event is not None and cancel_event.is_set():
            result.was_cancelled = True
            result.report.append("核对已取消（未处理的项目保持待核对）")
            break
        if progress_cb is not None:
            try:
                progress_cb(checked, total_to_check,
                            os.path.join(root, action.source_rel))
            except Exception:
                pass
        checked += 1

        src_abs = os.path.join(root, action.source_rel)
        tgt_abs = os.path.join(root, action.target_rel)

        src_exists = os.path.isfile(win_long_path(src_abs))
        tgt_exists = os.path.isfile(win_long_path(tgt_abs))
        tgt_sha = sha256_file(tgt_abs) if tgt_exists else None
        tgt_sha_matches = bool(action.source_sha256) and tgt_sha == action.source_sha256
        src_sha = sha256_file(src_abs) if src_exists else None
        src_sha_matches = bool(action.source_sha256) and src_sha == action.source_sha256
        if src_exists and not action.source_sha256:
            src_sha_matches = _size_matches(src_abs, action.source_size)
        can_confirm_moved = tgt_sha_matches or (
            tgt_exists and not action.source_sha256
            and _size_matches(tgt_abs, action.source_size))

        case_desc = ""

        if action.status == STATUS_RESTORING:
            # 撤销意图已落库：只可能是“已恢复”“还没恢复”或“不确定”
            if src_exists and not tgt_exists and src_sha_matches:
                case_desc = "R:src在+target不在+内容一致 → 补记undone"
                storage.update_action(batch_id, action.action_index,
                                      STATUS_UNDONE,
                                      error="[恢复补记] 撤销完成后核对",
                                      verified=True)
            elif (not src_exists and tgt_exists and can_confirm_moved):
                case_desc = "R:src不在+target在 → 恢复为moved，可再次撤销"
                storage.update_action(batch_id, action.action_index,
                                      STATUS_MOVED,
                                      error="[恢复] 撤销未完成，文件仍在目标位置",
                                      verified=True)
            else:
                case_desc = "R:状态不明确 → paused"
                storage.update_action(batch_id, action.action_index,
                                      STATUS_PAUSED,
                                      error="[恢复] 撤销过程中断，目标/原位置状态不明确",
                                      verified=False)
                result.paused += 1
            result.report.append(
                f"  [{action.status}] {action.source_rel} → "
                f"{action.target_rel} | {case_desc}")
            continue

        if src_exists and not tgt_exists:
            # 情况 A：还没执行（或 paused 后已恢复原状）→ 按磁盘事实留在原处
            if action.status == STATUS_PAUSED:
                if src_sha_matches:
                    # 之前暂停只是因为状态不明确；现在源文件在原处且内容一致，
                    # 应视为“已核对且尚未移动”，不再占用核对入口。
                    case_desc = "A:src在+target不在+内容一致 → paused改为planned（已核对）"
                    result.still_planned += 1
                    reclassified_paused += 1
                    storage.update_action(
                        batch_id, action.action_index, STATUS_PLANNED,
                        error="[恢复] 已核对仍在原处，按未执行处理",
                        verified=True)
                else:
                    # 源文件还在，但内容指纹与记录对不上：继续暂停
                    case_desc = "A:src在+target不在+内容不符 → 保持paused"
                    storage.update_action(
                        batch_id, action.action_index, STATUS_PAUSED,
                        error="[恢复] 源文件仍在原处，但内容指纹与记录不一致",
                        verified=False)
                    result.paused += 1
            else:
                case_desc = f"A:src在+target不在 → 确认{action.status}（已核对）"
                result.still_planned += 1
                storage.update_action(batch_id, action.action_index,
                                      action.status, error=action.error,
                                      verified=True)

        elif not src_exists and tgt_exists and can_confirm_moved:
            # 情况 B：移动成功但状态没写 → 补记 moved
            if tgt_sha_matches:
                case_desc = "B:src消失+target在+sha匹配 → 补记moved"
                note = "[恢复补记] 进程中断后核对"
            else:
                case_desc = "B:src消失+target在+大小一致（无指纹）→ 补记moved"
                note = "[恢复补记] 进程中断后核对（原记录无内容指纹，仅按大小核对）"
            storage.update_action(batch_id, action.action_index, STATUS_MOVED,
                                  error=note, verified=True)
            result.recovered_moved += 1

        elif not src_exists and tgt_exists and tgt_sha is None:
            case_desc = "C:src消失+target无法读取 → paused"
            storage.update_action(
                batch_id, action.action_index, STATUS_PAUSED,
                error="[恢复] 目标文件无法读取（权限/占用），需人工确认",
                verified=False)
            result.paused += 1

        elif not src_exists and tgt_exists and not can_confirm_moved:
            case_desc = "C:src消失+target在+内容不符 → paused"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error=f"[恢复] target SHA={tgt_sha[:10] if tgt_sha else 'none'} ≠ 预期",
                                  verified=False)
            result.paused += 1

        elif not src_exists and not tgt_exists:
            case_desc = "D:src消失+target消失 → paused"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error="[恢复] src 和 target 都不存在",
                                  verified=False)
            result.paused += 1

        elif src_exists and tgt_exists:
            if tgt_sha_matches:
                case_desc = "F:src在+target在+sha匹配 → paused（可能复制未删干净）"
                storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                      error="[恢复] src 和 target 同时存在，可能跨盘 copy 未删",
                                      verified=False)
            else:
                case_desc = "E:src在+target在+sha不匹配 → paused"
                storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                      error="[恢复] src 和 target 同时存在且 target 内容不符",
                                      verified=False)
            result.paused += 1

        else:
            case_desc = "???:无法分类"
            storage.update_action(batch_id, action.action_index, STATUS_PAUSED,
                                  error="[恢复] 无法分类的磁盘状态",
                                  verified=False)
            result.paused += 1

        result.report.append(
            f"  [{action.status}] {action.source_rel} → "
            f"{action.target_rel} | {case_desc}")

    # 核对被取消：没核对完的批次保持待核对，不写最终状态
    if result.was_cancelled:
        return

    actions_now = storage.get_actions(batch_id)
    moved = sum(1 for a in actions_now if a.status == STATUS_MOVED)
    failed = sum(1 for a in actions_now if a.status == STATUS_FAILED)
    skipped = sum(1 for a in actions_now if a.status == STATUS_SKIPPED)
    cancelled = sum(1 for a in actions_now if a.status == STATUS_CANCELLED)
    paused = sum(1 for a in actions_now if a.status == STATUS_PAUSED)
    planned_still = sum(1 for a in actions_now if a.status == STATUS_PLANNED)
    undone = sum(1 for a in actions_now if a.status == STATUS_UNDONE)
    # 原本就未执行（真正被中断）的 planned，和“暂停后核对为仍在原处”的分开看
    genuine_planned = max(0, planned_still - reclassified_paused)

    if paused > 0:
        final_status = BATCH_RECOVERED_PARTIAL
    elif planned_still > 0 and moved > 0:
        if genuine_planned > 0:
            # 有已移动的，也有真正还没执行的 —— 恢复后状态不完整
            final_status = BATCH_RECOVERED_PARTIAL
        else:
            # 剩余 planned 都是暂停核对后解释清楚的项，不是“仍未确定”
            final_status = BATCH_PARTIAL
    elif planned_still > 0 and moved == 0 and not (failed or skipped):
        # 每一项都核对过、且确实没有任何文件被移动 → 才允许干净取消
        final_status = BATCH_CANCELLED_CLEAN
    elif moved > 0:
        final_status = BATCH_PARTIAL if (failed or skipped) else BATCH_DONE
    elif undone > 0 and not (failed or skipped):
        # 撤销中断后补记完成：全部已恢复原位
        final_status = BATCH_UNDONE
    elif cancelled > 0 and moved == 0 and not (failed or skipped):
        final_status = BATCH_CANCELLED_CLEAN
    elif failed or skipped:
        final_status = BATCH_PARTIAL
    else:
        final_status = BATCH_CANCELLED_CLEAN

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


# ======================== 辅助 ========================

def _makedirs_tracked(root: str, target_dir: str) -> List[str]:
    """逐级创建目录，返回本次新建的每一级相对路径（父 → 子）。

    S3 / F-19：只记录叶子目录会让撤销后残留 `_已整理` 等父目录。
    """
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
    """只删除本批次登记为新建、且当前仍为空的目录。

    V1.7 安全复核：不再从登记目录一路向上删父目录。用户事先建好的空
    父目录（例如空的 _已整理/）不在登记里，必须保留。登记时
    _makedirs_tracked 只返回本次真正新建的层级，因此这里只处理这些目录。
    """
    root_abs = os.path.abspath(root)
    root_key = os.path.normcase(root_abs)
    candidates = set()
    for rel_dir in rel_dirs:
        abs_dir = os.path.abspath(os.path.join(root_abs, rel_dir))
        key = os.path.normcase(abs_dir)
        if key == root_key or not key.startswith(root_key + os.sep):
            continue
        candidates.add(abs_dir)
    # 深的先删；父级只有在同样被登记为新建时才会被处理到
    for abs_dir in sorted(candidates, key=lambda p: p.count(os.sep), reverse=True):
        if not os.path.isdir(win_long_path(abs_dir)):
            continue
        try:
            if os.listdir(win_long_path(abs_dir)):
                continue
            os.rmdir(win_long_path(abs_dir))
        except OSError:
            continue


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


def _move_no_replace(src_path: str, dst_path: str) -> tuple[bool, str, bool]:
    """把 src 移动为 dst；目标已存在时失败，绝不覆盖。

    同卷快路径不再用可能替换目标的 os.rename(POSIX)：
      - Windows：os.rename 目标存在会直接失败，可安全使用；
      - POSIX：用 os.link 先建立目标名（目标存在会抛 FileExistsError），
        成功后再删除源名，两个名字指向同一 inode。
    文件系统不支持硬链接或跨卷时，退回 _copy_then_replace（O_EXCL + 校验）。
    """
    src_long = win_long_path(src_path)
    dst_long = win_long_path(dst_path)
    try:
        if os.name == "nt":
            os.rename(src_long, dst_long)
            return True, "", False
        os.link(src_long, dst_long)
    except FileExistsError:
        return False, "目标路径已存在，为避免覆盖未移动", True
    except OSError:
        if os.path.exists(dst_long):
            return False, "目标路径已存在，为避免覆盖未移动", True
        # 跨卷 / 不支持硬链接 / 权限差异：走已校验的复制兜底
        return _copy_then_replace(src_path, dst_path)

    # 目标名已建立；删除源名。删除失败则回滚目标名，保留源文件。
    try:
        os.unlink(src_long)
    except OSError as e:
        try:
            os.unlink(dst_long)
        except OSError as rollback_err:
            return False, (
                f"删除源失败，回滚目标也失败。源文件仍在：{src_path}；"
                f"目标副本仍在：{dst_path}（删除源：{e}；删除副本：{rollback_err}）"
            ), False
        return False, f"已建立目标名但删除源失败（已回滚，源文件保留）：{e}", False
    return True, "", False


def _copy_then_replace(src_path: str, dst_path: str) -> tuple[bool, str, bool]:
    """跨卷/rename 失败时的"不覆盖"移动兜底。

    1) 复制源文件到目标同目录下的临时名（copy2 保留元数据）；
    2) 核对临时副本的大小与 SHA-256，和源文件一致才继续；
    3) 独占创建目标名挡住覆盖窗口；已存在则冲突，不覆盖、不删源；
    4) os.replace 原子替换（临时名与目标同目录）；
    5) 删除源文件；删除失败则回滚目标，保留源。

    复制中断时源文件仍在，目标最多是一个空的独占占位或半截临时名，
    都不会被恢复流程当成"已移动"（恢复要求内容指纹一致）。

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
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, f"复制到临时文件失败：{e}", False

    # 复制校验：大小和指纹一致才能认为副本完整
    try:
        src_size = os.path.getsize(win_long_path(src_path))
        tmp_size = os.path.getsize(win_long_path(tmp_path))
    except OSError as e:
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, f"复制后核对大小失败：{e}", False
    if src_size != tmp_size:
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, "复制校验失败（副本大小与源文件不一致），源文件保留", False
    src_sha = sha256_file(src_path)
    tmp_sha = sha256_file(tmp_path)
    if not src_sha or not tmp_sha or src_sha != tmp_sha:
        try:
            os.unlink(win_long_path(tmp_path))
        except OSError:
            pass
        return False, "复制校验失败（副本指纹与源文件不一致），源文件保留", False

    dst_long = win_long_path(dst_path)
    # 先独占创建目标名，挡住「检查时不存在、替换前被别人建出来」的覆盖窗口。
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

    V1.7：执行期间发生过写库失败时，批次保持 executing（待核对），
    绝不能标成 done 或普通 partial——结果没落库，必须重新核对磁盘事实。
    """
    if force_unfinished:
        # 保持"待核对"状态，不写完成时间；由核对恢复接手
        try:
            storage.update_batch_status(batch_id, BATCH_EXECUTING,
                                        finished_at=False)
            storage.conn.execute(
                "UPDATE batches SET summary = ? WHERE id = ?",
                (f"移动 {result.moved} / 跳过 {result.skipped} "
                 f"/ 失败 {result.failed} / 取消 {result.cancelled}"
                 "（结果尚未写入记录，需要核对）", batch_id),
            )
            storage.conn.commit()
        except Exception as e:
            result.errors.append(f"标记批次状态失败：{e}")
        return

    if result.was_cancelled:
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
             f" / 取消 {result.cancelled}", batch_id),
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
    """是否存在需要核对恢复的批次（含 paused / 结果未落库）。"""
    return storage.get_batch_needing_check() is not None


def interrupted_batch_id(storage: Storage) -> Optional[int]:
    """返回需要核对恢复的批次 ID（与主界面/历史页共用同一判定）。

    包含：planning/executing、还没核对过的 planned、paused、
    以及撤销中断留下的 restoring。普通的已完成/已失败项不再拦截。
    """
    return storage.get_verification_batch_id()
