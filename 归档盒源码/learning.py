"""
归档盒阶段 5 · 主动学习闭环（纯逻辑，无 Qt 依赖）

本模块只在旁边记录「用户这次实际做了什么」，不改预览、不改执行器：
- 反馈落在 {root}/.organizer/preferences.db 的新表里，与偏好表分开。
- 只有反复出现、且能用现有三种匹配（精确路径 / 文件名关键词 / 目录范围）
  明确归纳的判断，才会生成「待确认候选」。
- 候选不会直接写进 preferences 表；只有用户确认时才调用现有
  preferences.save_preference，因此校验、JSON 镜像和失败回滚都沿用旧路径。
- 已确认偏好只影响之后的预览建议；真正的移动仍走原有勾选与执行确认。

支持级别：
  judgment  用户明确给出的分类决定（仅改本次且与自动快照不同、跳过保持原位）
  clue      无逐文件原因的整批撤销，只作待核对线索，不参与候选计数
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import preferences
from classification_config import (
    ClassificationConfig, INNER_KEEP,
    MATCH_DIR_SCOPE, MATCH_EXACT_PATH, MATCH_FILENAME_KEYWORD,
    LongTermRule, compile_config, match_long_term_rule,
)
from planner import TARGET_ROOT

KIND_JUDGMENT = "judgment"
KIND_CLUE = "clue"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_DISABLED = "disabled"
STATUS_CONFLICT = "conflict"
STATUS_BLOCKED = "blocked"

STATUS_LABELS = {
    STATUS_PENDING: "待确认",
    STATUS_CONFIRMED: "已确认",
    STATUS_REJECTED: "已拒绝",
    STATUS_DISABLED: "已停用",
    STATUS_CONFLICT: "条件冲突（不可确认）",
    STATUS_BLOCKED: "暂不可确认",
}

CONFIRMABLE_STATUSES = (STATUS_PENDING,)
KEEP_STATUSES = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_DISABLED)

# 测试注入点：历史导入「先读、后写」之间调用。生产环境为 None。
_IMPORT_READ_HOOK = None

_LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_feedback (
    feedback_id TEXT PRIMARY KEY,
    space_root TEXT NOT NULL,
    evidence_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'live',
    source_rel TEXT NOT NULL DEFAULT '',
    before_target TEXT NOT NULL DEFAULT '',
    after_target TEXT NOT NULL DEFAULT '',
    keep_in_place INTEGER NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT '',
    semester TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    rule_key TEXT NOT NULL DEFAULT '',
    rule_applied TEXT NOT NULL DEFAULT '',
    batch_id INTEGER,
    action_index INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS learning_candidates (
    candidate_id TEXT PRIMARY KEY,
    space_root TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    match_type TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    include_subdirs INTEGER NOT NULL DEFAULT 1,
    keywords TEXT NOT NULL DEFAULT '[]',
    direction TEXT NOT NULL DEFAULT '',
    semester TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    target_dir TEXT NOT NULL DEFAULT '',
    condition_label TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'space',
    samples TEXT NOT NULL DEFAULT '[]',
    opposing TEXT NOT NULL DEFAULT '[]',
    conflicts TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    preference_id TEXT NOT NULL DEFAULT '',
    feedback_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


class LearningError(ValueError):
    """学习库不可用、候选不可确认或保存失败。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_root(root: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(root)))


def _norm_rel(rel_path: str) -> str:
    parts = [p for p in re.split(r"[/\\]", rel_path or "") if p]
    return os.sep.join(parts)


def _within_root(root: str, rel_path: str) -> bool:
    root_real = os.path.realpath(os.path.abspath(root))
    target = os.path.realpath(os.path.join(root_real, rel_path or ""))
    try:
        return os.path.commonpath([root_real, target]) == root_real
    except ValueError:
        return False


def _decision_key(root: str, source_rel: str) -> str:
    return f"decision:{_norm_root(root)}:{_norm_rel(source_rel)}"


def _clue_key(root: str, batch_id) -> str:
    return f"clue:{_norm_root(root)}:{batch_id}"


def _feedback_id(evidence_key: str) -> str:
    digest = hashlib.sha1(evidence_key.encode("utf-8")).hexdigest()
    return "fb-" + digest[:16]


def _candidate_id(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "lc-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _open(root: str) -> sqlite3.Connection:
    path = preferences.space_db_path(root)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_LEARNING_SCHEMA)
    return conn


# ======================== 数据模型 ========================

