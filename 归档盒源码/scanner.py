"""
归档盒 V0.1 — 核心扫描模块

职责：递归扫描目录，返回普通文件列表。
纯逻辑模块，零副作用，不创建任何文件或目录。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from safe_root import affected_reason, unsafe_root_reason
from utils import win_long_path, strip_long_prefix


# 扫描时跳过的目录名（全小写匹配 + 显式保留）
SKIP_DIRS = {
    # 归档盒自身产物
    "_已整理", ".organizer",
    # 版本控制
    ".git", ".svn", ".hg",
    # Python 构建（稳定噪声，几乎不会作为用户目录名）
    ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".cache",
    # Node / 前端（稳定噪声）
    "node_modules",
    # IDE / 编辑器
    ".gradle", ".idea", ".vscode",
    # Conda / 科学计算
    ".mamba-root", "site-packages",
    # macOS / 压缩包产物
    "__MACOSX", ".ipynb_checkpoints",
    # 备份快照（自身递归风险）
    ".dsh-backups",
    ".archivebox-backup",
    # 说明：未包含 out / build / target / dist / .cmake
    # —— 这些名称可能被用户合法使用（如"输出"、"课外"），
    #   交给 planner 的类型判定层处理（.class / .o 等归为"构建产物"类型）。
}

# 扫描时跳过的文件名（精确匹配，大小写不敏感）
SKIP_FILENAMES = {
    "icon", ".ds_store", "thumbs.db", "desktop.ini",
    ".gitignore", ".gitmodules", ".env.example", ".clang-format", ".editorconfig",
}

# Windows 隐藏/系统目录：不进入整理计划（S3 / filesafe F-13）
WINDOWS_SYSTEM_DIRS = {
    "$recycle.bin", "system volume information", "recycler", "$windows.~bt",
    "$windows.~ws", "recovery",
}

# Windows 文件属性位
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

# 文件扩展名 → 类型 映射（小写，key 带点）
EXT_TYPE_MAP = {
    # 文档
    ".pdf": "文档", ".doc": "文档", ".docx": "文档",
    ".txt": "文档", ".md": "文档", ".rtf": "文档",
    ".log": "文档",
    # 课件
    ".ppt": "课件", ".pptx": "课件", ".key": "课件",
    # 表格数据
    ".xls": "表格数据", ".xlsx": "表格数据", ".xlsm": "表格数据", ".numbers": "表格数据",
    ".csv": "表格数据", ".json": "表格数据", ".xml": "表格数据",
    ".yaml": "表格数据", ".yml": "表格数据",
    ".db": "表格数据", ".sqlite": "表格数据", ".sqlite3": "表格数据",
    ".mat": "表格数据", ".bag": "表格数据",
    # 图片
    ".png": "图片", ".jpg": "图片", ".jpeg": "图片",
    ".gif": "图片", ".bmp": "图片", ".webp": "图片",
    ".tiff": "图片", ".tif": "图片", ".heic": "图片", ".ico": "图片", ".svg": "图片",
    # 代码
    ".py": "代码", ".java": "代码", ".js": "代码", ".jsx": "代码",
    ".ts": "代码", ".tsx": "代码", ".c": "代码", ".cpp": "代码", ".cc": "代码",
    ".h": "代码", ".hpp": "代码", ".go": "代码", ".rs": "代码", ".r": "代码",
    ".sh": "代码", ".sql": "代码", ".ipynb": "代码", ".m": "代码",
    ".cs": "代码",
    # 网页
    ".html": "网页", ".htm": "网页", ".css": "网页",
    # 压缩包
    ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
    ".tar": "压缩包", ".gz": "压缩包", ".tgz": "压缩包", ".bz2": "压缩包", ".crx": "压缩包",
    # 构建产物
    ".class": "构建产物", ".o": "构建产物", ".obj": "构建产物", ".pyc": "构建产物",
    ".iml": "构建产物", ".dll": "构建产物", ".so": "构建产物", ".dylib": "构建产物",
    ".jar": "构建产物", ".war": "构建产物", ".a": "构建产物", ".lib": "构建产物",
    ".exe": "构建产物", ".onnx": "构建产物", ".pt": "构建产物", ".pth": "构建产物",
    ".pkl": "构建产物",
    # 模型 / 仿真
    ".stl": "模型仿真", ".3ds": "模型仿真", ".dae": "模型仿真",
    ".urdf": "模型仿真", ".srdf": "模型仿真", ".xacro": "模型仿真",
    ".obj": "模型仿真", ".fbx": "模型仿真", ".sdf": "模型仿真", ".stp": "模型仿真",
    # 工程图
    ".circ": "工程图", ".dia": "工程图", ".sch": "工程图", ".brd": "工程图",
    ".gbr": "工程图", ".vsd": "工程图", ".drawio": "工程图",
    # 音视频
    ".mp4": "音视频", ".mov": "音视频", ".mp3": "音视频", ".wav": "音视频",
    ".m4a": "音视频", ".avi": "音视频", ".mkv": "音视频", ".flac": "音视频",
}

# 无扩展名文件的启发式识别（名全小写匹配）
NO_EXT_NAME_MAP = {
    "makefile": "代码",
    "cmakelists.txt": "代码",
    "dockerfile": "代码",
    "license": "文档",
    "readme": "文档",
    "changes": "文档",
    "changelog": "文档",
}

# 项目根目录识别标记（同一目录下命中任一即视为项目根）
# 这些标记同时满足"项目根目录才有、项目内部普通目录不会有"
PROJECT_ROOT_MARKERS_DIR = {".git", ".hg", ".svn", ".fossil"}

PROJECT_ROOT_MARKERS_FILE = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "poetry.lock",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "cmakelists.txt", "cmake_modules",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "go.mod", "go.work", "cargo.toml", "gemfile", "composer.json",
    ".csproj", ".sln", "global.json",
    ".project", ".classpath", ".factorypath",  # Eclipse
    ".idea", ".vscode",  # IDE 目录标记
    "mak", "gn", "buck", "blade",
}


def _looks_like_project_root(dirpath: str) -> Optional[str]:
    """判断某个目录是否像项目根目录。

    返回：命中原因（字符串）；None 表示不是项目根。
    """
    try:
        entries = set(os.listdir(win_long_path(dirpath)))
    except OSError:
        return None
    lower = {e.lower(): e for e in entries}

    # 1) VCS 目录存在 → 最可靠
    for marker in PROJECT_ROOT_MARKERS_DIR:
        if marker in entries or marker.lower() in lower:
            return f"VCS 目录 {marker} 存在"

    # 2) 项目管理文件存在
    for marker in PROJECT_ROOT_MARKERS_FILE:
        if marker in entries or marker in lower:
            actual = lower.get(marker, marker)
            # .idea 和 .vscode 是目录，要再看看目录里是否有工程文件
            if actual in (".idea", ".vscode"):
                ide_root = os.path.join(dirpath, actual)
                if not os.path.isdir(ide_root):
                    continue
                # IDE 目录本身不独立成项目根——必须和至少一个项目标记共存
                has_other = any(
                    m in entries or m in lower
                    for m in PROJECT_ROOT_MARKERS_FILE - {actual}
                )
                if not has_other:
                    continue
            return f"项目标记 {actual} 存在"

    return None


def _is_junction_or_reparse(path: str) -> bool:
    """目录是否为 NTFS junction 或其他重解析点（不含普通符号链接）。

    Windows 上 os.path.islink() 对 junction 返回 False，但 os.walk 仍会走进去。
    调用方应先排除符号链接。非 Windows 上没有 st_reparse_tag，恒为 False。
    """
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) != 0


def _find_all_project_roots(root_dir: str,
                            on_error=None,
                            result: Optional["ScanResult"] = None) -> Dict[str, str]:
    """扫描 root_dir 下所有目录，返回所有像项目根的目录。

    不再做"最深优先"互斥——父目录有 .git、子目录有 package.json，两个都保护。
    文件只要落在任何一个保护根下就被跳过，父子/嵌套关系会自动覆盖。

    walk 时仍跳过隐藏目录（.git/.venv 等）——不需要进入它们内部来识别项目根，
    _looks_like_project_root 只看当前层的直接子项（listdir）。
    junction / 重解析点同样不进入。
    """
    roots: Dict[str, str] = {}
    root_key = os.path.normcase(os.path.abspath(strip_long_prefix(root_dir)))
    try:
        for dirpath, dirnames, _filenames in os.walk(
                win_long_path(root_dir), followlinks=False, onerror=on_error):
            kept = []
            for name in dirnames:
                if name.startswith(".") or name.lower() in SKIP_DIRS:
                    continue
                full = os.path.join(dirpath, name)
                if not os.path.islink(full) and _is_junction_or_reparse(full):
                    continue
                if affected_reason(full, is_dir=True):
                    continue
                kept.append(name)
            dirnames[:] = kept
            reason = _looks_like_project_root(dirpath)
            if reason is None:
                continue
            logical = strip_long_prefix(dirpath)
            # 学生常把课程资料和 requirements.txt 放在同一个根目录。
            # 根自身不保护，否则计划会变成 0 项且没有继续整理的入口。
            if os.path.normcase(os.path.abspath(logical)) == root_key:
                if result is not None:
                    result.root_project_marker = reason
                continue
            roots[logical] = reason
    except OSError:
        return roots
    return roots


DEFAULT_TYPE = "其他"


@dataclass
class FileEntry:
    """扫描结果中的单个文件条目。"""
    abs_path: str                    # 绝对路径
    rel_path: str                    # 相对于扫描根目录的路径
    filename: str                    # 文件名（不含路径）
    file_type: str                   # 分类类型
    size: int                        # 字节数
    size_display: str                # 可读大小
    mtime_ns: int = 0                # 修改时间（纳秒），用于判断指纹是否过期
    skipped_reason: Optional[str] = None  # 若为 None 表示正常；否则是跳过原因


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""
    root: str
    files: List[FileEntry] = field(default_factory=list)
    skipped: List[FileEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    hidden_count: int = 0
    symlink_count: int = 0
    git_count: int = 0
    skipdir_count: int = 0
    permission_errors: int = 0
    empty_dirs: int = 0
    # V1.1 项目保护相关
    protected_roots: Dict[str, str] = field(default_factory=dict)  # {绝对路径: 保护原因}
    protected_kinds: Dict[str, str] = field(default_factory=dict)  # {绝对路径: default/auto/manual 组合}
    protected_count: int = 0  # 被保护跳过的文件数
    junction_dirs: List[str] = field(default_factory=list)
    # 扫描根自身命中了项目标记。根不整体保护，只提示。
    root_project_marker: str = ""
    # S3 / filesafe F-12：跳过目录的完整路径与原因（不能只给一个数字）
    skipped_dirs: List[Tuple[str, str]] = field(default_factory=list)
    # S3 / filesafe F-16：os.walk 权限/路径错误（onerror）
    walk_errors: List[str] = field(default_factory=list)

    def add_skipped_dir(self, path: str, reason: str) -> None:
        self.skipped_dirs.append((strip_long_prefix(path), reason))

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_skipped(self) -> int:
        return len(self.skipped)

    @property
    def summary(self) -> str:
        parts = [f"共扫描 {self.total_files} 个文件"]
        if self.hidden_count:
            parts.append(f"跳过隐藏 {self.hidden_count}")
        if self.symlink_count:
            parts.append(f"跳过软链接 {self.symlink_count}")
        if self.git_count:
            parts.append(f"跳过 .git {self.git_count}")
        if self.skipdir_count:
            parts.append(f"跳过保留目录 {self.skipdir_count}")
        if self.protected_count:
            parts.append(f"项目保护 {self.protected_count}（{len(self.protected_roots)} 个根）")
        if self.junction_dirs:
            parts.append(f"跳过 junction/重解析点 {len(self.junction_dirs)}")
        if self.permission_errors:
            parts.append(f"权限错误 {self.permission_errors}")
        if self.empty_dirs:
            parts.append(f"空目录 {self.empty_dirs}")
        return "，".join(parts)

    @property
    def skipped_paths_text(self, limit: int = 20) -> str:
        """跳过目录/文件的路径清单（供界面与日志展示）。"""
        lines = [f"{p}（{reason}）" for p, reason in self.skipped_dirs[:limit]]
        if len(self.skipped_dirs) > limit:
            lines.append(f"… 另有 {len(self.skipped_dirs) - limit} 个跳过目录未列出")
        return "\n".join(lines)


def classify_type(filename: str) -> str:
    """根据文件名扩展名返回分类类型，大小写不敏感。"""
    name_lower = filename.lower()
    _, ext = os.path.splitext(filename)
    if ext:
        return EXT_TYPE_MAP.get(ext.lower(), DEFAULT_TYPE)
    # 无扩展名：按文件名启发式识别
    if name_lower in NO_EXT_NAME_MAP:
        return NO_EXT_NAME_MAP[name_lower]
    return DEFAULT_TYPE


def format_size(size_bytes: int) -> str:
    """将字节数转为可读字符串。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _file_attributes(path: str) -> int:
    """返回 Windows 文件属性位；其他平台或读取失败返回 0。"""
    try:
        st = os.lstat(win_long_path(path))
    except OSError:
        return 0
    return int(getattr(st, "st_file_attributes", 0) or 0)


