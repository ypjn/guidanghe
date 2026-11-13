"""
归档盒 V1.3 — 推荐模式分类配置（学期 / 顶层方向 / 学期内目录 / 关键词兜底）

设计原则：
- 纯配置与匹配逻辑，不负责界面；分类函数只接收显式配置，不读写文件。
- 内置默认配置与用户配置严格区分：default_config() 每次返回新对象，
  任何一方都不会被全局修改。
- 竞赛星级表、常见课程名和校园事务写在未匹配关键词里：
  已经按课程目录认走的文件不会改判。
- 用户配置按根目录保存到 {root}/.organizer/classification_config.json，
  原子写入；只读取默认配置不会创建文件。
- 损坏、版本不兼容、根目录不匹配、字段非法时抛出 ClassificationConfigError，
  调用方必须明确提示，不能静默回退后继续执行。

关键词匹配可以额外接收调用方传来的图片文字；本模块自己不读图片。
目录映射仍然只看路径。不传图片文字时，结果与只看文件名、路径时一致。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from contest_catalog import CONTEST_CATALOG
from general_catalog import GENERAL_CAMPUS_RULES, GENERAL_COURSE_RULES
from professional_catalog import PROFESSIONAL_COURSE_RULES

CONFIG_DIR = ".organizer"
CONFIG_FILENAME = "classification_config.json"
# V1.5：配置格式升到 2，新增 long_term_rules。
# 读取端接受 1 与 2：旧配置（无长期规则）可正常加载并在保存时升级为 2。
# 写入端一律写 2，因此旧程序读到新文件会因版本不兼容而拒绝加载并保留原文件，
# 不会静默忽略长期规则后继续整理。
CONFIG_VERSION = 2
SUPPORTED_READ_VERSIONS = (1, 2)

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

# ======================== 长期修正规则（V1.5） ========================

# 匹配方式：只开放三种，不做正则 / 脚本 / 任意代码
MATCH_EXACT_PATH = "exact_path"          # 来源相对路径精确匹配（默认）
MATCH_DIR_SCOPE = "dir_scope"            # 指定来源目录范围
MATCH_FILENAME_KEYWORD = "filename_keyword"  # 文件名关键词（可限定目录）
MATCH_TYPE_LABELS = {
    MATCH_EXACT_PATH: "精确路径",
    MATCH_DIR_SCOPE: "目录范围",
    MATCH_FILENAME_KEYWORD: "文件名关键词",
}
MATCH_LABEL_TO_TYPE = {v: k for k, v in MATCH_TYPE_LABELS.items()}

# 内部路径处理：保留自动结果 / 用规则替换 / 清空
INNER_KEEP = "keep"
INNER_REPLACE = "replace"
INNER_CLEAR = "clear"
INNER_MODE_LABELS = {
    INNER_KEEP: "保留自动内部路径",
    INNER_REPLACE: "替换为指定路径",
    INNER_CLEAR: "清空内部路径",
}
INNER_LABEL_TO_MODE = {v: k for k, v in INNER_MODE_LABELS.items()}
# 表格里用更短的显示
INNER_MODE_SHORT = {
    INNER_KEEP: "保留",
    INNER_REPLACE: "替换",
    INNER_CLEAR: "清空",
}
INNER_SHORT_TO_MODE = {v: k for k, v in INNER_MODE_SHORT.items()}

# 长期规则允许的目标方向（比原有映射多一个「待定」）
LONG_TERM_DIRECTIONS = tuple(DIRECTIONS) + (PENDING_DIRECTION,)


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
    "代码": "项目",
    "项目": "项目",
    "个人项目灵感": "项目",
    "大创答辩": "项目",
    "实习": "校园事务",
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
class LongTermRule:
    """长期修正规则（V1.5）。

    一条规则 = 一套匹配条件 + 一套目标分类，列表顺序即优先级。
    命中后**覆盖**原有推荐模式映射（学期 / 顶层方向 / 学期内目录等），
    但仍低于"当前计划的临时手动修正"。

    rule_id 是稳定标识（生成后不再变化），用于历史追溯；
    调整顺序、改名、编辑内容都不会改变它。
    """
    rule_id: str
    name: str
    match_type: str = MATCH_EXACT_PATH
    # exact_path: 来源文件的相对路径（含文件名）
    # dir_scope:   来源目录的相对路径（可为空表示根目录）
    # filename_keyword: 限定来源目录（可为空表示不限目录）
    source_path: str = ""
    include_subdirs: bool = True     # 仅 dir_scope 使用
    keywords: List[str] = field(default_factory=list)  # 仅 filename_keyword
    direction: str = DEFAULT_DIRECTION
    semester: str = ""
    category: str = ""
    inner_mode: str = INNER_KEEP
    inner_path: str = ""
    enabled: bool = True
    reason: str = ""                 # 生成依据说明，只读展示


def new_rule_id() -> str:
    """生成一个稳定、与行号无关的规则标识。"""
    import uuid
    return "lt-" + uuid.uuid4().hex[:12]


@dataclass
class ClassificationConfig:
    """一个根目录的推荐模式配置。"""
    semester_rules: List[SemesterRule] = field(default_factory=list)
    top_direction_rules: List[TopDirectionRule] = field(default_factory=list)
    semester_dir_rules: List[SemesterDirRule] = field(default_factory=list)
    generic_dir_rules: List[GenericDirRule] = field(default_factory=list)
    keyword_rules: List[KeywordRule] = field(default_factory=list)
    # V1.5：长期修正规则，顺序即优先级
    long_term_rules: List[LongTermRule] = field(default_factory=list)
    version: int = CONFIG_VERSION


def contest_keyword_rules() -> List[KeywordRule]:
    """星级表竞赛名单。只在即将进入待定，或学期内目录尚未登记时生效。

    按最长关键词降序，避免短名抢走已经写全的竞赛名称。
    """
    rules = [
        KeywordRule(
            keywords=list(keywords),
            target=category,
            direction="竞赛",
            scope=SCOPE_PATH,
            trigger=TRIGGER_UNMATCHED,
        )
        for keywords, category in CONTEST_CATALOG
    ]
    rules.sort(key=lambda rule: -max(len(keyword) for keyword in rule.keywords))
    return rules


def _catalog_keyword_rules(catalog, direction: str) -> List[KeywordRule]:
    """把 (关键词, 类别) 收成未匹配关键词，长名优先。"""
    rules = [
        KeywordRule(
            keywords=list(keywords),
            target=category,
            direction=direction,
            scope=SCOPE_PATH,
            trigger=TRIGGER_UNMATCHED,
        )
        for keywords, category in catalog
    ]
    rules.sort(key=lambda rule: -max(len(keyword) for keyword in rule.keywords))
    return rules


def general_keyword_rules() -> List[KeywordRule]:
    """课程名按长短一起排序，校园事务在后。竞赛名单由调用方排在更前面。"""
    return (
        _catalog_keyword_rules(
            GENERAL_COURSE_RULES + PROFESSIONAL_COURSE_RULES, "课程")
        + _catalog_keyword_rules(GENERAL_CAMPUS_RULES, "校园事务")
    )


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
        ] + contest_keyword_rules() + general_keyword_rules(),
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
        "long_term_rules": [
            {
                "rule_id": r.rule_id,
                "name": r.name,
                "match_type": r.match_type,
                "source_path": r.source_path,
                "include_subdirs": r.include_subdirs,
                "keywords": list(r.keywords),
                "direction": r.direction,
                "semester": r.semester,
                "category": r.category,
                "inner_mode": r.inner_mode,
                "inner_path": r.inner_path,
                "enabled": r.enabled,
                "reason": r.reason,
            }
            for r in cfg.long_term_rules
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


# 规则数组的必填文本字段与可选文本字段
# 兼容策略：规则数组可缺省（视为空）；以下字段中 required=True 的必须出现，
# 可选字段出现时仍必须是文本；同版本的未知额外字段一律忽略。
_RULE_TEXT_FIELDS = {
    "semester_map": (("source", True), ("semester", True)),
    "top_direction": (("source", True), ("direction", True)),
    "semester_dirs": (("semester", True), ("source", True),
                      ("direction", True), ("category", False)),
    "generic_dirs": (("prefix", True), ("direction", True),
                     ("category", True)),
    "keyword_rules": (("target", True), ("direction", False),
                      ("scope", False), ("trigger", False)),
    "long_term_rules": (("rule_id", True), ("name", True), ("match_type", True),
                        ("source_path", False), ("direction", True),
                        ("semester", False), ("category", False),
                        ("inner_mode", False), ("inner_path", False),
                        ("reason", False)),
}
_RULE_ARRAY_KEYS = tuple(_RULE_TEXT_FIELDS.keys())


def _raw_type(value) -> str:
    return f"{value!r}（类型 {type(value).__name__}）"


def validate_config_payload(data: dict) -> List[str]:
    """在类型转换之前校验原始 JSON 的类型。

    必须通过这里才允许 config_from_dict 做转换：任何 bool("false")、
    str(2026)、int(True) 之类的强制转换都会掩盖配置错误，因此这里
    只按 JSON 原始类型严格判断。

    兼容策略：
      - version 必填，且必须是整数（明确排除 JSON 布尔值）；
      - root / updated_at 可缺省；出现时必须是文本；
      - 五个规则数组可缺省（视为空数组），出现时必须是数组，
        元素必须是对象；
      - 规则对象中 _RULE_TEXT_FIELDS 标为必填的字段必须出现；
        enabled 可缺省（默认 true），出现时必须是真的 JSON 布尔值；
      - 同版本的未知额外字段忽略（向前兼容）。
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["配置顶层必须是对象"]

    if "version" not in data:
        errors.append("缺少必填字段 version")
    else:
        v = data["version"]
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(
                f"version 必须是整数（布尔值不是有效版本），当前：{_raw_type(v)}")

    for key in ("root", "updated_at"):
        if key in data and not isinstance(data[key], str):
            errors.append(f"字段 {key} 必须是文本，当前：{_raw_type(data[key])}")

    for key in _RULE_ARRAY_KEYS:
        if key not in data:
            continue
        rows = data[key]
        if not isinstance(rows, list):
            errors.append(f"字段 {key} 必须是数组，当前：{_raw_type(rows)}")
            continue
        text_fields = _RULE_TEXT_FIELDS[key]
        for i, row in enumerate(rows, 1):
            where = f"{key} 第 {i} 项"
            if not isinstance(row, dict):
                errors.append(f"{where}：必须是对象，当前：{_raw_type(row)}")
                continue
            for field, required in text_fields:
                if field not in row:
                    if required:
                        errors.append(f"{where}：缺少必填字段 {field}")
                    continue
                if not isinstance(row[field], str):
                    errors.append(
                        f"{where}：字段 {field} 必须是文本，"
                        f"当前：{_raw_type(row[field])}")
            if "enabled" in row and not isinstance(row["enabled"], bool):
                errors.append(
                    f"{where}：字段 enabled 必须是布尔值 true/false，"
                    f"当前：{_raw_type(row['enabled'])}")
            if key == "keyword_rules":
                if "keywords" not in row:
                    errors.append(f"{where}：缺少必填字段 keywords")
                elif not isinstance(row["keywords"], list):
                    errors.append(
                        f"{where}：字段 keywords 必须是文本数组，"
                        f"当前：{_raw_type(row['keywords'])}")
                else:
                    for j, kw in enumerate(row["keywords"], 1):
                        if not isinstance(kw, str):
                            errors.append(
                                f"{where}：keywords 第 {j} 个元素必须是文本，"
                                f"当前：{_raw_type(kw)}")
            if key == "long_term_rules":
                if "include_subdirs" in row and not isinstance(
                        row["include_subdirs"], bool):
                    errors.append(
                        f"{where}：字段 include_subdirs 必须是布尔值，"
                        f"当前：{_raw_type(row['include_subdirs'])}")
                if "keywords" in row:
                    if not isinstance(row["keywords"], list):
                        errors.append(
                            f"{where}：字段 keywords 必须是文本数组，"
                            f"当前：{_raw_type(row['keywords'])}")
                    else:
                        for j, kw in enumerate(row["keywords"], 1):
                            if not isinstance(kw, str):
                                errors.append(
                                    f"{where}：keywords 第 {j} 个元素必须是文本，"
                                    f"当前：{_raw_type(kw)}")
    return errors


