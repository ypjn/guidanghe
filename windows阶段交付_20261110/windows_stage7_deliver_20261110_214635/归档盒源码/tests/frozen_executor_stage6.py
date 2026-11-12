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
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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


# ======================== 异常移动：可证明所有权的临时副本 ========================
#
# 背景（V1.7 Windows 阶段实测反例）：
#   os.rename 失败后改用 shutil.move(src, target)，它可能“复制成功、删除源失败”，
#   也可能“只复制了一部分就失败”。此时若按“目标内容与源相同”就删除目标，
#   等于把“内容相同”当成“这个文件是本次操作创建的”证据——而目标可能是
#   在存在性检查之后由外部程序写入的，删除它会越权。
#
# 现在的做法：
#   1. 复制永远先写进**本进程用 O_EXCL 独占创建**的临时文件（唯一名，同目录 ⇒ 同卷）；
#   2. 发布用 os.rename(tmp, target)：目标已存在时失败，不覆盖；
#   3. 只有上面这种“确实由本次创建”的文件才允许删除；
#      清理失败时把临时文件路径原样写进历史，供诊断与新进程恢复；
#   4. 源文件删不掉时，撤回刚发布的目标（仍属于本次创建），使“失败 = 源在、无残留”
#      在能证明归属时成立；证明不了就保留并写清残留路径，不猜。
#
# 这些临时文件不是产品功能，只在异常路径出现；正常 rename 成功时不会产生。

TMP_PREFIX = ".abx-copy-"


def _owned_tmp_path(dest_dir: str) -> str:
    return os.path.join(dest_dir,
                        f"{TMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:10]}.tmp")


def _stage_copy(src_path: str, dest_dir: str,
                expected_sha256: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """把 src 复制成本进程独占拥有的临时文件。

    返回 (tmp_path, tmp_sha256, problem)：
      - tmp_path 为 None：临时文件都没创建成功，磁盘确定未改变；
      - problem 非空：复制失败或内容与预期不一致，tmp_path 可能已存在（需清理）；
      - problem 为空：复制完成且（给了预期时）哈希一致，可以发布。
    """
    tmp_path = _owned_tmp_path(dest_dir)
    try:
        fd = os.open(tmp_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_BINARY", 0), 0o600)
    except OSError as e:
        return None, None, f"无法创建本次临时副本：{e}"
    try:
        with os.fdopen(fd, "wb") as dst, open(src_path, "rb") as src:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    except OSError as e:
        return tmp_path, None, f"复制到临时副本时失败：{e}"
    got = sha256_file(tmp_path)
    if expected_sha256 and got != expected_sha256:
        return tmp_path, got, (
            f"复制期间源文件内容发生变化（临时副本 {got[:12]}… ≠ 计划时 {expected_sha256[:12]}…）")
    return tmp_path, got, ""


def _remove_owned(path: str, expect_sha256: Optional[str] = None) -> Tuple[bool, str]:
    """删除“本次操作创建”的文件。

    所有权来自调用点：路径由 _owned_tmp_path 以 O_EXCL 创建，或由本次
    os.rename 发布到目标位。expect_sha256 只在发布后作为额外护栏：
    内容已被替换则不删（避免误删发布之后别人改写过的文件）。
    """
    try:
        if expect_sha256 is not None:
            if not os.path.isfile(path) or sha256_file(path) != expect_sha256:
                return False, "文件内容在发布后已被修改，未删除"
        os.remove(path)
        return True, ""
    except OSError as e:
        return False, str(e)


# ======================== 执行器 ========================

def _move_via_owned_temp(src_path: str, target_path: str, dest_dir: str,
                         expected_sha256: Optional[str]) -> Tuple[bool, str]:
    """rename 失败后的兜底移动：复制到自有临时副本 → 不覆盖发布 → 删除源。

    返回值 (ok, detail)。ok=False 时 detail 必须写清磁盘现状
    （源/目标/临时文件各是否存在、哪一步失败、残留路径），便于跨进程诊断。
    """
    tmp_path, tmp_sha, problem = _stage_copy(src_path, dest_dir, expected_sha256)
    if tmp_path is None:
        return False, f"{problem}；本次未产生任何副本（原文件未动、目标未创建）"
    if problem:
        ok, why = _remove_owned(tmp_path)
        if ok:
            return False, (f"{problem}；本次临时副本已清理"
                           f"（{os.path.basename(tmp_path)}）")
        return False, (f"{problem}；本次临时副本清理失败：{why}；"
                       f"残留临时文件：{tmp_path}")

    try:
        os.rename(tmp_path, target_path)     # 目标已存在时失败：不覆盖
    except OSError as e_pub:
        ok, why = _remove_owned(tmp_path)
        state = ("本次临时副本已清理" if ok
                 else f"本次临时副本清理失败：{why}；残留临时文件：{tmp_path}")
        busy = ("；目标路径上的文件不是本次操作创建的，已保留未动"
                if os.path.exists(target_path) else "")
        return False, f"发布失败：{e_pub}；{state}{busy}"

    try:
        os.remove(src_path)
        return True, ""
    except OSError as e_unlink:
        ok, why = _remove_owned(target_path, tmp_sha)
        if ok:
            return False, (f"复制已完成但无法删除原文件：{e_unlink}；"
                           f"已撤回本次发布的目标副本（原文件未动）")
        return False, (f"复制已完成但无法删除原文件：{e_unlink}；"
                       f"撤回目标副本失败：{why}；残留目标：{target_path}"
                       f"（原文件仍在：{src_path}）")


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

        # ---- 移动（优先原子 rename；失败时走“自有临时副本”兜底）----
        try:
            os.rename(src_path, target_path)
            storage.update_action(result.batch_id, idx, STATUS_MOVED)
            result.moved += 1
        except OSError as e:
            detail = _move_via_owned_temp(src_path, target_path, target_dir,
                                          stored_sha)
            if detail[0]:
                storage.update_action(result.batch_id, idx, STATUS_MOVED)
                result.moved += 1
            else:
                msg = f"移动失败：{e}；{detail[1]}"
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
            # 与执行路径同一类问题：shutil.move 可能只复制了一部分就失败，
            # 在原始路径上留下半成品，下一次撤销又会以“原路径已被占用”卡住。
            # 因此同样改为“自有临时副本 + 不覆盖发布”。
            ok, detail = _move_via_owned_temp(
                target_abs, source_abs, os.path.dirname(source_abs) or root,
                action.source_sha256)
            if ok:
                storage.update_action(batch.batch_id, action.action_index, STATUS_UNDONE)
                result.restored += 1
            else:
                msg = f"恢复失败：{e_first}；{detail}"
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
