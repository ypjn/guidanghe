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

    @property
    def is_complete(self) -> bool:
        return self.conflict == 0 and self.errors == 0

    def summary(self) -> str:
        if self.is_complete:
            return f"完整撤销：恢复 {self.restored} 项"
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


# ======================== 异常移动：失败即保留，不做复制兜底 ========================
#
# 历史反例（stage5、stage6 的独立探针）：
#   os.rename 失败后曾改用 shutil.move 复制兜底；为了维持“失败 = 源在、目标不在”，
#   又去删除目标副本，判据是“源/目标都存在且内容哈希相同”。这等于把“内容相同”
#   当成“文件是本次创建的”，外部文件会被越权删除；而复制发布之后、删除源之前
#   还有一个窗口：目标可能被替换成同内容新文件、源可能被外部改写，事后无论再
#   查几次哈希或存在性，都不能把“检查”和“删除”之间的竞态消掉。
#
# 现在的决定（本轮采用的最小方案）：
#   同卷（整理本身的唯一场景）里 os.rename 失败就是失败：如实记录，源文件留在
#   原处，目标路径上的东西不碰、不删、不覆盖。不复制、不删除、不产生临时文件。
#   跨盘移动从来不是已验收能力，不为了维持它引入兜底。
#
# 平台边界：
#   下面只用 os.rename 的失败结果，没有平台分支，也不依赖“rename 不覆盖”的
#   Windows 语义——无论 rename 因何失败，本模块只记录、不改变磁盘。Mac 上走到
#   这里的表现与冻结基线一致：失败、保留、记录。


def _move_failure_detail(err: OSError, src_path: str, target_path: str) -> str:
    """把 rename 失败时的实际磁盘状态写进历史：不推测、不清理、不覆盖。"""
    parts = [f"移动失败：{err}",
             f"源文件保留在 {src_path}（未由本次操作改动）"]
    if os.path.exists(target_path):
        parts.append(f"目标路径已存在，未由本次操作创建或改动：{target_path}")
    else:
        parts.append(f"目标路径未创建：{target_path}")
    parts.append("本次未复制、未删除任何文件，也没有留下临时文件")
    return "；".join(parts)



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
    for idx, item in enumerate(selected_items):
        src_path = os.path.join(root, item.file.rel_path)
        sha = item.source_sha256 or sha256_file(src_path) or ""
        storage.create_action(result.batch_id, item, idx, sha)

    storage.update_batch_status(result.batch_id, BATCH_EXECUTING)

    # 步骤 3-4：逐项执行
    created_dirs: set[str] = set()

    for idx, item in enumerate(selected_items):
        # ---- 取消检查（在每个文件操作前/后）----
        if cancel_event and cancel_event.is_set():
            # 把剩余所有 planned 动作标记为 cancelled
            remaining = selected_items[idx:]
            for remaining_idx, _ in enumerate(remaining):
                real_idx = idx + remaining_idx
                storage.update_action(result.batch_id, real_idx, STATUS_CANCELLED,
                                      error="用户取消")
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
            storage.update_action(result.batch_id, idx, STATUS_SKIPPED,
                                  error="源文件已不存在")
            result.skipped += 1
            continue

        stored_sha = None
        for a in storage.get_actions(result.batch_id):
            if a.action_index == idx:
                stored_sha = a.source_sha256
                break
        current_sha = sha256_file(src_path)
        if stored_sha and current_sha and stored_sha != current_sha:
            storage.update_action(result.batch_id, idx, STATUS_SKIPPED,
                                  error="源文件内容已变化")
            result.skipped += 1
            continue

        if os.path.exists(target_path):
            storage.update_action(result.batch_id, idx, STATUS_SKIPPED,
                                  error="目标路径已被占用")
            result.skipped += 1
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
                storage.update_action(result.batch_id, idx, STATUS_FAILED,
                                      error=f"创建目录失败：{e}")
                result.failed += 1
                continue

        # ---- 移动（一次原子 rename；失败即如实失败，不复制、不删除）----
        try:
            os.rename(src_path, target_path)
            storage.update_action(result.batch_id, idx, STATUS_MOVED)
            result.moved += 1
        except OSError as e:
            msg = _move_failure_detail(e, src_path, target_path)
            storage.update_action(result.batch_id, idx, STATUS_FAILED,
                                  error=msg)
            result.failed += 1
            result.errors.append(f"移动失败：{item.file.rel_path} → {msg}")

    # ---- 批次最终状态 ----
    _finalize_batch(storage, result.batch_id, result)
    return result