def config_from_dict(data: dict) -> ClassificationConfig:
    """从字典构造配置。

    先做原始 JSON 类型校验；类型错误直接抛 ValueError，不做任何强制
    转换。通过后才按字段构造数据类。
    """
    if not isinstance(data, dict):
        raise ValueError("配置顶层必须是对象")
    errors = validate_config_payload(data)
    if errors:
        raise ValueError("；".join(errors))
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
        long_term_rules=[
            LongTermRule(
                rule_id=str(r.get("rule_id", "")),
                name=str(r.get("name", "")),
                match_type=str(r.get("match_type", MATCH_EXACT_PATH)),
                source_path=str(r.get("source_path", "") or ""),
                include_subdirs=bool(r.get("include_subdirs", True)),
                keywords=[str(k) for k in r.get("keywords", [])],
                direction=str(r.get("direction", DEFAULT_DIRECTION)),
                semester=str(r.get("semester", "") or ""),
                category=str(r.get("category", "") or ""),
                inner_mode=str(r.get("inner_mode", INNER_KEEP)),
                inner_path=str(r.get("inner_path", "") or ""),
                enabled=bool(r.get("enabled", True)),
                reason=str(r.get("reason", "") or ""),
            )
            for r in _rows(data, "long_term_rules")
        ],
        # 读入后统一规范化到当前版本：保存时一律写 CONFIG_VERSION
        version=CONFIG_VERSION,
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


