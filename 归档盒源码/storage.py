"""
归档盒 V0.3 — SQLite 持久化层

职责：保存批次记录、逐项动作记录、本批次创建的目录。
数据库固定位置：{root}/.organizer/history.db

表结构：
- batches: 批次级元数据
- actions:  逐项动作（执行意图先持久化，再移动，再更新结果）
- created_dirs: 本批次创建的目录（撤销时用于清理空目录）
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional

from scanner import FileEntry
from planner import OrganizePlan, PlanItem, CourseRule
from utils import win_long_path, strip_long_prefix


DB_FILENAME = "history.db"

# action.status 常量
STATUS_PLANNED = "planned"          # 已写入意图，尚未执行
STATUS_MOVED = "moved"              # 移动成功
STATUS_FAILED = "failed"            # 移动失败
STATUS_SKIPPED = "skipped"          # 执行前核验发现变化，跳过
STATUS_CANCELLED = "cancelled"      # 用户取消（尚未执行或执行中被取消）
STATUS_UNDONE = "undone"            # 撤销成功
STATUS_CONFLICT = "conflict"        # 撤销或恢复时发现冲突，保留现状
STATUS_PAUSED = "paused"            # 中断恢复时状态不明确，暂停人工处理

# 批次状态常量
BATCH_PLANNING = "planning"
BATCH_EXECUTING = "executing"
BATCH_DONE = "done"
BATCH_PARTIAL = "partial"
BATCH_CANCELLED = "cancelled"          # 用户取消（有已完成项）
BATCH_CANCELLED_CLEAN = "cancelled_clean"  # 用户取消 + 无一完成
BATCH_RECOVERED_PARTIAL = "recovered_partial"  # 中断恢复后仍有不确定项
BATCH_UNDONE = "undone"
BATCH_PARTIAL_UNDONE = "partial_undone"
BATCH_PARTIAL_FAILED_UNDONE = "partial_failed_undone"
# V1.7：冲突批次经用户确认「已人工处理」后的终态——不再每次打开都询问
BATCH_CONFLICT_ACK = "conflict_acknowledged"

# 这些状态表示仍需要用户注意（可能还能撤销 / 需要核对）
BATCH_PENDING_STATUSES = (
    BATCH_PLANNING, BATCH_EXECUTING, BATCH_PARTIAL, BATCH_CANCELLED,
    BATCH_RECOVERED_PARTIAL, BATCH_PARTIAL_UNDONE, BATCH_PARTIAL_FAILED_UNDONE,
)


@dataclass
class ActionRecord:
    """逐项动作的完整记录。"""
    action_index: int
    source_rel: str
    target_rel: str
    source_size: int
    source_sha256: str
    status: str = STATUS_PLANNED
    error: str = ""
    executed_at: str = ""
    undone_at: str = ""
    # V1.4：单文件修正（旧批次为空串 → 自动分类）
    auto_target_rel: str = ""       # 若用户手动改了目标：修正前的自动目标路径
    auto_rule_applied: str = ""     # 修正前的自动分类依据
    override_note: str = ""         # 用户修正说明（可为空）
    # V1.5：本条目实际命中的规则标识与依据（长期规则/原有映射/手动修正）
    # 旧批次为空串；历史只读这里，不按当前配置重新计算
    rule_key: str = ""
    rule_applied: str = ""
    # V1.8：跨卷复制时临时副本的相对路径（重启后凭此列找到完整原件）
    temp_rel: str = ""

    @property
    def classification_source(self) -> str:
        """历史页面用：手动 / 长期规则 / 自动。

        手动修正优先（rule_key 为 manual_override:*），
        其次是命中长期规则（rule_key 为 long_term:*），
        其余（含旧批次空 rule_key）一律视为自动。
        """
        if self.auto_target_rel or self.override_note:
            return "手动"
        if (self.rule_key or "").startswith("long_term:"):
            return "长期规则"
        return "自动"

    @property
    def long_term_rule_id(self) -> str:
        """长期规则标识（非长期规则时为空串）。仅用于展示。"""
        key = self.rule_key or ""
        return key.split(":", 1)[1] if key.startswith("long_term:") else ""


@dataclass
class BatchRecord:
    batch_id: int
    root: str
    mode: str
    status: str
    created_at: str
    finished_at: str
    rules_json: str
    summary: str
    # V1.3：本批次实际使用的分类配置快照（推荐模式）；旧库迁移后为空字符串
    config_json: str = ""

    # 运行时关联
    actions: List[ActionRecord] = field(default_factory=list)
    created_dirs: List[str] = field(default_factory=list)

    @property
    def rules(self) -> List[CourseRule]:
        raw = json.loads(self.rules_json) if self.rules_json else []
        return [CourseRule(**r) for r in raw]

    @property
    def config_snapshot(self) -> dict:
        """本批次使用的分类配置快照（旧批次没有则为空字典）。"""
        if not self.config_json:
            return {}
        try:
            data = json.loads(self.config_json)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @property
    def config_summary(self) -> str:
        """给历史页面用的一行配置摘要。"""
        if not self.config_json:
            return ""
        data = self.config_snapshot
        if not data:
            return "分类配置快照不可读（旧记录或文件损坏）"
        return (
            f"分类配置 v{data.get('version', '?')}：学期 "
            f"{len(data.get('semester_map', []))} / 顶层方向 "
            f"{len(data.get('top_direction', []))} / 学期内目录 "
            f"{len(data.get('semester_dirs', []))} / 通用前缀 "
            f"{len(data.get('generic_dirs', []))} / 关键词 "
            f"{len(data.get('keyword_rules', []))}"
        )

    @property
    def moved_count(self) -> int:
        return sum(1 for a in self.actions if a.status == STATUS_MOVED)

    @property
    def undone_count(self) -> int:
        return sum(1 for a in self.actions if a.status == STATUS_UNDONE)

    @property
    def manual_override_count(self) -> int:
        """本批次中经过手动修正的条目数（未修正=旧批次，字段为空串）。"""
        return sum(1 for a in self.actions if a.classification_source == "手动")

    @property
    def long_term_rule_count(self) -> int:
        """本批次中命中长期修正规则的条目数（旧批次为 0）。"""
        return sum(1 for a in self.actions
                   if a.classification_source == "长期规则")


class Storage:
    """SQLite 持久化管理器。"""

    def __init__(self, root: str):
        # S2-19：Windows 路径大小写不敏感。历史库根目录统一 normcase，
        # 否则 C:\Foo 与 c:\foo 会被当成两个空间、各存一套批次记录。
        raw_root = os.path.abspath(root)
        self.display_root = raw_root
        self.root = (os.path.normcase(raw_root)
                     if os.name == "nt" else raw_root)
        self.db_dir = os.path.join(self.root, ".organizer")
        self.db_path = os.path.join(self.db_dir, DB_FILENAME)
        # V1.7：历史库损坏时隔离旧文件并重建，提示用户
        self.recovery_note = ""
        self.conn = None
        self._ensure_db_dir()
        self._open_with_recovery()

    def _ensure_db_dir(self):
        os.makedirs(self.db_dir, exist_ok=True)

    def _open_with_recovery(self):
        """打开历史库；损坏（或不是 sqlite 文件）时改名留存并重建。

        S3 / filesafe F-14：history.db 是撤销能力的唯一载体。损坏时
        不能让整个目录直接打不开，也不能悄悄删掉旧文件——改名留存。
        """
        try:
            self._open_and_check()
            return
        except (sqlite3.DatabaseError, OSError) as e:
            first_error = e
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = ""
        try:
            if os.path.exists(self.db_path):
                backup_path = f"{self.db_path}.corrupt-{stamp}"
                os.replace(self.db_path, backup_path)
                for suffix in ("-wal", "-shm"):
                    if os.path.exists(self.db_path + suffix):
                        os.replace(self.db_path + suffix,
                                   backup_path + suffix)
        except OSError:
            backup_path = ""
        self._open_and_check()
        self.recovery_note = (
            "整理记录（history.db）无法读取，可能已损坏。\n"
            + (f"旧文件已改名为：\n{strip_long_prefix(backup_path)}\n" if backup_path
               else "旧文件未能改名留存。\n")
            + "已创建新的整理记录；旧记录不会再出现在历史里。\n"
            + f"原始错误：{first_error}")

    def _open_and_check(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        row = self.conn.execute("PRAGMA quick_check(1)").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(
                f"history.db 结构校验失败：{row[0] if row else '无结果'}")

    def recorded_roots(self) -> List[str]:
        """数据库里记录过的根目录（用于识别目录改名/移动）。"""
        cur = self.conn.execute("SELECT DISTINCT root FROM batches")
        return [row[0] for row in cur.fetchall() if row[0]]

    def root_mismatch_note(self) -> str:
        """根目录改名/移动时给用户的说明（历史与撤销仍然可用）。"""
        current = os.path.normcase(self.root)
        stale = [r for r in self.recorded_roots()
                 if os.path.normcase(os.path.abspath(r)) != current]
        if not stale:
            return ""
        return (
            "整理记录里保存的根目录与当前目录不一致（目录可能已改名或移动）：\n"
            + "\n".join(strip_long_prefix(r) for r in stale[:5])
            + f"\n当前目录：{strip_long_prefix(self.display_root)}\n"
            "历史记录与撤销仍按当前目录使用，不会静默失效。")

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                rules_json TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                config_json TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                source_rel TEXT NOT NULL,
                target_rel TEXT NOT NULL,
                source_size INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT DEFAULT '',
                executed_at TEXT DEFAULT '',
                undone_at TEXT DEFAULT '',
                temp_rel TEXT DEFAULT '',
                UNIQUE(batch_id, action_index),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );

            CREATE TABLE IF NOT EXISTS created_dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );
        """)
        # V1.3 迁移：旧库没有 config_json 列时按需添加（只加列，不改旧字段含义）
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(batches)")}
        if "config_json" not in cols:
            self.conn.execute(
                "ALTER TABLE batches ADD COLUMN config_json TEXT DEFAULT ''")
        # V1.4 迁移：actions 表增加手动修正信息（旧批次自动视为未修正）
        act_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(actions)")}
        for col in ("auto_target_rel", "auto_rule_applied", "override_note",
                    # V1.5：命中规则标识与依据
                    "rule_key", "rule_applied",
                    # V1.8：跨卷复制临时副本的相对路径（重启后定位原件）
                    "temp_rel"):
            if col not in act_cols:
                self.conn.execute(
                    f"ALTER TABLE actions ADD COLUMN {col} TEXT DEFAULT ''")
        self.conn.commit()

    # ============== 批次操作 ==============

    def create_batch(self, plan: OrganizePlan, summary: str) -> int:
        """创建批次记录（状态 BATCH_PLANNING），返回 batch_id。"""
        now = datetime.now().isoformat(timespec="seconds")
        rules_json = json.dumps(
            [{"keywords": r.keywords, "course_name": r.course_name, "order": r.order}
             for r in plan.rules],
            ensure_ascii=False,
        )
        config_json = json.dumps(
            plan.config_snapshot, ensure_ascii=False
        ) if plan.config_snapshot else ""
        cur = self.conn.execute(
            "INSERT INTO batches(root, mode, status, created_at, finished_at, "
            "rules_json, summary, config_json) "
            "VALUES(?, ?, ?, ?, '', ?, ?, ?)",
            (self.root, plan.mode, BATCH_PLANNING, now, rules_json, summary,
             config_json),
        )
        batch_id = cur.lastrowid
        self.conn.commit()
        return batch_id

    def update_batch_status(self, batch_id: int, status: str, finished_at: bool = False):
        sql = "UPDATE batches SET status = ?"
        params = [status]
        if finished_at:
            sql += ", finished_at = ?"
            params.append(datetime.now().isoformat(timespec="seconds"))
        sql += " WHERE id = ?"
        params.append(batch_id)
        self.conn.execute(sql, params)
        self.conn.commit()

    def get_batch(self, batch_id: int) -> Optional[BatchRecord]:
        cur = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,))
        row = cur.fetchone()
        if not row:
            return None
        br = self._row_to_batch(row)
        br.actions = self.get_actions(batch_id)
        br.created_dirs = self.get_created_dirs(batch_id)
        return br

    def get_latest_batch(self) -> Optional[BatchRecord]:
        # 历史库就在当前整理空间内，所有批次都属于这个空间；
        # 不再按 root 字符串精确过滤（目录改名/大小写变化会导致历史消失）。
        cur = self.conn.execute(
            "SELECT id FROM batches ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        return self.get_batch(row[0])

    def list_batches(self, include_actions: bool = True) -> List[BatchRecord]:
        """列出当前根目录的全部批次（新批次在前）。

        include_actions=False 时只读批次元数据，不加载逐项动作——
        历史页面先展示批次列表，选中后再按需加载明细，
        避免批次很多时一次加载全部动作。
        """
        cur = self.conn.execute(
            "SELECT * FROM batches ORDER BY id DESC")
        results = []
        for row in cur.fetchall():
            br = self._row_to_batch(row)
            if include_actions:
                br.actions = self.get_actions(br.batch_id)
                br.created_dirs = self.get_created_dirs(br.batch_id)
            results.append(br)
        return results

    def get_pending_batch(self) -> Optional[BatchRecord]:
        """获取最近一个仍需要用户注意的批次。

        V1.7：done / undone / cancelled_clean / conflict_acknowledged
        都不再算"待处理"——整理成功的批次不应每次打开目录都被追问撤销。
        仍在 planning/executing/partial/conflict 的批次保留，供核对或撤销。
        """
        placeholders = ",".join("?" for _ in BATCH_PENDING_STATUSES)
        cur = self.conn.execute(
            f"SELECT id FROM batches WHERE status IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            BATCH_PENDING_STATUSES,
        )
        row = cur.fetchone()
        if not row:
            return None
        return self.get_batch(row[0])

    def get_undoable_batch(self) -> Optional[BatchRecord]:
        """最近一个还能撤销的批次（含已完成的 done 批次）。

        S2 F-09/F-05：done 批次不再作为"未完成"被主动询问，
        但用户手动点「撤销上一次」时仍可以撤销它。
        """
        cur = self.conn.execute(
            "SELECT id FROM batches WHERE status != ? ORDER BY id DESC LIMIT 1",
            (BATCH_UNDONE,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self.get_batch(row[0])

    def acknowledge_conflict_batch(self, batch_id: int) -> None:
        """用户确认冲突已人工处理后，批次从"待处理"里移出（S3 F-24）。"""
        self.conn.execute(
            "UPDATE batches SET status = ?, summary = ? WHERE id = ?",
            (BATCH_CONFLICT_ACK, "冲突已人工处理（用户确认）", batch_id))
        self.conn.commit()

    def _row_to_batch(self, row) -> BatchRecord:
        # row columns: id, root, mode, status, created_at, finished_at,
        #              rules_json, summary, config_json
        return BatchRecord(
            batch_id=row[0], root=row[1], mode=row[2], status=row[3],
            created_at=row[4], finished_at=row[5] or "", rules_json=row[6] or "",
            summary=row[7] or "", config_json=row[8] or "",
        )

    # ============== 动作操作 ==============

    def _action_values(self, batch_id: int, item: PlanItem,
                       action_index: int, source_sha256: str) -> tuple:
        auto_target = ""
        auto_rule_applied = ""
        override_note = ""
        if item.is_manual_overridden:
            auto_rule_applied = item.auto_rule_applied or ""
            override_note = item.override_note or ""
            try:
                from planner import (
                    _build_target_dir_v2,
                    MODE_BY_DIRECTION_SEMESTER, MODE_BY_COURSE_THEN_TYPE,
                    MODE_BY_TYPE,
                )
                if item.auto_direction is not None or item.auto_category is not None:
                    auto_dir = _build_target_dir_v2(
                        item.auto_direction or "", item.auto_semester or "",
                        item.auto_category or "", item.file_type,
                        inner_path=item.auto_inner_path or "",
                    )
                    auto_target = os.path.join(auto_dir, item.file.filename)
            except Exception:
                auto_target = ""
        return (batch_id, action_index, item.file.rel_path, item.target_rel_path,
                item.file.size, source_sha256, STATUS_PLANNED,
                auto_target, auto_rule_applied, override_note,
                item.rule_key or "", item.rule_applied or "")

    def create_actions_bulk(self, batch_id: int, items, sha_by_index) -> None:
        """S3 / filesafe F-20：一次性写入整批执行意图（单次 commit）。

        sha_by_index: 与 items 等长的 SHA 列表（元素可为空串）。
        """
        rows = [self._action_values(batch_id, item, idx, sha)
                for idx, (item, sha) in enumerate(zip(items, sha_by_index))]
        self.conn.executemany(
            "INSERT INTO actions(batch_id, action_index, source_rel, target_rel, "
            "source_size, source_sha256, status, auto_target_rel, "
            "auto_rule_applied, override_note, rule_key, rule_applied) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def create_action(self, batch_id: int, item: PlanItem, action_index: int, source_sha256: str):
        """写入单条执行意图（status=planned）。内部使用，兼容旧调用。"""
        self.conn.execute(
            "INSERT INTO actions(batch_id, action_index, source_rel, target_rel, "
            "source_size, source_sha256, status, auto_target_rel, "
            "auto_rule_applied, override_note, rule_key, rule_applied) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._action_values(batch_id, item, action_index, source_sha256),
        )
        self.conn.commit()

    @staticmethod
    def _status_timestamps(status: str):
        """返回 (executed_at, undone_at)；空串表示该列保持原值不变。

        S4 / filesafe F-29：标记 undone/conflict 时不再清空 executed_at。
        """
        now = datetime.now().isoformat(timespec="seconds")
        if status in (STATUS_MOVED, STATUS_FAILED, STATUS_SKIPPED, STATUS_PAUSED,
                      STATUS_CANCELLED):
            return now, ""
        if status in (STATUS_UNDONE, STATUS_CONFLICT):
            return "", now
        return "", ""

    def update_action(self, batch_id: int, action_index: int, status: str,
                      error: str = "", commit: bool = True):
        executed_at, undone_at = self._status_timestamps(status)
        sql = "UPDATE actions SET status = ?, error = ?"
        params: list = [status, error]
        if executed_at:
            sql += ", executed_at = ?"
            params.append(executed_at)
        if undone_at:
            sql += ", undone_at = ?"
            params.append(undone_at)
        sql += " WHERE batch_id = ? AND action_index = ?"
        params.extend([batch_id, action_index])
        self.conn.execute(sql, params)
        if commit:
            self.conn.commit()

    def set_action_temp(self, batch_id: int, action_index: int,
                        temp_rel: str, commit: bool = True) -> None:
        """V1.8：记录跨卷复制的临时副本相对路径（动源之前必须先写库）。"""
        self.conn.execute(
            "UPDATE actions SET temp_rel = ? WHERE batch_id = ? AND action_index = ?",
            (temp_rel or "", batch_id, action_index),
        )
        if commit:
            self.conn.commit()

    def update_actions_bulk(self, batch_id: int, updates, commit: bool = True) -> None:
        """批量更新动作状态（单次 commit）。

        updates: [(action_index, status, error), ...]
        单条更新仍走 update_action（便于统一拦截/测试写库失败），只是不提交；
        最后一次性 commit。
        """
        for action_index, status, error in updates:
            self.update_action(batch_id, action_index, status,
                               error=error or "", commit=False)
        if commit:
            self.conn.commit()

    def get_actions(self, batch_id: int) -> List[ActionRecord]:
        cur = self.conn.execute(
            "SELECT action_index, source_rel, target_rel, source_size, source_sha256, "
            "status, error, executed_at, undone_at, "
            "auto_target_rel, auto_rule_applied, override_note, "
            "rule_key, rule_applied, temp_rel "
            "FROM actions WHERE batch_id = ? ORDER BY action_index ASC",
            (batch_id,),
        )
        results = []
        for row in cur.fetchall():
            results.append(ActionRecord(
                action_index=row[0], source_rel=row[1], target_rel=row[2],
                source_size=row[3], source_sha256=row[4],
                status=row[5], error=row[6] or "", executed_at=row[7] or "",
                undone_at=row[8] or "",
                auto_target_rel=row[9] or "",
                auto_rule_applied=row[10] or "",
                override_note=row[11] or "",
                rule_key=row[12] or "",
                rule_applied=row[13] or "",
                temp_rel=row[14] or "",
            ))
        return results

    def action_counts_by_batch(self) -> dict:
        """一次聚合查询返回 {batch_id: {动作状态: 数量}}。

        历史页面的批次列表只需要各状态计数（来自持久化的实际记录），
        不需要逐批加载全部动作。
        """
        cur = self.conn.execute(
            "SELECT batch_id, status, COUNT(*) FROM actions "
            "GROUP BY batch_id, status",
        )
        counts: dict = {}
        for batch_id, status, n in cur.fetchall():
            counts.setdefault(batch_id, {})[status] = n
        return counts

    def source_counts_by_batch(self) -> dict:
        """一次聚合查询返回 {batch_id: {"手动": n, "长期规则": n, "自动": n}}。

        批次列表不加载逐项动作（include_actions=False），
        但仍要在"注意"列提示本批次含手动修正/长期规则命中，
        因此用一条聚合 SQL 统计分类来源。
        """
        cur = self.conn.execute(
            "SELECT batch_id, "
            "SUM(CASE WHEN (auto_target_rel != '' OR override_note != '') "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN (auto_target_rel = '' AND override_note = '') "
            "          AND rule_key LIKE 'long_term:%' "
            "         THEN 1 ELSE 0 END), "
            "COUNT(*) "
            "FROM actions GROUP BY batch_id",
        )
        counts: dict = {}
        for batch_id, manual, long_term, total in cur.fetchall():
            manual = int(manual or 0)
            long_term = int(long_term or 0)
            counts[batch_id] = {
                "手动": manual,
                "长期规则": long_term,
                "自动": max(0, int(total or 0) - manual - long_term),
            }
        return counts

    # ============== 创建的目录 ==============

    def record_created_dir(self, batch_id: int, rel_path: str):
        self.conn.execute(
            "INSERT INTO created_dirs(batch_id, rel_path) VALUES(?, ?)",
            (batch_id, rel_path),
        )
        self.conn.commit()

    def get_created_dirs(self, batch_id: int) -> List[str]:
        cur = self.conn.execute(
            "SELECT rel_path FROM created_dirs WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        )
        return [row[0] for row in cur.fetchall()]

    # ============== 通用 ==============

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
