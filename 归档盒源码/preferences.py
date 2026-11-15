"""
归档盒阶段 2 — 个性化偏好库（纯逻辑，无 Qt 依赖）

设计要点：
- 偏好存在独立 SQLite 库，不写 history.db：
    整理空间偏好：{root}/.organizer/preferences.db
    全局偏好：    scheme_store.app_data_dir()/preferences.db
  历史库损坏改名重建时，偏好不会跟着丢；历史行也不会被改写。
- classification_config.json 里的长期规则只做一次性迁移：本空间偏好库写
  导入标记后不再重导，也不会按 JSON 里的 enabled 把已停用规则改回启用。
  JSON 里的 long_term_rules 同时作为空间偏好的镜像继续保留，历史批次
  config_json 快照与 load_config 仍可读取。
- 真正做长期匹配的是本模块。主窗口生成预览时先迁移，再合并「本空间已
  启用偏好」与「已启用全局偏好」，按匹配等级应用：
    精确路径 > 文件名关键词 > 目录范围
  同等级、目标不同时不按列表顺序静默取第一条，而是生成歧义候选。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from classification_config import (
    ClassificationConfig, ClassificationConfigError,
    INNER_CLEAR, INNER_KEEP, INNER_REPLACE, INNER_MODE_LABELS,
    LONG_TERM_DIRECTIONS, LongTermRule,
    MATCH_DIR_SCOPE, MATCH_EXACT_PATH, MATCH_FILENAME_KEYWORD,
    MATCH_TYPE_LABELS,
    clone_config, compile_config, config_path, default_config, ensure_valid,
    load_config, match_long_term_rule, save_config, validate_config,
)
from planner import (
    MODE_BY_DIRECTION_SEMESTER, PlanItem, OrganizePlan,
    _build_target_dir_v2, _sanitize_component, assign_unique_targets,
)
import scheme_store

PREF_DB_FILENAME = "preferences.db"
ORGANIZER_DIR = ".organizer"
META_IMPORT_MARKER = "json_long_term_imported"

SCOPE_SPACE = "space"
SCOPE_GLOBAL = "global"
SCOPE_LABELS = {
    SCOPE_SPACE: "当前整理空间",
    SCOPE_GLOBAL: "全局（所有整理空间）",
}
SCOPE_LABEL_TO_KEY = {v: k for k, v in SCOPE_LABELS.items()}

SOURCE_USER = "user_confirmed"
SOURCE_MIGRATED = "migrated_json"
SOURCE_KIND_LABELS = {
    SOURCE_USER: "用户确认",
    SOURCE_MIGRATED: "旧 JSON 迁入",
}

# 匹配等级：数字越小优先级越高。
MATCH_LEVEL = {
    MATCH_EXACT_PATH: 0,
    MATCH_FILENAME_KEYWORD: 1,
    MATCH_DIR_SCOPE: 2,
}
MATCH_LEVEL_LABELS = {0: "精确路径", 1: "文件名关键词", 2: "目录范围"}


class PreferenceError(ValueError):
    """偏好库不可用、校验失败或写入失败。"""


# ======================== 数据模型 ========================

@dataclass
class Preference:
    preference_id: str
    name: str
    match_type: str = MATCH_EXACT_PATH
    source_path: str = ""
    include_subdirs: bool = True
    keywords: List[str] = field(default_factory=list)
    direction: str = "课程"
    semester: str = ""
    category: str = ""
    inner_mode: str = INNER_KEEP
    inner_path: str = ""
    enabled: bool = True
    scope: str = SCOPE_SPACE
    space_root: str = ""
    reason: str = ""
    confirmed_at: str = ""
    confirmed_source_rel: str = ""
    source_kind: str = SOURCE_USER
    sort_order: int = 0

    @classmethod
    def from_rule(cls, rule: LongTermRule, *, scope: str = SCOPE_SPACE,
                  space_root: str = "", confirmed_at: str = "",
                  confirmed_source_rel: str = "",
                  source_kind: str = SOURCE_USER,
                  sort_order: int = 0) -> "Preference":
        return cls(
            preference_id=rule.rule_id,
            name=rule.name,
            match_type=rule.match_type,
            source_path=rule.source_path,
            include_subdirs=bool(rule.include_subdirs),
            keywords=list(rule.keywords),
            direction=rule.direction,
            semester=rule.semester or "",
            category=rule.category or "",
            inner_mode=rule.inner_mode,
            inner_path=rule.inner_path or "",
            enabled=bool(rule.enabled),
            scope=scope,
            space_root=space_root,
            reason=rule.reason or "",
            confirmed_at=confirmed_at,
            confirmed_source_rel=confirmed_source_rel,
            source_kind=source_kind,
            sort_order=sort_order,
        )

    def to_rule(self) -> LongTermRule:
        return LongTermRule(
            rule_id=self.preference_id,
            name=self.name,
            match_type=self.match_type,
            source_path=self.source_path,
            include_subdirs=bool(self.include_subdirs),
            keywords=list(self.keywords),
            direction=self.direction,
            semester=self.semester or "",
            category=self.category or "",
            inner_mode=self.inner_mode,
            inner_path=self.inner_path or "",
            enabled=bool(self.enabled),
            reason=self.reason or "",
        )

    @property
    def scope_label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope)

    @property
    def target_label(self) -> str:
        parts = [self.direction or "（未填方向）"]
        if self.semester:
            parts.append(self.semester)
        if self.category:
            parts.append(self.category)
        if self.inner_mode == INNER_REPLACE and self.inner_path:
            parts.append(self.inner_path)
        return " / ".join(parts)

    def match_label(self) -> str:
        mt = MATCH_TYPE_LABELS.get(self.match_type, self.match_type)
        if self.match_type == MATCH_EXACT_PATH:
            return f"{mt}：{self.source_path or '（空）'}"
        if self.match_type == MATCH_DIR_SCOPE:
            where = self.source_path or "根目录"
            return f"{mt}：{where}" + ("（含子目录）" if self.include_subdirs else "（仅本级）")
        kws = " / ".join(self.keywords) or "（未填关键词）"
        return f"{mt}：{kws}" + (f"，限定 {self.source_path}" if self.source_path else "")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_root(root: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(root)))


def validate_preference(pref: Preference) -> List[str]:
    """返回错误列表，空表示可用。复用长期规则的字段校验。"""
    errors: List[str] = []
    if pref.scope not in SCOPE_LABELS:
        errors.append(f"作用范围必须是 space/global（当前：{pref.scope!r}）")
    if pref.source_kind not in SOURCE_KIND_LABELS:
        errors.append(f"来源类型必须是 user_confirmed/migrated_json（当前：{pref.source_kind!r}）")
    if not isinstance(pref.enabled, bool):
        errors.append("启用状态必须是布尔值")
    if not isinstance(pref.include_subdirs, bool):
        errors.append("include_subdirs 必须是布尔值")
    if pref.direction not in LONG_TERM_DIRECTIONS:
        errors.append(f"方向必须是 {'/'.join(LONG_TERM_DIRECTIONS)}（当前：{pref.direction!r}）")
    if pref.inner_mode not in INNER_MODE_LABELS:
        errors.append(f"内部路径方式必须是 keep/replace/clear（当前：{pref.inner_mode!r}）")
    try:
        cfg = ClassificationConfig(long_term_rules=[pref.to_rule()])
        errors.extend(validate_config(cfg))
    except Exception as e:                     # 字段类型异常等
        errors.append(f"偏好字段不完整：{e}")
    return errors


def _raise_if_invalid(pref: Preference) -> None:
    errors = validate_preference(pref)
    if errors:
        raise PreferenceError("偏好未通过校验：\n" + "\n".join(f"· {e}" for e in errors))


# ======================== SQLite ========================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    match_type TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    include_subdirs INTEGER NOT NULL DEFAULT 1,
    keywords TEXT NOT NULL DEFAULT '[]',
    direction TEXT NOT NULL,
    semester TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    inner_mode TEXT NOT NULL DEFAULT 'keep',
    inner_path TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL DEFAULT 'space',
    space_root TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    confirmed_source_rel TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'user_confirmed',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

_COLUMNS = (
    "preference_id", "name", "match_type", "source_path", "include_subdirs",
    "keywords", "direction", "semester", "category", "inner_mode",
    "inner_path", "enabled", "scope", "space_root", "reason",
    "confirmed_at", "confirmed_source_rel", "source_kind", "sort_order",
    "created_at", "updated_at",
)


def space_db_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), ORGANIZER_DIR, PREF_DB_FILENAME)


def global_db_path() -> str:
    return os.path.join(scheme_store.app_data_dir(), PREF_DB_FILENAME)


def _open(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_SCHEMA)
    # 兼容旧库：只补列，不改旧字段含义
    cols = {row[1] for row in conn.execute("PRAGMA table_info(preferences)")}
    additions = {
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "TEXT NOT NULL DEFAULT ''",
        "confirmed_source_rel": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT 'user_confirmed'",
        "space_root": "TEXT NOT NULL DEFAULT ''",
        "reason": "TEXT NOT NULL DEFAULT ''",
    }
    for col, decl in additions.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE preferences ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def _row_to_pref(row: sqlite3.Row) -> Preference:
    try:
        keywords = json.loads(row["keywords"] or "[]")
        if not isinstance(keywords, list):
            keywords = []
    except (TypeError, ValueError):
        keywords = []
    return Preference(
        preference_id=row["preference_id"],
        name=row["name"],
        match_type=row["match_type"],
        source_path=row["source_path"] or "",
        include_subdirs=bool(row["include_subdirs"]),
        keywords=[str(k) for k in keywords],
        direction=row["direction"],
        semester=row["semester"] or "",
        category=row["category"] or "",
        inner_mode=row["inner_mode"] or INNER_KEEP,
        inner_path=row["inner_path"] or "",
        enabled=bool(row["enabled"]),
        scope=row["scope"] or SCOPE_SPACE,
        space_root=row["space_root"] or "",
        reason=row["reason"] or "",
        confirmed_at=row["confirmed_at"] or "",
        confirmed_source_rel=row["confirmed_source_rel"] or "",
        source_kind=row["source_kind"] or SOURCE_USER,
        sort_order=int(row["sort_order"] or 0),
    )


def _pref_values(pref: Preference) -> tuple:
    return (
        pref.preference_id, pref.name, pref.match_type, pref.source_path,
        1 if pref.include_subdirs else 0,
        json.dumps(list(pref.keywords), ensure_ascii=False),
        pref.direction, pref.semester or "", pref.category or "",
        pref.inner_mode, pref.inner_path or "",
        1 if pref.enabled else 0, pref.scope, pref.space_root or "",
        pref.reason or "", pref.confirmed_at or "",
        pref.confirmed_source_rel or "", pref.source_kind,
        int(pref.sort_order or 0),
    )


def _upsert(conn: sqlite3.Connection, pref: Preference,
            preserve_created: bool = True) -> None:
    now = _now()
    values = _pref_values(pref)
    if preserve_created:
        conn.execute(
            f"INSERT INTO preferences ({', '.join(_COLUMNS[:19])}, created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
            "ON CONFLICT(preference_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS[1:19])
            + ", updated_at=excluded.updated_at",
            values + (now, now),
        )
    else:
        conn.execute(
            f"INSERT OR REPLACE INTO preferences ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            values + (now, now),
        )


def _list(path: str) -> List[Preference]:
    if not os.path.isfile(path):
        return []
    conn = _open(path)
    try:
        rows = conn.execute(
            "SELECT * FROM preferences ORDER BY sort_order ASC, "
            "confirmed_at ASC, preference_id ASC").fetchall()
        return [_row_to_pref(r) for r in rows]
    finally:
        conn.close()


def list_space_preferences(root: str) -> List[Preference]:
    return _list(space_db_path(root))


def list_global_preferences() -> List[Preference]:
    return _list(global_db_path())


def list_effective_preferences(root: str) -> List[Preference]:
    """本空间已启用偏好 + 已启用全局偏好；同一 preference_id 只保留一次。"""
    seen: Dict[str, Preference] = {}
    for pref in list_space_preferences(root):
        if pref.enabled:
            seen.setdefault(pref.preference_id, pref)
    for pref in list_global_preferences():
        if pref.enabled:
            seen.setdefault(pref.preference_id, pref)
    return list(seen.values())


def list_all_preferences(root: str) -> List[Preference]:
    """配置页展示用：本空间全部偏好（含停用）+ 全局偏好。"""
    out = list_space_preferences(root)
    ids = {p.preference_id for p in out}
    for pref in list_global_preferences():
        if pref.preference_id not in ids:
            out.append(pref)
    return out


def _dedupe(preferences: Sequence[Preference]) -> List[Preference]:
    seen: Dict[str, Preference] = {}
    for pref in preferences:
        seen.setdefault(pref.preference_id, pref)
    return list(seen.values())


def effective_preferences_resilient(root: str) -> Tuple[List[Preference], str]:
    """读取生效偏好；任一库损坏时仍返回能读到的部分，并附带说明。

    返回 (偏好列表, 说明)。不会因空间库或全局库打不开而抛异常；
    空间库损坏时只返回全局偏好，绝不回退 JSON 长期规则。
    """
    notes: List[str] = []
    space: List[Preference] = []
    try:
        space = [p for p in list_space_preferences(root) if p.enabled]
    except Exception as e:
        notes.append(f"本空间偏好库不可用：{e}")
    globals_: List[Preference] = []
    try:
        globals_ = [p for p in list_global_preferences() if p.enabled]
    except Exception as e:
        notes.append(f"全局偏好库不可用：{e}")
    return _dedupe(space + globals_), "；".join(notes)


def list_all_preferences_resilient(root: str) -> Tuple[List[Preference], str]:
    """配置页展示用；任一库损坏时仍返回能读到的偏好（含停用）。"""
    notes: List[str] = []
    space: List[Preference] = []
    try:
        space = list_space_preferences(root)
    except Exception as e:
        notes.append(f"本空间偏好库不可用：{e}")
    globals_: List[Preference] = []
    try:
        globals_ = list_global_preferences()
    except Exception as e:
        notes.append(f"全局偏好库不可用：{e}")
    return _dedupe(space + globals_), "；".join(notes)


def get_preference(root: str, preference_id: str) -> Optional[Preference]:
    for pref in list_space_preferences(root):
        if pref.preference_id == preference_id:
            return pref
    for pref in list_global_preferences():
        if pref.preference_id == preference_id:
            return pref
    return None


# ======================== 一次性迁移 ========================

def ensure_migrated(root: str, config: Optional[ClassificationConfig]) -> int:
    """把 JSON 长期规则迁入本空间偏好库，返回本次插入条数。

    - 本空间偏好库已有导入标记：直接返回 0，不再插入，也不改 enabled。
    - rule_id / enabled / 匹配条件 / 目标原样保留。
    - 不改写、不删除 JSON 里的 long_term_rules。
    """
    rules = list(getattr(config, "long_term_rules", []) or [])
    if not rules:
        return 0
    path = space_db_path(root)
    conn = _open(path)
    try:
        marker = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (META_IMPORT_MARKER,)
        ).fetchone()
        if marker is not None and marker["value"] == "1":
            return 0
        inserted = 0
        now = _now()
        with conn:
            for index, rule in enumerate(rules):
                pref = Preference.from_rule(
                    rule, scope=SCOPE_SPACE, space_root=os.path.abspath(root),
                    confirmed_at=now, confirmed_source_rel="",
                    source_kind=SOURCE_MIGRATED, sort_order=index,
                )
                # 同一 preference_id 已存在时保持原行（包括停用状态），不覆盖
                cur = conn.execute(
                    "INSERT OR IGNORE INTO preferences "
                    f"({', '.join(_COLUMNS[:19])}, created_at, updated_at) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    _pref_values(pref) + (now, now),
                )
                inserted += cur.rowcount if cur.rowcount > 0 else 0
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'",
                (META_IMPORT_MARKER,),
            )
        return inserted
    finally:
        conn.close()


def import_marker_present(root: str) -> bool:
    path = space_db_path(root)
    if not os.path.isfile(path):
        return False
    conn = _open(path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (META_IMPORT_MARKER,)
        ).fetchone()
        return bool(row is not None and row["value"] == "1")
    finally:
        conn.close()


# ======================== JSON 镜像 ========================

def _upsert_rule(rules: List[LongTermRule], rule: LongTermRule) -> None:
    for index, existing in enumerate(rules):
        if existing.rule_id == rule.rule_id:
            rules[index] = rule
            return
    rules.append(rule)


def _remove_rule(rules: List[LongTermRule], rule_id: str) -> bool:
    before = len(rules)
    rules[:] = [r for r in rules if r.rule_id != rule_id]
    return len(rules) != before


def _read_json_config(root: str) -> ClassificationConfig:
    try:
        cfg, _source = load_config(root)
        return cfg
    except ClassificationConfigError as e:
        raise PreferenceError(
            f"该整理空间的 classification_config.json 不可用，无法同步长期规则镜像：{e}")


def _restore_json(root: str, cfg: ClassificationConfig) -> bool:
    try:
        save_config(root, cfg)
        return True
    except Exception:
        return False


def _read_config_state(root: str):
    """读取 classification_config.json 的现状。

    返回 (cfg, raw_bytes, existed)：
    - 文件不存在：(None, None, False)；
    - 文件可解析：(cfg, None, True)；
    - 文件损坏：(None, 原始字节, True)。损坏不阻止本次保存，失败时用原始
      字节恢复，不让一份“修了一半”的新配置留在磁盘上。
    文件本身读不出来（IO 错误）时抛 PreferenceError：没有原始字节，
    无法保证失败回滚。
    """
    path = config_path(root)
    if not os.path.isfile(path):
        return None, None, False
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise PreferenceError(
            f"该整理空间的 classification_config.json 无法读取，"
            f"不能保证保存失败时恢复原文件：{e}")
    try:
        cfg, _source = load_config(root)
        return cfg, None, True
    except ClassificationConfigError:
        return None, raw, True


def _write_config_bytes(path: str, raw: bytes) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".archive-box.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _restore_config_state(root: str, cfg: Optional[ClassificationConfig],
                          raw: Optional[bytes], existed: bool) -> bool:
    """把 JSON 恢复到保存前：可解析的旧配置、损坏的原始字节，或“原本没有”。"""
    path = config_path(root)
    try:
        if cfg is not None:
            return _restore_json(root, cfg)
        if raw is not None:
            _write_config_bytes(path, raw)
            return True
        if not existed:
            if os.path.exists(path):
                os.remove(path)
            return True
        return False
    except Exception:
        return False


_ABSENT = object()


def _begin_write(conn: sqlite3.Connection) -> None:
    """开始写事务：先拿写锁，再重读本次要改的行，避免检查后被别处覆盖。"""
    conn.execute("BEGIN IMMEDIATE")


def _restore_db_rows(conn: sqlite3.Connection,
                     snapshot: Dict[str, Preference],
                     written: Dict[str, Optional[Preference]]):
    """补偿已提交的偏好库。

    返回 (status, decisions)：status 为 restored / kept / mixed / failed，
    decisions 是每个 id 的处理结果（kept 表示保留了其他窗口的较新值，
    restored 表示已回到本次开始前）。JSON 镜像必须按同一份 decisions
    逐条处理，不能一处恢复、一处保留。
    """
    decisions: Dict[str, str] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for pid, written_pref in written.items():
            rows = conn.execute(
                "SELECT * FROM preferences WHERE preference_id = ?",
                (pid,)).fetchall()
            current = _row_to_pref(rows[0]) if rows else None
            current_fp = (preference_fingerprint(current)
                          if current is not None else None)
            written_fp = (preference_fingerprint(written_pref)
                          if written_pref is not None else None)
            if current_fp != written_fp:
                decisions[pid] = "kept"
                continue
            old = snapshot.get(pid)
            if old is not None:
                _upsert(conn, old)
            else:
                conn.execute(
                    "DELETE FROM preferences WHERE preference_id = ?", (pid,))
            decisions[pid] = "restored"
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return "failed", {}
    kept = [d for d in decisions.values() if d == "kept"]
    restored = [d for d in decisions.values() if d == "restored"]
    if kept and restored:
        return "mixed", decisions
    if kept:
        return "kept", decisions
    return "restored", decisions


_DROP_RULE = object()


def _json_state(root: str):
    """当前 JSON 文件状态：字节内容，或 _ABSENT（文件不存在）。"""
    path = config_path(root)
    if not os.path.isfile(path):
        return _ABSENT
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _merge_json_after_failure(root: str, decisions: Dict[str, str],
                              final_rows: Dict[str, Preference],
                              snapshot: Optional[Dict[str, Preference]],
                              old_cfg: Optional[ClassificationConfig]):
    """按空间库的逐条补偿结果修正 JSON 镜像。

    基线：另一个窗口已经写过这份 JSON，不能整份换回本次开始前的版本。
    未碰过的规则和 JSON 里的其他字段保留后来者写下的内容；本次碰过的
    每条空间规则按空间库同一份 decisions 处理：库保留较新值，JSON 也保留
    同一个较新值；库恢复成开始前，JSON 同一条也回到开始前。
    返回 (status, kept_ids)。
    """
    try:
        cfg, _raw, _existed = _read_config_state(root)
    except Exception:
        return "failed", []
    if cfg is None:
        return "failed", []
    merged = clone_config(cfg)
    rules = list(merged.long_term_rules)
    index = {r.rule_id: i for i, r in enumerate(rules)}
    planned: Dict[int, object] = {}

    def _old_rule(pid: str):
        if snapshot is not None:
            old = snapshot.get(pid)
            return old.to_rule() if old is not None else None
        if old_cfg is not None:
            for rule in old_cfg.long_term_rules:
                if rule.rule_id == pid:
                    return rule
        return None

    for pid, decision in decisions.items():
        if decision == "kept":
            pref = final_rows.get(pid)
            rule = pref.to_rule() if pref is not None else None
        else:
            rule = _old_rule(pid)
        if pid in index:
            planned[index[pid]] = rule if rule is not None else _DROP_RULE
        elif rule is not None:
            rules.append(rule)
            planned[len(rules) - 1] = rule
    merged.long_term_rules = [
        (planned[i] if i in planned else rule)
        for i, rule in enumerate(rules)
        if not (i in planned and planned[i] is _DROP_RULE)
    ]
    try:
        save_config(root, merged)
    except Exception:
        return "failed", []
    kept_ids = [pid for pid, d in decisions.items() if d == "kept"]
    return ("kept" if kept_ids else "restored"), kept_ids



def save_preference(root: str, pref: Preference) -> None:
    """保存一条用户确认偏好。

    写入前先打开要写入的库：库打不开时 JSON 完全不动。空间偏好随后先写
    JSON 镜像、再写 SQLite；SQLite 写入失败时把 JSON 回滚到调用前。
    全局偏好只写全局库，不写任何空间的 classification_config.json。
    """
    _raise_if_invalid(pref)
    if pref.scope == SCOPE_GLOBAL:
        conn = _open(global_db_path())
        try:
            with conn:
                _upsert(conn, pref)
        finally:
            conn.close()
        return

    if pref.scope != SCOPE_SPACE:
        raise PreferenceError(f"未知作用范围：{pref.scope!r}")
    old_cfg = _read_json_config(root)
    new_cfg = clone_config(old_cfg)
    _upsert_rule(new_cfg.long_term_rules, pref.to_rule())
    ensure_valid(new_cfg)
    # 先打开库：损坏/不可写时在写 JSON 之前就失败，镜像保持调用前内容。
    conn = _open(space_db_path(root))
    json_written = False
    try:
        save_config(root, new_cfg)
        json_written = True
        with conn:
            _upsert(conn, pref)
    except Exception as e:
        if not json_written:
            raise PreferenceError(f"偏好库写入失败，未写入任何一边：{e}")
        if _restore_json(root, old_cfg):
            raise PreferenceError(f"偏好库写入失败，已回滚 JSON 镜像：{e}")
        raise PreferenceError(
            f"偏好库写入失败，且 JSON 镜像未能确认恢复，请检查配置文件：{e}")
    finally:
        conn.close()


def delete_preference(root: str, preference_id: str) -> bool:
    """删除一条偏好；空间偏好同时从 JSON 镜像移除。"""
    for pref in list_space_preferences(root):
        if pref.preference_id == preference_id:
            old_cfg = _read_json_config(root)
            new_cfg = clone_config(old_cfg)
            _remove_rule(new_cfg.long_term_rules, preference_id)
            ensure_valid(new_cfg)
            save_config(root, new_cfg)
            conn = _open(space_db_path(root))
            try:
                with conn:
                    conn.execute("DELETE FROM preferences WHERE preference_id = ?",
                                 (preference_id,))
            except Exception as e:
                restored = _restore_json(root, old_cfg)
                if restored:
                    raise PreferenceError(
                        f"偏好库删除失败，已回滚 JSON 镜像：{e}")
                raise PreferenceError(
                    f"偏好库删除失败，且 JSON 镜像未能确认恢复，请检查：{e}")
            finally:
                conn.close()
            return True
    conn = _open(global_db_path())
    try:
        with conn:
            cur = conn.execute("DELETE FROM preferences WHERE preference_id = ?",
                               (preference_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def set_preference_enabled(root: str, preference_id: str, enabled: bool) -> bool:
    pref = None
    for candidate in list_space_preferences(root):
        if candidate.preference_id == preference_id:
            pref = candidate
            break
    if pref is not None:
        updated = Preference(**{**pref.__dict__, "enabled": bool(enabled)})
        save_preference(root, updated)
        return True
    for candidate in list_global_preferences():
        if candidate.preference_id == preference_id:
            updated = Preference(**{**candidate.__dict__, "enabled": bool(enabled)})
            save_preference(root, updated)
            return True
    return False


def _apply_rows(conn: sqlite3.Connection, prefs: Sequence[Preference],
                delete_ids: set) -> None:
    new_ids = set()
    for index, pref in enumerate(prefs):
        pref.sort_order = index
        _upsert(conn, pref)
        new_ids.add(pref.preference_id)
    for stale_id in set(delete_ids) - new_ids:
        conn.execute("DELETE FROM preferences WHERE preference_id = ?",
                     (stale_id,))


def _rows_from_conn(conn: sqlite3.Connection) -> Dict[str, Preference]:
    rows = conn.execute(
        "SELECT * FROM preferences ORDER BY sort_order ASC, "
        "confirmed_at ASC, preference_id ASC").fetchall()
    return {row["preference_id"]: _row_to_pref(row) for row in rows}


_FINGERPRINT_FIELDS = (
    "name", "match_type", "source_path", "include_subdirs", "direction",
    "semester", "category", "inner_mode", "inner_path", "enabled",
    "scope", "reason",
)


def preference_fingerprint(pref: Preference) -> str:
    """用于判断一条偏好在两次读取之间是否被改动（不含排序/确认时间）。"""
    payload = {name: getattr(pref, name) for name in _FINGERPRINT_FIELDS}
    payload["keywords"] = list(pref.keywords or [])
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _conflict_error(pref: Preference) -> PreferenceError:
    return PreferenceError(
        f"偏好「{pref.name}」({pref.preference_id}) 已在其他窗口被修改，"
        "本次保存已取消。请重新打开配置页后再修改。")


def persist_preferences(root: str, preferences: Sequence[Preference],
                        sync_json: bool = True, *,
                        original_ids: Optional[Sequence[str]] = None,
                        write_space: bool = True,
                        write_global: bool = True,
                        config: Optional[ClassificationConfig] = None,
                        baseline: Optional[Sequence[Preference]] = None) -> None:
    """原子保存配置页编辑后的偏好。

    - `baseline` 是打开编辑窗口时的偏好快照。相对快照没有改过的行不再写回，
      库里若已被其他窗口更新就保留库里的内容；同一 id 在快照之后被别处
      改过、本窗口又提交了不同内容时，整次保存取消（不写任何一边）。
    - 作用范围变化（空间 ↔ 全局）算本窗口的修改：原行在原库里且与快照一致
      时，从原库删除并写入新范围的库；原行已被别处改过时整次取消。
    - `original_ids` 仅在未提供 baseline 时用于删除：只删打开时已存在、
      本次又移除的行；编辑期间其他窗口新增的行保留。
    - `write_space=False` / `write_global=False` 时不触碰对应库。
    - 先打开所有需要的库，再写数据库语句；JSON 镜像在提交前写入。
      任一库提交失败都会把已提交的库补偿回保存前，并恢复 JSON，不会出现
      “报错但新目标已生效”。只有三处都确认恢复才会写“已整体回滚”。
    - 旧 JSON 无法解析时不阻止保存：偏好库都写成功就用新配置覆盖它；
      后续失败则把损坏的原文件字节放回去。
    """
    prefs = list(preferences)
    space_prefs = [p for p in prefs if p.scope == SCOPE_SPACE]
    global_prefs = [p for p in prefs if p.scope == SCOPE_GLOBAL]
    for pref in prefs:
        _raise_if_invalid(pref)
    originals = set(original_ids or [])
    snapshot_prefs = ({p.preference_id: p for p in baseline}
                      if baseline is not None else None)

    space_conn = None
    global_conn = None
    old_cfg = None
    old_raw = None
    old_existed = False
    new_cfg = None
    json_written = False
    written_json = None
    committed_space = False
    committed_global = False
    pre_space: Dict[str, Preference] = {}
    pre_global: Dict[str, Preference] = {}
    space_write: List[Preference] = []
    global_write: List[Preference] = []
    space_delete: set = set()
    global_delete: set = set()
    try:
        # 1) 先打开所有需要的库；数据库损坏会在任何写入之前失败。
        if write_space:
            space_conn = _open(space_db_path(root))
        if write_global:
            global_conn = _open(global_db_path())

        current_space = _rows_from_conn(space_conn) if space_conn else {}
        current_global = _rows_from_conn(global_conn) if global_conn else {}
        pre_space = dict(current_space)
        pre_global = dict(current_global)

        space_write = list(space_prefs)
        global_write = list(global_prefs)

        def _current_for(scope: str) -> Dict[str, Preference]:
            return current_space if scope == SCOPE_SPACE else current_global

        def _writable(scope: str) -> bool:
            return write_space if scope == SCOPE_SPACE else write_global

        def _unwritable_error(pref: Preference,
                              scope: Optional[str] = None) -> PreferenceError:
            scope = scope or pref.scope
            label = ("全局偏好库不可用" if scope == SCOPE_GLOBAL
                     else "本空间偏好库不可用")
            return PreferenceError(
                f"{label}，无法把偏好「{pref.name}」({pref.preference_id}) "
                f"保存为「{SCOPE_LABELS.get(scope, scope)}」；"
                "本次保存已取消，范围和目标保持保存前。")

        if snapshot_prefs is not None:
            # 目标库这次写不了时不能假装保存成功：新增或改过的行要明确失败。
            for pref in prefs:
                if _writable(pref.scope):
                    continue
                orig = snapshot_prefs.get(pref.preference_id)
                if orig is None or preference_fingerprint(pref) != \
                        preference_fingerprint(orig):
                    raise _unwritable_error(pref)
            submitted_ids = {p.preference_id for p in prefs}
            migrated_space: set = set()
            migrated_global: set = set()
            for pref in prefs:
                orig = snapshot_prefs.get(pref.preference_id)
                if orig is None:
                    continue                       # 本窗口新增
                old_fp = preference_fingerprint(orig)
                if preference_fingerprint(pref) == old_fp:
                    continue                       # 本窗口没改：保留库里的内容
                old_scope, new_scope = orig.scope, pref.scope
                if not _writable(new_scope):
                    raise _unwritable_error(pref, new_scope)
                if not _writable(old_scope):
                    raise _unwritable_error(pref, old_scope)
                if old_scope == new_scope:
                    current = _current_for(new_scope).get(pref.preference_id)
                    if current is None or preference_fingerprint(current) != old_fp:
                        raise _conflict_error(pref)
                    continue
                # 作用范围变化：原行必须还在原来的库里，且与打开时一致。
                old_row = _current_for(old_scope).get(pref.preference_id)
                if old_row is None or preference_fingerprint(old_row) != old_fp:
                    raise _conflict_error(pref)
                new_row = _current_for(new_scope).get(pref.preference_id)
                if new_row is not None and preference_fingerprint(new_row) != old_fp:
                    raise _conflict_error(pref)
                (migrated_space if old_scope == SCOPE_SPACE
                 else migrated_global).add(pref.preference_id)
            for stale_id in set(snapshot_prefs) - submitted_ids:
                orig = snapshot_prefs[stale_id]
                current = current_space.get(stale_id) or current_global.get(stale_id)
                if current is None:
                    continue
                if not _writable(current.scope):
                    continue
                if preference_fingerprint(current) != preference_fingerprint(orig):
                    raise _conflict_error(current)
                if stale_id in current_space and write_space:
                    space_delete.add(stale_id)
                if stale_id in current_global and write_global:
                    global_delete.add(stale_id)
            space_delete |= migrated_space
            global_delete |= migrated_global
            # 没改过的行不写回，避免覆盖其他窗口的更新；改了范围的行
            # 会进入新范围的写入列表，同时由上面的 delete 集合从原库移除。
            space_write = [
                p for p in space_prefs
                if snapshot_prefs.get(p.preference_id) is None
                or preference_fingerprint(p)
                != preference_fingerprint(snapshot_prefs[p.preference_id])]
            global_write = [
                p for p in global_prefs
                if snapshot_prefs.get(p.preference_id) is None
                or preference_fingerprint(p)
                != preference_fingerprint(snapshot_prefs[p.preference_id])]
        else:
            space_delete = originals - {p.preference_id for p in space_prefs}
            global_delete = originals - {p.preference_id for p in global_prefs}

        # 2) 准备 JSON 镜像。旧文件损坏时不阻止保存：用调用方给的配置
        #    （配置页草稿）或内置默认继续，并记住原始字节以便失败时放回。
        if sync_json:
            old_cfg, old_raw, old_existed = _read_config_state(root)
            base_cfg = (config if config is not None
                        else (old_cfg if old_cfg is not None
                              else default_config()))
            new_cfg = clone_config(base_cfg)

        # 3) 两个库各自开启写事务并写入，但先不提交
        if space_conn is not None:
            _begin_write(space_conn)
        if global_conn is not None:
            _begin_write(global_conn)

        # 3.1) 拿到写锁后再读一次本次要改的行：如果检查之后、写入之前
        #      别处已经保存了较新的值，这次保存整次取消，不覆盖别人的结果。
        fresh_space = _rows_from_conn(space_conn) if space_conn else {}
        fresh_global = _rows_from_conn(global_conn) if global_conn else {}
        for before, fresh, writes, deletes in (
                (current_space, fresh_space, space_write, space_delete),
                (current_global, fresh_global, global_write, global_delete)):
            touched = {p.preference_id for p in writes} | set(deletes)
            for pid in touched:
                old_row = before.get(pid)
                new_row = fresh.get(pid)
                old_fp = (preference_fingerprint(old_row)
                          if old_row is not None else None)
                new_fp = (preference_fingerprint(new_row)
                          if new_row is not None else None)
                if old_fp != new_fp:
                    target = next((p for p in writes
                                   if p.preference_id == pid), None)
                    raise _conflict_error(target or new_row or old_row)

        if space_conn is not None:
            _apply_rows(space_conn, space_write, space_delete)
        if global_conn is not None:
            _apply_rows(global_conn, global_write, global_delete)

        # 4) 用写后的最终行生成 JSON 镜像，写成功后才提交 SQLite
        if sync_json:
            if write_space and space_conn is not None:
                final_rows = _rows_from_conn(space_conn).values()
                new_cfg.long_term_rules = [p.to_rule() for p in final_rows]
            ensure_valid(new_cfg)
            save_config(root, new_cfg)
            json_written = True
            written_json = _json_state(root)

        if space_conn is not None:
            space_conn.commit()
            committed_space = True
        if global_conn is not None:
            global_conn.commit()
            committed_global = True
    except PreferenceError:
        for conn in (space_conn, global_conn):
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
        raise
    except Exception as e:
        for conn in (space_conn, global_conn):
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
        # 已经提交的库无法用 rollback 撤销：只补偿本次写下去、且现在仍
        # 保持本次写入值的内容；已被别的窗口改过的 id 保留那个较新的值。
        space_written: Dict[str, Optional[Preference]] = {
            p.preference_id: p for p in space_write}
        for pid in space_delete:
            space_written.setdefault(pid, None)
        global_written: Dict[str, Optional[Preference]] = {
            p.preference_id: p for p in global_write}
        for pid in global_delete:
            global_written.setdefault(pid, None)

        space_decisions: Dict[str, str] = {
            pid: "restored" for pid in space_written}
        space_state = "restored"
        if committed_space:
            space_state, space_decisions = _restore_db_rows(
                space_conn, pre_space, space_written)
        global_state = "restored"
        if committed_global:
            global_state, _global_decisions = _restore_db_rows(
                global_conn, pre_global, global_written)
        space_kept_ids = [pid for pid, d in space_decisions.items()
                          if d == "kept"]
        space_restored_ids = [pid for pid, d in space_decisions.items()
                              if d == "restored"]

        # JSON 镜像与空间库共用同一份逐条 decisions：另一个窗口写过整份
        # 文件时按条合并，而不是整份换回本次开始前（那会让同一条规则在
        # 库和 JSON 里一个旧、一个新）。文件字节仍是本次写下的内容时，
        # 才整份恢复。
        json_state = "restored"
        if json_written:
            if _json_state(root) == written_json:
                json_state = ("restored" if _restore_config_state(
                    root, old_cfg, old_raw, old_existed) else "failed")
            else:
                final_rows = (_rows_from_conn(space_conn)
                              if space_conn is not None else {})
                json_state, _json_kept = _merge_json_after_failure(
                    root, space_decisions, final_rows,
                    snapshot_prefs, old_cfg)

        failed_parts: List[str] = []
        if json_written and json_state == "failed":
            failed_parts.append("JSON 镜像")
        if space_conn is not None and space_state == "failed":
            failed_parts.append("空间偏好库")
        if global_conn is not None and global_state == "failed":
            failed_parts.append("全局偏好库")
        if failed_parts:
            raise PreferenceError(
                "偏好库写入失败，以下位置未能确认回到保存前："
                + "、".join(failed_parts)
                + f"。请检查后再操作：{e}")
        if space_kept_ids:
            message = (
                "偏好库写入失败；空间偏好库与 JSON 镜像保留了其他窗口的"
                f"较新内容（{'、'.join(space_kept_ids)}）")
            if space_restored_ids:
                message += (
                    "，其余本次改动的规则已恢复为保存前"
                    f"（{'、'.join(space_restored_ids)}）")
            if global_conn is not None:
                message += "；全局偏好库已恢复"
            raise PreferenceError(message + f"：{e}")
        if json_written and json_state == "kept":
            raise PreferenceError(
                "偏好库写入失败；JSON 镜像保留了其他窗口写入的较新内容"
                + ("；空间偏好库已恢复" if space_conn is not None else "")
                + f"：{e}")
        if global_state == "kept":
            raise PreferenceError(
                "偏好库写入失败；全局偏好库保留了其他窗口的较新内容"
                + f"：{e}")
        if json_written:
            raise PreferenceError(f"偏好库写入失败，已整体回滚：{e}")
        raise PreferenceError(f"偏好库写入失败，未写入任何一边：{e}")
    finally:
        for conn in (space_conn, global_conn):
            if conn is not None:
                conn.close()


def sync_space_preferences_from_config(root: str,
                                       config: ClassificationConfig) -> int:
    """把方案 / 备份恢复后的 JSON 长期规则同步为本空间偏好。

    只替换空间偏好，全局偏好完全不动。rule_id / enabled 原样保留；
    已有同 ID 行保留原确认信息，新行记为 JSON 迁入。
    """
    current = list_space_preferences(root)
    by_id = {p.preference_id: p for p in current}
    now = _now()
    new_prefs: List[Preference] = []
    for index, rule in enumerate(config.long_term_rules or []):
        old = by_id.get(rule.rule_id)
        new_prefs.append(Preference.from_rule(
            rule,
            scope=SCOPE_SPACE,
            space_root=os.path.abspath(root),
            confirmed_at=(old.confirmed_at if old and old.confirmed_at else now),
            confirmed_source_rel=(old.confirmed_source_rel if old else ""),
            source_kind=(old.source_kind if old else SOURCE_MIGRATED),
            sort_order=index,
        ))
    persist_preferences(
        root, new_prefs, sync_json=False,
        original_ids=[p.preference_id for p in current],
        write_space=True, write_global=False)
    return len(new_prefs)


# ======================== 匹配与应用 ========================

def _rule_matches(rel_path: str, pref: Preference,
                  extra_text: str = "", extra_source: str = "body"):
    from classification_config import (
        ClassificationConfig as _CC, compile_config, match_long_term_rule,
    )
    single = _CC(long_term_rules=[pref.to_rule()])
    compiled = compile_config(single)
    return match_long_term_rule(rel_path, compiled, extra_text,
                                extra_source=extra_source)


def match_preferences(rel_path: str, preferences: Sequence[Preference],
                      extra_text: str = "", extra_source: str = "body"
                      ) -> List[Tuple[Preference, str]]:
    """返回该文件命中的全部偏好（未启用/不匹配的不返回）。"""
    out: List[Tuple[Preference, str]] = []
    for pref in preferences:
        try:
            match = _rule_matches(rel_path, pref, extra_text, extra_source)
        except Exception:
            continue
        if match is not None:
            out.append((pref, match.evidence))
    return out


def _effective_inner_path(item: PlanItem, pref: Preference) -> str:
    """偏好应用后的内部路径（保留 / 清空 / 替换）。"""
    if pref.inner_mode == INNER_CLEAR:
        return ""
    if pref.inner_mode == INNER_REPLACE:
        return (pref.inner_path or "").strip()
    return (item.inner_path or "").strip()


def _target_dir_for(item: PlanItem, pref: Preference) -> str:
    base = _build_target_dir_v2(
        pref.direction or "", pref.semester or "",
        pref.category or "", item.file_type)
    inner = _effective_inner_path(item, pref)
    if not inner:
        return base
    segs = [_sanitize_component(p)
            for p in inner.replace("\\", "/").split("/") if p]
    return os.path.join(base, *segs) if segs else base


def _within_root(root: str, rel_path: str) -> bool:
    root_real = os.path.realpath(os.path.abspath(root))
    target_real = os.path.realpath(os.path.join(root_real, rel_path))
    try:
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:
        return False


def _apply_target(item: PlanItem, pref: Preference, target_dir: str) -> None:
    item.direction = pref.direction or None
    item.semester = pref.semester or None
    item.category = pref.category or None
    item.inner_path = _effective_inner_path(item, pref) or None
    item.course = pref.category or pref.direction or ""
    item.target_dir_rel = target_dir
    item.target_filename = item.file.filename
    item.target_rel_path = os.path.join(target_dir, item.file.filename)


def _pref_evidence(pref: Preference, evidence: str) -> str:
    return (f"长期偏好「{pref.name}」({pref.preference_id})：{evidence}")


def _candidate(pref: Preference, evidence: str, target_dir: str) -> dict:
    return {
        "direction": pref.direction or "",
        "semester": pref.semester or "",
        "category": pref.category or "",
        "evidence": _pref_evidence(pref, evidence),
        "target_dir_rel": target_dir,
        "label": (f"{pref.direction}/{pref.category}"
                  if pref.category else (pref.direction or "待定")),
        "preference_id": pref.preference_id,
        "preference_name": pref.name,
    }


@dataclass
class PreferenceApplyResult:
    adopted: int = 0
    conflicts: int = 0
    rejected: List[str] = field(default_factory=list)


def apply_preferences_to_plan(plan: OrganizePlan,
                              preferences: Optional[Sequence[Preference]] = None,
                              root: Optional[str] = None) -> PreferenceApplyResult:
    """把启用偏好应用到已生成计划。

    优先级：精确路径 > 文件名关键词 > 目录范围。只看最高等级；同等级
    目标不同生成歧义（不静默取第一条），目标相同采用一条。目标越界或
    校验失败时不采用该偏好，文件保持原分类并在依据里说明原因。
    """
    result = PreferenceApplyResult()
    prefs = list(preferences or [])
    if not prefs:
        return result
    # 长期偏好只属于推荐模式；「按类型」「按课程再按类型」保持原有落点。
    if plan.mode != MODE_BY_DIRECTION_SEMESTER:
        return result
    # 同一 preference_id 只匹配一次（空间偏好优先）
    seen_ids = set()
    unique: List[Preference] = []
    for pref in prefs:
        if pref.preference_id in seen_ids:
            continue
        seen_ids.add(pref.preference_id)
        unique.append(pref)
    plan_root = root or plan.root
    # 每条偏好只编译一次，避免按文件重复构造配置。
    compiled: List[Tuple[Preference, object]] = []
    for pref in unique:
        try:
            compiled.append((pref, compile_config(
                ClassificationConfig(long_term_rules=[pref.to_rule()]))))
        except Exception:
            continue
    for item in plan.items:
        if getattr(item, "skipped", False):
            continue
        matches: List[Tuple[Preference, str]] = []
        for pref, compiled_rule in compiled:
            try:
                # 偏好匹配等级里的「文件名关键词」只匹配文件名；
                # 正文/图片文字仍由阶段 1 的正文歧义结构处理。
                match = match_long_term_rule(
                    item.file.rel_path, compiled_rule, "", extra_source="body")
            except Exception:
                continue
            if match is not None:
                matches.append((pref, match.evidence))
        if not matches:
            continue
        levels = [MATCH_LEVEL.get(p.match_type, 9) for p, _ in matches]
        best = min(levels)
        best_matches = [m for m, level in zip(matches, levels) if level == best]
        valid: List[Tuple[Preference, str, str]] = []
        rejected_labels: List[str] = []
        for pref, evidence in best_matches:
            target_dir = _target_dir_for(item, pref)
            if not _within_root(plan_root, target_dir):
                rejected_labels.append(
                    f"「{pref.name}」({pref.preference_id})")
                result.rejected.append(
                    f"{item.file.rel_path}：偏好「{pref.name}」"
                    f"({pref.preference_id}) 目标越界，未采用")
                continue
            valid.append((pref, evidence, target_dir))
        if not valid:
            item.rule_applied = (
                (item.rule_applied or "")
                + f"；长期偏好 {'、'.join(rejected_labels)} 目标越界，"
                  "未采用，保持原分类")
            continue

        groups: Dict[str, List[Tuple[Preference, str, str]]] = {}
        order: List[str] = []
        for pref, evidence, target_dir in valid:
            sig = os.path.normcase(target_dir)
            if sig not in groups:
                groups[sig] = []
                order.append(sig)
            groups[sig].append((pref, evidence, target_dir))

        if len(order) > 1:
            current = {
                "direction": item.direction or "",
                "semester": item.semester or "",
                "category": item.category or "",
                "evidence": item.decision_evidence or "目录上下文",
                "target_dir_rel": item.target_dir_rel,
                "label": (f"{item.direction}/{item.category}"
                          if item.category else (item.direction or "待定")),
            }
            # 保留 generate_plan 已经识别出的正文/AIGC 候选，不能因为
            # 同级偏好冲突把正文去向从歧义面板里挤掉。
            existing = getattr(item, "ambiguity", None) or {}
            existing_candidates = existing.get("candidates") or []
            candidates = [dict(c) for c in existing_candidates]
            if not candidates:
                candidates = [current]
            for sig in order:
                pref, evidence, target_dir = groups[sig][0]
                candidate = _candidate(pref, evidence, target_dir)
                if not any(
                        os.path.normcase(str(c.get("target_dir_rel") or ""))
                        == os.path.normcase(target_dir)
                        for c in candidates):
                    candidates.append(candidate)
            item.ambiguity = {
                "reason": ("同一优先级的长期偏好指向不同去向"
                           + ("；保留正文候选" if existing_candidates else "")),
                "candidates": candidates,
            }
            item.selected = False
            names = "、".join(
                f"「{groups[sig][0][0].name}」({groups[sig][0][0].preference_id})"
                for sig in order)
            extra = f"；存在长期偏好歧义（{names}），默认不勾选，请在预览里改选或跳过"
            if extra not in (item.rule_applied or ""):
                item.rule_applied = (item.rule_applied or "") + extra
            item.decision_evidence = "；".join(dict.fromkeys(
                str(c.get("evidence") or "") for c in candidates
                if c.get("evidence")))
            result.conflicts += 1
            continue

        pref, evidence, target_dir = valid[0]
        _apply_target(item, pref, target_dir)
        item.rule_key = f"long_term:{pref.preference_id}"
        item.rule_applied = (
            f"{_pref_evidence(pref, evidence)} → "
            f"{pref.direction}/{pref.category or '（无类别）'}")
        item.decision_evidence = _pref_evidence(pref, evidence)
        # 正文歧义仍然保留：偏好作为首选候选加入，原有正文候选一个不删
        # （高等数学、AIGC 等仍可在预览里改选），仍默认不勾选。
        ambiguity = getattr(item, "ambiguity", None)
        if ambiguity:
            candidates = [dict(c) for c in (ambiguity.get("candidates") or [])]
            preferred = _candidate(pref, evidence, target_dir)
            kept = [
                c for c in candidates
                if os.path.normcase(str(c.get("target_dir_rel") or ""))
                != os.path.normcase(target_dir)]
            ambiguity["candidates"] = [preferred] + kept
            ambiguity["reason"] = (
                (ambiguity.get("reason") or "存在歧义")
                + "；已按最高等级长期偏好确定首选候选")
            item.selected = False
        else:
            # 没有剩余歧义、也没有被跳过：用户已确认的偏好直接参与本次批次。
            item.selected = True
            # 「恢复自动分类」应回到这条偏好，而不是套用偏好之前的待定分类。
            # 仅当没有歧义时才更新快照；有歧义时不单方面选定候选。
            item.auto_direction = item.direction
            item.auto_semester = item.semester
            item.auto_category = item.category
            item.auto_inner_path = item.inner_path
            item.auto_rule_key = item.rule_key
            item.auto_rule_applied = item.rule_applied
        result.adopted += 1

    assign_unique_targets(plan.items, plan.root)
    return result