def _check_rel_path(value, where: str, errors: List[str],
                    allow_empty: bool = False) -> None:
    """校验相对路径（可含多段）：必须相对、无 .. / 空段 / 盘符 / 控制字符。"""
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
    if v.startswith("/") or v.startswith("\\") or os.path.isabs(v):
        errors.append(f"{where}：必须是相对路径（当前：{value!r}）")
    if v.startswith("~"):
        errors.append(f"{where}：不能以 ~ 开头（禁止家目录路径）")
    if len(v) >= 2 and v[1] == ":":
        errors.append(f"{where}：不能是盘符路径（当前：{value!r}）")
    if "\x00" in v:
        errors.append(f"{where}：包含非法控制字符")
    segs = re.split(r"[/\\]", v)
    if any(not s for s in segs):
        errors.append(f"{where}：不能包含空目录段（如 // 或首尾分隔符）")
    for s in segs:
        if s in (".", ".."):
            errors.append(f"{where}：不能包含 . 或 .. 段（路径逃逸）")


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

    # ---- 长期修正规则（V1.5） ----
    seen_ids = set()
    seen_names = set()
    for i, r in enumerate(cfg.long_term_rules, 1):
        where = f"长期规则第 {i} 条"
        if not isinstance(r.rule_id, str) or not r.rule_id.strip():
            errors.append(f"{where}：规则标识不能为空")
        elif r.rule_id in seen_ids:
            errors.append(f"{where}：规则标识「{r.rule_id}」重复")
        else:
            seen_ids.add(r.rule_id)
        _check_component(r.name, f"{where} 名称", errors)
        if isinstance(r.name, str) and r.name.strip() in seen_names:
            errors.append(f"{where}：名称「{r.name}」重复（建议改名以便区分）")
        if isinstance(r.name, str):
            seen_names.add(r.name.strip())
        if r.match_type not in MATCH_TYPE_LABELS:
            errors.append(
                f"{where}：匹配方式必须是 exact_path/dir_scope/"
                f"filename_keyword（当前：{r.match_type!r}）")
        if r.direction not in LONG_TERM_DIRECTIONS:
            errors.append(
                f"{where}：方向必须是 {'/'.join(LONG_TERM_DIRECTIONS)}"
                f"（当前：{r.direction!r}）")
        # 学期：课程必填，其他方向必须为空（与手动修正规则一致）
        if r.direction == "课程":
            _check_component(r.semester, f"{where} 学期", errors)
        elif r.semester:
            errors.append(f"{where}：非课程方向的学期必须留空")
        _check_component(r.category, f"{where} 类别", errors, allow_empty=True)
        if r.inner_mode not in INNER_MODE_LABELS:
            errors.append(
                f"{where}：内部路径方式必须是 keep/replace/clear"
                f"（当前：{r.inner_mode!r}）")
        if r.inner_mode == INNER_REPLACE:
            _check_rel_path(r.inner_path, f"{where} 内部路径", errors)
        elif r.inner_path:
            errors.append(f"{where}：内部路径方式不是「替换」时，内部路径必须留空")
        if not isinstance(r.include_subdirs, bool):
            errors.append(f"{where}：include_subdirs 必须是布尔值")
        if not isinstance(r.enabled, bool):
            errors.append(f"{where}：启用状态必须是布尔值")

        if r.match_type == MATCH_EXACT_PATH:
            _check_rel_path(r.source_path, f"{where} 来源相对路径", errors)
        elif r.match_type == MATCH_DIR_SCOPE:
            _check_rel_path(r.source_path, f"{where} 来源目录", errors,
                            allow_empty=True)
        elif r.match_type == MATCH_FILENAME_KEYWORD:
            _check_rel_path(r.source_path, f"{where} 限定目录", errors,
                            allow_empty=True)
            if not r.keywords:
                errors.append(f"{where}：文件名关键词至少需要一个")
            else:
                for kw in r.keywords:
                    if not isinstance(kw, str) or not kw.strip():
                        errors.append(f"{where}：文件名关键词不能为空")
                    elif "/" in kw or "\\" in kw:
                        errors.append(
                            f"{where}：文件名关键词不能包含路径分隔符"
                            f"（当前：{kw!r}）")

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
    # 在版本比较与任何类型转换之前，先按原始 JSON 类型严格校验
    raw_errors = validate_config_payload(data)
    if raw_errors:
        raise ClassificationConfigError(
            f"分类配置字段类型不正确：{path}\n"
            + "\n".join(f"· {e}" for e in raw_errors)
            + "\n原文件已保留；请修复该文件，或选择「恢复默认」。"
        )
    version = data.get("version")
    if version not in SUPPORTED_READ_VERSIONS:
        raise ClassificationConfigError(
            f"分类配置版本不兼容：文件 version={version!r}，"
            f"当前程序支持 version={SUPPORTED_READ_VERSIONS}。\n"
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
            f"{len(cfg.generic_dir_rules)} / 关键词 {len(cfg.keyword_rules)}"
            f" / 长期规则 {len(cfg.long_term_rules)}")


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
    # 不为 None 时，计划直接使用这段内部路径，不再从锚点截取。
    fixed_inner: Optional[str] = None


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
    # V1.5：已启用的长期修正规则，保持配置里的顺序（即优先级）
    long_term: List[LongTermRule] = field(default_factory=list)