@dataclass
class Feedback:
    feedback_id: str
    space_root: str
    evidence_key: str
    kind: str
    origin: str = "live"
    source_rel: str = ""
    before_target: str = ""
    after_target: str = ""
    keep_in_place: bool = False
    direction: str = ""
    semester: str = ""
    category: str = ""
    rule_key: str = ""
    rule_applied: str = ""
    batch_id: Optional[int] = None
    action_index: Optional[int] = None
    active: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Candidate:
    candidate_id: str
    space_root: str
    status: str
    match_type: str
    source_path: str = ""
    include_subdirs: bool = True
    keywords: List[str] = field(default_factory=list)
    direction: str = ""
    semester: str = ""
    category: str = ""
    target_dir: str = ""
    condition_label: str = ""
    scope: str = preferences.SCOPE_SPACE
    samples: List[dict] = field(default_factory=list)
    opposing: List[dict] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)
    reason: str = ""
    preference_id: str = ""
    feedback_ids: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    preference_enabled: Optional[bool] = None

    @property
    def confirmable(self) -> bool:
        return self.status in CONFIRMABLE_STATUSES and not self.conflicts

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def target_label(self) -> str:
        parts = [self.direction or "（未填方向）"]
        if self.semester:
            parts.append(self.semester)
        if self.category:
            parts.append(self.category)
        return " / ".join(parts)

    def condition_text(self) -> str:
        if self.condition_label:
            return self.condition_label
        return _condition_label(self.match_type, self.source_path,
                                self.include_subdirs, self.keywords)

    def opposing_text(self) -> str:
        if not self.opposing:
            return "无"
        lines = []
        for item in self.opposing:
            lines.append(
                f"{item.get('source_rel')}：{item.get('reason')}")
        return "\n".join(lines)

    def conflicts_text(self) -> str:
        if not self.conflicts:
            return "无"
        lines = []
        for group in self.conflicts:
            sample = "、".join(group.get("samples") or []) or "（无样本）"
            lines.append(f"{group.get('label')}：{sample}")
        return "\n".join(lines)

    def describe(self) -> str:
        samples = "\n".join(
            f"  - {s.get('source_rel')} → {s.get('target_dir')}"
            for s in self.samples) or "  （无）"
        return (
            f"状态：{self.status_label}\n"
            f"触发条件：{self.condition_text()}\n"
            f"建议去向：{self.target_label}\n"
            f"适用范围：{preferences.SCOPE_LABELS.get(self.scope, self.scope)}"
            f"（{self.space_root}）\n"
            f"来源样本：\n{samples}\n"
            f"相反证据：\n{self.opposing_text()}\n"
            f"冲突选项：\n{self.conflicts_text()}\n"
            f"说明：{self.reason or '无'}"
        )


@dataclass
class ImportResult:
    scanned_actions: int = 0
    judgments_added: int = 0
    clues_added: int = 0
    note: str = ""


@dataclass
class BuildResult:
    candidates: List[Candidate] = field(default_factory=list)
    note: str = ""


