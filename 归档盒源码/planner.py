"""
归档盒 V0.2 — 整理计划模块

职责：在 scanner 结果之上，根据分类规则生成固定的整理计划。
纯逻辑模块，零副作用，不创建任何文件或目录。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from scanner import ScanResult, FileEntry, classify_type
from utils import sha256_file
from classification_config import (
    ClassificationConfig, CompiledConfig, classify_context, compile_config,
    match_long_term_rule,
    INNER_KEEP, INNER_CLEAR,
    default_config, DEFAULT_SEMESTER_MAP, DEFAULT_TOP_DIRECTION,
    DEFAULT_SEMESTER_DIRS, DEFAULT_KEYWORD_TO_COURSE, DEFAULT_GENERIC_DIR,
    PENDING_DIRECTION,
)


# ======================== 分类规则 ========================

@dataclass
class CourseRule:
    """一条课程关键词规则。"""
    keywords: List[str]   # 小写关键词列表，任一命中即触发
    course_name: str       # 目标课程名
    order: int = 0         # 在界面上的显示顺序（越小越优先）

    def matches(self, text_lower: str) -> bool:
        return any(kw in text_lower for kw in self.keywords)

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
    # V1.3：稳定的规则标识（命中的映射 / 兜底规则），供试运行统计与测试断言
    rule_key: str = ""
    # V1.4 单文件修正：自动分类快照（保留，恢复自动分类时用）
    auto_direction: Optional[str] = None
    auto_semester: Optional[str] = None
    auto_category: Optional[str] = None
    auto_inner_path: Optional[str] = None
    auto_rule_key: str = ""
    auto_rule_applied: str = ""
    # V1.4 单文件修正：手动修正信息（仅在当前计划生效；不写回配置）
    is_manual_overridden: bool = False
    override_note: str = ""      # 如：用户手动把课程改成"Python 进阶"
    # 图片文字：None 表示这次没有识别；空字符串表示识别过但没有文字
    ocr_text: Optional[str] = None
    ocr_error: str = ""

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
    # V1.3：本计划实际使用的分类配置快照（推荐模式）；其他模式为空字典
    config_snapshot: dict = field(default_factory=dict)
    # 试运行/预览未计算 SHA-256 时为 False，界面必须明确标注
    duplicates_computed: bool = True
    # 规则和 OCR 之后的 AI 补充分类说明；空字符串表示这次没有问 AI
    ai_note: str = ""

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

def _match_course(filename: str, rules: List[CourseRule],
                  extra_text: str = "") -> Tuple[str, Optional[int], str]:
    """
    根据文件名匹配第一条命中的课程规则。文件名都未命中时，再看图片文字。

    返回：(课程名, 规则序号, 规则描述)
    - 未命中时：("未分类课程", None, "未匹配任何规则")
    """
    fn_lower = filename.lower()
    extra_lower = (extra_text or "").lower()
    # 按 order 排序，保证显示顺序即优先级顺序
    sorted_rules = sorted(rules, key=lambda r: r.order)
    image_hit = None
    for rule in sorted_rules:
        if rule.matches(fn_lower):
            return rule.course_name, rule.order, rule.describe()
        if image_hit is None and extra_lower and rule.matches(extra_lower):
            image_hit = rule
    if image_hit is not None:
        return (image_hit.course_name, image_hit.order,
                image_hit.describe() + "（图片文字）")
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


# 没登记的目录名、相机自动编号，不能拿来当“内容重合”的证据。
_NEIGHBOR_GENERIC = {
    "images", "image", "img", "imgs", "pic", "pics", "photo", "photos",
    "picture", "pictures", "screenshot", "screenshots", "scan", "scans",
    "new", "copy", "tmp", "temp", "file", "files", "doc", "docs",
    "图片", "照片", "截图", "图像", "新建文件夹", "未命名", "副本", "其他",
    "下载", "文档", "资料", "附件", "压缩", "备份", "课程", "竞赛", "项目",
    "校园事务", "待定",
}
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{4,12}")
_CAMERA_NAME = re.compile(
    r"^(?:img|dsc|pic|photo|screenshot|image)?[_-]?\d+$", re.IGNORECASE)


def _open_unregistered(item: PlanItem) -> bool:
    """规则没认出来、还停在待定的顶层目录或散落文件。"""
    if item.rule_key != "fallback":
        return False
    text = item.rule_applied or ""
    return text == "顶层散落文件" or text.startswith("未登记顶层目录「")


def _neighbor_stem(filename: str) -> str:
    stem = os.path.splitext(filename)[0].strip().lower()
    stem = re.sub(r"\s+", "", stem)
    if not stem or stem in _NEIGHBOR_GENERIC or _CAMERA_NAME.fullmatch(stem):
        return ""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", stem))
    latin = len(re.findall(r"[a-z0-9]", stem))
    if cjk >= 3 or latin >= 4:
        return stem
    return ""


def _text_has(phrase: str, text_lower: str) -> bool:
    """中文按连续包含；英文按词边界，避免 code 撞上 encoder。"""
    needle = (phrase or "").lower()
    if not needle:
        return False
    if re.search(r"[a-z0-9]", needle) and not re.search(r"[\u4e00-\u9fff]", needle):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
            text_lower,
        ) is not None
    return needle in text_lower


def _group_unregistered_with_neighbors(items: List[PlanItem], root: str) -> None:
    """没登记的文件对照旁边已经归好的文件。

    只看文件名、图片文字和对方的类别名。对得上同一类，就归进那一类，
    并留下原来的目录，避免把整夹无关照片一起拖走。两边都能对上时不改。
    """
    for _ in range(4):
        donors: dict = {}
        for item in items:
            if _open_unregistered(item) or not item.direction or item.direction == "待定":
                continue
            if not item.category:
                continue
            key = (item.direction, item.semester or "", item.category)
            bucket = donors.setdefault(key, {"stems": set(), "phrases": set(), "sample": ""})
            stem = _neighbor_stem(item.file.filename)
            if stem:
                bucket["stems"].add(stem)
            if not bucket["sample"]:
                bucket["sample"] = item.file.filename
            blob = item.file.filename
            if item.ocr_text:
                blob = blob + "\n" + item.ocr_text[:500]
            if len(bucket["phrases"]) < 80:
                for phrase in _CJK_RUN.findall(blob):
                    if phrase not in _NEIGHBOR_GENERIC:
                        bucket["phrases"].add(phrase)
                    if len(bucket["phrases"]) >= 80:
                        break

        adopted = 0
        for item in items:
            if not _open_unregistered(item):
                continue
            pending_bits = [item.file.filename]
            if item.ocr_text:
                pending_bits.append(item.ocr_text[:2000])
            pending_text = "\n".join(pending_bits).lower()
            pending_stem = _neighbor_stem(item.file.filename)
            best = None
            best_score = 0
            second = 0
            best_token = ""
            best_sample = ""
            for (direction, semester, category), bucket in donors.items():
                score = 0
                token = ""
                if len(category) >= 2 and _text_has(category, pending_text):
                    score += 3
                    token = category
                for stem in bucket["stems"]:
                    if _text_has(stem, pending_text) or (
                            pending_stem and (
                                _text_has(pending_stem, stem)
                                or _text_has(stem, pending_stem))):
                        score += 2
                        token = token or stem
                        break
                if score < 3:
                    for phrase in bucket["phrases"]:
                        if _text_has(phrase, pending_text):
                            score += 2
                            token = token or phrase
                            break
                if score > best_score:
                    second = best_score
                    best_score = score
                    best = (direction, semester, category)
                    best_token = token
                    best_sample = bucket["sample"]
                elif score > second:
                    second = score
            if best is None or best_score < 2 or best_score <= second:
                continue
            direction, semester, category = best
            parts = item.file.rel_path.split(os.sep)
            inner = os.sep.join(parts[:-1])
            reason = (
                f"{item.rule_applied}。与旁边的「{best_sample}」重合「{best_token}」，"
                f"归入{direction}"
                + (f"/{semester}" if semester else "")
                + f"/{category}"
            )
            item.direction = direction
            item.semester = semester or None
            item.category = category
            item.course = category or direction
            item.inner_path = inner or None
            item.rule_key = f"neighbor:{direction}/{semester}/{category}"
            item.rule_applied = reason
            item.auto_direction = item.direction
            item.auto_semester = item.semester
            item.auto_category = item.category
            item.auto_inner_path = item.inner_path
            item.auto_rule_key = item.rule_key
            item.auto_rule_applied = reason
            target_dir = _build_target_dir_v2(direction, semester, category, item.file_type)
            if inner:
                target_dir = os.path.join(target_dir, inner)
            item.target_dir_rel = target_dir
            item.target_filename = item.file.filename
            item.conflict_with_existing = False
            others = [other for other in items if other is not item]
            _assign_unique_names(others, root, item)
            adopted += 1
        if adopted == 0:
            break


def classify_entries(
    scan_result: ScanResult,
    mode: str,
    rules: List[CourseRule],
    config: Optional[ClassificationConfig] = None,
    cancel_event=None,
    progress_cb=None,
    ocr_texts: Optional[Dict[str, str]] = None,
    ocr_errors: Optional[Dict[str, str]] = None,
) -> Tuple[List[PlanItem], Optional[CompiledConfig]]:
    """按模式与显式配置分类，并构造目标目录（不做 SHA / 重复检测）。

    这是正式计划与规则试运行共用的唯一分类 + 路径构造入口：
    - 推荐模式使用传入的配置（None 时使用内置默认配置）；
    - 按类型 / 按课程再按类型保持原有行为，不受分类配置影响；
    - 每个条目都带 rule_applied（可读依据）与 rule_key（稳定规则标识）；
    - 零副作用：只读磁盘判断同名冲突，不创建任何文件或目录。
    """
    if mode not in ALL_MODES:
        raise ValueError(f"未知分类模式: {mode}")

    root = os.path.abspath(scan_result.root)
    sorted_rules = sorted(rules, key=lambda r: r.order) if rules else []
    compiled = (compile_config(config)
                if mode == MODE_BY_DIRECTION_SEMESTER else None)

    items: List[PlanItem] = []
    total = len(scan_result.files)

    # 源文件已在 scanner.py 中按 rel_path 排序，天然稳定
    for idx, fentry in enumerate(scan_result.files):
        if cancel_event and cancel_event.is_set():
            break

        extra = ""
        if ocr_texts and fentry.rel_path in ocr_texts:
            extra = (ocr_texts[fentry.rel_path] or "").strip()[:8000]

        if mode == MODE_BY_COURSE_THEN_TYPE:
            course, rule_idx, rule_desc = _match_course(
                fentry.filename, sorted_rules, extra)
            rule_key = f"course_rule:{rule_idx}" if rule_idx is not None \
                else "course_rule:unmatched"
            direction = semester = category = inner_path = ""
            anchor_index = -1
        elif mode == MODE_BY_DIRECTION_SEMESTER:
            match = classify_context(fentry.rel_path, compiled, extra)
            direction, semester, category = (
                match.direction, match.semester, match.category)
            anchor_index, rule_desc, rule_key = (
                match.anchor_index, match.rule_desc, match.rule_key)
            # PlanItem.course 用 category 填充，保持旧字段兼容
            course = category or direction
            rule_idx = None
            # anchor_index 指向最后一个已确定类别的目录节点。
            # inner_path = 锚点之后、文件名之前的所有目录层级；文件名不在其中。
            parts = fentry.rel_path.split(os.sep)
            if match.fixed_inner is not None:
                inner_path = match.fixed_inner
            elif anchor_index >= 0 and anchor_index + 1 < len(parts) - 1:
                inner_path = os.sep.join(parts[anchor_index + 1:-1])
            else:
                inner_path = ""

            # ---- V1.5：长期修正规则覆盖原有映射 ----
            # 优先级：当前计划的临时手动修正（在 plan_patch 里做，晚于本函数）
            #         > 已启用的长期规则（按配置顺序取第一条）
            #         > 上述原有推荐模式分类
            lt_match = match_long_term_rule(fentry.rel_path, compiled, extra)
            if lt_match is not None:
                lt_rule = lt_match.rule
                direction = lt_rule.direction
                semester = lt_rule.semester or ""
                category = lt_rule.category or ""
                if lt_rule.inner_mode == INNER_KEEP:
                    pass  # 保留上面按原锚点算出的内部路径
                elif lt_rule.inner_mode == INNER_CLEAR:
                    inner_path = ""
                else:
                    segs = [p for p in lt_rule.inner_path.replace("\\", "/")
                            .split("/") if p]
                    inner_path = os.sep.join(segs)
                course = category or direction
                rule_desc = (
                    f"长期规则「{lt_rule.name}」：{lt_match.evidence} → "
                    f"{direction}/{category or '（无类别）'}")
                rule_key = f"long_term:{lt_rule.rule_id}"
        else:
            course = ""
            rule_idx = None
            rule_desc = "按类型模式"
            rule_key = "by_type"
            direction = semester = category = inner_path = ""
            anchor_index = -1

        ftype = fentry.file_type
        if mode == MODE_BY_DIRECTION_SEMESTER:
            target_dir = _build_target_dir_v2(direction, semester, category, ftype)
            if inner_path:
                target_dir = os.path.join(target_dir, inner_path)
        else:
            target_dir = _build_target_dir(mode, course, ftype)

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
            direction=direction or None,
            semester=semester or None,
            category=category or None,
            inner_path=inner_path or None,
            rule_key=rule_key,
            auto_direction=direction or None,
            auto_semester=semester or None,
            auto_category=category or None,
            auto_inner_path=inner_path or None,
            auto_rule_key=rule_key,
            auto_rule_applied=rule_desc,
        )
        if ocr_texts is not None and fentry.rel_path in ocr_texts:
            item.ocr_text = extra
        if ocr_errors and fentry.rel_path in ocr_errors:
            item.ocr_error = ocr_errors[fentry.rel_path] or ""
        item = _assign_unique_names(items, root, item)
        items.append(item)

        if progress_cb and total > 0:
            progress_cb(idx + 1, total, fentry.rel_path)

    if (mode == MODE_BY_DIRECTION_SEMESTER and compiled is not None
            and not (cancel_event and cancel_event.is_set())):
        _group_unregistered_with_neighbors(items, root)

    if mode == MODE_BY_DIRECTION_SEMESTER:
        for item in items:
            # 规则没判断出来、仍停在「待定」的项默认不勾选。
            # 邻居归组或长期规则改判之后 rule_key 不再是 fallback，保持勾选。
            if (item.rule_key == "fallback"
                    and (item.direction or "") == PENDING_DIRECTION):
                item.selected = False

    return items, compiled


def ocr_maps_from_plan(plan: Optional["OrganizePlan"]):
    """从已生成的计划里取出图片文字，供试运行复用，避免再读一遍图片。"""
    if plan is None:
        return None, None
    texts: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    used = False
    for item in plan.items:
        if item.ocr_text is not None:
            texts[item.file.rel_path] = item.ocr_text
            used = True
        if item.ocr_error:
            errors[item.file.rel_path] = item.ocr_error
            used = True
    if not used:
        return None, None
    return texts, errors


def build_plan(
    scan_result: ScanResult,
    mode: str,
    rules: List[CourseRule],
    config: Optional[ClassificationConfig] = None,
    compute_duplicates: bool = True,
    cancel_event=None,
    progress_cb=None,
    ocr_texts: Optional[Dict[str, str]] = None,
    ocr_errors: Optional[Dict[str, str]] = None,
) -> OrganizePlan:
    """构造计划。compute_duplicates=False 时跳过 SHA-256（试运行/影响预览用）。

    正式计划与试运行共用 classify_entries，因此同一批输入、同一配置下
    分类与目标路径构造完全一致；差异只在于试运行不读取文件内容。
    """
    if mode not in ALL_MODES:
        raise ValueError(f"未知分类模式: {mode}")

    root = os.path.abspath(scan_result.root)
    sorted_rules = sorted(rules, key=lambda r: r.order) if rules else []

    dup_groups: dict = {}
    file_sha_map: dict = {}
    if compute_duplicates:
        dup_groups, file_sha_map = detect_duplicates(
            scan_result, cancel_event=cancel_event, progress_cb=progress_cb,
            progress_base=0, progress_total=len(scan_result.files),
        )
        if cancel_event and cancel_event.is_set():
            return OrganizePlan(
                root=root, mode=mode, rules=sorted_rules,
                dup_groups=dup_groups,
                dup_total_files=sum(len(v) for v in dup_groups.values()),
                config_snapshot=(compile_config(config).snapshot
                                 if mode == MODE_BY_DIRECTION_SEMESTER else {}),
                duplicates_computed=True,
            )

    items, compiled = classify_entries(
        scan_result, mode, rules, config=config,
        cancel_event=cancel_event,
        progress_cb=(progress_cb if not compute_duplicates else None),
        ocr_texts=ocr_texts, ocr_errors=ocr_errors,
    )

    rel_to_group: dict = {}
    for gid, paths in dup_groups.items():
        for p in paths:
            rel_to_group[p] = (gid, len(paths))

    for item in items:
        gid, gsize = rel_to_group.get(item.file.rel_path, (None, 1))
        item.dup_group_id = gid
        item.dup_count = gsize
        item.source_sha256 = file_sha_map.get(item.file.rel_path, "")

    return OrganizePlan(
        root=root, mode=mode, rules=sorted_rules, items=items,
        dup_groups=dup_groups,
        dup_total_files=sum(len(v) for v in dup_groups.values()),
        config_snapshot=(compiled.snapshot if compiled is not None else {}),
        duplicates_computed=compute_duplicates,
    )


def generate_plan(
    scan_result: ScanResult,
    mode: str,
    rules: List[CourseRule],
    cancel_event=None,
    progress_cb=None,
    config: Optional[ClassificationConfig] = None,
    ocr_texts: Optional[Dict[str, str]] = None,
    ocr_errors: Optional[Dict[str, str]] = None,
) -> OrganizePlan:
    """生成正式计划（含 SHA-256 与内容重复检测）。

    config 只影响推荐模式（MODE_BY_DIRECTION_SEMESTER）；为 None 时使用
    内置默认配置，保证无自定义配置时与 V1.1/V1.2 行为一致。
    取消后可能返回半成品 plan，调用方需检查 cancel_event 决定是否接受。
    """
    return build_plan(
        scan_result, mode, rules, config=config, compute_duplicates=True,
        cancel_event=cancel_event, progress_cb=progress_cb,
        ocr_texts=ocr_texts, ocr_errors=ocr_errors,
    )


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
# 默认映射已迁移到 classification_config.py；这里保留同名常量作为兼容别名，
# 内容与原 V1.1 定义一致，任何配置都不修改这些全局默认对象。

def _match_by_context(
    rel_path: str,
    semester_map: Optional[dict] = None,
    top_direction: Optional[dict] = None,
    semester_dirs: Optional[dict] = None,
    keyword_course: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[str, str, str, int, str]:
    """兼容旧接口：返回 (方向, 学期, 类别, 锚点索引, 规则描述)。

    V1.3 起推荐模式的实际分类统一走 classify_entries + 显式配置；
    本函数仅为旧调用/旧测试保留，显式传入映射时会临时构造一份配置，
    不修改任何全局默认对象。
    """
    from classification_config import (
        ClassificationConfig, GenericDirRule, KeywordRule, SemesterDirRule,
        SemesterRule, TopDirectionRule, SCOPE_PATH, TRIGGER_SUBDIR,
        compile_config,
    )

    if (semester_map is None and top_direction is None
            and semester_dirs is None and keyword_course is None):
        cfg = default_config()
    else:
        cfg = ClassificationConfig(
            semester_rules=[
                SemesterRule(source=k, semester=v)
                for k, v in (semester_map or DEFAULT_SEMESTER_MAP).items()
            ],
            top_direction_rules=[
                TopDirectionRule(source=k, direction=v)
                for k, v in (top_direction or DEFAULT_TOP_DIRECTION).items()
            ],
            semester_dir_rules=[
                SemesterDirRule(
                    semester=sem, source=src, direction=direction,
                    category=category or "",
                )
                for sem, table in (semester_dirs or DEFAULT_SEMESTER_DIRS).items()
                for src, (direction, category) in table.items()
            ],
            generic_dir_rules=[
                GenericDirRule(prefix=p, direction=d, category=c)
                for p, d, c in DEFAULT_GENERIC_DIR
            ],
            keyword_rules=[
                KeywordRule(keywords=[kw], target=course, scope=SCOPE_PATH,
                            trigger=TRIGGER_SUBDIR)
                for kw, course in (keyword_course or DEFAULT_KEYWORD_TO_COURSE)
            ],
        )
    m = classify_context(rel_path, compile_config(cfg))
    return m.direction, m.semester, m.category, m.anchor_index, m.rule_desc


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