def compile_config(cfg: Optional[ClassificationConfig] = None) -> CompiledConfig:
    cfg = cfg or default_config()
    semester_by_source: Dict[str, SemesterRule] = {}
    for r in cfg.semester_rules:
        key = r.source.casefold()
        if r.enabled and key not in semester_by_source:
            semester_by_source[key] = r
    top_by_source: Dict[str, TopDirectionRule] = {}
    for r in cfg.top_direction_rules:
        key = r.source.casefold()
        if r.enabled and key not in top_by_source:
            top_by_source[key] = r
    semester_dirs: Dict[Tuple[str, str], SemesterDirRule] = {}
    for r in cfg.semester_dir_rules:
        key = (r.semester.casefold(), r.source.casefold())
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
        long_term=[r for r in cfg.long_term_rules if r.enabled],
    )


_CJK_RE = re.compile(r"[\u3400-\u9fff]")

# 常见教学日历学期写法（S2-24）：命中即视为学期目录，学期标签用目录名本身
_SEMESTER_NAME_RE = re.compile(
    r"^(?:"
    r"20\d{2}\s*[-_—~至]\s*20\d{2}\s*[-_]\s*[12]"
    r"|20\d{2}\s*[-_—~]?\s*20\d{2}\s*学年\s*(?:第)?\s*[一二12]\s*学期"
    r"|20\d{2}\s*学年?\s*(?:第)?\s*[一二12]\s*学期"
    r"|20\d{2}\s*[春秋]\s*(?:学期)?"
    r"|20\d{2}\s*[-_]\s*[12]\s*(?:学期)?"
    r"|大[一二三四]\s*[上下]"
    r")$"
)


