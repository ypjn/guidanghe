"""
归档盒 V0.2 — 整理计划模块

职责：在 scanner 结果之上，根据分类规则生成固定的整理计划。
纯逻辑模块，零副作用，不创建任何文件或目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from scanner import ScanResult, FileEntry, classify_type
from utils import sha256_file


# ======================== 分类规则 ========================

@dataclass
class CourseRule:
    """一条课程关键词规则。"""
    keywords: List[str]   # 小写关键词列表，任一命中即触发
    course_name: str       # 目标课程名
    order: int = 0         # 在界面上的显示顺序（越小越优先）

    def matches(self, filename_lower: str) -> bool:
        return any(kw in filename_lower for kw in self.keywords)

    def describe(self) -> str:
        """人类可读的规则描述。"""
        kws = " / ".join(self.keywords)
        return f"[{self.order}] 关键词「{kws}」→ {self.course_name}"


# 默认课程规则（用户可编辑）
DEFAULT_COURSE_RULES: List[CourseRule] = [
    CourseRule(keywords=["高数", "高等数学", "calculus"],
               course_name="高等数学", order=1),
    CourseRule(keywords=["aigc", "生成式人工智能", "ai"],
               course_name="AIGC", order=2),
    CourseRule(keywords=["线代", "线性代数", "linear"],
               course_name="线性代数", order=3),
    CourseRule(keywords=["概率", "统计", "probability"],
               course_name="概率论与统计", order=4),
    CourseRule(keywords=["物理", "physics"],
               course_name="大学物理", order=5),
    CourseRule(keywords=["英语", "english", "essay"],
               course_name="大学英语", order=6),
]

DEFAULT_UNCLASSIFIED = "未分类课程"

# 目标目录固定名
TARGET_ROOT = "_已整理"


# ======================== 分类模式 ========================

MODE_BY_TYPE = "by_type"           # 按类型：_已整理/{类型}/
MODE_BY_COURSE_THEN_TYPE = "by_course_then_type"  # 按课程再按类型：_已整理/{课程}/{类型}/
MODE_BY_DIRECTION_SEMESTER = "by_direction_semester"  # 按方向→学期→课程/类别，保留内部结构

MODE_LABELS = {
    MODE_BY_TYPE: "按类型",
    MODE_BY_COURSE_THEN_TYPE: "按课程再按类型",
    MODE_BY_DIRECTION_SEMESTER: "按方向/学期/类别（推荐）",
}

ALL_MODES = [MODE_BY_TYPE, MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER]


# ======================== 计划条目 ========================

@dataclass
class PlanItem:
    """整理计划中的单个文件条目。"""
    # 源信息（必填）
    file: FileEntry
    # 分类结果（必填）
    course: str                    # 课程名（按类型模式下为空字符串）
    file_type: str                 # 类型（复用 scanner 的 file_type）
    rule_applied: str              # 命中规则描述
    # 目标信息（必填）
    target_dir_rel: str            # 目标目录相对路径（不含文件名）
    target_filename: str           # 目标文件名（含扩展名），可能带 (1) (2)
    target_rel_path: str           # 目标完整相对路径 = target_dir_rel + "/" + target_filename
    # 可选字段（有默认值）
    rule_index: Optional[int] = None  # 命中规则的顺序号，None 表示未命中
    conflict_with_existing: bool = False  # 规划时目标路径已存在同名文件
    selected: bool = True          # 用户是否勾选
    # V0.4 新增：重复组信息（None 表示不是重复的）
    dup_group_id: Optional[str] = None   # 内容重复组标识，如 "g1" "g2"
    dup_count: int = 1                     # 同组有多少个文件（含自己）
    source_sha256: str = ""                # 源文件 SHA-256（供 executor 执行前核验）
    # V1.1 新增：上下文分类（方向/学期/类别/内部路径）
    # 仅在 MODE_BY_DIRECTION_SEMESTER 模式下填充，其他模式为 None / ""
    direction: Optional[str] = None   # 方向（课程 / 竞赛 / 项目 / 校园事务 / 待定）
    semester: Optional[str] = None    # 学期（2026秋-大二上 等，非课程方向为空）
    category: Optional[str] = None    # 具体类别（如"数据结构"、"APMCM 数学建模"）
    inner_path: Optional[str] = None  # 锚点之后的内部相对路径（用于保留目录内部结构）

    @property
    def has_duplicates(self) -> bool:
        return self.dup_count > 1

    @property
    def target_display(self) -> str:
        return f"{TARGET_ROOT}{os.sep}{self.target_dir_rel.replace('_已整理'+os.sep, '')}{os.sep}{self.target_filename}"


# ======================== 固定计划 ========================

@dataclass
class OrganizePlan:
    """一次固定的整理计划。"""
    root: str                              # 扫描根目录
    mode: str                              # 分类模式
    rules: List[CourseRule]                # 使用的规则
    items: List[PlanItem] = field(default_factory=list)
    # V0.4 新增：重复组信息
    dup_groups: dict = field(default_factory=dict)  # group_id → [rel_path, ...]
    dup_total_files: int = 0                          # 进入重复组的文件总数

    # ---- 汇总 ----
    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def selected_count(self) -> int:
        return sum(1 for i in self.items if i.selected)

    @property
    def conflict_count(self) -> int:
        return sum(1 for i in self.items if i.conflict_with_existing)

    def summary(self) -> str:
        parts = [f"计划 {self.total} 个文件，已选 {self.selected_count}"]
        if self.conflict_count:
            parts.append(f"目标冲突 {self.conflict_count}")
        # 按课程/类型汇总
        if self.mode == MODE_BY_COURSE_THEN_TYPE:
            courses = {}
            for i in self.items:
                courses[i.course] = courses.get(i.course, 0) + 1
            parts.append("课程分布：" + "，".join(f"{k} {v}" for k, v in courses.items()))
        else:
            types = {}
            for i in self.items:
                types[i.file_type] = types.get(i.file_type, 0) + 1
            parts.append("类型分布：" + "，".join(f"{k} {v}" for k, v in types.items()))
        return " | ".join(parts)


# ======================== 计划生成核心 ========================

def _match_course(filename: str, rules: List[CourseRule]) -> Tuple[str, Optional[int], str]:
    """
    根据文件名匹配第一条命中的课程规则。

    返回：(课程名, 规则序号, 规则描述)
    - 未命中时：("未分类课程", None, "未匹配任何规则")
    """
    fn_lower = filename.lower()
    # 按 order 排序，保证显示顺序即优先级顺序
    sorted_rules = sorted(rules, key=lambda r: r.order)
    for idx, rule in enumerate(sorted_rules):
        if rule.matches(fn_lower):
            return rule.course_name, rule.order, rule.describe()
    return DEFAULT_UNCLASSIFIED, None, "未匹配任何规则"


def _sanitize_component(name: str) -> str:
    """
    清理目录/文件名中的非法字符（跨平台）。
    macOS 对 : 和 / 特殊敏感；Windows 对 <>:"/\\|?* 敏感。
    """
    # 去除路径分隔符和常见非法字符
    for ch in '<>:"/\\|?*\x00':
        name = name.replace(ch, "_")
    # 去除首尾空白和点（Windows 限制）
    name = name.strip().strip(".")
    return name or "_"


def _build_target_dir(mode: str, course: str, file_type: str) -> str:
    """
    根据模式构造目标目录的**相对路径**（相对于 root）。
    固定以 TARGET_ROOT = "_已整理" 开头。
    """
    if mode == MODE_BY_TYPE:
        return f"{TARGET_ROOT}/{_sanitize_component(file_type)}"
    elif mode == MODE_BY_COURSE_THEN_TYPE:
        return f"{TARGET_ROOT}/{_sanitize_component(course)}/{_sanitize_component(file_type)}"
    else:
        raise ValueError(f"未知模式: {mode}")


def _assign_unique_names(
    items_so_far: List[PlanItem],
    root: str,
    new_item: PlanItem,
) -> PlanItem:
    """
    为 new_item 分配唯一的 target_filename。

    冲突来源：
    1. 同一目标目录下，本批次已有文件占用此名称
    2. 磁盘上已存在的文件（包括 V0.1 扫描到的本批次文件之外的，但
       注意 scanner 已跳过 _已整理/ 目录，所以磁盘检查只需要对非
       计划来源的目标文件做）

    分配策略：首选原名 → 冲突则加 " (1)" → " (2)" ...
    返回已修改好 target_filename 和 target_rel_path 的 PlanItem。
    """
    # 收集已占用名称（本批次）
    used_in_plan = {
        os.path.join(i.target_dir_rel, i.target_filename)
        for i in items_so_far
    }

    # 收集磁盘上已存在的目标路径（只针对非 _已整理 目录中可能已有的
    # 同名目标——实际上 V0.1  scanner 已跳过 _已整理，所以这一步很重要）
    disk_exists = lambda rel: os.path.exists(os.path.join(root, rel))

    base_name = new_item.target_filename  # 完整文件名，如 "实验报告.docx"
    name_no_ext, ext = os.path.splitext(base_name)

    candidate = base_name
    counter = 1
    # 循环直到找到可用名
    while True:
        cand_rel = os.path.join(new_item.target_dir_rel, candidate)
        already_in_plan = cand_rel in used_in_plan
        already_on_disk = disk_exists(cand_rel)
        if not already_in_plan and not already_on_disk:
            break
        if already_on_disk and not already_in_plan:
            new_item.conflict_with_existing = True
        # 生成下一个候选
        candidate = f"{name_no_ext} ({counter}){ext}"
        counter += 1

    new_item.target_filename = candidate
    new_item.target_rel_path = os.path.join(new_item.target_dir_rel, candidate)
    return new_item


def detect_duplicates(
    scan_result: ScanResult,
    cancel_event=None,
    progress_cb=None,
    progress_base: int = 0,
    progress_total: int = 0,
) -> Tuple[dict, dict]:
    """
    按 (size, SHA-256) 检测内容重复组。

    算法：先按 size 分桶（快速剪枝），桶内再计算 SHA-256 精确比对。
    同名不同内容 → 不同 size 或不同 SHA → 不会进同组。
    不同名同内容 → 同 size + 同 SHA → 进同组。

    支持 cancel_event 和 progress_cb（供后台线程使用）。

    返回：(dup_groups, file_sha_map)
      - dup_groups: Dict[str, List[str]]   — group_id → 同组文件的 rel_path 列表
      - file_sha_map: Dict[str, str]       — rel_path → sha256（供 executor 复用）
    """
    root = os.path.abspath(scan_result.root)

    # Step 1: 按 size 分桶（快速剪枝——size 不同不可能同内容）
    size_buckets: dict = {}  # size → List[FileEntry]
    for f in scan_result.files:
        size_buckets.setdefault(f.size, []).append(f)

    # Step 2: 对 size >= 2 的桶内算 SHA-256 精确比对
    dup_groups: dict = {}
    file_sha_map: dict = {}
    group_counter = 0

    file_count = len(scan_result.files)
    idx = 0

    for size, bucket in size_buckets.items():
        if cancel_event and cancel_event.is_set():
            break

        if len(bucket) < 2:
            # 只有一个文件的桶不可能有重复，但仍记录 SHA（executor 用）
            f = bucket[0]
            if cancel_event and cancel_event.is_set():
                break
            sha = sha256_file(os.path.join(root, f.rel_path)) or ""
            file_sha_map[f.rel_path] = sha
            idx += 1
            if progress_cb and progress_total > 0:
                progress_cb(progress_base + idx, progress_total, f.rel_path)
            continue

        # 桶内按 SHA-256 分组
        sha_to_files: dict = {}
        for f in bucket:
            if cancel_event and cancel_event.is_set():
                break
            sha = sha256_file(os.path.join(root, f.rel_path)) or ""
            file_sha_map[f.rel_path] = sha
            sha_to_files.setdefault(sha, []).append(f)
            idx += 1
            if progress_cb and progress_total > 0:
                progress_cb(progress_base + idx, progress_total, f.rel_path)

        # 只保留组内 >= 2 个文件的
        for sha, group in sha_to_files.items():
            if len(group) >= 2:
                group_id = f"g{group_counter + 1}"
                dup_groups[group_id] = [f.rel_path for f in group]
                group_counter += 1

    return dup_groups, file_sha_map


def generate_plan(
    scan_result: ScanResult,
    mode: str,
    rules: List[CourseRule],
    cancel_event=None,
    progress_cb=None,
) -> OrganizePlan:
    """
    根据扫描结果、分类模式和课程规则，生成固定整理计划。

    核心保证：
    - 目标路径永远在 root/_已整理/ 下，不越界
    - 同名冲突在计划生成时一次性全部解决（加 " (1)" " (2)" ...）
    - 按源文件相对路径排序，保证稳定可重复
    - 零副作用：不创建任何文件或目录

    支持 cancel_event 和 progress_cb（供后台线程使用）。
    取消后如果已计算出部分结果，函数会返回一个半成品 plan，
    调用方需检查 cancel_event 来决定是否接受。
    """
    if mode not in ALL_MODES:
        raise ValueError(f"未知分类模式: {mode}")

    root = os.path.abspath(scan_result.root)
    sorted_rules = sorted(rules, key=lambda r: r.order) if rules else []

    # V0.4：先做重复检测（顺便算好每个源文件的 SHA-256，executor 复用）
    # 第一步：SHA 计算（进度 0 → len(files)）
    dup_groups, file_sha_map = detect_duplicates(
        scan_result, cancel_event=cancel_event, progress_cb=progress_cb,
        progress_base=0, progress_total=len(scan_result.files),
    )
    if cancel_event and cancel_event.is_set():
        # 取消了，但仍返回已有的半成品（让调用方决定是否丢弃）
        plan = OrganizePlan(root=root, mode=mode, rules=sorted_rules,
                            dup_groups=dup_groups,
                            dup_total_files=sum(len(v) for v in dup_groups.values()))
        return plan

    # 反向索引：rel_path → (group_id, group_size)
    rel_to_group: dict = {}
    for gid, paths in dup_groups.items():
        for p in paths:
            rel_to_group[p] = (gid, len(paths))

    plan = OrganizePlan(root=root, mode=mode, rules=sorted_rules,
                        dup_groups=dup_groups,
                        dup_total_files=sum(len(v) for v in dup_groups.values()))

    # 源文件已在 scanner.py 中按 rel_path 排序，天然稳定
    for idx, fentry in enumerate(scan_result.files):
        if cancel_event and cancel_event.is_set():
            break

        # 1. 匹配分类信息
        if mode == MODE_BY_COURSE_THEN_TYPE:
            course, rule_idx, rule_desc = _match_course(fentry.filename, sorted_rules)
            direction = semester = category = inner_path = ""
            anchor_index = -1
        elif mode == MODE_BY_DIRECTION_SEMESTER:
            direction, semester, category, anchor_index, rule_desc = \
                _match_by_context(fentry.rel_path)
            # PlanItem.course 用 category 填充，保持旧字段兼容
            course = category or direction
            rule_idx = None
            # anchor_index 指向最后一个已确定类别的目录节点。
            # inner_path = 锚点之后、文件名之前的所有目录层级；文件名不在其中。
            parts = fentry.rel_path.split(os.sep)
            if anchor_index >= 0 and anchor_index + 1 < len(parts) - 1:
                inner_path = os.sep.join(parts[anchor_index + 1:-1])
            else:
                inner_path = ""
        else:
            # MODE_BY_TYPE
            course = ""
            rule_idx = None
            rule_desc = "按类型模式"
            direction = semester = category = inner_path = ""
            anchor_index = -1

        ftype = fentry.file_type
        if mode == MODE_BY_DIRECTION_SEMESTER:
            target_dir = _build_target_dir_v2(direction, semester, category, ftype)
            # inner_path 的目录部分拼到 target_dir 之后
            if inner_path:
                target_dir = os.path.join(target_dir, inner_path)
        else:
            target_dir = _build_target_dir(mode, course, ftype)

        # V0.4：重复组信息
        gid, gsize = rel_to_group.get(fentry.rel_path, (None, 1))
        sha_val = file_sha_map.get(fentry.rel_path, "")

        item = PlanItem(
            file=fentry,
            course=course,
            file_type=ftype,
            rule_applied=rule_desc,
            rule_index=rule_idx,
            target_dir_rel=target_dir,
            target_filename=fentry.filename,
            target_rel_path=os.path.join(target_dir, fentry.filename),
            conflict_with_existing=False,
            dup_group_id=gid,
            dup_count=gsize,
            source_sha256=sha_val,
            direction=direction or None,
            semester=semester or None,
            category=category or None,
            inner_path=inner_path or None,
        )

        item = _assign_unique_names(plan.items, root, item)
        plan.items.append(item)

    return plan


# ======================== 解析课程规则文本 ========================

def parse_rules_text(text: str) -> List[CourseRule]:
    """
    从用户输入文本解析课程规则。

    支持格式（每行一条）：
      高数,高等数学 → 高等数学
      AIGC;生成式人工智能 → AIGC
      高数/高等数学/calculus → 高等数学

    分隔符支持 ,  ;  /  |  和中文逗号。
    → 前后可有空格。按出现顺序确定优先级。
    """
    rules: List[CourseRule] = []
    sep_pattern = ",;，/|"

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "→" in line:
            parts = line.split("→", 1)
        elif "->" in line:
            parts = line.split("->", 1)
        else:
            continue

        keywords_str = parts[0].strip()
        course_name = parts[1].strip()

        # 分割关键词
        keywords = []
        # 依次尝试每种分隔符
        for sep in [",", ";", "，", "/", "|"]:
            if sep in keywords_str:
                keywords = [k.strip() for k in keywords_str.split(sep) if k.strip()]
                break
        if not keywords:
            keywords = [keywords_str.strip()]

        if not course_name or not keywords:
            continue

        rules.append(CourseRule(keywords=keywords, course_name=course_name, order=lineno))

    return rules


def rules_to_text(rules: List[CourseRule]) -> str:
    """将课程规则列表转回用户可编辑的文本。"""
    sorted_rules = sorted(rules, key=lambda r: r.order)
    lines = []
    for r in sorted_rules:
        lines.append(f"{','.join(r.keywords)} → {r.course_name}")
    return "\n".join(lines)


# ======================== V1.1 上下文分类（方向/学期/类别） ========================

# 学期映射：目录名 → 学期标注
DEFAULT_SEMESTER_MAP: dict = {
    "大一上": "大一上",
    "大一下": "大一下",
    "大二上": "大二上",
    "大二下": "大二下",
    "大三上": "大三上",
    "大三下": "大三下",
    "大四上": "大四上",
    "大四下": "大四下",
}

# 顶层目录 → 方向（非学期、非显式登记的顶层）
DEFAULT_TOP_DIRECTION: dict = {
    "校园事务": "校园事务",
    "Code": "项目",
    "个人项目灵感": "项目",
    "大创答辩": "项目",
}

# 学期内一级目录 → (方向, 类别)
# key = 学期目录名（如 "大二上"），value = {一级目录名: (方向, 类别)}
# 类别为 None 时表示需要进一步关键词细分（如"已完成期末"）
DEFAULT_SEMESTER_DIRS: dict = {
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

# 关键词→课程映射（用于"已完成期末"等需子路径细分的目录）
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

# 通用目录锚定（未登记的一级目录 → 方向）
# key = 一级目录名（精确匹配，中文），value = 方向
DEFAULT_GENERIC_DIR: dict = {
    "周报": "校园事务",
    "附件-": "校园事务",  # 前缀匹配
    "已完成期末": "课程",
}


def _match_english_word(text_lower: str, keyword: str) -> bool:
    """英文关键词的词边界匹配，避免子串误命中。

    例如 keyword="ai" 不应命中 "training"（tr-ai-ning）。
    规则：keyword 全为 ASCII 字母时使用词边界；中文或含非字母时退化为包含匹配。
    """
    import re
    kw_lower = keyword.lower()
    if kw_lower.isalpha():
        pattern = r"(?<![a-z0-9])" + re.escape(kw_lower) + r"(?![a-z0-9])"
        return re.search(pattern, text_lower) is not None
    return kw_lower in text_lower


def _match_by_context(
    rel_path: str,
    semester_map: Optional[dict] = None,
    top_direction: Optional[dict] = None,
    semester_dirs: Optional[dict] = None,
    keyword_course: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[str, str, str, int, str]:
    """根据路径上下文匹配 (方向, 学期, 类别, 锚点索引, 规则描述)。

    锚点索引 anchor_index：
      - 指向 rel_path 中最后一个"已确定类别"的**目录组件**的下标。
        inner_path = parts[anchor_index + 1: -1] （锚点之后、文件名之前）
        文件名永远在 parts[-1]，绝不参与目录锚定或 category 取值。
      - -1 表示无锚点。

    category 永远是目录名（或其规范名），不会是文件名。
    未命中时 direction="待定"。
    """
    semester_map = semester_map or DEFAULT_SEMESTER_MAP
    top_direction = top_direction or DEFAULT_TOP_DIRECTION
    semester_dirs = semester_dirs or DEFAULT_SEMESTER_DIRS
    keyword_course = keyword_course or DEFAULT_KEYWORD_TO_COURSE

    parts = rel_path.split(os.sep)
    # parts[-1] 一定是文件名；parts[:-1] 是目录路径
    dir_parts = parts[:-1]
    text_lower = rel_path.lower()

    # 1) 查找学期目录（在所有路径组件里找，包括顶层）
    sem_index, sem_label = -1, ""
    for i, p in enumerate(parts):
        if p in semester_map:
            sem_index = i
            sem_label = semester_map[p]
            break

    # 2) 顶层目录匹配（非学期场景）
    if sem_index < 0 and parts:
        top = parts[0]
        if top in top_direction:
            direction = top_direction[top]
            # 取 category 时跳过文件名：
            # - 如果顶层下还有子目录（len(dir_parts) >= 2），用第一个子目录当 category
            # - 如果顶层下直接就是文件（len(dir_parts) == 1，且 parts[0]==top），用顶层自己当 category
            if len(dir_parts) >= 2:
                cat = dir_parts[1]
                anchor = 1
            elif len(dir_parts) == 1:
                cat = top
                anchor = 0
            else:
                cat = top
                anchor = -1
            rule = f"顶层目录「{top}」→ {direction}/{cat}"
            return direction, "", cat, anchor, rule

        # 顶层散落文件（没有任何子目录锚定）
        if len(parts) == 1:
            # category 留空，避免把文件名当成目录名
            # _build_target_dir_v2 会在待定方向 + category 为空时直接拼到 _已整理/待定/
            return "待定", "", "", -1, "顶层散落文件"
        return "待定", "", top, -1, f"未登记顶层目录「{top}」"

    # 3) 学期场景：匹配学期内一级目录（sem_index 在 parts 里可能不是 0）
    if sem_index >= 0:
        # child_dir 是学期目录下的**第一个子目录**（如果存在）
        # 注意：parts[sem_index] 是学期目录名，parts[sem_index + 1] 是它下面的东西
        # 如果 sem_index + 1 < len(dir_parts)，说明学期下有子目录；否则学期下直接放文件
        child_idx = sem_index + 1
        child_dir = parts[child_idx] if child_idx < len(dir_parts) else None
        table = semester_dirs.get(parts[sem_index], {})

        if child_dir and child_dir in table:
            direction, cat = table[child_dir]
            if cat is not None:
                rule = f"学期「{sem_label}」/「{child_dir}」→ {direction}/{cat}"
                return direction, sem_label, cat, child_idx, rule

            # cat is None → 需关键词细分（如"已完成期末"）
            for kw, course in keyword_course:
                if _match_english_word(text_lower, kw):
                    rule = (f"学期「{sem_label}」/「{child_dir}」+关键词"
                            f"「{kw}」→ {direction}/{course}")
                    return direction, sem_label, course, child_idx, rule
            return direction, sem_label, f"{child_dir}（未细分）", child_idx, \
                f"学期「{sem_label}」/「{child_dir}」关键词未命中，保留目录名"

        if child_dir:
            # 未登记的学期内一级目录 → 目录锚定
            if child_dir.startswith("附件-"):
                return "校园事务", sem_label, "通知", child_idx, \
                    f"学期「{sem_label}」/「{child_dir}」→ 通知"
            return "课程", sem_label, child_dir, child_idx, \
                f"学期「{sem_label}」/「{child_dir}」（目录锚定）"

        # 学期根目录下直接放文件（无子目录）
        return "待定", sem_label, parts[sem_index], sem_index, \
            f"学期「{sem_label}」根目录下"

    return "待定", "", parts[0] if parts else "", -1, "无上下文"


def _build_target_dir_v2(
    direction: str,
    semester: str,
    category: str,
    file_type: str,
    inner_path: str = "",
) -> str:
    """MODE_BY_DIRECTION_SEMESTER 下的目标目录构造。

    规则：
      - 课程 → _已整理/课程/{学期}/{类别}/{类型}/
      - 竞赛 → _已整理/竞赛/{类别}/
      - 项目 → _已整理/项目/{类别}/
      - 校园事务 → _已整理/校园事务/{类别}/{类型}/
      - 待定 → _已整理/待定/（category 为空时，顶层散落文件直接拼文件名到这里）
               _已整理/待定/{类别}/（category 不为空时，未登记目录等场景）
    inner_path 会在调用方拼接时附加到类型目录之后，这里不处理。
    """
    s = _sanitize_component
    cat = s(category) if category else ""

    if direction == "课程":
        base = f"{TARGET_ROOT}/{s('课程')}/{s(semester or '未识别学期')}/{cat}/{s(file_type)}"
    elif direction == "竞赛":
        base = f"{TARGET_ROOT}/{s('竞赛')}/{cat}" if cat else f"{TARGET_ROOT}/{s('竞赛')}"
    elif direction == "项目":
        base = f"{TARGET_ROOT}/{s('项目')}/{cat}" if cat else f"{TARGET_ROOT}/{s('项目')}"
    elif direction == "校园事务":
        base = f"{TARGET_ROOT}/{s('校园事务')}/{cat}/{s(file_type)}" if cat else f"{TARGET_ROOT}/{s('校园事务')}/{s(file_type)}"
    else:
        # 待定 — category 为空时直接到 _已整理/待定/，给顶层散落文件留位置
        base = f"{TARGET_ROOT}/{s('待定')}/{cat}" if cat else f"{TARGET_ROOT}/{s('待定')}"

    return base
