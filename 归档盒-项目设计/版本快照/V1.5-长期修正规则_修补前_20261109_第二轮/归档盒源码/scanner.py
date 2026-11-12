"""
归档盒 V0.1 — 核心扫描模块

职责：递归扫描目录，返回普通文件列表。
纯逻辑模块，零副作用，不创建任何文件或目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


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
    # 说明：未包含 out / build / target / dist / .cmake
    # —— 这些名称可能被用户合法使用（如"输出"、"课外"），
    #   交给 planner 的类型判定层处理（.class / .o 等归为"构建产物"类型）。
}

# 扫描时跳过的文件名（精确匹配，大小写不敏感）
SKIP_FILENAMES = {
    "icon", ".ds_store", "thumbs.db",
    ".gitignore", ".gitmodules", ".env.example", ".clang-format", ".editorconfig",
}

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
        entries = set(os.listdir(dirpath))
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


def _find_all_project_roots(root_dir: str) -> Dict[str, str]:
    """扫描 root_dir 下所有目录，返回所有像项目根的目录。

    不再做"最深优先"互斥——父目录有 .git、子目录有 package.json，两个都保护。
    文件只要落在任何一个保护根下就被跳过，父子/嵌套关系会自动覆盖。

    walk 时仍跳过隐藏目录（.git/.venv 等）——不需要进入它们内部来识别项目根，
    _looks_like_project_root 只看当前层的直接子项（listdir）。
    """
    roots: Dict[str, str] = {}
    try:
        for dirpath, dirnames, _filenames in os.walk(root_dir, followlinks=False):
            # 跳过隐藏目录（.git/.venv 等）和明确的 skip 目录
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in SKIP_DIRS
            ]
            reason = _looks_like_project_root(dirpath)
            if reason is not None:
                roots[dirpath] = reason
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
        if self.permission_errors:
            parts.append(f"权限错误 {self.permission_errors}")
        if self.empty_dirs:
            parts.append(f"空目录 {self.empty_dirs}")
        return "，".join(parts)


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


def scan_directory(
    root_path: str,
    protect_dirs: Optional[List[str]] = None,
    detect_project_roots: bool = True,
) -> ScanResult:
    """
    递归扫描目录，返回普通文件列表。

    扫描规则：
    - 跳过 .git、_已整理、.organizer 目录
    - 跳过以 '.' 开头的隐藏文件/目录（保留隐藏目录中的 .git 等显式规则已覆盖）
    - 跳过符号链接（os.path.islink 为 True 的）
    - 只读扫描，不创建任何文件或目录

    V1.1 新增：
    - protect_dirs: 显式保护目录，整体不整理。元素为路径字符串或
      (路径, 类别, 显示原因) 三元组，类别取 default / auto / manual
    - detect_project_roots: 自动识别含项目标记的根目录并整体保护（默认 True）
    """
    root = os.path.abspath(root_path)
    result = ScanResult(root=root)
    protect_dirs = protect_dirs or []

    if not os.path.exists(root):
        result.errors.append(f"路径不存在: {root}")
        return result
    if not os.path.isdir(root):
        result.errors.append(f"路径不是目录: {root}")
        return result
    if not os.access(root, os.R_OK):
        result.errors.append(f"无读取权限: {root}")
        result.permission_errors += 1
        return result

    # 使用 os.walk；对 skipdirs 设置 topdown 过滤
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 计算当前路径相对于 root 的层级，以决定跳过规则
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        # --- 过滤子目录（原地修改 dirnames 以阻止 walk 进入） ---
        dirs_to_remove = []
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            # 1. 符号链接目录
            if os.path.islink(full):
                result.symlink_count += 1
                dirs_to_remove.append(d)
                continue
            # 2. 显式跳过的目录名（大小写不敏感）
            if d.lower() in SKIP_DIRS:
                if d.lower() == ".git":
                    result.git_count += 1
                else:
                    result.skipdir_count += 1
                dirs_to_remove.append(d)
                continue
            # 3. 隐藏目录（以 '.' 开头，且不是 .git——.git 已在上面处理）
            if d.startswith("."):
                result.hidden_count += 1
                dirs_to_remove.append(d)
                continue
        for d in dirs_to_remove:
            dirnames.remove(d)

        # --- 处理当前目录下的文件 ---
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)

            # 符号链接文件
            if os.path.islink(full_path):
                result.symlink_count += 1
                skipped = FileEntry(
                    abs_path=full_path,
                    rel_path="",
                    filename=fname,
                    file_type="",
                    size=0,
                    size_display="",
                    skipped_reason="符号链接",
                )
                result.skipped.append(skipped)
                continue

            # 隐藏文件（以 '.' 开头）
            if fname.startswith("."):
                result.hidden_count += 1
                continue

            # 可配置跳过的文件名（大小写不敏感，如 Icon / .DS_Store）
            if fname.lower() in SKIP_FILENAMES:
                result.skipdir_count += 1  # 复用 skipdir_count 表示被显式排除
                continue

            # 权限检查
            if not os.access(full_path, os.R_OK):
                result.permission_errors += 1
                err_entry = FileEntry(
                    abs_path=full_path,
                    rel_path="",
                    filename=fname,
                    file_type="",
                    size=0,
                    size_display="",
                    skipped_reason="无读取权限",
                )
                result.skipped.append(err_entry)
                continue

            try:
                stat = os.stat(full_path, follow_symlinks=False)
            except OSError as e:
                result.errors.append(f"stat 失败 {full_path}: {e}")
                continue

            # 必须是普通文件（不是设备、socket、fifo 等）
            import stat as st_mod
            if not st_mod.S_ISREG(stat.st_mode):
                continue

            rel_path = os.path.relpath(full_path, root)
            ftype = classify_type(fname)
            fsize = stat.st_size

            entry = FileEntry(
                abs_path=full_path,
                rel_path=rel_path,
                filename=fname,
                file_type=ftype,
                size=fsize,
                size_display=format_size(fsize),
            )
            result.files.append(entry)

        # --- 空目录计数 ---
        if not filenames and not dirnames:
            result.empty_dirs += 1

    # 按相对路径稳定排序
    # V1.1 后处理：项目保护
    # 保护来源分三类：
    #   default — 程序默认保护（如归档盒自身源码）
    #   auto    — 扫描时自动识别的项目根目录
    #   manual  — 用户手动添加的保护目录
    # protect_dirs 元素可以是 str，也可以是 (path, kind, label) 三元组。
    explicit: Dict[str, tuple] = {}
    for p in protect_dirs:
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
        if os.path.isdir(ap) and (ap == root or ap.startswith(root + os.sep)):
            explicit[ap] = (kind, label)

    # 2) 自动识别所有项目根目录（不再互斥，父子/嵌套都保护）
    detected: Dict[str, str] = {}
    if detect_project_roots:
        detected = _find_all_project_roots(root)

    # 合并：显式保护 + 自动识别，不做父子互斥。
    # 文件只要落在任何一个保护根下就被跳过。
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

    # 3) 从 files 中剔除落在保护目录下的文件，加到 skipped
    if merged:
        protected_abs_set = set(merged.keys())
        new_files: List[FileEntry] = []
        for entry in result.files:
            is_protected = False
            for proot in protected_abs_set:
                if entry.abs_path == proot or entry.abs_path.startswith(proot + os.sep):
                    is_protected = True
                    break
            if is_protected:
                # 找命中的最深保护目录的原因
                hit_root = max(
                    (p for p in protected_abs_set
                     if entry.abs_path.startswith(p + os.sep) or entry.abs_path == p),
                    key=lambda x: x.count(os.sep),
                    default=None,
                )
                reason = result.protected_roots.get(hit_root, "项目保护") if hit_root else "项目保护"
                entry.skipped_reason = f"项目保护（{reason}）"
                result.skipped.append(entry)
                result.protected_count += 1
            else:
                new_files.append(entry)
        result.files = new_files

    result.files.sort(key=lambda e: e.rel_path.lower())
    result.skipped.sort(key=lambda e: e.abs_path.lower())

    return result
