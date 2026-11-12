"""
归档盒 V1.6 — 本地整理方案库（纯逻辑，无 Qt 依赖）

存放位置：系统标准用户配置目录下的稳定应用标识目录，绝不放在源码目录、
扫描根目录或临时目录。测试通过环境变量覆盖为独立临时目录。

- macOS:  ~/Library/Application Support/归档盒/schemes
- Windows: %APPDATA%/归档盒/schemes
- Linux:   $XDG_CONFIG_HOME/归档盒/schemes 或 ~/.config/归档盒/schemes

每个方案是一个独立 JSON 文件（与方案导出格式一致）。
应用方案时总是深拷贝其配置再写入目标根目录，目录之间不共享可变对象。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from classification_config import (
    CONFIG_VERSION, ClassificationConfig, ClassificationConfigError,
    clone_config, default_config, validate_config,
)
from config_transfer import (
    ParsedScheme, export_scheme_payload, load_scheme_file,
    parse_scheme_payload, rule_counts, save_scheme_file,
)

APP_DIR_NAME = "归档盒"
SCHEMES_SUBDIR = "schemes"
ENV_OVERRIDE = "ARCHIVEBOX_SCHEME_DIR"

BUILTIN_DEFAULT_ID = "builtin-default"
BUILTIN_BLANK_ID = "builtin-blank"
BUILTIN_IDS = (BUILTIN_DEFAULT_ID, BUILTIN_BLANK_ID)


def library_dir() -> str:
    """方案库目录（可用环境变量覆盖，便于测试隔离）。"""
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR_NAME, SCHEMES_SUBDIR)


def _new_scheme_id() -> str:
    return "sc-" + uuid.uuid4().hex[:12]


def blank_config() -> ClassificationConfig:
    """空白自定义方案：不含任何映射或规则。"""
    return ClassificationConfig(version=CONFIG_VERSION)


@dataclass
class SchemeRecord:
    scheme_id: str
    name: str
    description: str = ""
    updated_at: str = ""
    builtin: bool = False
    read_only: bool = False
    config: Optional[ClassificationConfig] = None
    error: str = ""
    path: str = ""

    @property
    def usable(self) -> bool:
        return self.config is not None and not self.error

    @property
    def counts_text(self) -> str:
        if self.config is None:
            return "不可用"
        c = rule_counts(self.config)
        return (f"学期 {c['semester_map']} / 方向 {c['top_direction']} / "
                f"学期内 {c['semester_dirs']} / 前缀 {c['generic_dirs']} / "
                f"关键词 {c['keyword_rules']} / 长期规则 {c['long_term_rules']}")


def _builtin_records() -> List[SchemeRecord]:
    blank = blank_config()
    blank_ok = not validate_config(blank)
    return [
        SchemeRecord(
            scheme_id=BUILTIN_DEFAULT_ID, name="内置默认方案",
            description="归档盒内置的推荐模式默认映射；只读，可复制后修改。",
            updated_at="内置", builtin=True, read_only=True,
            config=default_config()),
        SchemeRecord(
            scheme_id=BUILTIN_BLANK_ID, name="空白方案",
            description="不含任何映射与规则的起点；只读，可复制后编辑。",
            updated_at="内置", builtin=True, read_only=True,
            config=blank if blank_ok else None,
            error="" if blank_ok else "空白方案未通过校验：" + "；".join(validate_config(blank)),
        ),
    ]


def _load_file_record(path: str) -> SchemeRecord:
    name = os.path.splitext(os.path.basename(path))[0]
    rec = SchemeRecord(scheme_id=name, name=name, path=path,
                       read_only=True)
    try:
        data = load_scheme_file(path)
        parsed = parse_scheme_payload(data)
    except ClassificationConfigError as e:
        rec.error = str(e)
        return rec
    rec.scheme_id = parsed.scheme_id if _valid_scheme_id(parsed.scheme_id) else name
    rec.name = parsed.name
    rec.description = parsed.description
    rec.updated_at = str(data.get("exported_at", ""))
    rec.config = parsed.config
    rec.read_only = False
    return rec


def list_schemes() -> List[SchemeRecord]:
    """列出内置方案与本地自定义方案（内置在前，自定义按文件名倒序）。"""
    out = _builtin_records()
    d = library_dir()
    if os.path.isdir(d):
        names = sorted((n for n in os.listdir(d) if n.endswith(".json")),
                       reverse=True)
        for n in names:
            out.append(_load_file_record(os.path.join(d, n)))
    return out


def get_scheme(scheme_id: str) -> Optional[SchemeRecord]:
    for rec in list_schemes():
        if rec.scheme_id == scheme_id:
            return rec
    return None


def _scheme_path(scheme_id: str) -> str:
    """方案文件路径：标识必须合法，且结果必须仍在方案库目录内。"""
    if not _valid_scheme_id(scheme_id):
        raise ClassificationConfigError(
            f"方案标识非法：{scheme_id!r}。标识只能包含字母、数字、下划线和连字符，"
            "且不能是内置标识。")
    base = os.path.realpath(os.path.abspath(library_dir()))
    path = os.path.abspath(os.path.join(base, f"{scheme_id}.json"))
    if os.path.commonpath([base, os.path.realpath(path)]) != base:
        raise ClassificationConfigError(
            f"方案路径越界，已拒绝写入方案库之外：{scheme_id!r}")
    return path


_SCHEME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _valid_scheme_id(scheme_id: str) -> bool:
    sid = (scheme_id or "").strip()
    return bool(_SCHEME_ID_RE.match(sid)) and sid not in BUILTIN_IDS


def save_scheme(name: str, description: str, cfg: ClassificationConfig,
                scheme_id: Optional[str] = None) -> SchemeRecord:
    name = (name or "").strip()
    if not name:
        raise ClassificationConfigError("方案名称不能为空。")
    errors = validate_config(cfg)
    if errors:
        raise ClassificationConfigError(
            "方案配置未通过校验：\n" + "\n".join(f"· {e}" for e in errors))
    sid = (scheme_id or "").strip()
    if not sid:
        sid = _new_scheme_id()
    elif not _valid_scheme_id(sid):
        raise ClassificationConfigError(
            f"方案标识非法，已拒绝保存：{sid!r}。"
            "标识只能包含字母、数字、下划线和连字符，且不能是内置标识。")
    payload = export_scheme_payload(clone_config(cfg), name,
                                    description, scheme_id=sid)
    path = _scheme_path(sid)
    save_scheme_file(path, payload)
    return SchemeRecord(
        scheme_id=sid, name=name, description=(description or "").strip(),
        updated_at=str(payload.get("exported_at", "")), config=clone_config(cfg),
        path=path)


def duplicate_scheme(scheme_id: str, new_name: str = "") -> SchemeRecord:
    rec = get_scheme(scheme_id)
    if rec is None or rec.config is None:
        raise ClassificationConfigError("方案不存在或不可用，无法复制。")
    name = (new_name or "").strip() or f"{rec.name} 副本"
    return save_scheme(name, rec.description, clone_config(rec.config))


def update_scheme_meta(scheme_id: str, name: str,
                       description: str) -> SchemeRecord:
    if scheme_id in BUILTIN_IDS:
        raise ClassificationConfigError("内置方案只读，不能改名或改说明。")
    rec = get_scheme(scheme_id)
    if rec is None or rec.config is None or not rec.path:
        raise ClassificationConfigError("方案不存在或不可用，无法修改。")
    return save_scheme(name, description, clone_config(rec.config),
                       scheme_id=scheme_id)


def delete_scheme(scheme_id: str) -> None:
    if scheme_id in BUILTIN_IDS:
        raise ClassificationConfigError("内置方案只读，不能删除。")
    rec = get_scheme(scheme_id)
    if rec is None or not rec.path:
        raise ClassificationConfigError("方案不存在，无法删除。")
    try:
        os.remove(rec.path)
    except OSError as e:
        raise ClassificationConfigError(f"删除方案失败：{e}")


def import_scheme_file(path: str, overwrite_id: str = "") -> SchemeRecord:
    data = load_scheme_file(path)
    parsed: ParsedScheme = parse_scheme_payload(data)
    sid = (overwrite_id or parsed.scheme_id or "").strip()
    # 导入文件的 scheme_id 属不可信元数据：非法或已占用时重新生成安全标识
    if not _valid_scheme_id(sid) or os.path.exists(_scheme_path(sid)):
        sid = _new_scheme_id()
    return save_scheme(parsed.name, parsed.description, parsed.config,
                       scheme_id=sid)


def export_scheme_to(scheme_id: str, path: str) -> None:
    rec = get_scheme(scheme_id)
    if rec is None or rec.config is None:
        raise ClassificationConfigError("方案不存在或不可用，无法导出。")
    payload = export_scheme_payload(clone_config(rec.config), rec.name,
                                    rec.description, scheme_id=rec.scheme_id)
    save_scheme_file(path, payload)


def export_current_config(path: str, cfg: ClassificationConfig,
                          name: str, description: str = "") -> None:
    payload = export_scheme_payload(clone_config(cfg), name, description)
    save_scheme_file(path, payload)
