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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from classification_config import (
    ClassificationConfig, ClassificationConfigError,
    INNER_CLEAR, INNER_KEEP, INNER_REPLACE, INNER_MODE_LABELS,
    LONG_TERM_DIRECTIONS, LongTermRule,
    MATCH_DIR_SCOPE, MATCH_EXACT_PATH, MATCH_FILENAME_KEYWORD,
    MATCH_TYPE_LABELS,
    clone_config, compile_config, ensure_valid, load_config,
    match_long_term_rule, save_config, validate_config,
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
    - `original_ids` 仅在未提供 baseline 时用于删除：只删打开时已存在、
      本次又移除的行；编辑期间其他窗口新增的行保留。
    - `write_space=False` / `write_global=False` 时不触碰对应库。
    - 先打开所有需要的库，再写数据库语句；JSON 镜像在提交前写入。
      任一步失败都会回滚 SQLite，并恢复 JSON，不会出现“报错但新目标已生效”。
    """
    prefs = list(preferences)
    space_prefs = [p for p in prefs if p.scope == SCOPE_SPACE]
    global_prefs = [p for p in prefs if p.scope == SCOPE_GLOBAL]
    for pref in prefs:
        _raise_if_invalid(pref)
    originals = set(original_ids or [])
    snapshot = ({p.preference_id: preference_fingerprint(p) for p in baseline}
                if baseline is not None else None)

    space_conn = None
    global_conn = None
    old_cfg = None
    new_cfg = None
    json_written = False
    try:
        # 1) 先打开所有需要的库；数据库损坏会在任何写入之前失败。
        if write_space:
            space_conn = _open(space_db_path(root))
        if write_global:
            global_conn = _open(global_db_path())

        current_space = _rows_from_conn(space_conn) if space_conn else {}
        current_global = _rows_from_conn(global_conn) if global_conn else {}

        space_write = list(space_prefs)
        global_write = list(global_prefs)
        if snapshot is not None:
            submitted_ids = {p.preference_id for p in prefs}
            # 本窗口改过/删掉的行，如果库里已不是打开时的内容 -> 整次取消。
            for pref in prefs:
                if pref.scope == SCOPE_SPACE and not write_space:
                    continue
                if pref.scope == SCOPE_GLOBAL and not write_global:
                    continue
                old = snapshot.get(pref.preference_id)
                if old is None or preference_fingerprint(pref) == old:
                    continue
                current = (current_space.get(pref.preference_id)
                           if pref.scope == SCOPE_SPACE
                           else current_global.get(pref.preference_id))
                if current is None or preference_fingerprint(current) != old:
                    raise _conflict_error(pref)
            for stale_id in set(snapshot) - submitted_ids:
                current = current_space.get(stale_id) or \
                    current_global.get(stale_id)
                if current is None:
                    continue
                if current.scope == SCOPE_SPACE and not write_space:
                    continue
                if current.scope == SCOPE_GLOBAL and not write_global:
                    continue
                if preference_fingerprint(current) != snapshot[stale_id]:
                    raise _conflict_error(current)
            # 没改过的行不写回，避免覆盖其他窗口的更新。
            space_write = [
                p for p in space_prefs
                if snapshot.get(p.preference_id) is None
                or preference_fingerprint(p) != snapshot[p.preference_id]]
            global_write = [
                p for p in global_prefs
                if snapshot.get(p.preference_id) is None
                or preference_fingerprint(p) != snapshot[p.preference_id]]
            space_delete = {i for i in set(snapshot) - submitted_ids
                            if i in current_space}
            global_delete = {i for i in set(snapshot) - submitted_ids
                             if i in current_global and i not in current_space}
        else:
            space_delete = originals - {p.preference_id for p in space_prefs}
            global_delete = originals - {p.preference_id for p in global_prefs}

        # 2) 准备 JSON 镜像（长期规则稍后从写后的库读取）
        if sync_json:
            old_cfg = _read_json_config(root)
            new_cfg = clone_config(config if config is not None else old_cfg)

        # 3) 两个库各自开启事务并写入，但先不提交
        if space_conn is not None:
            space_conn.execute("BEGIN")
            _apply_rows(space_conn, space_write, space_delete)
        if global_conn is not None:
            global_conn.execute("BEGIN")
            _apply_rows(global_conn, global_write, global_delete)

        # 4) 用写后的最终行生成 JSON 镜像，写成功后才提交 SQLite
        if sync_json:
            if write_space and space_conn is not None:
                final_rows = _rows_from_conn(space_conn).values()
                new_cfg.long_term_rules = [p.to_rule() for p in final_rows]
            ensure_valid(new_cfg)
            save_config(root, new_cfg)
            json_written = True

        if space_conn is not None:
            space_conn.commit()
        if global_conn is not None:
            global_conn.commit()
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
        restored = True
        if json_written and old_cfg is not None:
            restored = _restore_json(root, old_cfg)
        if not json_written:
            raise PreferenceError(f"偏好库写入失败，未写入任何一边：{e}")
        if restored:
            raise PreferenceError(f"偏好库写入失败，已整体回滚：{e}")
        raise PreferenceError(
            f"偏好库写入失败，且 JSON 镜像未能确认恢复，请检查配置文件：{e}")
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
        # 正文歧义仍然保留：把首选候选换成这条偏好，仍默认不勾选
        ambiguity = getattr(item, "ambiguity", None)
        if ambiguity:
            candidates = ambiguity.get("candidates") or []
            if candidates:
                candidates[0] = _candidate(pref, evidence, target_dir)
            else:
                ambiguity["candidates"] = [_candidate(pref, evidence, target_dir)]
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
