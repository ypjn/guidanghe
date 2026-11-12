"""
归档盒 V1.3 — 推荐模式分类配置（学期 / 顶层方向 / 学期内目录 / 关键词兜底）

设计原则：
- 纯配置与匹配逻辑，不负责界面；分类函数只接收显式配置，不读写文件。
- 内置默认配置与用户配置严格区分：default_config() 每次返回新对象，
  任何一方都不会被全局修改。
- 用户配置按根目录保存到 {root}/.organizer/classification_config.json，
  原子写入；只读取默认配置不会创建文件。
- 损坏、版本不兼容、根目录不匹配、字段非法时抛出 ClassificationConfigError，
  调用方必须明确提示，不能静默回退后继续执行。

本模块不增加 AI、OCR、目录监测或跨磁盘能力，只把 V1.1 推荐模式中写在
代码里的映射转为可编辑配置，默认值与原行为逐条一致。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

CONFIG_DIR = ".organizer"
CONFIG_FILENAME = "classification_config.json"
CONFIG_VERSION = 1

# 允许的方向（本轮沿用已有方向，不开放任意方向）
DIRECTIONS = ("课程", "竞赛", "项目", "校园事务")
PENDING_DIRECTION = "待定"
DEFAULT_DIRECTION = "课程"

SCOPE_FILENAME = "filename"      # 只匹配文件名
SCOPE_PATH = "path"              # 匹配相对路径（含文件名）
SCOPE_LABELS = {
    SCOPE_FILENAME: "文件名",
    SCOPE_PATH: "路径（含文件名）",
}

TRIGGER_SUBDIR = "subdir"        # 仅用于「类别留空」的目录映射
TRIGGER_UNMATCHED = "unmatched"  # 仅在将进入待定时兜底
TRIGGER_LABELS = {
    TRIGGER_SUBDIR: "类别留空的目录",
    TRIGGER_UNMATCHED: "未匹配任何映射",
}


class ClassificationConfigError(ValueError):
    """分类配置不可用（格式损坏 / 版本不兼容 / 字段非法 / 保存失败）。"""


# ======================== 默认配置（与 V1.1 行为一致） ========================

DEFAULT_SEMESTER_MAP: Dict[str, str] = {
    "大一上": "大一上",
    "大一下": "大一下",
    "大二上": "大二上",
    "大二下": "大二下",
    "大三上": "大三上",
    "大三下": "大三下",
    "大四上": "大四上",
    "大四下": "大四下",
}

DEFAULT_TOP_DIRECTION: Dict[str, str] = {
    "校园事务": "校园事务",
    "Code": "项目",
    "个人项目灵感": "项目",
    "大创答辩": "项目",
}

# 学期内一级目录 → (方向, 类别)
# 类别为空字符串表示需要关键词细分（如「已完成期末」）
DEFAULT_SEMESTER_DIRS: Dict[str, Dict[str, Tuple[str, Optional[str]]]] = {
    "大二上": {
        "数据结构": ("课程", "数据结构"),
        "数据结构实验": ("课程", "数据结构"),
        "C++程序设计": ("课程", "C++程序设计"),
        "python": ("课程", "Python程序设计"),
        "概率论与数理统计": ("课程", "概率论与数理统计"),
        "计组": ("课程", "计算机组成原理"),
        "软件工程专业概论": ("课程", "软件工程专业概论"),
        "软概": ("课程", "软件工程专业概论"),
        "软件需求分析与设计": ("课程", "软件需求分析与设计"),
        "马克思主义基本原理": ("课程", "马克思主义基本原理"),
        "体育（轮滑）": ("课程", "体育（轮滑）"),
        "aigc2": ("课程", "AIGC（生成式人工智能）"),
        "线上课": ("课程", "线上课"),
        "周报": ("校园事务", "周报"),
        "siri": ("项目", "siri"),
    },
    "大一下": {
        "AIGC": ("课程", "AIGC（生成式人工智能）"),
        "编程作业": ("课程", "程序设计基础"),
        "大英论文": ("课程", "大学英语"),
        "纲要": ("课程", "中国近现代史纲要"),
        "高数期末": ("课程", "高等数学"),
        "离散实验": ("课程", "离散数学"),
        "思政实践": ("课程", "思政实践"),
        "已完成期末": ("课程", None),  # 需关键词细分
        "国创赛": ("竞赛", "国创赛"),
        "2026年第十六届APMCM 亚太地区大学生数学建模竞赛（中文赛项）赛题发布":
            ("竞赛", "APMCM 数学建模"),
    },
}

DEFAULT_KEYWORD_TO_COURSE: List[Tuple[str, str]] = [
    ("java", "程序设计(Java)"),
    ("离散", "离散数学"),
    ("高数", "高等数学"),
    ("概率", "概率论与数理统计"),
    ("线代", "线性代数"),
    ("大英", "大学英语"),
    ("物理", "大学物理"),
    ("数据结构", "数据结构"),
    ("python", "Python程序设计"),
    ("c++", "C++程序设计"),
    ("aigc", "AIGC（生成式人工智能）"),
    ("思政", "思政"),
]

# 未登记的学期内目录前缀 → (方向, 类别)
# 默认只有 V1.1 中硬编码的「附件-」→ 校园事务/通知
DEFAULT_GENERIC_DIR: List[Tuple[str, str, str]] = [
    ("附件-", "校园事务", "通知"),
]


# ======================== 配置数据结构 ========================

@dataclass
class SemesterRule:
    """来源目录名 → 学期标签。"""
    source: str
    semester: str
    enabled: bool = True


@dataclass
class TopDirectionRule:
    """顶层来源目录 → 已有方向。"""
    source: str
    direction: str
    enabled: bool = True


@dataclass
class SemesterDirRule:
    """某个学期内的一级目录 → 方向 + 类别/课程。"""
    semester: str
    source: str
    direction: str
    category: str = ""
    enabled: bool = True


@dataclass
class GenericDirRule:
    """未登记的学期内目录（按前缀）→ 方向 + 类别。"""
    prefix: str
    direction: str
    category: str
    enabled: bool = True


@dataclass
class KeywordRule:
    """关键词兜底规则。列表顺序即匹配优先级。"""
    keywords: List[str]
    target: str                    # 目标类别/课程
    direction: str = DEFAULT_DIRECTION
    scope: str = SCOPE_PATH        # filename / path
    trigger: str = TRIGGER_SUBDIR  # subdir / unmatched
    enabled: bool = True


@dataclass
class ClassificationConfig:
    """一个根目录的推荐模式配置。"""
    semester_rules: List[SemesterRule] = field(default_factory=list)
    top_direction_rules: List[TopDirectionRule] = field(default_factory=list)
    semester_dir_rules: List[SemesterDirRule] = field(default_factory=list)
    generic_dir_rules: List[GenericDirRule] = field(default_factory=list)
    keyword_rules: List[KeywordRule] = field(default_factory=list)
    version: int = CONFIG_VERSION


def default_config() -> ClassificationConfig:
    """构造一份全新的内置默认配置（不与任何全局对象共享）。"""
    return ClassificationConfig(
        semester_rules=[
            SemesterRule(source=k, semester=v)
            for k, v in DEFAULT_SEMESTER_MAP.items()
        ],
        top_direction_rules=[
            TopDirectionRule(source=k, direction=v)
            for k, v in DEFAULT_TOP_DIRECTION.items()
        ],
        semester_dir_rules=[
            SemesterDirRule(
                semester=sem,
                source=src,
                direction=direction,
                category=category or "",
            )
            for sem, table in DEFAULT_SEMESTER_DIRS.items()
            for src, (direction, category) in table.items()
        ],
        generic_dir_rules=[
            GenericDirRule(prefix=p, direction=d, category=c)
            for p, d, c in DEFAULT_GENERIC_DIR
        ],
        keyword_rules=[
            KeywordRule(
                keywords=[kw],
                target=course,
                direction=DEFAULT_DIRECTION,
                scope=SCOPE_PATH,
                trigger=TRIGGER_SUBDIR,
            )
            for kw, course in DEFAULT_KEYWORD_TO_COURSE
        ],
    )


# ======================== 序列化 ========================

def config_to_dict(cfg: ClassificationConfig) -> dict:
    """只导出规则内容（不含 root / updated_at），用于比较与快照。"""
    return {
        "version": cfg.version,
        "semester_map": [
            {"source": r.source, "semester": r.semester, "enabled": r.enabled}
            for r in cfg.semester_rules
        ],
        "top_direction": [
            {"source": r.source, "direction": r.direction, "enabled": r.enabled}
            for r in cfg.top_direction_rules
        ],
        "semester_dirs": [
            {
                "semester": r.semester,
                "source": r.source,
                "direction": r.direction,
                "category": r.category,
                "enabled": r.enabled,
            }
            for r in cfg.semester_dir_rules
        ],
        "generic_dirs": [
            {
                "prefix": r.prefix,
                "direction": r.direction,
                "category": r.category,
                "enabled": r.enabled,
            }
            for r in cfg.generic_dir_rules
        ],
        "keyword_rules": [
            {
                "keywords": list(r.keywords),
                "target": r.target,
                "direction": r.direction,
                "scope": r.scope,
                "trigger": r.trigger,
                "enabled": r.enabled,
            }
            for r in cfg.keyword_rules
        ],
    }


def _rows(data: dict, key: str) -> list:
    """取出配置数组并校验元素类型；类型错误时抛 ValueError。"""
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"字段 {key} 必须是数组（当前：{type(value).__name__}）")
    for i, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise ValueError(f"字段 {key} 第 {i} 项必须是对象")
    return value


def config_from_dict(data: dict) -> ClassificationConfig:
    """从字典构造配置（假定已通过版本与字段校验）。"""
    if not isinstance(data, dict):
        raise ValueError("配置顶层必须是对象")
    return ClassificationConfig(
        semester_rules=[
            SemesterRule(
                source=str(r.get("source", "")),
                semester=str(r.get("semester", "")),
                enabled=bool(r.get("enabled", True)),
            )
            for r in _rows(data, "semester_map")
        ],
        top_direction_rules=[
            TopDirectionRule(
                source=str(r.get("source", "")),
                direction=str(r.get("direction", "")),
                enabled=bool(r.get("enabled", True)),
            )
            for r in _rows(data, "top_direction")
        ],
        semester_dir_rules=[
            SemesterDirRule(
                semester=str(r.get("semester", "")),
                source=str(r.get("source", "")),
                direction=str(r.get("direction", "")),
                category=str(r.get("category", "") or ""),
                enabled=bool(r.get("enabled", True)),
            )
            for r in _rows(data, "semester_dirs")
        ],
        generic_dir_rules=[
            GenericDirRule(
                prefix=str(r.get("prefix", "")),
                direction=str(r.get("direction", "")),
                category=str(r.get("category", "") or ""),
                enabled=bool(r.get("enabled", True)),
            )
            for r in _rows(data, "generic_dirs")
        ],
        keyword_rules=[
            KeywordRule(
                keywords=[str(k) for k in r.get("keywords", [])],
                target=str(r.get("target", "")),
                direction=str(r.get("direction", DEFAULT_DIRECTION)),
                scope=str(r.get("scope", SCOPE_PATH)),
                trigger=str(r.get("trigger", TRIGGER_SUBDIR)),
                enabled=bool(r.get("enabled", True)),
            )
            for r in _rows(data, "keyword_rules")
        ],
        version=int(data.get("version", CONFIG_VERSION)),
    )


def clone_config(cfg: ClassificationConfig) -> ClassificationConfig:
    """深拷贝配置（编辑草稿 / 后台快照用）。"""
    return config_from_dict(config_to_dict(cfg))


def is_default_config(cfg: ClassificationConfig) -> bool:
    """结构上是否与内置默认配置完全一致（含顺序与启用状态）。"""
    return config_to_dict(cfg) == config_to_dict(default_config())


# ======================== 校验 ========================

def _check_component(value, where: str, errors: List[str],
                     allow_empty: bool = False) -> None:
    """校验目录名/类别名：不能为空、不能含路径分隔符、不能是 . / .. 或绝对路径。"""
    if not isinstance(value, str):
        errors.append(f"{where}：必须是文本")
        return
    v = value.strip()
    if not v:
        if allow_empty:
            return
        errors.append(f"{where}：不能为空")
        return
    if v != value:
        errors.append(f"{where}：首尾不能有空白（当前：{value!r}）")
    if "/" in v or "\\" in v:
        errors.append(f"{where}：不能包含路径分隔符（当前：{value!r}）")
    if v in (".", ".."):
        errors.append(f"{where}：不能是 . 或 ..（路径逃逸）")
    if v.startswith("~"):
        errors.append(f"{where}：不能以 ~ 开头（禁止绝对/家目录路径）")
    if os.path.isabs(v):
        errors.append(f"{where}：不能是绝对路径（当前：{value!r}）")
    if len(v) >= 2 and v[1] == ":":
        errors.append(f"{where}：不能是盘符路径（当前：{value!r}）")
    if "\x00" in v:
        errors.append(f"{where}：包含非法控制字符")


def validate_config(cfg: ClassificationConfig) -> List[str]:
    """返回全部错误（中文、带具体规则位置）。空列表表示通过。"""
    errors: List[str] = []
    if not isinstance(cfg.version, int) or cfg.version != CONFIG_VERSION:
        errors.append(
            f"配置版本：仅支持 version={CONFIG_VERSION}，当前为 {cfg.version!r}")
        return errors

    # ---- 学期映射 ----
    seen_sem = set()
    semester_labels = set()
    for i, r in enumerate(cfg.semester_rules, 1):
        where = f"学期映射第 {i} 条"
        _check_component(r.source, f"{where} 来源目录名", errors)
        _check_component(r.semester, f"{where} 学期标签", errors)
        if isinstance(r.source, str) and r.source in seen_sem:
            errors.append(f"{where}：来源目录名「{r.source}」重复")
        seen_sem.add(r.source if isinstance(r.source, str) else "")
        if isinstance(r.semester, str) and r.semester.strip():
            semester_labels.add(r.semester)
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

    # ---- 顶层方向 ----
    seen_top = set()
    for i, r in enumerate(cfg.top_direction_rules, 1):
        where = f"顶层方向映射第 {i} 条"
        _check_component(r.source, f"{where} 来源目录名", errors)
        if r.direction not in DIRECTIONS:
            errors.append(
                f"{where}：方向必须是 {'/'.join(DIRECTIONS)}（当前：{r.direction!r}）")
        if isinstance(r.source, str) and r.source in seen_top:
            errors.append(f"{where}：来源目录名「{r.source}」重复")
        seen_top.add(r.source if isinstance(r.source, str) else "")
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

    # ---- 学期内目录 ----
    seen_dir = set()
    for i, r in enumerate(cfg.semester_dir_rules, 1):
        where = f"学期内目录第 {i} 条"
        _check_component(r.semester, f"{where} 学期", errors)
        _check_component(r.source, f"{where} 目录名", errors)
        _check_component(r.category, f"{where} 类别/课程", errors,
                         allow_empty=True)
        if r.direction not in DIRECTIONS:
            errors.append(
                f"{where}：方向必须是 {'/'.join(DIRECTIONS)}（当前：{r.direction!r}）")
        key = (r.semester, r.source)
        if key in seen_dir:
            errors.append(
                f"{where}：学期「{r.semester}」下的目录「{r.source}」重复")
        seen_dir.add(key)
        if r.semester not in semester_labels:
            errors.append(
                f"{where}：学期「{r.semester}」未在学期映射中登记（该规则不会生效）")
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

    # ---- 通用目录前缀 ----
    seen_prefix = set()
    for i, r in enumerate(cfg.generic_dir_rules, 1):
        where = f"通用目录前缀第 {i} 条"
        _check_component(r.prefix, f"{where} 目录名前缀", errors)
        _check_component(r.category, f"{where} 类别", errors)
        if r.direction not in DIRECTIONS:
            errors.append(
                f"{where}：方向必须是 {'/'.join(DIRECTIONS)}（当前：{r.direction!r}）")
        if r.prefix in seen_prefix:
            errors.append(f"{where}：前缀「{r.prefix}」重复")
        seen_prefix.add(r.prefix)
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

    # ---- 关键词兜底 ----
    seen_kw = set()
    for i, r in enumerate(cfg.keyword_rules, 1):
        where = f"关键词规则第 {i} 条"
        if not isinstance(r.keywords, list) or not r.keywords:
            errors.append(f"{where}：至少需要一个关键词")
        else:
            for kw in r.keywords:
                if not isinstance(kw, str) or not kw.strip():
                    errors.append(f"{where}：关键词不能为空")
            normalized = tuple(sorted(k.strip().lower()
                                     for k in r.keywords if isinstance(k, str)))
            if normalized and normalized in seen_kw:
                errors.append(f"{where}：关键词组合「{'/'.join(r.keywords)}」重复")
            seen_kw.add(normalized)
        _check_component(r.target, f"{where} 目标类别", errors)
        if r.direction not in DIRECTIONS:
            errors.append(
                f"{where}：方向必须是 {'/'.join(DIRECTIONS)}（当前：{r.direction!r}）")
        if r.scope not in SCOPE_LABELS:
            errors.append(
                f"{where}：匹配范围必须是 filename/path（当前：{r.scope!r}）")
        if r.trigger not in TRIGGER_LABELS:
            errors.append(
                f"{where}：触发场景必须是 subdir/unmatched（当前：{r.trigger!r}）")
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

    return errors


def ensure_valid(cfg: ClassificationConfig) -> None:
    errors = validate_config(cfg)
    if errors:
        raise ClassificationConfigError(
            "分类配置未通过校验：\n" + "\n".join(f"· {e}" for e in errors))


# ======================== 文件位置 / 读写 ========================

def config_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), CONFIG_DIR, CONFIG_FILENAME)


def _norm_root(path: str) -> str:
    """根目录规范化：abspath + realpath；不做大小写变换、不用前缀比较。"""
    return os.path.realpath(os.path.abspath(path))


def load_config(root: str) -> Tuple[ClassificationConfig, str]:
    """读取该根目录的配置。

    返回 (配置, 来源)，来源为 "default"（文件不存在）或 "custom"。
    文件损坏 / 版本不兼容 / 根目录不匹配 / 字段非法时抛出
    ClassificationConfigError，原文件保持不变。
    """
    path = config_path(root)
    if not os.path.isfile(path):
        return default_config(), "default"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ClassificationConfigError(
            f"分类配置文件无法读取或不是有效 JSON：{path}\n"
            f"原因：{e}\n原文件已保留；请修复该文件，或在配置界面选择「恢复默认」。"
        )
    if not isinstance(data, dict):
        raise ClassificationConfigError(
            f"分类配置文件格式不正确（顶层必须是对象）：{path}\n"
            "原文件已保留；请修复该文件，或在配置界面选择「恢复默认」。"
        )
    version = data.get("version")
    if version != CONFIG_VERSION:
        raise ClassificationConfigError(
            f"分类配置版本不兼容：文件 version={version!r}，"
            f"当前程序仅支持 version={CONFIG_VERSION}。\n"
            f"文件：{path}\n原文件已保留；请升级程序或选择「恢复默认」。"
        )
    stored_root = data.get("root")
    if stored_root and _norm_root(str(stored_root)) != _norm_root(root):
        raise ClassificationConfigError(
            "分类配置不属于当前根目录，已拒绝加载（不同目录的配置不能串用）。\n"
            f"配置记录：{stored_root}\n当前根目录：{os.path.abspath(root)}\n"
            f"文件：{path}\n原文件已保留。"
        )
    try:
        cfg = config_from_dict(data)
    except (TypeError, ValueError) as e:
        raise ClassificationConfigError(
            f"分类配置字段类型不正确：{path}\n原因：{e}\n"
            "原文件已保留；请修复该文件，或选择「恢复默认」。")
    errors = validate_config(cfg)
    if errors:
        raise ClassificationConfigError(
            f"分类配置字段非法：{path}\n"
            + "\n".join(f"· {e}" for e in errors)
            + "\n原文件已保留；请修复该文件，或选择「恢复默认」。"
        )
    return cfg, "custom"


def save_config(root: str, cfg: ClassificationConfig) -> bool:
    """原子保存配置。

    - 校验失败：抛 ClassificationConfigError，旧文件不动。
    - 内容与内置默认完全一致：删除已有配置文件（恢复默认），返回 False。
    - 写入失败：删除临时文件并抛错，旧文件保持原样。
    返回 True 表示写入了自定义配置。
    """
    ensure_valid(cfg)
    path = config_path(root)

    if is_default_config(cfg):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            raise ClassificationConfigError(
                f"恢复默认失败：无法删除旧配置文件。\n{path}\n原因：{e}")
        return False

    payload = {
        "version": CONFIG_VERSION,
        "root": os.path.abspath(root),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload.update(config_to_dict(cfg))

    target_dir = os.path.dirname(path)
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".classification_config.", suffix=".tmp", dir=target_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as e:
        raise ClassificationConfigError(
            f"保存分类配置失败，原配置未改动。\n{path}\n原因：{e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return True


def config_summary(cfg: ClassificationConfig) -> str:
    """一行摘要（界面状态标签用）。"""
    return (f"学期 {len(cfg.semester_rules)} / 顶层方向 "
            f"{len(cfg.top_direction_rules)} / 学期内目录 "
            f"{len(cfg.semester_dir_rules)} / 通用前缀 "
            f"{len(cfg.generic_dir_rules)} / 关键词 {len(cfg.keyword_rules)}")


# ======================== 匹配核心 ========================

@dataclass
class ContextMatch:
    """一次推荐模式分类的结果（显式、可解释）。"""
    direction: str
    semester: str
    category: str
    anchor_index: int
    rule_desc: str
    rule_key: str


@dataclass
class CompiledConfig:
    """预处理后的只读配置（一次计划生成只编译一次）。"""
    config: ClassificationConfig
    snapshot: dict
    semester_by_source: Dict[str, SemesterRule]
    top_by_source: Dict[str, TopDirectionRule]
    semester_dirs: Dict[Tuple[str, str], SemesterDirRule]
    generic_rules: List[GenericDirRule]
    keyword_subdir: List[KeywordRule]
    keyword_unmatched: List[KeywordRule]


def compile_config(cfg: Optional[ClassificationConfig] = None) -> CompiledConfig:
    cfg = cfg or default_config()
    semester_by_source: Dict[str, SemesterRule] = {}
    for r in cfg.semester_rules:
        if r.enabled and r.source not in semester_by_source:
            semester_by_source[r.source] = r
    top_by_source: Dict[str, TopDirectionRule] = {}
    for r in cfg.top_direction_rules:
        if r.enabled and r.source not in top_by_source:
            top_by_source[r.source] = r
    semester_dirs: Dict[Tuple[str, str], SemesterDirRule] = {}
    for r in cfg.semester_dir_rules:
        key = (r.semester, r.source)
        if r.enabled and key not in semester_dirs:
            semester_dirs[key] = r
    generic_rules = [r for r in cfg.generic_dir_rules if r.enabled]
    keyword_subdir = [
        r for r in cfg.keyword_rules
        if r.enabled and r.trigger == TRIGGER_SUBDIR
    ]
    keyword_unmatched = [
        r for r in cfg.keyword_rules
        if r.enabled and r.trigger == TRIGGER_UNMATCHED
    ]
    return CompiledConfig(
        config=cfg,
        snapshot=config_to_dict(cfg),
        semester_by_source=semester_by_source,
        top_by_source=top_by_source,
        semester_dirs=semester_dirs,
        generic_rules=generic_rules,
        keyword_subdir=keyword_subdir,
        keyword_unmatched=keyword_unmatched,
    )


def _match_english_word(text_lower: str, keyword: str) -> bool:
    """英文关键词按词边界匹配，避免 "ai" 命中 "training"。"""
    kw_lower = keyword.lower()
    if kw_lower.isalpha():
        pattern = r"(?<![a-z0-9])" + re.escape(kw_lower) + r"(?![a-z0-9])"
        return re.search(pattern, text_lower) is not None
    return kw_lower in text_lower


def _keyword_matches(rule: KeywordRule, rel_path: str,
                     filename: str) -> Optional[str]:
    """返回命中的关键词；未命中返回 None。"""
    text = (filename if rule.scope == SCOPE_FILENAME else rel_path).lower()
    for kw in rule.keywords:
        if _match_english_word(text, kw):
            return kw
    return None


def _unmatched_fallback(compiled: CompiledConfig, rel_path: str,
                        filename: str, base_reason: str,
                        semester: str = "", anchor: int = -1,
                        category: str = "") -> ContextMatch:
    """待定兜底：检查 trigger=unmatched 的关键词规则。

    未命中关键词时保持调用方给出的原始锚点与类别（与 V1.1 一致）。
    """
    for i, rule in enumerate(compiled.keyword_unmatched, 1):
        kw = _keyword_matches(rule, rel_path, filename)
        if kw:
            return ContextMatch(
                direction=rule.direction,
                semester=semester,
                category=rule.target,
                anchor_index=anchor,
                rule_desc=f"关键词兜底「{kw}」→ {rule.direction}/{rule.target}",
                rule_key=f"keyword_unmatched:{i}:{kw}",
            )
    return ContextMatch("待定", semester, category, anchor, base_reason,
                        "fallback")


def classify_context(rel_path: str, compiled: CompiledConfig) -> ContextMatch:
    """按配置对单个相对路径分类。

    匹配顺序（固定、可解释）：
      1. 学期映射：按路径组件从上到下找第一个已登记学期目录；
      2. 非学期时：顶层方向映射（parts[0] 精确匹配）；
      3. 学期内一级目录映射（含类别留空 → 关键词细分）；
      4. 通用目录前缀（未登记的学期内目录）；
      5. 未登记目录按目录名锚定（方向=课程）；
      6. 均未命中 → 待定（可选 unmatched 关键词兜底）。
    锚点 anchor_index 指向最后一个已确定类别的目录组件；
    inner_path = parts[anchor_index + 1:-1]，文件名永不参与目录锚定。
    """
    parts = rel_path.split(os.sep)
    filename = parts[-1] if parts else ""
    dir_parts = parts[:-1]
    text_lower = rel_path.lower()

    # 1) 学期目录（与 V1.1 一致：按路径组件顺序找第一个命中）
    sem_index, sem_rule = -1, None
    for i, p in enumerate(parts):
        rule = compiled.semester_by_source.get(p)
        if rule is not None:
            sem_index, sem_rule = i, rule
            break
    sem_label = sem_rule.semester if sem_rule is not None else ""

    # 2) 顶层方向（非学期场景）
    if sem_index < 0 and parts:
        top = parts[0]
        top_rule = compiled.top_by_source.get(top)
        if top_rule is not None:
            direction = top_rule.direction
            if len(dir_parts) >= 2:
                cat = dir_parts[1]
                anchor = 1
            elif len(dir_parts) == 1:
                cat = top
                anchor = 0
            else:
                cat = top
                anchor = -1
            return ContextMatch(
                direction, "", cat, anchor,
                f"顶层目录「{top}」→ {direction}/{cat}",
                f"top_direction:{top}",
            )
        if len(parts) == 1:
            return _unmatched_fallback(
                compiled, rel_path, filename, "顶层散落文件")
        return _unmatched_fallback(
            compiled, rel_path, filename, f"未登记顶层目录「{top}」",
            category=top)

    # 3) 学期内一级目录
    if sem_index >= 0:
        child_idx = sem_index + 1
        child_dir = parts[child_idx] if child_idx < len(dir_parts) else None
        # 目录映射按「学期标签」关联（默认配置中标签与来源目录同名，
        # 自定义学期标签不同名时也能命中）
        rule = (compiled.semester_dirs.get((sem_label, child_dir))
                if child_dir else None)

        if rule is not None:
            if rule.category:
                return ContextMatch(
                    rule.direction, sem_label, rule.category, child_idx,
                    f"学期「{sem_label}」/「{child_dir}」→ "
                    f"{rule.direction}/{rule.category}",
                    f"semester_dir:{sem_label}/{child_dir}",
                )
            # 类别留空 → 关键词细分（按配置顺序取第一条命中）
            for i, kw_rule in enumerate(compiled.keyword_subdir, 1):
                kw = _keyword_matches(kw_rule, rel_path, filename)
                if kw:
                    return ContextMatch(
                        rule.direction, sem_label, kw_rule.target, child_idx,
                        f"学期「{sem_label}」/「{child_dir}」+关键词「{kw}」"
                        f"→ {kw_rule.direction}/{kw_rule.target}",
                        f"keyword_subdir:{sem_label}/{child_dir}:{i}",
                    )
            return ContextMatch(
                rule.direction, sem_label, f"{child_dir}（未细分）", child_idx,
                f"学期「{sem_label}」/「{child_dir}」关键词未命中，保留目录名",
                f"semester_dir_unmatched:{sem_label}/{child_dir}",
            )

        if child_dir:
            # 4) 通用目录前缀
            for i, g in enumerate(compiled.generic_rules, 1):
                if child_dir.startswith(g.prefix):
                    return ContextMatch(
                        g.direction, sem_label, g.category, child_idx,
                        f"学期「{sem_label}」/「{child_dir}」→ {g.category}",
                        f"generic_dir:{g.prefix}",
                    )
            # 5) 未登记目录按目录名锚定
            return ContextMatch(
                DEFAULT_DIRECTION, sem_label, child_dir, child_idx,
                f"学期「{sem_label}」/「{child_dir}」（目录锚定）",
                f"generic_fallback:{sem_label}/{child_dir}",
            )

        # 学期根目录下直接放文件
        return _unmatched_fallback(
            compiled, rel_path, filename,
            f"学期「{sem_label}」根目录下",
            semester=sem_label, anchor=sem_index,
            category=parts[sem_index])

    return _unmatched_fallback(compiled, rel_path, filename, "无上下文")