def _walk_error(result: "ScanResult", err: OSError) -> None:
    """os.walk 的 onerror：权限/路径错误进入扫描结果，不静默少扫。"""
    path = getattr(err, "filename", "") or ""
    msg = f"扫描目录失败（{path}）：{getattr(err, 'strerror', '') or err}"
    result.walk_errors.append(msg)
    result.errors.append(msg)
    result.permission_errors += 1
    result.add_skipped_dir(path or "（未知目录）", "无法读取")


def _prune_dirnames(result: "ScanResult", dirpath: str, dirnames: List[str]) -> None:
    """按扫描规则就地去掉不应进入的子目录。"""
    kept = []
    for name in dirnames:
        full = os.path.join(dirpath, name)
        if os.path.islink(full):
            result.symlink_count += 1
            result.add_skipped_dir(full, "符号链接")
            continue
        if _is_junction_or_reparse(full):
            result.junction_dirs.append(strip_long_prefix(full))
            result.add_skipped_dir(full, "junction / 重解析点（目录联接）")
            continue
        if name.lower() in SKIP_DIRS:
            if name.lower() == ".git":
                result.git_count += 1
            else:
                result.skipdir_count += 1
            result.add_skipped_dir(full, "保留目录名")
            continue
        if name.lower() in WINDOWS_SYSTEM_DIRS:
            result.skipdir_count += 1
            result.add_skipped_dir(full, "Windows 隐藏/系统目录")
            continue
        if _file_attributes(full) & FILE_ATTRIBUTE_SYSTEM:
            result.skipdir_count += 1
            result.add_skipped_dir(full, "Windows 系统目录（系统属性）")
            continue
        if name.startswith("."):
            result.hidden_count += 1
            result.add_skipped_dir(full, "隐藏目录（点号开头）")
            continue
        danger = affected_reason(full, is_dir=True)
        if danger:
            result.skipdir_count += 1
            result.add_skipped_dir(full, danger)
            continue
        kept.append(name)
    dirnames[:] = kept


