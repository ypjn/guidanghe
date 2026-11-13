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
)
from utils import sha256_file


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
    batch_id: int = 0
    # S1-02/S1-03：批次状态未确定（planning/executing/planned/paused）时拒绝撤销
    refused: bool = False

    @property
    def is_complete(self) -> bool:
        # S1-02/S1-03：完整撤销必须满足——没有拒绝、没有冲突/错误，
        # 并且确实有恢复。恢复 0 项不能叫完整成功。
        return (not self.refused and self.restored > 0
                and self.conflict == 0 and self.errors == 0)

    def summary(self) -> str:
        if self.refused:
            return "撤销未执行（已拒绝）"
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
        parts = [f"核对批次 #{self.batch_id}"]
        if self.recovered_moved: parts.append(f"补记已移动 {self.recovered_moved}")
        if self.still_planned: parts.append(f"仍未执行 {self.still_planned}")
        if self.paused: parts.append(f"暂停待人工确认 {self.paused}")
        parts.append(f"批次状态 → {self.batch_status_after}")
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
    for idx, item in enumerate(selected_items):
        src_path = os.path.join(root, item.file.rel_path)
        sha = item.source_sha256 or sha256_file(src_path) or ""
        try:
            storage.create_action(result.batch_id, item, idx, sha)
        except Exception as e:
            result.errors.append(
                f"写入执行意图失败（{item.file.rel_path}），已中止整理：{e}")
            try:
                storage.update_batch_status(result.batch_id, BATCH_PLANNING)
                storage.conn.commit()
            except Exception:
                pass
            return result
    storage.update_batch_status(result.batch_id, BATCH_EXECUTING)

    # 步骤 3-4：逐项执行
    created_dirs: set[str] = set()
    # S1-02：单项写库失败不能让异常穿透整个执行循环——记录下来，
    # 循环结束后把批次明确标成未完成并提交。
    db_write_failed = False

    def _record(action_idx: int, status: str, error: str = "") -> None:
        nonlocal db_write_failed
        try:
            storage.update_action(result.batch_id, action_idx, status,
                                  error=error)
        except Exception as e:
            db_write_failed = True
            result.errors.append(
                f"写入整理记录失败（第 {action_idx + 1} 项，状态 {status}）：{e}")

    for idx, item in enumerate(selected_items):
        # ---- 取消检查（在每个文件操作前/后）----
        if cancel_event and cancel_event.is_set():
            # 把剩余所有 planned 动作标记为 cancelled
            remaining = selected_items[idx:]
            for remaining_idx, _ in enumerate(remaining):
                real_idx = idx + remaining_idx
                _record(real_idx, STATUS_CANCELLED, error="用户取消")
                result.cancelled += 1
            result.was_cancelled = True
            break

        src_path = os.path.join(root, item.file.rel_path)
        target_path = os.path.join(root, item.target_rel_path)
        target_dir = os.path.dirname(target_path)

        if progress_cb:
            progress_cb(idx + 1, len(selected_items), item.file.rel_path)

        # ---- 核验 ----
        if not os.path.isfile(src_path):
            _record(idx, STATUS_SKIPPED, error="源文件已不存在")
            result.skipped += 1
            result.errors.append(f"跳过（源文件已不存在）：{item.file.rel_path}")
            continue

        stored_sha = None
        for a in storage.get_actions(result.batch_id):
            if a.action_index == idx:
                stored_sha = a.source_sha256
                break
        current_sha = sha256_file(src_path)
        if stored_sha and current_sha and stored_sha != current_sha:
            _record(idx, STATUS_SKIPPED, error="源文件内容已变化")
            result.skipped += 1
            result.errors.append(f"跳过（源文件内容已变化）：{item.file.rel_path}")
            continue

        if os.path.exists(target_path):
            _record(idx, STATUS_SKIPPED, error="目标路径已被占用")
            result.skipped += 1
            result.errors.append(
                f"跳过（目标路径已被占用）：{item.file.rel_path} → "
                f"{item.target_rel_path}")
            continue

        # ---- 创建目标目录 ----
        if not os.path.isdir(target_dir):
            try:
                os.makedirs(target_dir, exist_ok=True)
                rel_dir = os.path.relpath(target_dir, root)
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

        # ---- 移动（原子操作，不中断）----
        try:
            os.rename(src_path, target_path)
        except OSError:
            # S1-04：不再用 shutil.move 兜底——它在目标已存在时会静默覆盖。
            # 改为：复制到临时名 → 再次确认目标不存在 → 原子替换 → 删源。
            # 目标已存在时跳过：不覆盖、不删源。
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
) -> UndoResult:
    """撤销最近批次。支持 conflict 状态重试（V0.4）。

    S1-02/S1-03：撤销不能再报假成功——
    - 批次还在 planning/executing，或仍有 planned/paused 条目时，拒绝撤销
      （这些项的实际状态未确定，需要先「核对恢复」）；
    - 「完整撤销」要求确实恢复了至少 1 项，且没有冲突/错误；
    - 恢复 0 项时不把批次标成 undone，不算完整成功。
    """
    result = UndoResult()
    batch = storage.get_pending_batch()
    if batch is None:
        result.errors_list.append("没有可撤销的批次")
        return result

    result.batch_id = batch.batch_id
    root = os.path.abspath(batch.root)

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

    for idx, action in enumerate(recoverables):
        target_abs = os.path.join(root, action.target_rel)
        source_abs = os.path.join(root, action.source_rel)

        if progress_cb:
            progress_cb(idx + 1, len(recoverables), action.source_rel)

        if not os.path.isfile(target_abs):
            try:
                storage.update_action(batch.batch_id, action.action_index,
                                      STATUS_CONFLICT, error="目标文件已消失")
            except Exception as e:
                db_write_failed = True
                result.errors_list.append(f"写库失败（目标文件已消失）：{e}")
            result.conflict += 1
            continue

        current_sha = sha256_file(target_abs)
        if current_sha != action.source_sha256:
            try:
                storage.update_action(batch.batch_id, action.action_index,
                                      STATUS_CONFLICT, error="目标内容已被修改")
            except Exception as e:
                db_write_failed = True
                result.errors_list.append(f"写库失败（目标内容已被修改）：{e}")
            result.conflict += 1
            continue

        if os.path.exists(source_abs):
            try:
                storage.update_action(batch.batch_id, action.action_index,
                                      STATUS_CONFLICT, error="原路径已被占用")
            except Exception as e:
                db_write_failed = True
                result.errors_list.append(f"写库失败（原路径已被占用）：{e}")
            result.conflict += 1
            continue

        try:
            source_dir = os.path.dirname(source_abs)
            if source_dir:
                os.makedirs(source_dir, exist_ok=True)
            os.rename(target_abs, source_abs)
        except OSError:
            # S1-04：同执行器——shutil.move 兜底会静默覆盖，改为
            # 复制到临时名 → 确认原位置为空 → 原子替换 → 删目标。
            moved_ok, move_err, _is_conflict = _copy_then_replace(
                target_abs, source_abs)
            if not moved_ok:
                try:
                    storage.update_action(
                        batch.batch_id, action.action_index, STATUS_CONFLICT,
                        error=f"恢复失败：{move_err}")
                except Exception as e:
                    db_write_failed = True
                result.errors += 1
                result.errors_list.append(
                    f"恢复失败：{action.source_rel} ← {move_err}")
                continue
        try:
            storage.update_action(batch.batch_id, action.action_index, STATUS_UNDONE)
        except Exception as e:
            # 文件已恢复但写库失败：不能假装撤销完成，批次保持未撤销状态，
            # 记录错误供人工核对（此时文件在源位置，重试撤销会按冲突处理）。
            db_write_failed = True
            result.errors += 1
            result.errors_list.append(
                f"文件已恢复但写入记录失败（{action.source_rel}）：{e}")
        result.restored += 1

    # 清理本批次创建且当前为空的目录
    for rel_dir in batch.created_dirs:
        abs_dir = os.path.join(root, rel_dir)
        if os.path.isdir(abs_dir):
            try:
                if len(os.listdir(abs_dir)) == 0:
                    os.rmdir(abs_dir)
            except OSError:
                pass

    # ---- 批次最终状态（S1-02/S1-03：不得假成功）----
    if db_write_failed:
        # 写库失败的批次保持原状态（未撤销），不标记 undone
        pass
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
                  progress_cb: Optional[Callable[[int, int, str], None]] = None) -> RecoverResult:
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
            "SELECT id FROM batches WHERE root = ? AND status IN (?, ?, ?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (storage.root, BATCH_PLANNING, BATCH_EXECUTING,
             BATCH_PARTIAL, BATCH_CANCELLED),
        )
        row = cur.fetchone()
        if not row:
            cur = storage.conn.execute(
                "SELECT id FROM batches WHERE root = ? ORDER BY id DESC LIMIT 1",
                (storage.root,),
            )
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
    root = os.path.abspath(batch.root)

    # 只处理 status ∈ {planned, cancelled} 的动作
    # 进度统计口径：待核对（planned/cancelled）动作数
    all_actions = storage.get_actions(batch_id)
    to_check = [a for a in all_actions
                if a.status in (STATUS_PLANNED, STATUS_CANCELLED)]
    total_to_check = len(to_check)
    checked = 0

    for action in to_check:
        if progress_cb is not None:
            try:
                progress_cb(checked, total_to_check,
                            os.path.join(root, action.source_rel))
            except Exception:
                pass   # 进度回调异常不影响核对本身
        checked += 1

        src_abs = os.path.join(root, action.source_rel)
        tgt_abs = os.path.join(root, action.target_rel)

        src_exists = os.path.isfile(src_abs)
        tgt_exists = os.path.isfile(tgt_abs)
        tgt_sha = sha256_file(tgt_abs) if tgt_exists else None
        tgt_sha_matches = (tgt_sha == action.source_sha256) and (action.source_sha256 != "")

        case_desc = ""

        if src_exists and not tgt_exists:
            # 情况 A：还没执行 → 保持 planned
            case_desc = f"A:src在+target不在 → 保持{action.status}"
            result.still_planned += 1
            # 不改 action 的 status

        elif not src_exists and tgt_exists and tgt_sha_matches:
            # 情况 B：移动成功但状态没写 → 补记 moved
            case_desc = f"B:src消失+target在+sha匹配 → 补记moved"
            storage.update_action(batch_id, action.action_index, STATUS_MOVED,
                                  error=f"[恢复补记] 进程中断后核对")
            result.recovered_moved += 1

        elif not src_exists and tgt_exists and not tgt_sha_matches:
            # 情况 C
            case_desc = f"C:src消失+target在+sha不匹配 → paused"
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

    storage.update_batch_status(batch_id, final_status, finished_at=True)
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
    while os.path.exists(tmp_path):
        counter += 1
        tmp_path = f"{dst_path}.archivetmp{counter}"
    try:
        shutil.copy2(src_path, tmp_path)
    except OSError as e:
        return False, f"复制到临时文件失败：{e}", False

    if os.path.exists(dst_path):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False, "目标路径已存在，为避免覆盖未移动", True

    try:
        os.replace(tmp_path, dst_path)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False, f"原子替换目标失败：{e}", False

    try:
        os.unlink(src_path)
    except OSError as e:
        # 源删除失败：回滚目标，保留源，宁可不动也不留两份
        try:
            os.unlink(dst_path)
        except OSError:
            pass
        return False, f"已复制但删除源失败（已回滚，源文件保留）：{e}", False
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
    """是否存在可撤销的最近批次。"""
    return storage.get_pending_batch() is not None


def has_unrecovered_batch(storage: Storage) -> bool:
    """是否存在需要中断恢复的批次（executing/planning/partial/cancelled）。"""
    cur = storage.conn.execute(
        "SELECT COUNT(*) FROM batches WHERE root = ? AND status IN (?, ?, ?, ?)",
        (storage.root, BATCH_PLANNING, BATCH_EXECUTING,
         BATCH_PARTIAL, BATCH_CANCELLED),
    )
    return cur.fetchone()[0] > 0


def interrupted_batch_id(storage: Storage) -> Optional[int]:
    """返回仍在执行中（上次运行被中断）的批次 ID；没有则 None。"""
    cur = storage.conn.execute(
        "SELECT id FROM batches WHERE root = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
        (storage.root, BATCH_PLANNING, BATCH_EXECUTING),
    )
    row = cur.fetchone()
    return row[0] if row else None
