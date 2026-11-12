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
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from scanner import FileEntry
from planner import OrganizePlan, PlanItem, CourseRule


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

    @property
    def classification_source(self) -> str:
        """历史页面用：自动 / 手动。旧批次（两个新字段都空）视为自动。"""
        if self.auto_target_rel or self.override_note:
            return "手动"
        return "自动"


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


class Storage:
    """SQLite 持久化管理器。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.db_dir = os.path.join(self.root, ".organizer")
        self.db_path = os.path.join(self.db_dir, DB_FILENAME)
        self._ensure_db_dir()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _ensure_db_dir(self):
        os.makedirs(self.db_dir, exist_ok=True)

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
        for col in ("auto_target_rel", "auto_rule_applied", "override_note"):
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
        cur = self.conn.execute(
            "SELECT id FROM batches WHERE root = ? ORDER BY id DESC LIMIT 1",
            (self.root,),
        )
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
            "SELECT * FROM batches WHERE root = ? ORDER BY id DESC",
            (self.root,),
        )
        results = []
        for row in cur.fetchall():
            br = self._row_to_batch(row)
            if include_actions:
                br.actions = self.get_actions(br.batch_id)
                br.created_dirs = self.get_created_dirs(br.batch_id)
            results.append(br)
        return results

    def get_pending_batch(self) -> Optional[BatchRecord]:
        """获取最近一个尚未**完整撤销成功**的批次。

        只排除 BATCH_UNDONE（完全撤销）。
        BATCH_PARTIAL_UNDONE / BATCH_PARTIAL_FAILED_UNDONE 表示有冲突，
        用户修好冲突条件后应该能再次撤销，所以不排除。
        """
        cur = self.conn.execute(
            "SELECT id FROM batches WHERE root = ? AND status != ? "
            "ORDER BY id DESC LIMIT 1",
            (self.root, BATCH_UNDONE),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self.get_batch(row[0])

    def _row_to_batch(self, row) -> BatchRecord:
        # row columns: id, root, mode, status, created_at, finished_at,
        #              rules_json, summary, config_json
        return BatchRecord(
            batch_id=row[0], root=row[1], mode=row[2], status=row[3],
            created_at=row[4], finished_at=row[5] or "", rules_json=row[6] or "",
            summary=row[7] or "", config_json=row[8] or "",
        )

    # ============== 动作操作 ==============

    def create_action(self, batch_id: int, item: PlanItem, action_index: int, source_sha256: str):
        """写入执行意图（status=planned）。V1.4 起同时写入手动修正信息。

        自动目标路径用 item.auto_* 快照 + planner._compute_target_dir 重建；
        storage 不顶层导入 planner，避免循环依赖——在函数内延迟导入。
        未修正的文件三个新字段都为空串，历史页面视为"自动分类"。
        """
        auto_target = ""
        auto_rule_applied = ""
        override_note = ""
        if item.is_manual_overridden:
            auto_rule_applied = item.auto_rule_applied or ""
            override_note = item.override_note or ""
            try:
                from planner import (
                    _build_target_dir_v2, _build_target_dir,
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
                # 极端情况下 planner 导入失败或 auto_* 快照不完整，
                # 自动目标留空；撤销仍基于 target_rel（已记录实际路径）
                # 不会受影响；只是历史追溯少了自动分类参考。
                auto_target = ""

        self.conn.execute(
            "INSERT INTO actions(batch_id, action_index, source_rel, target_rel, source_size, "
            "source_sha256, status, auto_target_rel, auto_rule_applied, override_note) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, action_index, item.file.rel_path, item.target_rel_path,
             item.file.size, source_sha256, STATUS_PLANNED,
             auto_target, auto_rule_applied, override_note),
        )
        self.conn.commit()

    def update_action(self, batch_id: int, action_index: int, status: str, error: str = ""):
        now = datetime.now().isoformat(timespec="seconds")
        # 通用 UPDATE：取消状态/恢复状态/执行状态都用同一条 SQL
        # executed_at / undone_at 只在对应阶段赋值；其他状态置空（保持历史记录完整）
        executed_at = ""
        undone_at = ""
        if status in (STATUS_MOVED, STATUS_FAILED, STATUS_SKIPPED, STATUS_PAUSED, STATUS_CANCELLED):
            executed_at = now
        elif status in (STATUS_UNDONE, STATUS_CONFLICT):
            undone_at = now

        self.conn.execute(
            "UPDATE actions SET status = ?, error = ?, executed_at = ?, undone_at = ? "
            "WHERE batch_id = ? AND action_index = ?",
            (status, error, executed_at, undone_at, batch_id, action_index),
        )
        self.conn.commit()

    def get_actions(self, batch_id: int) -> List[ActionRecord]:
        cur = self.conn.execute(
            "SELECT action_index, source_rel, target_rel, source_size, source_sha256, "
            "status, error, executed_at, undone_at, "
            "auto_target_rel, auto_rule_applied, override_note "
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