def _consider_file(result: "ScanResult", root: str, full_path: str) -> None:
    """把一个路径按扫描规则纳入或跳过。规则与整目录扫描一致。"""
    import stat as st_mod

    fname = os.path.basename(full_path)
    if ".archivetmp" in fname.lower():
        # 跨盘复制中断留下的临时副本：超过一天就删掉，任何时候都不进计划。
        try:
            stat = os.stat(win_long_path(full_path), follow_symlinks=False)
            if time.time() - stat.st_mtime > 86400:
                os.unlink(win_long_path(full_path))
        except OSError:
            pass
        result.skipdir_count += 1
        return
    if os.path.islink(full_path):
        result.symlink_count += 1
        result.skipped.append(FileEntry(
            abs_path=strip_long_prefix(full_path), rel_path="", filename=fname,
            file_type="", size=0, size_display="",
            skipped_reason="符号链接",
        ))
        return
    danger = affected_reason(full_path, is_dir=False)
    if danger:
        result.skipdir_count += 1
        result.skipped.append(FileEntry(
            abs_path=strip_long_prefix(full_path), rel_path="", filename=fname,
            file_type="", size=0, size_display="",
            skipped_reason=danger,
        ))
        return
    if fname.startswith("."):
        result.hidden_count += 1
        return
    if fname.lower() in SKIP_FILENAMES:
        result.skipdir_count += 1
        result.add_skipped_dir(full_path, "Windows/系统保留文件名")
        return
    attrs = _file_attributes(full_path)
    if attrs & FILE_ATTRIBUTE_SYSTEM:
        result.skipdir_count += 1
        result.add_skipped_dir(full_path, "Windows 系统文件")
        return
    if not os.access(win_long_path(full_path), os.R_OK):
        result.permission_errors += 1
        result.skipped.append(FileEntry(
            abs_path=strip_long_prefix(full_path), rel_path="", filename=fname,
            file_type="", size=0, size_display="",
            skipped_reason="无读取权限",
        ))
        return
    try:
        stat = os.stat(win_long_path(full_path), follow_symlinks=False)
    except OSError as e:
        msg = f"stat 失败 {strip_long_prefix(full_path)}: {e}"
        result.errors.append(msg)
        result.permission_errors += 1
        return
    if not st_mod.S_ISREG(stat.st_mode):
        return
    result.files.append(FileEntry(
        abs_path=strip_long_prefix(full_path),
        rel_path=os.path.relpath(full_path, win_long_path(root)),
        filename=fname,
        file_type=classify_type(fname),
        size=stat.st_size,
        size_display=format_size(stat.st_size),
        mtime_ns=getattr(stat, "st_mtime_ns", 0),
    ))