def looks_like_semester(name: str) -> bool:
    """目录名是否像教学日历里的学期（2024-2025-1 / 2024秋 / 大二上）。"""
    text = (name or "").strip()
    return bool(text) and _SEMESTER_NAME_RE.match(text) is not None


def keyword_matches(text_lower: str, keyword: str) -> bool:
    """关键词匹配（S2-14/15）。

    - 含中文等非 ASCII 字符的关键词：直接子串匹配——
      否则「高数2」「线代A」这类"课名+班号"永远命中不了；
    - 纯 ASCII 关键词：仍按词边界，避免 ai 命中 training/main/email；
      右侧允许紧跟数字（python 能命中 python3），左侧不允许字母数字。
    """
    kw_lower = (keyword or "").lower()
    if not kw_lower:
        return False
    if not kw_lower.isascii() or _CJK_RE.search(kw_lower):
        return kw_lower in text_lower
    pattern = r"(?<![a-z0-9])" + re.escape(kw_lower) + r"(?![a-z])"
    return re.search(pattern, text_lower) is not None


def _match_english_word(text_lower: str, keyword: str) -> bool:
    """兼容旧名：实际走 keyword_matches（保留英文词边界行为）。"""
    return keyword_matches(text_lower, keyword)


# ======================== 长期修正规则匹配（V1.5） ========================

@dataclass
class LongTermMatch:
    """一次长期规则命中的结果（规则 + 可读依据）。"""
    rule: LongTermRule
    evidence: str


def _split_rel(value: str) -> List[str]:
    """把相对路径（接受 / 与 \\）拆成非空组件。"""
    return [p for p in re.split(r"[/\\]", value or "") if p]


def _norm_rel(value: str) -> str:
    """规范化为平台分隔符形式，用于精确比较。"""
    return os.sep.join(_split_rel(value))


