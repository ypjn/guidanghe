"""阶段 6 · 个人文件管理 Agent（自然语言查找 + 整理方案）。

这一层只做三件事：
1. 把一句自然语言指令拆成「学期 / 课程 / 文件类型」三类意图；
2. 用现有扫描、正文提取和偏好库找出相关文件，并给每个命中写出理由；
3. 优先把文件归入整理空间里已经存在的课程/实验目录，生成可交给
   现有预览、执行与撤销链路的 OrganizePlan。

它不移动文件、不写历史、不调用外部模型。真正执行仍由
executor.execute_plan 完成。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from classification_config import (
    DEFAULT_KEYWORD_TO_COURSE,
    DEFAULT_SEMESTER_MAP,
    default_config,
    load_config,
)
from content_extract import extract_contents
from planner import (
    MODE_BY_DIRECTION_SEMESTER,
    OrganizePlan,
    PlanItem,
    assign_unique_targets,
)
from preferences import effective_preferences_resilient, match_preferences
from scanner import FileEntry, ScanResult, scan_directory
from utils import sha256_file

TARGET_ROOT = "_已整理"
SEMESTER_RE = re.compile(r"^大[一二三四][上下]$")

# 这些目录不参与「现有目录」候选，也不作为课程名。
SKIP_DIR_NAMES = {
    ".organizer", TARGET_ROOT, ".git", ".svn", "__pycache__",
    ".venv", "venv", "node_modules", "待整理", "临时", "下载",
    "inbox", "incoming", "to_sort",
}

# 只用于理解指令，不会当成课程名。
GENERIC_WORDS = {
    "帮我", "找出", "找到", "看看", "整理", "归到", "归入", "现有", "目录",
    "文件", "材料", "这学期", "本学期", "当前学期", "先给", "我", "一下",
    "怎么", "哪些", "报告和代码", "和", "的", "把", "按", "到", "里",
    "全部", "所有", "相关", "需要", "进行",
}

DOMAIN_TERMS = (
    "实验", "报告", "代码", "材料", "作业", "课件", "笔记",
    "源码", "程序", "文档", "习题", "期末", "期中", "项目",
)

# 用户没有点名这些用途时，命中它们只能算「归属不确定」，不能默认搬走。
NEGATIVE_TERMS = ("复习", "笔记", "习题", "答案", "提纲", "总结", "往年")

REPORT_ALIASES = ("报告", "文档", "实验报告", "report", "docs", "doc")
CODE_ALIASES = ("代码", "源码", "程序", "实现", "code", "src", "source")

# content_extract 只读 txt/md/ooxml；查找代码时补读一小段纯文本源码。
CODE_TEXT_EXTS = {
    ".py", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".js", ".jsx",
    ".ts", ".tsx", ".go", ".rs", ".r", ".sh", ".sql", ".ipynb", ".m", ".cs",
}
MAX_CODE_TEXT = 4096


# ======================== 数据结构 ========================

@dataclass
class AgentQuestion:
    """需要用户补一句才能继续的澄清问题。"""
    kind: str                      # semester / course / no_match
    text: str
    options: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class AgentHit:
    """一个被查找到的文件，以及它为什么被命中。"""
    file: FileEntry
    score: int = 0
    evidence: str = ""
    kind: str = ""                 # report / code / other
    target_dir_rel: str = ""
    target_rel_path: str = ""
    conflict: bool = False
    already: bool = False          # 已经在目标目录，不需要移动
    uncertain: bool = False        # 归属不确定，只展示、不默认进方案


@dataclass
class AgentResult:
    root: str
    instruction: str
    scan: ScanResult
    texts: Dict[str, str]
    hits: List[AgentHit] = field(default_factory=list)
    question: Optional[AgentQuestion] = None
    plan: Optional[OrganizePlan] = None
    summary: str = ""
    semester: Optional[str] = None
    course: Optional[str] = None
    notes: List[str] = field(default_factory=list)


# ======================== 学期 / 课程意图 ========================

def _list_dirs(root: str) -> List[str]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(".") or name in SKIP_DIR_NAMES:
            continue
        if os.path.isdir(os.path.join(root, name)):
            out.append(name)
    return out


def discover_semesters(root: str) -> List[str]:
    """整理空间顶层看起来像学期的目录（大二上、大一下……）。"""
    return [name for name in _list_dirs(root) if SEMESTER_RE.match(name)]


def _known_semester_labels(root: str) -> List[str]:
    """用户可能说出口的学期名，不要求目录真的存在。"""
    labels = set(DEFAULT_SEMESTER_MAP.values())
    try:
        config, _source = load_config(root)
    except Exception:
        config = default_config()
    for rule in getattr(config, "semester_rules", []) or []:
        if getattr(rule, "enabled", True):
            labels.add(rule.source)
            labels.add(rule.semester)
    return sorted(labels, key=len, reverse=True)


def _term_window(now: Optional[_dt.datetime] = None) -> Tuple[_dt.datetime, _dt.datetime, str]:
    """当前学期的大致时间窗，用于没有学期目录时按修改时间筛选。"""
    now = now or _dt.datetime.now()
    year, month = now.year, now.month
    if month >= 9:
        start = _dt.datetime(year, 9, 1)
        end = _dt.datetime(year + 1, 2, 15)
        label = f"{year}秋"
    elif month <= 2:
        start = _dt.datetime(year - 1, 9, 1)
        end = _dt.datetime(year, 2, 15)
        label = f"{year - 1}秋"
    else:
        start = _dt.datetime(year, 2, 1)
        end = _dt.datetime(year, 9, 1)
        label = f"{year}春"
    return start, end, label


def _mtime_in_window(mtime_ns: int, now: Optional[_dt.datetime] = None) -> bool:
    if not mtime_ns:
        return False
    try:
        stamp = _dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000)
    except (OverflowError, OSError, ValueError):
        return False
    start, end, _ = _term_window(now)
    return start <= stamp < end


def _resolve_semester(root: str, instruction: str,
                      choice: Optional[str],
                      now: Optional[_dt.datetime] = None
                      ) -> Tuple[Optional[str], Optional[AgentQuestion]]:
    """返回学期值（目录名 / __date__ / None）或澄清问题。"""
    available = discover_semesters(root)
    known = _known_semester_labels(root)
    explicit = next((name for name in known if name in instruction), "")
    if choice == "__all__":
        return None, None
    if choice:
        return choice, None
    if explicit:
        if explicit in available:
            return explicit, None
        # 用户明确说了某个学期，但空间里没有这个目录：先问，不拿别的学期顶替。
        options = []
        for name in available:
            count = sum(1 for _dirpath, _dirnames, files in
                        os.walk(os.path.join(root, name))
                        for fname in files if not fname.startswith("."))
            options.append({
                "value": name,
                "label": f"改在「{name}」里找",
                "detail": f"这个学期目录下有 {count} 个文件",
            })
        if available:
            options.append({
                "value": "__all__",
                "label": "不限学期",
                "detail": "在整理空间的所有学期目录里查找",
            })
            hint = f"，只有：{'、'.join(available)}"
        else:
            options = [
                {
                    "value": "__date__",
                    "label": "按当前学期的时间范围查找",
                    "detail": "按文件修改时间筛选当前学期",
                },
                {
                    "value": "__all__",
                    "label": "不限学期",
                    "detail": "在整理空间的所有文件里查找",
                },
            ]
            hint = "，当前也没有可用的学期目录"
        return None, AgentQuestion(
            "semester_missing",
            f"整理空间里没有「{explicit}」目录{hint}。"
            "要改成在这些范围里找吗？确认后才会生成方案：",
            options,
        )

    mentions_current = any(word in instruction for word in
                           ("这学期", "本学期", "当前学期"))
    if not mentions_current:
        return None, None

    if len(available) == 1:
        return available[0], None
    if not available:
        # 没有学期目录时按当前学期的时间窗筛选，不打断用户。
        return "__date__", None

    options = []
    for name in available:
        count = sum(1 for dirpath, _, files in os.walk(os.path.join(root, name))
                    for fname in files if not fname.startswith("."))
        options.append({
            "value": name,
            "label": name,
            "detail": f"这个学期目录下有 {count} 个文件",
        })
    options.append({
        "value": "__all__",
        "label": "不限学期",
        "detail": "在整理空间的所有学期目录里查找",
    })
    return None, AgentQuestion(
        "semester",
        f"「这学期」对应 {len(available)} 个学期目录，"
        "请先确认是哪一学期：",
        options,
    )


def _course_aliases(root: str) -> Dict[str, str]:
    """别名 → 课程类别。优先使用空间配置与内置课程表。"""
    table: Dict[str, str] = {}

    def add(alias: str, category: str) -> None:
        alias = (alias or "").strip()
        category = (category or alias or "").strip()
        if len(alias) < 2 or not category:
            return
        if alias in GENERIC_WORDS or alias in DOMAIN_TERMS:
            return
        if SEMESTER_RE.match(alias) or alias in SKIP_DIR_NAMES:
            return
        table.setdefault(alias.lower(), category)

    try:
        config, _source = load_config(root)
    except Exception:
        config = default_config()
    for rule in getattr(config, "semester_dir_rules", []) or []:
        if getattr(rule, "enabled", True):
            add(rule.source, rule.category or rule.source)
    for keyword, category in DEFAULT_KEYWORD_TO_COURSE:
        add(keyword, category)

    # 空间里已经存在的课程目录也可以当别名（如「操作系统」）。
    for semester in discover_semesters(root):
        base = os.path.join(root, semester)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if not os.path.isdir(path) or name.startswith("."):
                continue
            if name in SKIP_DIR_NAMES:
                continue
            add(name, table.get(name.lower(), name))
    return table


def _course_mentions(instruction: str,
                     aliases: Dict[str, str]) -> List[Tuple[str, str]]:
    """指令里命中的课程别名，最长匹配优先，按类别去重。"""
    text = instruction.lower()
    found: List[Tuple[str, str]] = []
    occupied: List[Tuple[int, int]] = []
    for alias, category in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        start = text.find(alias)
        if start < 0:
            continue
        end = start + len(alias)
        if any(not (end <= s or start >= e) for s, e in occupied):
            continue
        occupied.append((start, end))
        found.append((alias, category))
    ordered: List[Tuple[str, str]] = []
    seen = set()
    for alias, category in found:
        if category in seen:
            continue
        seen.add(category)
        ordered.append((alias, category))
    return ordered


def _course_question(categories: Sequence[str],
                     scan: Optional[ScanResult] = None,
                     texts: Optional[Dict[str, str]] = None) -> AgentQuestion:
    options = []
    for category in categories:
        count = 0
        if scan is not None:
            count = sum(1 for entry in scan.files
                        if category.lower() in entry.rel_path.lower()
                        or category.lower() in entry.filename.lower()
                        or category.lower() in (texts or {}).get(
                            entry.rel_path, "").lower())
        options.append({
            "value": category,
            "label": category,
            "detail": f"命中约 {count} 个文件" if scan is not None else "",
        })
    return AgentQuestion(
        "course",
        "这句话对应多门课程，请先确认是哪一门：",
        options,
    )


def _infer_courses(scan: ScanResult, texts: Dict[str, str],
                   aliases: Dict[str, str],
                   semester: Optional[str],
                   instruction: str,
                   now: Optional[_dt.datetime] = None) -> List[str]:
    """没有明说课程时，从候选文件名/正文/路径反推课程。"""
    wanted_terms = [term for term in DOMAIN_TERMS if term in instruction]
    if not wanted_terms:
        wanted_terms = ["实验", "报告", "代码"]
    counts: Dict[str, int] = {}
    for entry in scan.files:
        if not _semester_ok(entry, semester, now):
            continue
        name = entry.filename.lower()
        path = entry.rel_path.lower()
        content = (texts.get(entry.rel_path) or "").lower()
        if not any(term in name or term in content for term in wanted_terms):
            continue
        for alias, category in aliases.items():
            if alias in name or alias in content or alias in path:
                counts[category] = counts.get(category, 0) + 1
    return [category for category, _ in sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _semester_ok(entry: FileEntry, semester: Optional[str],
                 now: Optional[_dt.datetime] = None) -> bool:
    if not semester:
        return True
    if semester == "__date__":
        return _mtime_in_window(entry.mtime_ns, now)
    rel = entry.rel_path.replace("\\", "/")
    return rel == semester or rel.startswith(semester + "/")


# ======================== 查找与目标目录 ========================

def _aliases_for_category(aliases: Dict[str, str], category: str) -> List[str]:
    out = [category]
    for alias, cat in aliases.items():
        if cat == category and alias not in out:
            out.append(alias)
    return out


def _file_kind(entry: FileEntry) -> str:
    if entry.file_type == "代码":
        return "code"
    if entry.file_type == "文档":
        return "report"
    return "other"


def _requested_kinds(instruction: str) -> List[str]:
    kinds = []
    if any(word in instruction for word in ("报告", "文档")):
        kinds.append("report")
    if any(word in instruction for word in ("代码", "源码", "程序")):
        kinds.append("code")
    return kinds


def _normalize_protect_dirs(protect_dirs: Optional[Sequence]) -> List[Tuple[str, str, str]]:
    """统一保护目录为真实绝对路径，避免 /var 与 /private/var 前缀不一致。"""
    out: List[Tuple[str, str, str]] = []
    for item in protect_dirs or []:
        if isinstance(item, (tuple, list)):
            path = item[0] if item else ""
            kind = item[1] if len(item) > 1 and item[1] else "manual"
            label = item[2] if len(item) > 2 and item[2] else "用户手动保护"
        else:
            path, kind, label = item, "manual", "用户手动保护"
        if not path:
            continue
        out.append((
            os.path.realpath(os.path.abspath(str(path))),
            str(kind),
            str(label),
        ))
    return out


def _protected_roots(protect_dirs: Optional[Sequence]) -> List[str]:
    """把 scan_directory 的保护参数统一成真实绝对路径。"""
    return [path for path, _kind, _label in
            _normalize_protect_dirs(protect_dirs)]


def _is_within_path(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(os.path.abspath(path)), parent]) == parent
    except ValueError:
        return False


def _discover_existing_dirs(root: str,
                            protect_dirs: Optional[Sequence] = None,
                            limit: int = 4000) -> List[str]:
    """收集整理空间内已存在的目录相对路径，跳过保留目录、隐藏目录与保护目录。"""
    out: List[str] = []
    root_abs = os.path.abspath(root)
    protected = _protected_roots(protect_dirs)
    for dirpath, dirnames, _ in os.walk(root_abs):
        keep = []
        for name in dirnames:
            if name.startswith(".") or name in SKIP_DIR_NAMES:
                continue
            full = os.path.join(dirpath, name)
            if any(_is_within_path(full, p) for p in protected):
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)
        rel = os.path.relpath(dirpath, root_abs)
        if rel in (".", ""):
            continue
        out.append(rel)
        if len(out) >= limit:
            break
    return out


def _choose_target_dir(dirs: Sequence[str], semester: Optional[str],
                       category: str, aliases: Dict[str, str],
                       kind: str) -> str:
    """优先选已有的「课程/实验/报告（代码）」目录。"""
    category_aliases = [a.lower() for a in _aliases_for_category(aliases, category)]
    kind_aliases = (REPORT_ALIASES if kind == "report"
                    else CODE_ALIASES if kind == "code" else ())
    best = ""
    best_score = -10**6
    for rel in dirs:
        norm = rel.replace("\\", "/")
        parts = [p for p in norm.split("/") if p]
        lowered = [p.lower() for p in parts]
        if semester and semester not in parts:
            continue
        if not any(any(alias in part for alias in category_aliases)
                   for part in lowered):
            continue
        score = 0
        if semester and semester in parts:
            score += 2
        if any(any(alias in part for alias in category_aliases)
               for part in lowered):
            score += 3
        if kind_aliases and any(
                part == alias or part.endswith(alias)
                for part in lowered for alias in kind_aliases):
            score += 8
        elif kind_aliases and any(
                alias in part for part in lowered for alias in kind_aliases):
            score += 4
        score -= len(parts)
        if score > best_score:
            best, best_score = norm, score
    return best


def _fallback_target(semester: Optional[str], category: str,
                     file_type: str) -> str:
    return os.path.join(
        TARGET_ROOT, "课程", semester or "未识别学期",
        category or "未分类课程", file_type)


def _read_code_texts(scan: ScanResult,
                     texts: Dict[str, str]) -> Dict[str, str]:
    """补读代码文件的一小段正文，仅用于解释命中，不写入任何缓存。"""
    out = dict(texts)
    for entry in scan.files:
        if entry.rel_path in out:
            continue
        ext = os.path.splitext(entry.filename)[1].lower()
        if ext not in CODE_TEXT_EXTS:
            continue
        try:
            with open(entry.abs_path, "rb") as handle:
                raw = handle.read(MAX_CODE_TEXT)
        except OSError:
            continue
        out[entry.rel_path] = raw.decode("utf-8", errors="ignore")
    return out


def _find_hits(scan: ScanResult, texts: Dict[str, str],
               aliases: Dict[str, str], course: str,
               semester: Optional[str], instruction: str,
               dirs: Sequence[str],
               now: Optional[_dt.datetime] = None) -> List[AgentHit]:
    category_aliases = [a.lower() for a in _aliases_for_category(aliases, course)]
    domain_terms = [term for term in DOMAIN_TERMS if term in instruction]
    requested = _requested_kinds(instruction)
    experiment_intent = "实验" in instruction
    hits: List[AgentHit] = []

    for entry in scan.files:
        if not _semester_ok(entry, semester, now):
            continue
        name = entry.filename.lower()
        rel = entry.rel_path.replace("\\", "/")
        path = rel.lower()
        name_path = name + " " + path
        content = (texts.get(entry.rel_path) or "").lower()

        course_in_name = any(alias in name for alias in category_aliases)
        course_in_path = any(alias in path for alias in category_aliases)
        course_in_content = any(alias in content for alias in category_aliases)
        if not (course_in_name or course_in_path or course_in_content):
            continue

        kind = _file_kind(entry)
        if kind == "other":
            if any(word in name for word in ("报告", "文档")):
                kind = "report"
            elif any(word in name for word in ("代码", "程序", "源码")):
                kind = "code"

        term_hits = [term for term in domain_terms
                     if term in name or term in path or term in content]
        if requested and kind not in requested:
            # 指令明确要报告/代码时，不能把另一类也默认带进来。
            if not (kind == "other" and term_hits):
                continue
        elif not requested and not term_hits and not course_in_name:
            # 没写类型时，至少要文件名或正文能说明和课程有关。
            continue

        # 只按「实验材料」找时，名字/路径没有实验、或明显是复习资料，
        # 不能默认搬进实验目录；只展示给用户确认。
        uncertain_reasons = []
        negative_hits = [term for term in NEGATIVE_TERMS if term in name_path]
        if negative_hits and not any(term in instruction for term in negative_hits):
            uncertain_reasons.append("文件名/路径含「" + "」「".join(negative_hits) + "」")
        if experiment_intent and "实验" not in name_path:
            uncertain_reasons.append("文件名/路径没有「实验」")
        if requested and kind not in requested:
            uncertain_reasons.append("文件类型与本次要求不一致")

        uncertain = bool(uncertain_reasons)
        if uncertain:
            target_dir = ""
        else:
            target_dir = _choose_target_dir(dirs, semester, course, aliases, kind)
            if not target_dir:
                target_dir = _fallback_target(semester, course, entry.file_type)
        already = bool(target_dir) and (
            os.path.dirname(rel).replace("\\", "/")
            == target_dir.replace("\\", "/"))

        parts = []
        if course_in_name:
            matched = next(alias for alias in category_aliases if alias in name)
            parts.append(f"文件名含「{matched}」")
        if course_in_path:
            parts.append(f"路径含「{course}」")
        if course_in_content:
            parts.append(f"正文含「{course}」")
        if term_hits:
            parts.append("文件名/路径/正文含「" + "」「".join(term_hits[:3]) + "」")
        parts.append(f"类型：{entry.file_type}")
        if uncertain:
            parts.append("归属不确定（" + "；".join(uncertain_reasons) + "），保持原位")
        elif already:
            parts.append("已在目标目录，无需移动")
        else:
            parts.append(f"归入现有目录「{target_dir}」")

        score = (6 if course_in_name else 0) + (4 if course_in_path else 0) \
            + (3 if course_in_content else 0) + 2 * len(term_hits)
        hits.append(AgentHit(
            file=entry,
            score=score,
            evidence="；".join(parts),
            kind=kind,
            target_dir_rel=target_dir,
            target_rel_path=(os.path.join(target_dir, entry.filename)
                             if target_dir else ""),
            already=already,
            uncertain=uncertain,
        ))

    hits.sort(key=lambda h: (
        0 if not h.already and not h.uncertain else 1,
        -h.score,
        h.file.rel_path.lower(),
    ))
    return hits


# ======================== 偏好（只读复用） ========================

def _preference_target_dir(pref, file_type: str) -> str:
    direction = getattr(pref, "direction", "") or ""
    semester = getattr(pref, "semester", "") or ""
    category = getattr(pref, "category", "") or ""
    if direction == "课程":
        base = os.path.join(TARGET_ROOT, "课程", semester or "未识别学期",
                            category or "未分类课程", file_type)
    elif direction == "竞赛":
        base = os.path.join(TARGET_ROOT, "竞赛", category) if category \
            else os.path.join(TARGET_ROOT, "竞赛")
    elif direction == "项目":
        base = os.path.join(TARGET_ROOT, "项目", category) if category \
            else os.path.join(TARGET_ROOT, "项目")
    elif direction == "校园事务":
        base = os.path.join(TARGET_ROOT, "校园事务", category or "",
                            file_type) if category \
            else os.path.join(TARGET_ROOT, "校园事务", file_type)
    else:
        base = os.path.join(TARGET_ROOT, "待定", category) if category \
            else os.path.join(TARGET_ROOT, "待定")
    inner = (getattr(pref, "inner_path", "") or "").strip()
    if inner:
        segs = [p for p in inner.replace("\\", "/").split("/") if p]
        if segs:
            base = os.path.join(base, *segs)
    return base


def _apply_preference_evidence(root: str, items: List[PlanItem],
                               notes: List[str]) -> None:
    """命中已保存偏好时把它写进依据；目标目录已存在才真正改落点。"""
    try:
        prefs, error = effective_preferences_resilient(root)
    except Exception as exc:
        notes.append(f"偏好库暂时不可用，本次只按现有目录整理：{exc}")
        return
    if error:
        notes.append(error)
    if not prefs:
        return
    for item in items:
        matches = match_preferences(item.file.rel_path, prefs)
        if not matches:
            continue
        pref, evidence = matches[0]
        candidate = _preference_target_dir(pref, item.file.file_type)
        if os.path.isdir(os.path.join(root, candidate)):
            item.target_dir_rel = candidate
            item.target_rel_path = os.path.join(candidate, item.file.filename)
            item.rule_key = f"long_term:{pref.preference_id}"
            item.rule_applied += f"；命中已保存偏好「{pref.name}」：{evidence}"
            item.decision_evidence = evidence
        else:
            item.rule_applied += (
                f"；已保存偏好「{pref.name}」指向「{candidate}」，"
                "该目录还不存在，本次归入现有目录")


# ======================== 主入口 ========================

def run_agent(root: str, instruction: str,
              scan: Optional[ScanResult] = None,
              texts: Optional[Dict[str, str]] = None,
              semester_choice: Optional[str] = None,
              course_choice: Optional[str] = None,
              protect_dirs: Optional[Sequence] = None,
              now: Optional[_dt.datetime] = None,
              progress_cb: Optional[Callable[[int, int, str], None]] = None,
              cancel_event=None) -> AgentResult:
    """执行一次自然语言查找并给出方案。

    semester_choice / course_choice 是用户回答澄清问题后的选择；
    protect_dirs 与普通扫描使用同一套手动/默认保护规则；
    传回同一份 scan/texts 时不会重新扫描。
    """
    root = os.path.realpath(os.path.abspath(root))
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("请先输入一句要查找或整理的话")
    if not os.path.isdir(root):
        raise ValueError("整理空间不存在，请重新选择文件夹")
    protect_specs = _normalize_protect_dirs(protect_dirs)

    semester, question = _resolve_semester(
        root, instruction, semester_choice, now)
    aliases = _course_aliases(root)

    course: Optional[str] = None
    course_question: Optional[AgentQuestion] = None
    mentions = _course_mentions(instruction, aliases)
    if course_choice == "__all__":
        course = None
    elif course_choice:
        course = course_choice
    elif len(mentions) > 1:
        course_question = _course_question([cat for _, cat in mentions])
    elif len(mentions) == 1:
        course = mentions[0][1]

    if scan is None:
        # 扫描回调是 (已扫描数, 路径)，正文提取是 (当前, 总数, 文件名)。
        scan_progress = None
        if progress_cb is not None:
            scan_progress = lambda cur, name: progress_cb(cur, 0, name)
        scan = scan_directory(root, protect_dirs=protect_specs,
                              cancel_event=cancel_event,
                              progress_cb=scan_progress)
    if texts is None:
        extraction = extract_contents(
            scan.files, cancel_event=cancel_event,
            progress_cb=progress_cb, root=root)
        texts = _read_code_texts(scan, extraction.texts)
    else:
        texts = _read_code_texts(scan, texts)

    if semester is None and question is not None:
        return AgentResult(root, instruction, scan, texts,
                           question=question, semester=None, course=course)

    if course is None and course_question is None and not mentions:
        inferred = _infer_courses(scan, texts, aliases, semester,
                                  instruction, now)
        if len(inferred) > 1:
            course_question = _course_question(inferred, scan, texts)
        elif len(inferred) == 1:
            course = inferred[0]

    if course_question is not None:
        return AgentResult(root, instruction, scan, texts,
                           question=course_question,
                           semester=semester, course=None)

    if course is None:
        return AgentResult(
            root, instruction, scan, texts,
            question=AgentQuestion(
                "no_match",
                "这句话里没有识别出具体课程。可以说得更具体一些，"
                "例如「这学期的数据结构实验材料」。",
                []),
            semester=semester, course=None)

    dirs = _discover_existing_dirs(root, protect_specs)
    hits = _find_hits(scan, texts, aliases, course, semester,
                      instruction, dirs, now)
    if not hits:
        return AgentResult(
            root, instruction, scan, texts,
            question=AgentQuestion(
                "no_match",
                f"没有找到「{course}」在这一学期里的相关文件。"
                "可以换一个说法，或确认课程名是否写对。",
                []),
            semester=semester, course=course)

    items: List[PlanItem] = []
    for hit in hits:
        if hit.already or hit.uncertain:
            continue
        items.append(PlanItem(
            file=hit.file,
            course=course,
            file_type=hit.file.file_type,
            rule_applied="AI 管家：" + hit.evidence,
            target_dir_rel=hit.target_dir_rel,
            target_filename=hit.file.filename,
            target_rel_path=hit.target_rel_path,
            direction="课程",
            semester=semester if semester and semester != "__date__" else None,
            category=course,
            rule_key="agent:course",
            content_text=(texts.get(hit.file.rel_path) or "")[:200],
            content_source="正文" if texts.get(hit.file.rel_path) else "路径",
            path_evidence=hit.file.rel_path,
            decision_evidence=hit.evidence,
            source_sha256=sha256_file(hit.file.abs_path) or "",
        ))

    notes: List[str] = []
    if not items:
        already_only = any(hit.already for hit in hits)
        uncertain_only = any(hit.uncertain for hit in hits)
        if uncertain_only and not already_only:
            message = "找到的文件归属不确定，已保持原位，没有放入方案。"
        elif uncertain_only:
            message = "找到的文件都已在目标目录，或归属不确定，没有需要移动的文件。"
        else:
            message = "找到的文件都已经在目标目录里，不需要移动。"
        return AgentResult(
            root, instruction, scan, texts, hits=hits,
            question=None, plan=None,
            semester=semester, course=course,
            summary=message,
            notes=notes)

    _apply_preference_evidence(root, items, notes)
    assign_unique_targets(items, root)

    by_rel = {item.file.rel_path: item for item in items}
    for hit in hits:
        item = by_rel.get(hit.file.rel_path)
        if item is None:
            continue
        hit.target_dir_rel = item.target_dir_rel
        hit.target_rel_path = item.target_rel_path
        hit.conflict = item.conflict_with_existing

    plan = OrganizePlan(
        root=root,
        mode=MODE_BY_DIRECTION_SEMESTER,
        rules=[],
        items=items,
        duplicates_computed=False,
    )
    plan.ai_note = "；本次按课程关键词与文件名/正文查找，未调用外部模型"

    already = sum(1 for hit in hits if hit.already)
    uncertain = sum(1 for hit in hits if hit.uncertain)
    conflicts = sum(1 for item in items if item.conflict_with_existing)
    reports = sum(1 for item in items if _file_kind(item.file) == "report")
    codes = sum(1 for item in items if _file_kind(item.file) == "code")
    summary = (
        f"找到 {len(hits)} 个相关文件；计划移动 {len(items)} 个"
        f"（报告 {reports} / 代码 {codes}）"
    )
    if already:
        summary += f"；{already} 个已在目标目录"
    if uncertain:
        summary += f"；{uncertain} 个归属不确定，保持原位"
    if conflicts:
        summary += f"；{conflicts} 个目标同名，将自动改名"
    if notes:
        summary += "；" + "；".join(notes)

    return AgentResult(
        root=root,
        instruction=instruction,
        scan=scan,
        texts=texts,
        hits=hits,
        question=None,
        plan=plan,
        summary=summary,
        semester=semester,
        course=course,
        notes=notes,
    )