# ======================== 撤销器 ========================

def undo_latest_batch(
    storage: Storage,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> UndoResult:
    """撤销最近批次。支持 conflict 状态重试（V0.4）。"""
    result = UndoResult()
    batch = storage.get_pending_batch()
    if batch is None:
        result.errors_list.append("没有可撤销的批次")
        return result

    result.batch_id = batch.batch_id
    root = os.path.abspath(batch.root)

    actions = storage.get_actions(batch.batch_id)
    recoverables = [a for a in actions if a.status in (STATUS_MOVED, STATUS_CONFLICT)]

    for idx, action in enumerate(recoverables):
        target_abs = os.path.join(root, action.target_rel)
        source_abs = os.path.join(root, action.source_rel)

        if progress_cb:
            progress_cb(idx + 1, len(recoverables), action.source_rel)

        if not os.path.isfile(target_abs):
            storage.update_action(batch.batch_id, action.action_index, STATUS_CONFLICT,
                                  error="目标文件已消失")
            result.conflict += 1
            continue

        current_sha = sha256_file(target_abs)
        if current_sha != action.source_sha256:
            storage.update_action(batch.batch_id, action.action_index, STATUS_CONFLICT,
                                  error="目标内容已被修改")
            result.conflict += 1
            continue

        if os.path.exists(source_abs):
            storage.update_action(batch.batch_id, action.action_index, STATUS_CONFLICT,
                                  error="原路径已被占用")
            result.conflict += 1
            continue

        try:
            source_dir = os.path.dirname(source_abs)
            if source_dir:
                os.makedirs(source_dir, exist_ok=True)
            os.rename(target_abs, source_abs)
            storage.update_action(batch.batch_id, action.action_index, STATUS_UNDONE)
            result.restored += 1
        except OSError as e_first:
            # 与执行路径同一安全边界：不复制、不删除、不覆盖。
            # rename 失败说明磁盘没有被这次撤销改变；原始路径不留半成品，
            # 目标文件保持原样，下一次条件恢复后可以重试。
            msg = (f"恢复失败：{e_first}；"
                   f"目标文件保留在 {target_abs}（未由本次操作改动）；"
                   f"原始路径未写入任何内容：{source_abs}；"
                   f"本次未复制、未删除任何文件")
            if os.path.exists(source_abs):
                msg += f"；原始路径上已有其它文件，未动它：{source_abs}"
            storage.update_action(batch.batch_id, action.action_index,
                                  STATUS_CONFLICT, error=msg)
            result.errors += 1
            result.errors_list.append(f"恢复失败：{action.source_rel} ← {msg}")

    # 清理本批次创建且当前为空的目录
    for rel_dir in batch.created_dirs:
        abs_dir = os.path.join(root, rel_dir)
        if os.path.isdir(abs_dir):
            try:
                if len(os.listdir(abs_dir)) == 0:
                    os.rmdir(abs_dir)
            except OSError:
                pass

    if result.conflict == 0 and result.errors == 0 and result.restored > 0:
        storage.update_batch_status(batch.batch_id, BATCH_UNDONE, finished_at=True)
    elif result.conflict > 0 or result.errors > 0:
        storage.update_batch_status(batch.batch_id, BATCH_PARTIAL_FAILED_UNDONE, finished_at=True)
    else:
        storage.update_batch_status(batch.batch_id, BATCH_UNDONE, finished_at=True)

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
         → 可能是历史版本（含已删除的复制兜底）留下的双份；现版本不再产生
           这种状态，磁盘与 planned 预期不符 → paused

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

def _finalize_batch(storage: Storage, batch_id: int, result: ExecuteResult):
    """根据执行结果更新批次最终状态。"""
    if result.was_cancelled:
        if result.moved == 0 and result.skipped == 0 and result.failed == 0:
            final_status = BATCH_CANCELLED_CLEAN
        else:
            final_status = BATCH_CANCELLED
    elif result.failed == 0 and result.skipped == 0 and result.cancelled == 0:
        final_status = BATCH_DONE
    else:
        final_status = BATCH_PARTIAL

    storage.update_batch_status(batch_id, final_status, finished_at=True)

    storage.conn.execute(
        "UPDATE batches SET summary = ? WHERE id = ?",
        (f"移动 {result.moved} / 跳过 {result.skipped} / 失败 {result.failed}"
         f" / 取消 {result.cancelled}",
         batch_id),
    )
    storage.conn.commit()


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