def _row_to_feedback(row: sqlite3.Row) -> Feedback:
    return Feedback(
        feedback_id=row["feedback_id"],
        space_root=row["space_root"],
        evidence_key=row["evidence_key"],
        kind=row["kind"],
        origin=row["origin"] or "live",
        source_rel=row["source_rel"] or "",
        before_target=row["before_target"] or "",
        after_target=row["after_target"] or "",
        keep_in_place=bool(row["keep_in_place"]),
        direction=row["direction"] or "",
        semester=row["semester"] or "",
        category=row["category"] or "",
        rule_key=row["rule_key"] or "",
        rule_applied=row["rule_applied"] or "",
        batch_id=row["batch_id"],
        action_index=row["action_index"],
        active=bool(row["active"]),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    def load(name: str, default):
        try:
            value = json.loads(row[name] or "null")
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    return Candidate(
        candidate_id=row["candidate_id"],
        space_root=row["space_root"],
        status=row["status"],
        match_type=row["match_type"],
        source_path=row["source_path"] or "",
        include_subdirs=bool(row["include_subdirs"]),
        keywords=list(load("keywords", [])),
        direction=row["direction"] or "",
        semester=row["semester"] or "",
        category=row["category"] or "",
        target_dir=row["target_dir"] or "",
        condition_label=row["condition_label"] or "",
        scope=row["scope"] or preferences.SCOPE_SPACE,
        samples=list(load("samples", [])),
        opposing=list(load("opposing", [])),
        conflicts=list(load("conflicts", [])),
        reason=row["reason"] or "",
        preference_id=row["preference_id"] or "",
        feedback_ids=list(load("feedback_ids", [])),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


# ======================== 反馈写入 ========================

def _write_decision(root: str, source_rel: str, *, kind: str, origin: str,
                    before_target: str, after_target: str,
                    keep_in_place: bool, direction: str = "",
                    semester: str = "", category: str = "",
                    rule_key: str = "", rule_applied: str = "",
                    batch_id=None, action_index=None,
                    reactivate: bool = True,
                    history_alternates: Optional[Sequence[dict]] = None) -> bool:
    """写入一条用户决定；同一文件只保留一条（最终去向）。

    返回是否新增了行（更新已有行不算新增）。
    历史导入不走无条件覆盖：先插入，冲突后在同一个连接里重读并只做
    条件更新，避免覆盖导入期间另一进程刚写下的现场判断。
    """
    key = _decision_key(root, source_rel)
    now = _now()
    conn = _open(root)
    try:
        if origin == "history":
            payload = {
                "ident": (batch_id if batch_id is not None else -1,
                          action_index if action_index is not None else -1),
                "source_rel": _norm_rel(source_rel),
                "before_target": before_target or "",
                "after_target": after_target or "",
                "direction": direction or "",
                "semester": semester or "",
                "category": category or "",
                "rule_key": rule_key or "",
                "rule_applied": rule_applied or "",
                "batch_id": batch_id,
                "action_index": action_index,
            }
            return _write_history_decision(
                conn, root, key, payload, now,
                list(history_alternates or []))
        existing = conn.execute(
            "SELECT * FROM learning_feedback WHERE evidence_key = ?",
            (key,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO learning_feedback("
                "feedback_id, space_root, evidence_key, kind, origin, "
                "source_rel, before_target, after_target, keep_in_place, "
                "direction, semester, category, rule_key, rule_applied, "
                "batch_id, action_index, active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_feedback_id(key), _norm_root(root), key, kind, origin,
                 _norm_rel(source_rel), before_target or "", after_target or "",
                 1 if keep_in_place else 0, direction or "", semester or "",
                 category or "", rule_key or "", rule_applied or "",
                 batch_id, action_index, 1, now, now))
            conn.commit()
            return True
        if not reactivate and not bool(existing["active"]):
            return False
        # 新的现场决定尚未执行：清掉旧批次身份，只保留最终去向这一条。
        conn.execute(
            "UPDATE learning_feedback SET kind=?, origin=?, source_rel=?, "
            "before_target=?, after_target=?, keep_in_place=?, direction=?, "
            "semester=?, category=?, rule_key=?, rule_applied=?, "
            "batch_id=?, action_index=?, active=1, updated_at=? "
            "WHERE evidence_key=?",
            (kind, origin, _norm_rel(source_rel), before_target or "",
             after_target or "", 1 if keep_in_place else 0, direction or "",
             semester or "", category or "", rule_key or "",
             rule_applied or "", batch_id, action_index, now, key))
        conn.commit()
        return False
    finally:
        conn.close()


def record_override(root: str, source_rel: str, before_target: str,
                    after_target: str, direction: str = "",
                    semester: str = "", category: str = "",
                    rule_key: str = "", rule_applied: str = "",
                    batch_id=None, action_index=None) -> bool:
    """记录「仅改本次」且结果与自动快照不同。返回是否新增行。"""
    return _write_decision(
        root, source_rel, kind=KIND_JUDGMENT, origin="live",
        before_target=before_target, after_target=after_target,
        keep_in_place=False, direction=direction, semester=semester,
        category=category, rule_key=rule_key, rule_applied=rule_applied,
        batch_id=batch_id, action_index=action_index, reactivate=True)


def record_skip(root: str, source_rel: str, before_target: str = "",
                rule_key: str = "", rule_applied: str = "") -> bool:
    """记录「跳过，保持原位」。返回是否新增行。"""
    return _write_decision(
        root, source_rel, kind=KIND_JUDGMENT, origin="skip",
        before_target=before_target, after_target="", keep_in_place=True,
        rule_key=rule_key, rule_applied=rule_applied, reactivate=True)


def withdraw_decision(root: str, source_rel: str) -> bool:
    """撤回该文件的判断（恢复自动分类 / 改回自动快照）。

    行保留、active 置 0，重复写入同一旧证据也不会重新计数。
    """
    key = _decision_key(root, source_rel)
    conn = _open(root)
    try:
        cur = conn.execute(
            "UPDATE learning_feedback SET active=0, updated_at=? "
            "WHERE evidence_key=? AND active=1", (_now(), key))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def record_undo_clue(root: str, batch_id: int) -> bool:
    """记录一条「无逐文件原因的整批撤销」线索；每批至多一条。"""
    key = _clue_key(root, batch_id)
    now = _now()
    conn = _open(root)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO learning_feedback("
            "feedback_id, space_root, evidence_key, kind, origin, "
            "source_rel, before_target, after_target, keep_in_place, "
            "direction, semester, category, rule_key, rule_applied, "
            "batch_id, action_index, active, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_feedback_id(key), _norm_root(root), key, KIND_CLUE, "history",
             "", "", "", 0, "", "", "", "", "批次已撤销，没有逐文件分类原因",
             int(batch_id), None, 1, now, now))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_feedback(root: str, kind: Optional[str] = None,
                  active_only: bool = True) -> List[Feedback]:
    path = preferences.space_db_path(root)
    if not os.path.isfile(path):
        return []
    conn = _open(root)
    try:
        sql = "SELECT * FROM learning_feedback WHERE space_root = ?"
        params: list = [_norm_root(root)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at ASC, feedback_id ASC"
        return [_row_to_feedback(r) for r in conn.execute(sql, params)]
    except sqlite3.Error as e:
        raise LearningError(f"学习反馈库不可用：{e}")
    finally:
        conn.close()


def count_judgments(root: str, active_only: bool = True) -> int:
    return sum(1 for fb in list_feedback(root, KIND_JUDGMENT, active_only)
               if fb.kind == KIND_JUDGMENT)


# ======================== 历史导入 ========================

def _parse_direction_fields(rule_applied: str, rule_key: str,
                            target_dir: str) -> Tuple[str, str, str]:
    direction = semester = category = ""
    for pattern, name in (
            (r"方向「([^」]*)」", "direction"),
            (r"学期「([^」]*)」", "semester"),
            (r"类别「([^」]*)」", "category")):
        match = re.search(pattern, rule_applied or "")
        if not match:
            continue
        if name == "direction":
            direction = match.group(1)
        elif name == "semester":
            semester = match.group(1)
        else:
            category = match.group(1)
    if rule_key.startswith("ambiguity_choice:"):
        rest = rule_key.split(":", 1)[1]
        d, _sep, c = rest.partition("/")
        direction = direction or d
        category = category or c
    if not direction:
        parts = [p for p in re.split(r"[/\\]", target_dir or "") if p]
        if parts and parts[0] == TARGET_ROOT:
            parts = parts[1:]
        if parts:
            direction = parts[0]
            if direction == "课程":
                semester = semester or (parts[1] if len(parts) > 1 else "")
                category = category or (parts[2] if len(parts) > 2 else "")
            elif direction in ("竞赛", "项目"):
                category = category or (parts[1] if len(parts) > 1 else "")
            elif direction == "校园事务":
                category = category or (parts[1] if len(parts) > 1 else "")
            else:
                category = category or (parts[1] if len(parts) > 1 else "")
    return direction, semester, category


def _dirname(rel_path: str) -> str:
    return os.path.dirname(_norm_rel(rel_path)) if rel_path else ""


def import_history_feedback(root: str,
                            history_db_path: Optional[str] = None) -> ImportResult:
    """把历史库里可复用的 moved 记录一次性导入为反馈（幂等）。

    只认两类 moved 记录：
      - auto_target_rel 非空或 rule_key 以 manual_override: 开头 → 改过去向
      - rule_key 以 ambiguity_choice: 开头 → 歧义里选定的去向
    failed / skipped / cancelled / planned / paused 不导入。
    undone 且没有分类原因的批次，每批至多一条 clue。
    """
    path = history_db_path or os.path.join(
        os.path.abspath(root), preferences.ORGANIZER_DIR, "history.db")
    result = ImportResult()
    if not os.path.isfile(path):
        return result
    conn = None
    try:
        uri = "file:" + quote(path) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
        if "action_index" not in columns:
            return result
        select_cols = [
            "batch_id", "action_index", "source_rel", "target_rel", "status",
            "error",
            "auto_target_rel" if "auto_target_rel" in columns else "'' AS auto_target_rel",
            "rule_key" if "rule_key" in columns else "'' AS rule_key",
            "rule_applied" if "rule_applied" in columns else "'' AS rule_applied",
        ]
        rows = conn.execute(
            "SELECT " + ", ".join(select_cols) + " FROM actions "
            "ORDER BY batch_id ASC, action_index ASC").fetchall()
        undone_batches = set()
        # 同一 source_rel 只保留 batch_id、action_index 最大的那条去向；
        # 另按目标目录保留各去向的最新批次，供现场判断按目标补齐身份。
        choices: Dict[str, dict] = {}
        by_target: Dict[str, Dict[str, dict]] = {}
        for row in rows:
            result.scanned_actions += 1
            status = row["status"] or ""
            if status == "undone":
                undone_batches.add(int(row["batch_id"]))
                continue
            if status != "moved":
                continue
            source_rel = row["source_rel"] or ""
            if not source_rel:
                continue
            rule_key = row["rule_key"] or ""
            auto_target = row["auto_target_rel"] or ""
            target_rel = row["target_rel"] or ""
            is_override = bool(auto_target.strip()) or \
                rule_key.startswith("manual_override:")
            is_choice = rule_key.startswith("ambiguity_choice:")
            if not (is_override or is_choice):
                continue
            batch_id = int(row["batch_id"])
            action_index = int(row["action_index"])
            ident = (batch_id, action_index)
            normalized = _norm_rel(source_rel)
            after_target = _dirname(target_rel)
            before_target = _dirname(auto_target)
            direction, semester, category = _parse_direction_fields(
                row["rule_applied"] or "", rule_key, after_target)
            payload = {
                "ident": ident,
                "source_rel": source_rel,
                "before_target": before_target,
                "after_target": after_target,
                "direction": direction,
                "semester": semester,
                "category": category,
                "rule_key": rule_key,
                "rule_applied": row["rule_applied"] or "",
                "batch_id": batch_id,
                "action_index": action_index,
            }
            current = choices.get(normalized)
            if current is None or ident > current["ident"]:
                choices[normalized] = payload
            bucket = by_target.setdefault(normalized, {})
            target_key = _target_key(after_target)
            seen = bucket.get(target_key)
            if seen is None or ident > seen["ident"]:
                bucket[target_key] = payload
        for normalized, payload in choices.items():
            added = _write_decision(
                root, payload["source_rel"], kind=KIND_JUDGMENT,
                origin="history",
                before_target=payload["before_target"],
                after_target=payload["after_target"],
                keep_in_place=False,
                direction=payload["direction"],
                semester=payload["semester"],
                category=payload["category"],
                rule_key=payload["rule_key"],
                rule_applied=payload["rule_applied"],
                batch_id=payload["batch_id"],
                action_index=payload["action_index"],
                reactivate=False,
                history_alternates=list(by_target.get(normalized, {}).values()))
            if added:
                result.judgments_added += 1
        done_clues = set()
        pending_clues = []
        for batch_id in sorted(undone_batches):
            if batch_id not in done_clues:
                pending_clues.append(batch_id)
        for batch_id in pending_clues:
            if record_undo_clue(root, batch_id):
                result.clues_added += 1
            done_clues.add(batch_id)
    except sqlite3.Error as e:
        return ImportResult(scanned_actions=result.scanned_actions,
                            note=f"历史记录暂不可读，跳过学习导入：{e}")
    finally:
        if conn is not None:
            conn.close()
    return result


def _history_insert_values(root: str, key: str, payload: dict,
                           now: str) -> tuple:
    return (
        _feedback_id(key), _norm_root(root), key, KIND_JUDGMENT, "history",
        payload["source_rel"], payload["before_target"],
        payload["after_target"], 0, payload["direction"],
        payload["semester"], payload["category"], payload["rule_key"],
        payload["rule_applied"], payload["batch_id"],
        payload["action_index"], 1, now, now)


def _insert_history_row(conn: sqlite3.Connection, root: str, key: str,
                        payload: dict, now: str):
    return conn.execute(
        "INSERT INTO learning_feedback("
        "feedback_id, space_root, evidence_key, kind, origin, "
        "source_rel, before_target, after_target, keep_in_place, "
        "direction, semester, category, rule_key, rule_applied, "
        "batch_id, action_index, active, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(evidence_key) DO NOTHING",
        _history_insert_values(root, key, payload, now))


def _write_history_decision(conn: sqlite3.Connection, root: str, key: str,
                            payload: dict, now: str,
                            alternates: Sequence[dict]) -> bool:
    """历史导入写入：先读后写，冲突时在同一连接里重读并条件合并。"""
    conn.execute(
        "SELECT 1 FROM learning_feedback WHERE evidence_key = ?",
        (key,)).fetchone()
    hook = _IMPORT_READ_HOOK
    if hook is not None:
        # 测试用：在读写之间让另一个进程提交，验证不会覆盖现场判断。
        hook(root, payload["source_rel"])
    cur = _insert_history_row(conn, root, key, payload, now)
    conn.commit()
    if cur.rowcount > 0:
        return True
    _merge_history_into_existing(conn, root, key, payload, now, alternates)
    return False


def _merge_history_into_existing(conn: sqlite3.Connection, root: str,
                                 key: str, payload: dict, now: str,
                                 alternates: Sequence[dict]) -> None:
    """插入冲突后重读同一行，只做允许的条件更新。"""
    for _attempt in range(4):
        row = conn.execute(
            "SELECT * FROM learning_feedback WHERE evidence_key = ?",
            (key,)).fetchone()
        if row is None:
            cur = _insert_history_row(conn, root, key, payload, now)
            conn.commit()
            if cur.rowcount > 0:
                return
            continue
        if not bool(row["active"]):
            return
        origin = row["origin"] or ""
        if origin == "history":
            changed = _replace_history_decision(conn, key, payload, now)
            conn.commit()
            if changed:
                return
            continue
        if origin != "live" or bool(row["keep_in_place"]):
            # skip 或未知来源：整行不动，绝不把撤回的判断重新激活。
            return
        target_key = _target_key(row["after_target"])
        matching = [
            item for item in list(alternates) + [payload]
            if _target_key(item["after_target"]) == target_key]
        if not matching:
            # 现场去向与历史不一致：保留现场去向和类别。
            return
        best = max(matching, key=lambda item: item["ident"])
        changed = _set_batch_identity(
            conn, key, "live", row["after_target"],
            best["batch_id"], best["action_index"], now)
        conn.commit()
        if changed:
            return


def _set_batch_identity(conn: sqlite3.Connection, key: str,
                        expected_origin: str, expected_after: str,
                        batch_id: int, action_index: int,
                        now: str) -> int:
    """只回填批次身份：仍是指定来源/目标、仍启用、且身份仍更旧或为空。"""
    cur = conn.execute(
        "UPDATE learning_feedback SET batch_id=?, action_index=?, "
        "updated_at=? "
        "WHERE evidence_key=? AND active=1 AND origin=? AND after_target=? "
        "AND (batch_id IS NULL OR batch_id < ? "
        "     OR (batch_id = ? AND COALESCE(action_index, -1) < ?))",
        (batch_id, action_index, now, key, expected_origin,
         expected_after, batch_id, batch_id, action_index))
    return cur.rowcount


def _replace_history_decision(conn: sqlite3.Connection, key: str,
                              payload: dict, now: str) -> int:
    """纯历史判断：只在仍启用、仍为 history、且批次身份更旧时替换。"""
    cur = conn.execute(
        "UPDATE learning_feedback SET origin='history', source_rel=?, "
        "before_target=?, after_target=?, keep_in_place=0, direction=?, "
        "semester=?, category=?, rule_key=?, rule_applied=?, "
        "batch_id=?, action_index=?, active=1, updated_at=? "
        "WHERE evidence_key=? AND active=1 AND origin='history' "
        "AND (batch_id IS NULL OR batch_id < ? "
        "     OR (batch_id = ? AND COALESCE(action_index, -1) < ?))",
        (payload["source_rel"], payload["before_target"],
         payload["after_target"], payload["direction"],
         payload["semester"], payload["category"], payload["rule_key"],
         payload["rule_applied"], payload["batch_id"],
         payload["action_index"], now, key, payload["batch_id"],
         payload["batch_id"], payload["action_index"]))
    return cur.rowcount


def _target_key(target_dir: str) -> str:
    return os.path.normcase(os.path.normpath(target_dir or ""))


# ======================== 候选归纳 ========================

def _condition_label(match_type: str, source_path: str,
                     include_subdirs: bool, keywords: Sequence[str]) -> str:
    if match_type == MATCH_EXACT_PATH:
        return f"精确路径：{source_path or '（空）'}"
    if match_type == MATCH_DIR_SCOPE:
        where = source_path or "根目录"
        return (f"目录范围：{where}"
                + ("（含子目录）" if include_subdirs else "（仅本级）"))
    if match_type == MATCH_FILENAME_KEYWORD:
        kws = " / ".join(keywords) or "（未填关键词）"
        return (f"文件名关键词：{kws}"
                + (f"，限定目录 {source_path}" if source_path else ""))
    return match_type


def _stems(samples: Sequence[Feedback]) -> List[str]:
    out = []
    for fb in samples:
        name = os.path.basename(_norm_rel(fb.source_rel))
        stem, _ext = os.path.splitext(name)
        out.append(stem)
    return out


def _longest_common_substring(a: str, b: str) -> str:
    if not a or not b:
        return ""
    best_len = 0
    best_end = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len = cur[j]
                    best_end = i
        prev = cur
    return a[best_end - best_len:best_end]


def _clean_keyword(text: str) -> str:
    return (text or "").strip(" .-_—–·()（）[]【】{}<>《》\"'“”‘’")


def _usable_keyword(keyword: str) -> bool:
    if len(keyword) < 2:
        return False
    if keyword.isdigit():
        return False
    if keyword.isascii() and len(keyword) < 3:
        return False
    return True


def _propose_condition(samples: Sequence[Feedback]):
    """给一组同去向样本提议一个现有匹配条件；无法明确时返回 None。"""
    stems = _stems(samples)
    keyword = stems[0] if stems else ""
    for stem in stems[1:]:
        keyword = _longest_common_substring(keyword, stem)
        if not keyword:
            break
    keyword = _clean_keyword(keyword)
    if _usable_keyword(keyword):
        return {
            "match_type": MATCH_FILENAME_KEYWORD,
            "source_path": "",
            "include_subdirs": True,
            "keywords": [keyword],
        }
    parents = {os.path.normcase(_dirname(fb.source_rel)) for fb in samples}
    if len(parents) == 1:
        parent = next(iter(parents))
        if parent:
            return {
                "match_type": MATCH_DIR_SCOPE,
                "source_path": parent,
                "include_subdirs": True,
                "keywords": [],
            }
    return None


def _target_signature(fb: Feedback) -> Tuple[str, str, str, str]:
    return (fb.direction or "", fb.semester or "", fb.category or "",
            os.path.normcase(fb.after_target or ""))


def _condition_signature(cond: dict) -> str:
    return json.dumps(cond, ensure_ascii=False, sort_keys=True, default=str)


def _build_rule(cond: dict, target: Tuple[str, str, str, str]) -> LongTermRule:
    return LongTermRule(
        rule_id="learning-candidate",
        name="学习候选",
        match_type=cond["match_type"],
        source_path=cond.get("source_path") or "",
        include_subdirs=bool(cond.get("include_subdirs", True)),
        keywords=list(cond.get("keywords") or []),
        direction=target[0] or "待定",
        semester=target[1] or "",
        category=target[2] or "",
        enabled=True,
        reason="阶段 5 学习候选（尚未确认）",
    )


def _rule_matches(rule: LongTermRule, rel_path: str) -> bool:
    compiled = compile_config(ClassificationConfig(long_term_rules=[rule]))
    return match_long_term_rule(rel_path, compiled) is not None


def _target_payload(target: Tuple[str, str, str, str]) -> dict:
    direction, semester, category, target_dir = target
    label = (f"{direction}/{category}" if category
             else (direction or "待定"))
    return {
        "direction": direction,
        "semester": semester,
        "category": category,
        "target_dir": target_dir,
        "label": label,
    }


def _sample_payload(fb: Feedback) -> dict:
    return {
        "source_rel": fb.source_rel,
        "feedback_id": fb.feedback_id,
        "before_target": fb.before_target,
        "target_dir": fb.after_target,
        "batch_id": fb.batch_id,
        "action_index": fb.action_index,
    }


def _existing_preference_signatures(root: str) -> set:
    """已有偏好（含停用）的「条件 + 去向」签名；避免重复生成候选。"""
    try:
        prefs, _note = preferences.list_all_preferences_resilient(root)
    except Exception:
        return set()
    out = set()
    for pref in prefs:
        out.add((
            pref.match_type, pref.source_path or "",
            bool(pref.include_subdirs),
            tuple(pref.keywords or []),
            pref.direction or "", pref.semester or "", pref.category or "",
        ))
    return out


def _candidate_matches_pref(sig: set, cond: dict,
                            target: Tuple[str, str, str, str]) -> bool:
    return (
        cond["match_type"], cond.get("source_path") or "",
        bool(cond.get("include_subdirs", True)),
        tuple(cond.get("keywords") or []),
        target[0] or "", target[1] or "", target[2] or "",
    ) in sig


def build_candidates(root: str) -> List[Candidate]:
    """按当前有效 judgment 重新归纳候选；保留已确认/已拒绝/已停用状态。"""
    path = preferences.space_db_path(root)
    if not os.path.isfile(path):
        return []
    judgments = [fb for fb in list_feedback(root, KIND_JUDGMENT, True)]
    groups: Dict[Tuple[str, str, str, str], List[Feedback]] = {}
    for fb in judgments:
        if fb.keep_in_place or not fb.after_target:
            continue
        groups.setdefault(_target_signature(fb), []).append(fb)
    pref_sigs = _existing_preference_signatures(root)
    desired: Dict[str, dict] = {}
    for target, samples in groups.items():
        distinct = {fb.source_rel for fb in samples}
        if len(distinct) < 2 or len({fb.feedback_id for fb in samples}) < 2:
            continue
        cond = _propose_condition(samples)
        if cond is None:
            cid = _candidate_id({
                "root": _norm_root(root), "kind": "whole", "target": target})
            desired[cid] = {
                "match_type": MATCH_DIR_SCOPE, "source_path": "",
                "include_subdirs": True, "keywords": [],
                "target": target,
                "condition_label": "整个整理空间（无法用文件名关键词或目录范围明确）",
                "status": STATUS_BLOCKED,
                "reason": ("这些样本只能归纳成整个整理空间，"
                           "范围太宽，不生成可确认候选。"),
                "samples": samples, "opposing": [], "conflicts": [],
            }
            continue
        if cond["match_type"] == MATCH_EXACT_PATH:
            continue
        rule = _build_rule(cond, target)
        support: List[Feedback] = []
        keep_opposing: List[Feedback] = []
        other_targets: Dict[Tuple[str, str, str, str], List[Feedback]] = {}
        for fb in judgments:
            if not fb.source_rel:
                continue
            if not _rule_matches(rule, fb.source_rel):
                continue
            if fb.keep_in_place:
                keep_opposing.append(fb)
                continue
            sig = _target_signature(fb)
            if sig == target:
                support.append(fb)
            else:
                other_targets.setdefault(sig, []).append(fb)
        used = support or samples
        payload_base = {
            "root": _norm_root(root), "condition": _condition_signature(cond),
            "target": target,
        }
        if other_targets:
            cid = _candidate_id({"root": _norm_root(root),
                                 "condition": _condition_signature(cond),
                                 "kind": "conflict"})
            conflicts = []
            for sig, fbs in sorted(other_targets.items()):
                item = _target_payload(sig)
                item["samples"] = sorted({fb.source_rel for fb in fbs})
                item["target_dir"] = sig[3]
                conflicts.append(item)
            desired[cid] = {
                "match_type": cond["match_type"],
                "source_path": cond.get("source_path") or "",
                "include_subdirs": bool(cond.get("include_subdirs", True)),
                "keywords": list(cond.get("keywords") or []),
                "target": target,
                "condition_label": _condition_label(
                    cond["match_type"], cond.get("source_path") or "",
                    bool(cond.get("include_subdirs", True)),
                    cond.get("keywords") or []),
                "status": STATUS_CONFLICT,
                "reason": ("同一条件会命中不同去向，按冲突展示；"
                           "不会静默取第一条，也不会写入偏好。"),
                "samples": used, "opposing": [], "conflicts": conflicts,
            }
            continue
        if keep_opposing:
            cid = _candidate_id({**payload_base, "kind": "blocked"})
            opposing = [{
                "source_rel": fb.source_rel,
                "reason": "同一条件会命中「保持原位」",
                "feedback_id": fb.feedback_id,
            } for fb in keep_opposing]
            desired[cid] = {
                "match_type": cond["match_type"],
                "source_path": cond.get("source_path") or "",
                "include_subdirs": bool(cond.get("include_subdirs", True)),
                "keywords": list(cond.get("keywords") or []),
                "target": target,
                "condition_label": _condition_label(
                    cond["match_type"], cond.get("source_path") or "",
                    bool(cond.get("include_subdirs", True)),
                    cond.get("keywords") or []),
                "status": STATUS_BLOCKED,
                "reason": ("该条件还会命中用户明确「保持原位」的样本，"
                           "不生成可确认候选。"),
                "samples": used, "opposing": opposing, "conflicts": [],
            }
            continue
        if _candidate_matches_pref(pref_sigs, cond, target):
            continue
        cid = _candidate_id({**payload_base, "kind": "pending"})
        desired[cid] = {
            "match_type": cond["match_type"],
            "source_path": cond.get("source_path") or "",
            "include_subdirs": bool(cond.get("include_subdirs", True)),
            "keywords": list(cond.get("keywords") or []),
            "target": target,
            "condition_label": _condition_label(
                cond["match_type"], cond.get("source_path") or "",
                bool(cond.get("include_subdirs", True)),
                cond.get("keywords") or []),
            "status": STATUS_PENDING,
            "reason": "来自多条独立判断，等待你确认；确认前不会写进偏好表。",
            "samples": used, "opposing": [], "conflicts": [],
        }
    _sync_candidates(root, desired)
    return list_candidates(root)


def _sync_candidates(root: str, desired: Dict[str, dict]) -> None:
    conn = _open(root)
    try:
        existing_rows = conn.execute(
            "SELECT * FROM learning_candidates WHERE space_root = ?",
            (_norm_root(root),)).fetchall()
        existing = {row["candidate_id"]: row for row in existing_rows}
        now = _now()
        for cid, data in desired.items():
            target = data["target"]
            samples = sorted(
                {fb.source_rel: fb for fb in data["samples"]}.values(),
                key=lambda fb: fb.source_rel)
            sample_payload = [_sample_payload(fb) for fb in samples]
            feedback_ids = sorted({fb.feedback_id for fb in samples})
            current = existing.get(cid)
            status = data["status"]
            scope = preferences.SCOPE_SPACE
            preference_id = ""
            created_at = now
            if current is not None:
                if current["status"] in KEEP_STATUSES:
                    status = current["status"]
                scope = current["scope"] or preferences.SCOPE_SPACE
                preference_id = current["preference_id"] or ""
                created_at = current["created_at"] or now
            conn.execute(
                "INSERT INTO learning_candidates("
                "candidate_id, space_root, status, match_type, source_path, "
                "include_subdirs, keywords, direction, semester, category, "
                "target_dir, condition_label, scope, samples, opposing, "
                "conflicts, reason, preference_id, feedback_ids, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET status=excluded.status, "
                "match_type=excluded.match_type, source_path=excluded.source_path, "
                "include_subdirs=excluded.include_subdirs, "
                "keywords=excluded.keywords, direction=excluded.direction, "
                "semester=excluded.semester, category=excluded.category, "
                "target_dir=excluded.target_dir, "
                "condition_label=excluded.condition_label, scope=excluded.scope, "
                "samples=excluded.samples, opposing=excluded.opposing, "
                "conflicts=excluded.conflicts, reason=excluded.reason, "
                "preference_id=excluded.preference_id, "
                "feedback_ids=excluded.feedback_ids, "
                "updated_at=excluded.updated_at",
                (cid, _norm_root(root), status, data["match_type"],
                 data["source_path"], 1 if data["include_subdirs"] else 0,
                 json.dumps(data["keywords"], ensure_ascii=False),
                 target[0], target[1], target[2], target[3],
                 data["condition_label"], scope,
                 json.dumps(sample_payload, ensure_ascii=False),
                 json.dumps(data["opposing"], ensure_ascii=False),
                 json.dumps(data["conflicts"], ensure_ascii=False),
                 data["reason"], preference_id,
                 json.dumps(feedback_ids, ensure_ascii=False),
                 created_at, now))
        for cid, row in existing.items():
            if cid in desired or row["status"] in KEEP_STATUSES:
                continue
            conn.execute(
                "DELETE FROM learning_candidates WHERE candidate_id = ?",
                (cid,))
        conn.commit()
    finally:
        conn.close()


def list_candidates(root: str) -> List[Candidate]:
    path = preferences.space_db_path(root)
    if not os.path.isfile(path):
        return []
    conn = _open(root)
    try:
        rows = conn.execute(
            "SELECT * FROM learning_candidates WHERE space_root = ? "
            "ORDER BY CASE status "
            "WHEN 'pending' THEN 0 WHEN 'conflict' THEN 1 "
            "WHEN 'blocked' THEN 2 WHEN 'confirmed' THEN 3 "
            "WHEN 'disabled' THEN 4 ELSE 5 END, created_at ASC, "
            "candidate_id ASC", (_norm_root(root),)).fetchall()
    except sqlite3.Error as e:
        raise LearningError(f"学习候选库不可用：{e}")
    finally:
        conn.close()
    out = [_row_to_candidate(row) for row in rows]
    for cand in out:
        if cand.preference_id:
            try:
                pref = preferences.get_preference(root, cand.preference_id)
            except Exception:
                pref = None
            cand.preference_enabled = (None if pref is None else bool(pref.enabled))
    return out


def _get_candidate(root: str, candidate_id: str) -> Candidate:
    for cand in list_candidates(root):
        if cand.candidate_id == candidate_id:
            return cand
    raise LearningError(f"找不到候选：{candidate_id}")


# ======================== 确认 / 拒绝 / 停用 ========================

def _preference_from_candidate(root: str, cand: Candidate,
                               scope: str) -> preferences.Preference:
    if scope not in preferences.SCOPE_LABELS:
        raise LearningError(f"未知作用范围：{scope!r}")
    if not cand.samples:
        raise LearningError("候选没有来源样本，无法确认。")
    if not _within_root(root, cand.target_dir):
        raise LearningError("候选目标越出当前整理空间，不能保存。")
    sample_rels = [s.get("source_rel") for s in cand.samples if s.get("source_rel")]
    target_label = cand.target_label.replace("/", "｜")
    return preferences.Preference(
        preference_id=cand.candidate_id,
        name=f"学习建议：{cand.condition_text()} → {target_label}",
        match_type=cand.match_type,
        source_path=cand.source_path,
        include_subdirs=bool(cand.include_subdirs),
        keywords=list(cand.keywords),
        direction=cand.direction or "待定",
        semester=cand.semester,
        category=cand.category,
        inner_mode=INNER_KEEP,
        inner_path="",
        enabled=True,
        scope=scope,
        space_root=os.path.abspath(root),
        reason=(
            "由阶段 5 学习建议确认；来源样本："
            + "、".join(sample_rels[:6])
            + ("…" if len(sample_rels) > 6 else "")),
        confirmed_at=_now(),
        confirmed_source_rel=sample_rels[0] if sample_rels else "",
        source_kind=preferences.SOURCE_USER,
    )


def confirm_candidate(root: str, candidate_id: str,
                      scope: str = preferences.SCOPE_SPACE,
                      saver=None) -> preferences.Preference:
    """把一条待确认候选保存成偏好；失败时候选保持待确认。"""
    cand = _get_candidate(root, candidate_id)
    if cand.status == STATUS_CONFIRMED and cand.preference_id:
        existing = preferences.get_preference(root, cand.preference_id)
        if existing is not None:
            return existing
    if cand.status != STATUS_PENDING:
        raise LearningError(
            f"候选当前状态为「{cand.status_label}」，不能确认。")
    if cand.conflicts:
        raise LearningError("候选存在冲突去向，不能确认。")
    pref = _preference_from_candidate(root, cand, scope)
    if saver is not None:
        saver(pref)
    else:
        preferences.save_preference(root, pref)
    conn = _open(root)
    try:
        conn.execute(
            "UPDATE learning_candidates SET status=?, preference_id=?, scope=?, "
            "updated_at=? WHERE candidate_id=?",
            (STATUS_CONFIRMED, pref.preference_id, scope, _now(),
             candidate_id))
        conn.commit()
    finally:
        conn.close()
    return pref


def reject_candidate(root: str, candidate_id: str) -> bool:
    cand = _get_candidate(root, candidate_id)
    if cand.status == STATUS_CONFIRMED:
        raise LearningError("已确认的候选不能改成拒绝；如需停用请使用停用。")
    conn = _open(root)
    try:
        conn.execute(
            "UPDATE learning_candidates SET status=?, updated_at=? "
            "WHERE candidate_id=?",
            (STATUS_REJECTED, _now(), candidate_id))
        conn.commit()
        return True
    finally:
        conn.close()


def disable_candidate(root: str, candidate_id: str) -> bool:
    cand = _get_candidate(root, candidate_id)
    if not cand.preference_id:
        raise LearningError("候选还没有已保存的偏好，不能停用。")
    if cand.status == STATUS_DISABLED:
        return True
    if not preferences.set_preference_enabled(root, cand.preference_id, False):
        raise LearningError("找不到对应的偏好，未停用。")
    conn = _open(root)
    try:
        conn.execute(
            "UPDATE learning_candidates SET status=?, updated_at=? "
            "WHERE candidate_id=?",
            (STATUS_DISABLED, _now(), candidate_id))
        conn.commit()
    finally:
        conn.close()
    return True
