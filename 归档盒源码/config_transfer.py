"""
归档盒 V1.6 — 整理方案的导入导出、合并、差异与配置备份（纯逻辑）

边界：
- 本模块不依赖 Qt，可在无界面环境与测试中直接使用；
- 方案文件只携带推荐模式配置与长期规则，不含根目录绑定、历史数据库、
  文件清单/指纹、手动保护配置、临时手动修正或本机环境路径；
- 导出路径一律规范化为 `/` 分隔，保证跨平台可迁移；
- 所有合并策略都是显式、可解释的，不做静默重命名或猜测。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from classification_config import (
    CONFIG_VERSION, SUPPORTED_READ_VERSIONS, ClassificationConfig,
    ClassificationConfigError, LongTermRule, clone_config, config_path,
    config_to_dict, config_from_dict, default_config, ensure_valid,
    validate_config_payload, validate_config, config_summary,
)

SCHEME_FORMAT = "archive-box-scheme"
SCHEME_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = (1,)

BACKUP_DIRNAME = "config_backups"
BACKUP_PREFIX = "config-backup-"
MAX_BACKUPS = 5

# 五类推荐模式规则 + 长期规则
RULE_GROUPS = (
    "semester_map", "top_direction", "semester_dirs",
    "generic_dirs", "keyword_rules", "long_term_rules",
)

GROUP_LABELS = {
    "semester_map": "学期映射",
    "top_direction": "顶层方向",
    "semester_dirs": "学期内目录",
    "generic_dirs": "通用目录前缀",
    "keyword_rules": "关键词规则",
    "long_term_rules": "长期修正规则",
}


# ======================== 通用 ========================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_rel(path: str) -> str:
    """导出用相对路径：统一 `/` 分隔，去掉空段（不传绝对路径）。"""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return "/".join(parts)


def _atomic_write_text(path: str, text: str) -> None:
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".archive-box.", suffix=".tmp",
                               dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise ClassificationConfigError(f"写入文件失败：{path}\n原因：{e}")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def rule_counts(cfg: ClassificationConfig) -> Dict[str, int]:
    return {
        "semester_map": len(cfg.semester_rules),
        "top_direction": len(cfg.top_direction_rules),
        "semester_dirs": len(cfg.semester_dir_rules),
        "generic_dirs": len(cfg.generic_dir_rules),
        "keyword_rules": len(cfg.keyword_rules),
        "long_term_rules": len(cfg.long_term_rules),
        "long_term_enabled": sum(1 for r in cfg.long_term_rules if r.enabled),
    }


# ======================== 方案导出 / 解析 ========================

def export_scheme_payload(cfg: ClassificationConfig, name: str,
                          description: str = "", scheme_id: str = "",
                          app_version: str = "1.6") -> dict:
    """构造可迁移的方案文件内容（不含任何本机绑定信息）。"""
    name = (name or "").strip()
    if not name:
        raise ClassificationConfigError("方案名称不能为空。")
    config = config_to_dict(cfg)
    # 长期规则的相对路径统一为 `/`，避免跨平台分隔符差异
    for row in config.get("long_term_rules", []):
        row["source_path"] = _safe_rel(row.get("source_path", ""))
        if row.get("inner_path"):
            row["inner_path"] = _safe_rel(row["inner_path"])
    return {
        "format": SCHEME_FORMAT,
        "format_version": SCHEME_FORMAT_VERSION,
        "scheme_id": scheme_id or "",
        "name": name,
        "description": (description or "").strip(),
        "exported_at": _now(),
        "app": {"name": "归档盒", "version": app_version},
        "config_version": CONFIG_VERSION,
        "counts": rule_counts(cfg),
        "config": config,
    }


def save_scheme_file(path: str, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_scheme_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ClassificationConfigError(
            f"方案文件无法读取或不是有效 JSON：{path}\n原因：{e}")


@dataclass
class ParsedScheme:
    config: ClassificationConfig
    name: str
    description: str = ""
    scheme_id: str = ""
    format_version: int = SCHEME_FORMAT_VERSION
    config_version_in_file: int = CONFIG_VERSION
    upgraded: bool = False
    warnings: List[str] = field(default_factory=list)


def parse_scheme_payload(data) -> ParsedScheme:
    """解析并严格校验方案内容；任何问题都抛出 ClassificationConfigError。"""
    if not isinstance(data, dict):
        raise ClassificationConfigError("方案文件顶层必须是对象（JSON object）。")
    fmt = data.get("format")
    if fmt != SCHEME_FORMAT:
        raise ClassificationConfigError(
            f"未知方案格式：format={fmt!r}，期望 {SCHEME_FORMAT!r}。\n"
            "请确认选择的是归档盒导出的方案文件。")
    fv = data.get("format_version")
    if isinstance(fv, bool) or not isinstance(fv, int):
        raise ClassificationConfigError(
            f"方案格式版本必须是整数，当前：{fv!r}。")
    if fv not in SUPPORTED_FORMAT_VERSIONS:
        raise ClassificationConfigError(
            f"不支持的方案格式版本：{fv}；当前程序支持 "
            f"{SUPPORTED_FORMAT_VERSIONS}。请升级程序后再导入。")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ClassificationConfigError("方案缺少有效名称（name）。")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ClassificationConfigError("方案说明（description）必须是文本。")
    scheme_id = data.get("scheme_id", "")
    if scheme_id is not None and not isinstance(scheme_id, str):
        raise ClassificationConfigError("方案标识（scheme_id）必须是文本。")

    raw_cfg = data.get("config")
    if not isinstance(raw_cfg, dict):
        raise ClassificationConfigError("方案缺少 config 对象，无法读取规则。")
    cfg_ver = raw_cfg.get("version")
    if isinstance(cfg_ver, bool) or not isinstance(cfg_ver, int):
        raise ClassificationConfigError(
            f"配置版本无效：{cfg_ver!r}（必须是整数）。")
    if cfg_ver not in SUPPORTED_READ_VERSIONS:
        raise ClassificationConfigError(
            f"配置版本不兼容：方案为 version={cfg_ver}，"
            f"当前程序支持 {SUPPORTED_READ_VERSIONS}。\n"
            "请升级程序，或联系方案提供者导出兼容版本。")

    raw_errors = validate_config_payload(raw_cfg)
    if raw_errors:
        raise ClassificationConfigError(
            "方案字段类型不正确：\n" + "\n".join(f"· {e}" for e in raw_errors))
    try:
        cfg = config_from_dict(raw_cfg)
    except (TypeError, ValueError) as e:
        raise ClassificationConfigError(f"方案规则无法解析：{e}")
    errors = validate_config(cfg)
    if errors:
        raise ClassificationConfigError(
            "方案规则未通过校验：\n" + "\n".join(f"· {e}" for e in errors))

    warnings: List[str] = []
    upgraded = cfg_ver < CONFIG_VERSION
    if upgraded:
        warnings.append(
            f"方案为旧配置版本 v{cfg_ver}，导入时已升级为 v{CONFIG_VERSION}；"
            "旧方案不含长期修正规则。")
    declared = data.get("config_version")
    if isinstance(declared, int) and not isinstance(declared, bool) \
            and declared != cfg_ver:
        warnings.append(
            f"文件声明 config_version={declared} 与内嵌配置 version={cfg_ver} 不一致，"
            f"以内嵌配置为准。")
    return ParsedScheme(
        config=cfg, name=name.strip(), description=description.strip(),
        scheme_id=(scheme_id or "").strip(),
        format_version=fv, config_version_in_file=cfg_ver,
        upgraded=upgraded, warnings=warnings)


# ======================== 差异 ========================

@dataclass
class DiffEntry:
    group: str
    key: str
    label: str


@dataclass
class ConfigDiff:
    added: int = 0
    modified: int = 0
    removed: int = 0
    unchanged: int = 0
    added_items: List[DiffEntry] = field(default_factory=list)
    modified_items: List[DiffEntry] = field(default_factory=list)
    removed_items: List[DiffEntry] = field(default_factory=list)
    long_term_before: List[str] = field(default_factory=list)
    long_term_after: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        return [
            f"新增 {self.added} 条，修改 {self.modified} 条，"
            f"删除 {self.removed} 条，未变化 {self.unchanged} 条",
            "长期规则顺序（前 → 后）："
            + (" → ".join(self.long_term_after) if self.long_term_after
               else "（无长期规则）"),
        ]


def _group_rows(cfg: ClassificationConfig, group: str) -> List[dict]:
    return config_to_dict(cfg).get(group, [])


def _row_identity(group: str, row: dict) -> str:
    if group == "semester_map":
        return f"source:{row.get('source','')}"
    if group == "top_direction":
        return f"source:{row.get('source','')}"
    if group == "semester_dirs":
        return f"{row.get('semester','')}/{row.get('source','')}"
    if group == "generic_dirs":
        return f"prefix:{row.get('prefix','')}"
    if group == "keyword_rules":
        kws = tuple(sorted(str(k).strip().lower() for k in row.get("keywords", [])))
        return "kw:" + "/".join(kws)
    if group == "long_term_rules":
        return f"id:{row.get('rule_id','')}"
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _row_signature(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _row_label(group: str, row: dict) -> str:
    if group == "semester_map":
        return f"{row.get('source','')} → {row.get('semester','')}"
    if group == "top_direction":
        return f"{row.get('source','')} → {row.get('direction','')}"
    if group == "semester_dirs":
        return (f"{row.get('semester','')}/{row.get('source','')} → "
                f"{row.get('direction','')}/{row.get('category','') or '关键词细分'}")
    if group == "generic_dirs":
        return f"{row.get('prefix','')} → {row.get('direction','')}/{row.get('category','')}"
    if group == "keyword_rules":
        return ("/".join(row.get("keywords", []))
                + f" → {row.get('target','')}")
    if group == "long_term_rules":
        return f"{row.get('name','')}（{row.get('rule_id','')}）"
    return str(row)


def _rule_label(group: str, rule) -> str:
    """数据类版本的行标签（用于合并冲突展示）。"""
    if group == "semester_map":
        return f"{rule.source} → {rule.semester}"
    if group == "top_direction":
        return f"{rule.source} → {rule.direction}"
    if group == "semester_dirs":
        return (f"{rule.semester}/{rule.source} → {rule.direction}/"
                f"{rule.category or '关键词细分'}")
    if group == "generic_dirs":
        return f"{rule.prefix} → {rule.direction}/{rule.category}"
    if group == "keyword_rules":
        return "/".join(rule.keywords) + f" → {rule.target}"
    if group == "long_term_rules":
        return f"{rule.name}（{rule.rule_id}）"
    return str(rule)


def config_diff(base: ClassificationConfig,
                incoming: ClassificationConfig) -> ConfigDiff:
    diff = ConfigDiff()
    for group in RULE_GROUPS:
        base_rows = {_row_identity(group, r): r for r in _group_rows(base, group)}
        new_rows = {_row_identity(group, r): r for r in _group_rows(incoming, group)}
        for key, row in new_rows.items():
            if key not in base_rows:
                diff.added += 1
                diff.added_items.append(DiffEntry(group, key, _row_label(group, row)))
            elif _row_signature(row) != _row_signature(base_rows[key]):
                diff.modified += 1
                diff.modified_items.append(
                    DiffEntry(group, key, _row_label(group, row)))
            else:
                diff.unchanged += 1
        for key, row in base_rows.items():
            if key not in new_rows:
                diff.removed += 1
                diff.removed_items.append(DiffEntry(group, key, _row_label(group, row)))
    diff.long_term_before = [r.name for r in base.long_term_rules]
    diff.long_term_after = [r.name for r in incoming.long_term_rules]
    return diff


# ======================== 合并 ========================

@dataclass
class MergeConflict:
    kind: str          # same_id_changed / name_duplicate / mapping_changed
    group: str
    key: str
    label: str
    detail: str
    decision: str = "keep"   # keep / take（仅长期规则冲突可切换）

    @property
    def selectable(self) -> bool:
        return self.kind in ("same_id_changed", "name_duplicate")


@dataclass
class MergeResult:
    config: ClassificationConfig
    added: int = 0
    updated: int = 0
    skipped: int = 0
    invalid: int = 0
    conflicts: List[MergeConflict] = field(default_factory=list)
    long_term_before: List[str] = field(default_factory=list)
    long_term_after: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def selectable_conflicts(self) -> List[MergeConflict]:
        return [c for c in self.conflicts if c.selectable]


def _lt_signature(rule: LongTermRule) -> str:
    row = config_to_dict(ClassificationConfig(
        long_term_rules=[rule])).get("long_term_rules", [{}])[0]
    row.pop("reason", None)
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def merge_configs(base: ClassificationConfig, incoming: ClassificationConfig,
                  decisions: Optional[Dict[str, str]] = None) -> MergeResult:
    """把 incoming 合并到 base（现有规则优先）。

    decisions: {长期规则 key: "keep"|"take"}，key 为冲突的 rule_id
    （同 id 冲突用现有 id；名称冲突用导入规则的 id）。
    """
    decisions = decisions or {}
    result_cfg = clone_config(base)
    res = MergeResult(config=result_cfg,
                      long_term_before=[r.name for r in base.long_term_rules])

    # ---- 1) 五类推荐模式规则：按身份键合并，现有优先 ----
    groups = {
        "semester_map": (result_cfg.semester_rules, incoming.semester_rules,
                         lambda r: f"source:{r.source}"),
        "top_direction": (result_cfg.top_direction_rules,
                          incoming.top_direction_rules,
                          lambda r: f"source:{r.source}"),
        "semester_dirs": (result_cfg.semester_dir_rules,
                          incoming.semester_dir_rules,
                          lambda r: f"{r.semester}/{r.source}"),
        "generic_dirs": (result_cfg.generic_dir_rules,
                         incoming.generic_dir_rules,
                         lambda r: f"prefix:{r.prefix}"),
    }
    for group, (target, source, key_fn) in groups.items():
        existing = {key_fn(r): r for r in target}
        for rule in source:
            key = key_fn(rule)
            if key in existing:
                if dataclasses.asdict(existing[key]) == dataclasses.asdict(rule):
                    res.skipped += 1
                else:
                    res.skipped += 1
                    res.conflicts.append(MergeConflict(
                        kind="mapping_changed", group=group, key=key,
                        label=_rule_label(group, rule),
                        detail="现有映射优先；导入的同名映射未覆盖",
                        decision="keep"))
                continue
            target.append(rule)
            existing[key] = rule
            res.added += 1

    # 关键词规则：身份是关键词组合
    existing_kw = {tuple(sorted(k.strip().lower() for k in r.keywords)): r
                   for r in result_cfg.keyword_rules}
    for rule in incoming.keyword_rules:
        key = tuple(sorted(k.strip().lower() for k in rule.keywords))
        if key in existing_kw:
            if dataclasses.asdict(existing_kw[key]) == dataclasses.asdict(rule):
                res.skipped += 1
            else:
                res.skipped += 1
                res.conflicts.append(MergeConflict(
                    kind="mapping_changed", group="keyword_rules",
                    key="/".join(key),
                    label="/".join(rule.keywords) + f" → {rule.target}",
                    detail="现有映射优先；导入的同关键词规则未覆盖",
                    decision="keep"))
            continue
        result_cfg.keyword_rules.append(rule)
        existing_kw[key] = rule
        res.added += 1

    # ---- 2) 学期内目录的学期标签必须存在（否则整份配置校验失败） ----
    labels = {r.semester for r in result_cfg.semester_rules}
    kept_dirs = []
    for rule in result_cfg.semester_dir_rules:
        if rule.semester not in labels:
            res.invalid += 1
            res.conflicts.append(MergeConflict(
                kind="mapping_changed", group="semester_dirs",
                key=f"{rule.semester}/{rule.source}",
                label=f"{rule.semester}/{rule.source}",
                detail=f"学期「{rule.semester}」未在合并后的学期映射中登记，"
                       "该条已跳过（未猜测学期关联）",
                decision="keep"))
            continue
        kept_dirs.append(rule)
    result_cfg.semester_dir_rules = kept_dirs

    # ---- 3) 长期规则：现有顺序在前，导入规则追加；同 id/同名走冲突 ----
    by_id = {r.rule_id: r for r in result_cfg.long_term_rules}
    by_name = {r.name.strip(): r for r in result_cfg.long_term_rules}
    for rule in incoming.long_term_rules:
        if rule.rule_id in by_id:
            current = by_id[rule.rule_id]
            if _lt_signature(current) == _lt_signature(rule):
                res.skipped += 1
                continue
            decision = decisions.get(rule.rule_id, "keep")
            conflict = MergeConflict(
                kind="same_id_changed", group="long_term_rules",
                key=rule.rule_id,
                label=f"{rule.name}（{rule.rule_id}）",
                detail="同一规则标识但内容不同；默认保留现有",
                decision="keep")
            if decision == "take":
                idx = result_cfg.long_term_rules.index(current)
                result_cfg.long_term_rules[idx] = rule
                by_id[rule.rule_id] = rule
                by_name[rule.name.strip()] = rule
                conflict.decision = "take"
                res.updated += 1
            else:
                res.skipped += 1
            res.conflicts.append(conflict)
            continue

        name = rule.name.strip()
        if name in by_name and by_name[name].rule_id != rule.rule_id:
            current = by_name[name]
            decision = decisions.get(rule.rule_id, "keep")
            conflict = MergeConflict(
                kind="name_duplicate", group="long_term_rules",
                key=rule.rule_id,
                label=f"{rule.name}（{rule.rule_id}）",
                detail="与现有规则同名但标识不同；默认保留现有",
                decision="keep")
            if decision == "take":
                idx = result_cfg.long_term_rules.index(current)
                result_cfg.long_term_rules[idx] = rule
                by_id.pop(current.rule_id, None)
                by_id[rule.rule_id] = rule
                by_name[name] = rule
                conflict.decision = "take"
                res.updated += 1
            else:
                res.skipped += 1
            res.conflicts.append(conflict)
            continue

        result_cfg.long_term_rules.append(rule)
        by_id[rule.rule_id] = rule
        by_name[name] = rule
        res.added += 1

    res.long_term_after = [r.name for r in result_cfg.long_term_rules]
    res.errors = validate_config(result_cfg)
    return res


# ======================== 配置备份 / 恢复 ========================

def backup_dir(root: str) -> str:
    return os.path.join(os.path.abspath(root), ".organizer", BACKUP_DIRNAME)


def _backup_files(root: str) -> List[str]:
    d = backup_dir(root)
    if not os.path.isdir(d):
        return []
    names = [n for n in os.listdir(d)
             if n.startswith(BACKUP_PREFIX) and n.endswith(".json")]
    names.sort(reverse=True)   # 文件名包含时间戳，倒序即最新在前
    return [os.path.join(d, n) for n in names]


def create_backup(root: str, keep: int = MAX_BACKUPS) -> str:
    """在应用新配置前，把当前配置（或默认/损坏状态）备份一份。

    备份失败会抛出 ClassificationConfigError：调用方必须停止应用，
    保留原配置与计划状态，避免出现“已覆盖但无法恢复”的情况。
    """
    path = config_path(root)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise ClassificationConfigError(
                f"无法读取当前配置以建立备份，已停止应用：\n{path}\n原因：{e}")
        try:
            data = json.loads(text)
        except ValueError:
            payload = {"state": "corrupt", "raw": text,
                       "error": "不是有效 JSON", "created_at": _now()}
        else:
            if isinstance(data, dict):
                payload = {"state": "custom", "config": data,
                           "created_at": _now()}
            else:
                payload = {"state": "corrupt", "raw": text,
                           "error": "顶层不是对象", "created_at": _now()}
    else:
        payload = {"state": "default", "created_at": _now()}

    d = backup_dir(root)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise ClassificationConfigError(
            f"无法创建配置备份目录，已停止应用：\n{d}\n原因：{e}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = os.path.join(d, f"{BACKUP_PREFIX}{stamp}.json")
    try:
        _atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2))
    except ClassificationConfigError as e:
        raise ClassificationConfigError(
            f"备份当前配置失败，已停止应用（原配置保持不变）：\n{e}")
    # 只保留最近 keep 份；清理失败不影响本次备份可用性
    files = _backup_files(root)
    for old in files[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return out


@dataclass
class BackupInfo:
    path: str
    state: str          # custom / default / corrupt
    created_at: str = ""
    valid: bool = True
    error: str = ""


def list_backups(root: str) -> List[BackupInfo]:
    out: List[BackupInfo] = []
    for path in _backup_files(root):
        info = BackupInfo(path=path, state="corrupt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            info.error = f"备份无法读取：{e}"
            out.append(info)
            continue
        if not isinstance(data, dict):
            info.error = "备份格式不正确"
            out.append(info)
            continue
        info.state = str(data.get("state", "corrupt"))
        info.created_at = str(data.get("created_at", ""))
        if info.state == "custom":
            cfg = data.get("config")
            try:
                if not isinstance(cfg, dict):
                    raise ValueError("缺少配置对象")
                errs = validate_config_payload(cfg)
                if errs:
                    raise ValueError("；".join(errs))
                cfg_obj = config_from_dict(cfg)
                errs = validate_config(cfg_obj)
                if errs:
                    raise ValueError("；".join(errs))
            except Exception as e:
                info.valid = False
                info.error = str(e)
        elif info.state == "default":
            info.valid = True
        else:
            info.valid = False
            info.error = str(data.get("error", "损坏的配置备份"))
        out.append(info)
    return out


def restore_latest_backup(root: str) -> Tuple[str, str]:
    """恢复最近一次备份（S1-09：不再一次性消费）。

    返回 (state, message)。state 为 default / custom。
    备份损坏时抛 ClassificationConfigError，且不消费备份文件。
    - 恢复前先把当前配置备份一份（备份失败则拒绝恢复，不做任何改动）；
    - 恢复后不删除所用备份。
    """
    infos = list_backups(root)
    if not infos:
        raise ClassificationConfigError("该目录没有可用的配置备份。")
    # 先取"恢复所用"的备份（再备份当前配置会生成更新的备份文件，
    # 必须在备份当前配置之前确定要恢复哪一份）
    info = infos[0]
    if not info.valid:
        raise ClassificationConfigError(
            "最近一次备份无法作为有效配置启用（原始文件仍保留）：\n"
            f"{info.path}\n{info.error}")
    # S1-09：恢复前先备份当前配置；备份失败则中止，不做任何改动
    new_backup_path = create_backup(root)
    path = config_path(root)
    if info.state == "default":
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                raise ClassificationConfigError(
                    f"恢复为默认配置失败：无法删除现有配置文件。\n{path}\n原因：{e}")
        message = "已恢复为内置默认配置（原配置文件已移除）。"
    else:
        with open(info.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = config_from_dict(data["config"])
        ensure_valid(cfg)
        # 重新绑定当前根目录，写回统一版本
        payload = {
            "version": CONFIG_VERSION,
            "root": os.path.abspath(root),
            "updated_at": _now(),
        }
        payload.update(config_to_dict(cfg))
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        message = "已恢复上一次应用的配置。"
    # S1-09：不再删除所用备份；告知用户当前配置已另存为新备份
    message += (
        f"\n恢复前的当前配置已备份为 {os.path.basename(new_backup_path)}；"
        f"本次使用的备份 {os.path.basename(info.path)} 已保留。")
    return info.state, message


def backup_summary(root: str) -> str:
    infos = list_backups(root)
    if not infos:
        return "暂无配置备份"
    states = {"custom": "自定义配置", "default": "内置默认", "corrupt": "损坏（不可启用）"}
    return f"可用备份 {sum(1 for i in infos if i.valid)} 份（共 {len(infos)}）：" \
           + "，".join(states.get(i.state, i.state) for i in infos[:3])