def match_long_term_rule(rel_path: str,
                         compiled: CompiledConfig,
                         extra_text: str = "") -> Optional[LongTermMatch]:
    """按配置顺序返回第一条命中的长期规则；未命中返回 None。

    顺序即优先级：调用方（classify_entries）取第一条命中后即覆盖原有分类。
    目录匹配一律按**路径组件边界**比较，所以「课程A」不会命中「课程AB」。
    """
    if not compiled.long_term:
        return None
    dirs = _split_rel(rel_path)[:-1]
    filename = (os.path.basename(_norm_rel(rel_path)) or "")
    filename_lower = filename.lower()
    target_norm = _norm_rel(rel_path)

    for rule in compiled.long_term:
        scope = _split_rel(rule.source_path)
        if rule.match_type == MATCH_EXACT_PATH:
            if target_norm == _norm_rel(rule.source_path):
                return LongTermMatch(
                    rule, f"精确路径「{_norm_rel(rule.source_path)}」")
            continue

        if rule.match_type == MATCH_DIR_SCOPE:
            if len(dirs) < len(scope):
                continue
            if dirs[:len(scope)] != scope:
                continue
            if not rule.include_subdirs and len(dirs) != len(scope):
                continue
            label = ("来源根目录" if not scope
                     else "来源目录「" + os.sep.join(scope) + "」")
            label += "（含子目录）" if rule.include_subdirs else "（仅本级）"
            return LongTermMatch(rule, label)

        if rule.match_type == MATCH_FILENAME_KEYWORD:
            if scope:
                if len(dirs) < len(scope) or dirs[:len(scope)] != scope:
                    continue
            where = (f"，限定目录「{os.sep.join(scope)}」" if scope else "")
            for kw in rule.keywords:
                if _match_english_word(filename_lower, kw):
                    return LongTermMatch(
                        rule, f"文件名关键词「{kw}」{where}")
            extra_lower = (extra_text or "").lower()
            if extra_lower:
                for kw in rule.keywords:
                    if _match_english_word(extra_lower, kw):
                        return LongTermMatch(
                            rule, f"图片文字关键词「{kw}」{where}")
    return None


def _ocr_mark(source: str) -> str:
    """关键词来自图片文字时，在分类依据末尾标明。"""
    return "（图片文字）" if source == "image" else ""


def _keyword_matches(rule: KeywordRule, rel_path: str,
                     filename: str, extra_text: str = "") -> Tuple[Optional[str], str]:
    """返回 (命中的关键词, 来源)。来源是 name 或 image；未命中时关键词为 None。

    文件名或路径先匹配。只有那边没有命中时，才看调用方传来的图片文字。
    """
    text = (filename if rule.scope == SCOPE_FILENAME else rel_path).lower()
    for kw in rule.keywords:
        if _match_english_word(text, kw):
            return kw, "name"
    extra = (extra_text or "").lower()
    if extra:
        for kw in rule.keywords:
            if _match_english_word(extra, kw):
                return kw, "image"
    return None, ""


def _pick_keyword(rules, rel_path: str, filename: str, extra_text: str = ""):
    """按列表顺序选关键词。文件名或路径命中优先于图片文字。

    返回 (从 1 计的序号, 规则, 关键词, 来源)；都未命中时返回 None。
    """
    image_hit = None
    for i, rule in enumerate(rules, 1):
        kw, source = _keyword_matches(rule, rel_path, filename, extra_text)
        if not kw:
            continue
        if source != "image":
            return i, rule, kw, source
        if image_hit is None:
            image_hit = (i, rule, kw, source)
    return image_hit


_OUTPUT_TYPES = (
    ("软著", ("软件著作权", "著作权登记", "软著")),
    ("专利", ("发明专利", "实用新型专利", "外观设计专利", "专利申请",
             "专利交底", "权利要求书", "专利证书", "实用新型", "专利")),
    ("论文", ("期刊论文", "会议论文", "学术论文", "论文投稿", "论文录用",
             "论文初稿", "小论文", "论文")),
)
_THESIS_MARKERS = ("毕业设计", "毕业论文", "学位论文", "毕设", "开题报告")
_NOT_PROJECT_DIR = {
    "文档", "材料", "申请", "提交", "附件", "扫描件", "最终版", "草稿", "定稿",
    "备份", "新建文件夹", "资料", "申报", "申请材料", "交底", "交底书", "证书",
    "受理", "授权", "初稿", "修改稿", "终稿", "查重", "录用", "投稿", "登记",
    "盖章", "签字", "版本", "扫描",
}


def _output_type_in(text: str):
    """从目录名或文件名里认出软著、专利、论文。短名必须整段相等，避免「专利法」被当成专利。"""
    best = None
    for type_name, markers in _OUTPUT_TYPES:
        for marker in markers:
            if marker == "专利":
                hit = text == marker
            else:
                hit = marker in text
            if hit and (best is None or len(marker) > len(best[1])):
                best = (type_name, marker)
    return best