def _apply_protection(result: "ScanResult", root: str,
                      protect_dirs, detected: Dict[str, str]) -> None:
    """把显式保护和自动识别的项目目录从可整理文件里剔除。

    S3 / filesafe F-15：Windows 路径大小写不敏感，所有"是否在根内"的
    比较统一先 normcase，避免合法子目录被误报为"不在根内"。
    """
    root_key = os.path.normcase(os.path.abspath(root))
    explicit: Dict[str, tuple] = {}
    for p in protect_dirs or []:
        if not p:
            continue
        if isinstance(p, (tuple, list)):
            if len(p) < 2:
                continue
            path_part = p[0]
            kind = p[1] or "manual"
            label = p[2] if len(p) > 2 else "用户手动保护"
        else:
            path_part, kind, label = p, "manual", "用户手动保护"
        if not path_part:
            continue
        ap = os.path.abspath(path_part)
        ap_key = os.path.normcase(ap)
        if os.path.isdir(win_long_path(ap)) and (
                ap_key == root_key or ap_key.startswith(root_key + os.sep)):
            explicit[ap] = (kind, label)

    merged: Dict[str, dict] = {}

    def _add(ap: str, kind: str, label: str):
        slot = merged.setdefault(ap, {"kinds": set(), "labels": []})
        slot["kinds"].add(kind)
        if label not in slot["labels"]:
            slot["labels"].append(label)

    for ap, reason in detected.items():
        _add(ap, "auto", f"自动识别（{reason}）")
    for ap, (kind, label) in explicit.items():
        _add(ap, kind, label)

    result.protected_roots = {
        p: "；".join(slot["labels"]) for p, slot in merged.items()
    }
    result.protected_kinds = {
        p: ",".join(sorted(slot["kinds"])) for p, slot in merged.items()
    }
    if not merged:
        return
    protected_abs_set = {os.path.normcase(os.path.abspath(p)): p
                         for p in merged.keys()}
    new_files: List[FileEntry] = []
    for entry in result.files:
        entry_key = os.path.normcase(os.path.abspath(entry.abs_path))
        hit = [p for key, p in protected_abs_set.items()
               if entry_key == key or entry_key.startswith(key + os.sep)]
        if not hit:
            new_files.append(entry)
            continue
        hit_root = max(hit, key=lambda x: x.count(os.sep))
        reason = result.protected_roots.get(hit_root, "项目保护")
        entry.skipped_reason = f"项目保护（{reason}）"
        result.skipped.append(entry)
        result.protected_count += 1
    result.files = new_files