def _project_output_match(rel_path: str, compiled: CompiledConfig):
    """软著、专利、论文分开；路径里能看出项目名时，按项目各放一处。

    已经由学期课程目录或顶层「项目」目录认走的文件不会进这里。
    毕业论文、学位论文留给毕业设计规则。
    """
    if any(marker in rel_path for marker in _THESIS_MARKERS):
        return None
    parts = [part for part in rel_path.split(os.sep) if part]
    if not parts:
        return None
    dirs = parts[:-1]
    stem = os.path.splitext(parts[-1])[0]
    type_index = None
    type_name = ""
    for index, name in enumerate(dirs):
        hit = _output_type_in(name)
        if hit is not None:
            type_index, type_name = index, hit[0]
            break
    if not type_name:
        hit = _output_type_in(stem)
        if hit is None:
            return None
        type_name = hit[0]

    def is_project(name: str) -> bool:
        if name in _NOT_PROJECT_DIR:
            return False
        if name.casefold() in compiled.semester_by_source:
            return False
        if looks_like_semester(name):
            return False
        return _output_type_in(name) is None

    project_index = None
    if type_index is not None:
        if type_index > 0 and is_project(dirs[type_index - 1]):
            project_index = type_index - 1
        elif (type_index + 1 < len(dirs)
              and is_project(dirs[type_index + 1])):
            project_index = type_index + 1
    elif dirs:
        for index, name in enumerate(dirs):
            if is_project(name):
                project_index = index
                break

    if project_index is None:
        category = type_name
        rest = [name for index, name in enumerate(dirs) if index != type_index]
        desc = f"{type_name}。路径里没有项目名，先单独放在「{type_name}」。"
    else:
        category = dirs[project_index]
        rest = [
            name for index, name in enumerate(dirs)
            if index not in (project_index, type_index)
        ]
        rest = [type_name] + rest
        desc = f"{type_name}，项目「{category}」。不同项目分开，三类材料也分开。"
    return ContextMatch(
        direction="项目",
        semester="",
        category=category,
        anchor_index=-1,
        rule_desc=desc,
        rule_key=f"project_output:{type_name}",
        fixed_inner=os.sep.join(rest),
    )


def _unmatched_fallback(compiled: CompiledConfig, rel_path: str,
                        filename: str, base_reason: str,
                        semester: str = "", anchor: int = -1,
                        category: str = "",
                        extra_text: str = "") -> ContextMatch:
    """待定兜底：检查 trigger=unmatched 的关键词规则。

    未命中关键词时保持调用方给出的原始锚点与类别（与 V1.1 一致）。
    extra_text 只参与关键词，不参与目录判断。文件名已经能命中时，
    不用图片文字改判。

    S2-16：已经落在某个已识别目录（anchor >= 0，例如学期内的课程目录）下时，
    不再被「软著/专利/论文」的项目输出规则抢走——否则同一课程目录会被
    拆到「项目/论文」和「课程」两处。只有还没有目录锚点的文件才做项目识别。
    """
    if anchor < 0:
        output = _project_output_match(rel_path, compiled)
        if output is not None:
            return output
    hit = _pick_keyword(
        compiled.keyword_unmatched, rel_path, filename, extra_text)
    if hit is not None:
        i, rule, kw, source = hit
        return ContextMatch(
            direction=rule.direction,
            semester=semester,
            category=rule.target,
            anchor_index=anchor,
            rule_desc=(
                f"关键词兜底「{kw}」→ {rule.direction}/{rule.target}"
                f"{_ocr_mark(source)}"
            ),
            rule_key=f"keyword_unmatched:{i}:{kw}",
        )
    return ContextMatch("待定", semester, category, anchor, base_reason,
                        "fallback")