def scan_directory(
    root_path: str,
    protect_dirs: Optional[List[str]] = None,
    detect_project_roots: bool = True,
    cancel_event=None,
    progress_cb=None,
) -> ScanResult:
    """
    递归扫描目录，返回普通文件列表。

    扫描规则：
    - 跳过 .git、_已整理、.organizer 目录
    - 跳过以 '.' 开头的隐藏文件/目录（保留隐藏目录中的 .git 等显式规则已覆盖）
    - 跳过符号链接（os.path.islink 为 True 的）
    - 磁盘根、用户主目录、资源库和系统目录直接拒绝，不往下走
    - 软件包、备份和落在系统位置的子目录整夹跳过
    - 只读扫描，不创建任何文件或目录

    V1.1 新增：
    - protect_dirs: 显式保护目录，整体不整理。元素为路径字符串或
      (路径, 类别, 显示原因) 三元组，类别取 default / auto / manual
    - detect_project_roots: 自动识别含项目标记的根目录并整体保护（默认 True）
    """
    root = os.path.abspath(root_path)
    result = ScanResult(root=root)
    protect_dirs = protect_dirs or []
    blocked = unsafe_root_reason(root)
    if blocked:
        result.errors.append(blocked)
        result.add_skipped_dir(root, blocked)
        return result

    if not os.path.exists(win_long_path(root)):
        result.errors.append(f"路径不存在: {root}")
        return result
    if not os.path.isdir(win_long_path(root)):
        result.errors.append(f"路径不是目录: {root}")
        return result
    if not os.access(win_long_path(root), os.R_OK):
        result.errors.append(f"无读取权限: {root}")
        result.permission_errors += 1
        return result

    # 使用 os.walk；对 skipdirs 设置 topdown 过滤
    # S3 / filesafe F-16/F-22：onerror 记录权限错误；walk 根使用 \\?\ 长路径前缀。
    walk_root = win_long_path(root)
    seen = 0
    last_emit = 0.0
    if progress_cb is not None:
        progress_cb(0, "")
    for dirpath, dirnames, filenames in os.walk(
            walk_root, followlinks=False,
            onerror=lambda e: _walk_error(result, e)):
        # S3 / guia F-06：扫描可协作取消
        if cancel_event is not None and cancel_event.is_set():
            result.errors.append("扫描已取消（结果不完整）")
            break
        # 计算当前路径相对于 root 的层级，以决定跳过规则
        rel_dir = os.path.relpath(dirpath, walk_root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        _prune_dirnames(result, dirpath, dirnames)
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            _consider_file(result, root, full)
            seen += 1
            now = time.monotonic()
            if (progress_cb is not None
                    and (seen % 200 == 0 or now - last_emit >= 0.1)):
                last_emit = now
                progress_cb(
                    seen,
                    strip_long_prefix(os.path.relpath(full, walk_root)))
        if not filenames and not dirnames:
            result.empty_dirs += 1

    if progress_cb is not None and seen:
        progress_cb(seen, "")

    detected: Dict[str, str] = {}
    if detect_project_roots:
        detected = _find_all_project_roots(
            root, on_error=lambda e: _walk_error(result, e), result=result)
    _apply_protection(result, root, protect_dirs, detected)
    result.files.sort(key=lambda e: e.rel_path.lower())
    result.skipped.sort(key=lambda e: e.abs_path.lower())
    return result


def _ancestors_within(path: str, root: str) -> List[str]:
    """从 path 的所在目录向上，直到 root（含）。比较统一 normcase。"""
    root = os.path.abspath(root)
    root_key = os.path.normcase(root)
    current = path if os.path.isdir(win_long_path(path)) else os.path.dirname(path)
    current = os.path.abspath(current)
    found = []
    while True:
        key = os.path.normcase(current)
        if key == root_key or key.startswith(root_key + os.sep):
            found.append(current)
        if key == root_key:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return found


def scan_paths(
    root_path: str,
    paths,
    protect_dirs: Optional[List[str]] = None,
    detect_project_roots: bool = True,
    cancel_event=None,
    progress_cb=None,
) -> ScanResult:
    """只读取给定的文件或目录，不遍历整理空间里的其他内容。

    用于拖入或选中少量文件后立刻计算目标位置。
    保护规则仍生效：显式保护目录，以及这些路径沿途和内部的项目根。
    """
    root = os.path.abspath(root_path)
    result = ScanResult(root=root)
    protect_dirs = protect_dirs or []
    blocked = unsafe_root_reason(root)
    if blocked:
        result.errors.append(blocked)
        result.add_skipped_dir(root, blocked)
        return result
    if not os.path.isdir(root):
        result.errors.append(f"路径不是目录: {root}")
        return result

    checked_dirs = set()
    root_key = os.path.normcase(root)
    seen = 0
    last_emit = 0.0

    def emit(full: str) -> None:
        nonlocal seen, last_emit
        seen += 1
        now = time.monotonic()
        if (progress_cb is not None
                and (seen % 200 == 0 or now - last_emit >= 0.1)):
            last_emit = now
            try:
                rel = os.path.relpath(strip_long_prefix(full), root)
            except ValueError:
                rel = os.path.basename(full)
            progress_cb(seen, rel)

    def _within_root(p: str) -> bool:
        key = os.path.normcase(os.path.abspath(p))
        return key == root_key or key.startswith(root_key + os.sep)

    def note_dir(directory: str):
        directory = strip_long_prefix(os.path.abspath(directory))
        if directory in checked_dirs:
            return
        if _within_root(directory):
            checked_dirs.add(directory)

    def walk_dir(start: str):
        danger = affected_reason(start, is_dir=True)
        if danger:
            result.skipdir_count += 1
            result.add_skipped_dir(start, danger)
            return
        if not os.path.islink(start) and _is_junction_or_reparse(start):
            result.junction_dirs.append(os.path.abspath(start))
            result.add_skipped_dir(start, "junction / 重解析点（目录联接）")
            return
        for dirpath, dirnames, filenames in os.walk(
                win_long_path(start), followlinks=False,
                onerror=lambda e: _walk_error(result, e)):
            if cancel_event is not None and cancel_event.is_set():
                result.errors.append("扫描已取消（结果不完整）")
                return
            note_dir(dirpath)
            _prune_dirnames(result, dirpath, dirnames)
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                note_dir(os.path.dirname(full))
                _consider_file(result, root, full)
                emit(full)
            if not filenames and not dirnames:
                result.empty_dirs += 1

    for raw in paths or []:
        if not raw:
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(win_long_path(path)):
            result.errors.append(f"路径不存在: {path}")
            continue
        if not _within_root(path):
            result.errors.append(f"路径不在整理空间内: {path}")
            continue
        if os.path.isdir(win_long_path(path)):
            walk_dir(path)
        else:
            for directory in _ancestors_within(path, root):
                note_dir(directory)
            _consider_file(result, root, path)
            emit(path)

    if progress_cb is not None and seen:
        progress_cb(seen, "")

    detected: Dict[str, str] = {}
    if detect_project_roots:
        for directory in checked_dirs:
            reason = _looks_like_project_root(directory)
            if not reason:
                continue
            if os.path.normcase(os.path.abspath(directory)) == root_key:
                result.root_project_marker = reason
                continue
            detected[directory] = reason
    _apply_protection(result, root, protect_dirs, detected)
    result.files.sort(key=lambda e: e.rel_path.lower())
    result.skipped.sort(key=lambda e: e.abs_path.lower())
    return result