def _classify_context_impl(rel_path: str, compiled: CompiledConfig,
                           extra_text: str = "") -> ContextMatch:
    """按配置对单个相对路径分类。

    匹配顺序（固定、可解释）：
      1. 学期映射：按路径组件从上到下找第一个已登记学期目录；
      2. 非学期时：顶层方向映射（parts[0] 精确匹配）；
      3. 学期内一级目录映射（含类别留空 → 关键词细分）；
      4. 通用目录前缀（未登记的学期内目录）；
      5. 未登记的学期内目录先查未匹配关键词（含竞赛名单），命中则用该规则；
      6. 仍未命中则按目录名锚定（方向=课程）；
      7. 顶层散落、未登记顶层目录、学期根目录下的文件 → 待定
         （同一份未匹配关键词，含竞赛名单）。
    锚点 anchor_index 指向最后一个已确定类别的目录组件；
    inner_path = parts[anchor_index + 1:-1]，文件名永不参与目录锚定。
    """
    parts = rel_path.split(os.sep)
    filename = parts[-1] if parts else ""
    dir_parts = parts[:-1]
    text_lower = rel_path.lower()

    # 1) 学期目录（与 V1.1 一致：按路径组件顺序找第一个命中）
    #    S2-25：配置的学期源按 casefold 比较（DAA/daa 视为同一学期目录）；
    #    S2-24：未登记但形如 2024-2025-1 / 2024秋 / 学年第一学期 的目录
    #    也识别为学期，学期标签使用目录名本身，不再静默落到待定。
    sem_index, sem_rule, sem_label = -1, None, ""
    for i, p in enumerate(dir_parts):
        rule = compiled.semester_by_source.get(p.casefold())
        if rule is not None:
            sem_index, sem_rule, sem_label = i, rule, rule.semester
            break
    if sem_index < 0:
        for i, p in enumerate(dir_parts):
            if looks_like_semester(p):
                sem_index, sem_label = i, p.strip()
                break

    # 2) 顶层方向（非学期场景）
    if sem_index < 0 and parts:
        top = parts[0]
        top_rule = compiled.top_by_source.get(top.casefold())
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
                compiled, rel_path, filename, "顶层散落文件",
                extra_text=extra_text)
        return _unmatched_fallback(
            compiled, rel_path, filename, f"未登记顶层目录「{top}」",
            category=top, extra_text=extra_text)

    # 3) 学期内一级目录
    if sem_index >= 0:
        child_idx = sem_index + 1
        child_dir = parts[child_idx] if child_idx < len(dir_parts) else None
        # 目录映射按「学期标签」关联（默认配置中标签与来源目录同名，
        # 自定义学期标签不同名时也能命中）
        rule = (compiled.semester_dirs.get(
                    (sem_label.casefold(), child_dir.casefold()))
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
            # 文件名或路径里的关键词优先于图片文字。
            picked = _pick_keyword(
                compiled.keyword_subdir, rel_path, filename, extra_text)
            if picked is not None:
                i, kw_rule, kw, source = picked
                # 类别留空时，方向和目标类别都以命中关键词规则为准；
                # 目录映射的方向只用于「关键词未命中」时的兜底。
                return ContextMatch(
                    kw_rule.direction, sem_label, kw_rule.target, child_idx,
                    f"学期「{sem_label}」/「{child_dir}」+关键词「{kw}」"
                    f"→ {kw_rule.direction}/{kw_rule.target}"
                    f"{_ocr_mark(source)}",
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
            # 5) 未登记目录：竞赛名单和未匹配关键词优先于「一律当课程」
            hit = _unmatched_fallback(
                compiled, rel_path, filename,
                f"学期「{sem_label}」/「{child_dir}」",
                semester=sem_label, anchor=child_idx, category=child_dir,
                extra_text=extra_text,
            )
            if hit.rule_key != "fallback":
                return hit
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
            category=parts[sem_index], extra_text=extra_text)

    return _unmatched_fallback(
        compiled, rel_path, filename, "无上下文", extra_text=extra_text)


def _circ_to_jizu(rel_path: str, match: ContextMatch) -> ContextMatch:
    """Logisim 电路文件直接归计算机组成原理。

    已经由「计组」目录认出的文件保持原锚点，子目录不会被打平。
    其余 .circ 使用路径里的学期；路径里没有学期时用大二上。
    """
    filename = os.path.basename(rel_path)
    if not filename.lower().endswith(".circ"):
        return match
    if (match.rule_key.startswith("semester_dir:")
            and match.category == "计算机组成原理"):
        return match
    parent = os.path.dirname(rel_path)
    if parent in ("", "."):
        parent = ""
    # 锚点已经能留下子目录时沿用锚点。没有锚点时，把现有目录整段留在计组下面。
    fixed = parent if match.anchor_index < 0 and parent else None
    return ContextMatch(
        direction="课程",
        semester=match.semester or "大二上",
        category="计算机组成原理",
        anchor_index=match.anchor_index,
        rule_desc="Logisim 电路文件（.circ）→ 课程/计算机组成原理",
        rule_key="ext:circ",
        fixed_inner=fixed,
    )


def classify_context(rel_path: str, compiled: CompiledConfig,
                     extra_text: str = "") -> ContextMatch:
    """按配置分类；.circ 在目录规则没有认出计组时，直接归计算机组成原理。

    extra_text 是图片里识别出的文字。它只参与关键词，不参与目录组件匹配。
    """
    return _circ_to_jizu(
        rel_path, _classify_context_impl(rel_path, compiled, extra_text))
