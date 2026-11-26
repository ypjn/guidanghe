"""整理空间的安全边界。

选成整理空间之前拒绝磁盘根、用户主目录和系统目录。
扫描和移动时再跳过资源库、软件包和备份，避免把系统文件拆开搬走。
"""

from __future__ import annotations

import os

# 目录名以这些后缀结尾时，它是一个整体文件包。拆开内部文件会损坏软件或文档。
BUNDLE_SUFFIXES = (
    ".app", ".framework", ".kext", ".plugin", ".bundle", ".appex",
    ".photoslibrary", ".musiclibrary", ".tvlibrary", ".aplibrary",
    ".sparsebundle", ".vmwarevm", ".pvm",
    ".prefpane", ".qlgenerator", ".mdimporter", ".saver",
    ".xcodeproj", ".xcworkspace", ".playground",
    ".logicx", ".band", ".fcpbundle", ".imovielibrary",
    ".rtfd", ".pages", ".numbers", ".key",
    ".pkg", ".mpkg",
)

# 路径已经规范化之后，整棵子树都不能整理。键用小写。
_POSIX_PREFIXES = (
    ("/system/library", "这是 macOS 系统资源库"),
    ("/system/applications", "这是系统应用程序目录"),
    ("/system/iossupport", "这是系统目录"),
    ("/system/driverkit", "这是系统驱动目录"),
    ("/system/volumes/preboot", "这是系统预启动卷"),
    ("/system/volumes/vm", "这是系统虚拟内存卷"),
    ("/system/volumes/update", "这是系统更新卷"),
    ("/system/volumes/hardware", "这是系统目录"),
    ("/system/volumes/xarts", "这是系统目录"),
    ("/system/volumes/iscpreboot", "这是系统目录"),
    ("/library", "这是系统资源库"),
    ("/applications", "这是应用程序目录"),
    ("/usr", "这是系统程序目录"),
    ("/bin", "这是系统程序目录"),
    ("/sbin", "这是系统程序目录"),
    ("/opt", "这是系统或软件安装目录"),
    ("/dev", "这是设备目录"),
    ("/cores", "这是系统转储目录"),
    ("/private/etc", "这是系统配置目录"),
    ("/etc", "这是系统配置目录"),
    ("/private/var/db", "这是系统数据库目录"),
    ("/private/var/vm", "这是虚拟内存目录"),
    ("/private/var/root", "这是系统管理员目录"),
    ("/private/var/log", "这是系统日志目录"),
    ("/private/var/audit", "这是系统审计目录"),
    ("/private/var/install", "这是系统安装目录"),
    ("/private/var/protected", "这是受保护的系统目录"),
    ("/private/var/networkd", "这是系统网络目录"),
    ("/var/db", "这是系统数据库目录"),
    ("/var/vm", "这是虚拟内存目录"),
    ("/var/log", "这是系统日志目录"),
    ("/var/root", "这是系统管理员目录"),
    ("/boot", "这是启动目录"),
    ("/proc", "这是系统进程目录"),
    ("/sys", "这是系统内核目录"),
    ("/run", "这是系统运行目录"),
    ("/snap", "这是软件安装目录"),
    ("/lib", "这是系统库目录"),
    ("/lib64", "这是系统库目录"),
)

# 只能拒绝目录自己，不能把子目录一起拒绝。临时目录的子文件夹仍可整理。
_POSIX_EXACT = {
    "/": "这是磁盘根目录，里面包含系统和全部用户的文件",
    "/users": "这是所有用户的目录",
    "/home": "这是所有用户的目录",
    "/volumes": "这是外置磁盘的挂载目录",
    "/private": "这是系统内部目录",
    "/private/var": "这是系统运行数据目录",
    "/private/tmp": "这是系统临时目录，请改选里面的具体文件夹",
    "/tmp": "这是系统临时目录，请改选里面的具体文件夹",
    "/var": "这是系统运行数据目录",
    "/private/var/folders": "这是系统临时目录，请改选里面的具体文件夹",
    "/system": "这是系统目录",
    "/system/volumes": "这是系统卷目录",
    "/root": "这是系统管理员的主目录",
}

_HOME_REASON = (
    "这是用户主目录。里面有资源库、应用程序，"
    "以及各软件直接放在主目录下的数据，不能从这里开始整理"
)

_VOLUME_REASON = "这是一整块磁盘的根目录，请改选里面的文件夹"

_UNRESOLVED = "无法确认这个文件夹的真实位置，不能整理"

_WIN_DRIVE_ROOT_NAMES = {
    "windows.old", "$windows.~bt", "$windows.~ws", "perflogs", "recovery",
    "config.msi", "system volume information", "$recycle.bin", "recycler",
    "msocache",
}

_WIN_PROFILE_DATA = {"appdata", "application data", "local settings"}

_NOT_A_PROFILE = {"public", "default", "default user", "all users", "shared"}


def unsafe_root_reason(path: str) -> str:
    """非空表示这个目录不能作为整理空间。"""
    if os.name == "nt":
        try:
            real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        except (OSError, ValueError):
            return _UNRESOLVED
        return windows_root_reason(real)
    canonical = _canonical_posix(path)
    if not canonical:
        return _UNRESOLVED
    home = _canonical_posix(os.path.expanduser("~")) or ""
    return posix_root_reason(canonical, home=home)


def unsafe_root_message(path: str) -> str:
    """选择被拒绝时给用户看的说明。"""
    reason = unsafe_root_reason(path) or "这个文件夹不能作为整理空间"
    return (
        f"{reason}\n\n{path}\n\n"
        "请改选「桌面」「文稿」「下载」，或你自己建立的资料夹。\n"
        "磁盘根、整块外置盘、用户主目录、资源库、应用程序和系统目录都不能作为整理空间。"
        "整理空间里如果混有软件包、资源库或备份，扫描时会整夹跳过，不会拆开移动。"
    )


def affected_reason(path: str, is_dir: bool = False) -> str:
    """文件或目录落在不能移动的位置时返回原因。空串表示可以整理。

    目录会解析真实路径，用来认出指向资源库的目录。普通文件先按路径文字判断；
    扫描时符号链接已经单独跳过，避免每个文件都再解析一次。
    """
    if not path:
        return ""
    try:
        absolute = os.path.abspath(os.path.expanduser(path))
    except (OSError, ValueError):
        return "无法确认真实路径，不移动"
    if os.name == "nt":
        reason = _bundle_reason(absolute, is_dir=is_dir) or windows_tree_reason(absolute)
        if reason or not is_dir:
            return reason
        try:
            resolved = os.path.realpath(absolute)
        except (OSError, ValueError):
            return "无法确认真实路径，不移动"
        return _bundle_reason(resolved, is_dir=True) or windows_tree_reason(resolved)
    reason = _posix_affected(absolute, is_dir=is_dir)
    if reason or not is_dir:
        return reason
    canonical = _canonical_posix(absolute)
    if not canonical:
        return "无法确认真实路径，不移动"
    if os.path.normcase(canonical) == os.path.normcase(os.path.normpath(absolute)):
        return ""
    return _posix_affected(canonical, is_dir=True)


def posix_root_reason(canonical: str, home: str = "") -> str:
    """canonical 已是真实路径，并且去掉了 Data 卷前缀。不访问磁盘。"""
    canonical = os.path.normpath(canonical or "")
    if canonical in {"", "."}:
        return _UNRESOLVED
    bundle = _bundle_reason(canonical, is_dir=True)
    if bundle:
        return bundle
    tree = posix_tree_reason(canonical)
    if tree:
        return tree
    key = canonical.lower().rstrip("/") or "/"
    exact = _POSIX_EXACT.get(key)
    if exact:
        return exact
    parent = os.path.dirname(canonical).lower()
    if parent == "/volumes":
        return _VOLUME_REASON
    if home and _same_posix(canonical, home):
        return _HOME_REASON
    if _is_posix_home(canonical):
        return _HOME_REASON
    base = os.path.basename(canonical)
    if base.startswith(".") and base not in {".", ".."}:
        return "这是隐藏目录，里面通常是软件自己的配置，不能作为整理空间"
    return ""


def posix_tree_reason(canonical: str) -> str:
    """子树不能进入整理计划的原因。磁盘根和用户主目录不在这里拒绝。"""
    named = _named_danger(canonical)
    if named:
        return named
    library = _user_library_reason(canonical)
    if library:
        return library
    lowered = canonical.lower().rstrip("/") or "/"
    for prefix, reason in _POSIX_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            return reason
    return ""


def windows_root_reason(
    path: str,
    home: str = "",
    system_root: str = r"C:\Windows",
    program_dirs: tuple[str, ...] | None = None,
    program_data: str = r"C:\ProgramData",
) -> str:
    """Windows 路径判断。不访问磁盘，供测试直接调用。"""
    path = _norm_win(path)
    if not path:
        return _UNRESOLVED
    bundle = _bundle_reason(path, is_dir=True)
    if bundle:
        return bundle
    if program_dirs is None:
        program_dirs = (r"C:\Program Files", r"C:\Program Files (x86)")
    tree = windows_tree_reason(
        path, system_root=system_root, program_dirs=program_dirs,
        program_data=program_data)
    if tree:
        return tree
    if _is_windows_root(path):
        return "这是磁盘或共享的根目录，请改选里面的文件夹"
    profile = _windows_profile_reason(path)
    if profile:
        return profile
    if home and _same_win(path, home):
        return _HOME_REASON
    return ""


def windows_tree_reason(
    path: str,
    system_root: str = r"C:\Windows",
    program_dirs: tuple[str, ...] | None = None,
    program_data: str = r"C:\ProgramData",
) -> str:
    path = _norm_win(path)
    named = _named_danger(path)
    if named:
        return named
    if program_dirs is None:
        program_dirs = (r"C:\Program Files", r"C:\Program Files (x86)")
    for prefix, reason in (
        (system_root, "这是 Windows 系统目录"),
        (program_data, "这是 Windows 的程序数据目录"),
    ):
        if prefix and _win_under(path, prefix):
            return reason
    for prefix in program_dirs:
        if prefix and _win_under(path, prefix):
            return "这是程序安装目录"
    parts = [part for part in path.split("\\") if part]
    if (len(parts) >= 4 and parts[1].casefold() == "users"
            and parts[2].casefold() not in _NOT_A_PROFILE
            and parts[3].casefold() in _WIN_PROFILE_DATA):
        return "这是 Windows 用户的应用数据，不能整理"
    if len(parts) >= 2 and parts[1].casefold() in _WIN_DRIVE_ROOT_NAMES:
        return "这是 Windows 系统目录"
    return ""


def _posix_affected(path: str, is_dir: bool) -> str:
    lexical = _strip_data_prefix(os.path.normpath(path))
    bundle = _bundle_reason(path, is_dir=is_dir) or _bundle_reason(lexical, is_dir=is_dir)
    if bundle:
        return bundle
    return posix_tree_reason(lexical)


def _strip_data_prefix(path: str) -> str:
    data = "/System/Volumes/Data"
    if path.lower() == data.lower():
        return "/"
    prefix = data + "/"
    if path.lower().startswith(prefix.lower()):
        return os.path.normpath("/" + path[len(prefix):])
    return path


def _canonical_posix(path: str) -> str:
    """解析符号链接，并把 /System/Volumes/Data 折叠回对应的系统路径。"""
    try:
        real = os.path.normpath(os.path.realpath(
            os.path.abspath(os.path.expanduser(path))))
    except (OSError, ValueError):
        return ""
    data = "/System/Volumes/Data"
    if real.lower() == data.lower():
        return "/"
    prefix = data + "/"
    if real.lower().startswith(prefix.lower()):
        return os.path.normpath("/" + real[len(prefix):])
    return real


def _bundle_reason(path: str, is_dir: bool) -> str:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not is_dir and parts:
        parts = parts[:-1]
    for part in parts:
        lower = part.casefold()
        for suffix in BUNDLE_SUFFIXES:
            if lower.endswith(suffix) and len(lower) > len(suffix):
                return f"「{part}」是一个整体文件包，不拆开、也不移动里面的文件"
    return ""


def _named_danger(path: str) -> str:
    for part in path.replace("\\", "/").split("/"):
        if part.casefold() == "backups.backupdb":
            return "这是时光机备份，不能整理"
    return ""


def _user_library_reason(canonical: str) -> str:
    parts = [part for part in canonical.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index].lower() != "users":
            continue
        if parts[index + 1].lower() in {"shared", "public"}:
            continue
        kind = parts[index + 2].lower()
        if kind == "library":
            return "这是用户资源库（Library），里面是各软件的数据和偏好"
        if kind == "applications":
            return "这是用户的应用程序目录"
    return ""


def _is_posix_home(canonical: str) -> bool:
    parts = [part for part in canonical.split("/") if part]
    if len(parts) != 2:
        return False
    head, name = parts[0].lower(), parts[1].lower()
    if head == "users" and name not in {"shared", "public"}:
        return True
    if head == "home" and name not in {"shared"}:
        return True
    return False


def _same_posix(left: str, right: str) -> bool:
    return os.path.normpath(left).lower() == os.path.normpath(right).lower()


def _norm_win(path: str) -> str:
    text = (path or "").replace("/", "\\")
    if text.startswith("\\\\"):
        body = text[2:]
        while "\\\\" in body:
            body = body.replace("\\\\", "\\")
        text = "\\\\" + body
    else:
        while "\\\\" in text:
            text = text.replace("\\\\", "\\")
    if len(text) > 3 and text.endswith("\\"):
        text = text.rstrip("\\")
    return text


def _win_under(path: str, prefix: str) -> bool:
    folder = _norm_win(prefix).casefold().rstrip("\\")
    if not folder:
        return False
    current = path.casefold()
    return current == folder or current.startswith(folder + "\\")


def _is_windows_root(path: str) -> bool:
    if path.startswith("\\\\"):
        parts = [part for part in path.split("\\") if part]
        return len(parts) <= 2
    return len(path) in {2, 3} and path[1:2] == ":"


def _windows_profile_reason(path: str) -> str:
    parts = [part for part in path.split("\\") if part]
    if len(parts) == 2 and parts[1].casefold() == "users":
        return "这是所有用户的目录"
    if (len(parts) == 3 and parts[1].casefold() == "users"
            and parts[2].casefold() not in _NOT_A_PROFILE):
        return _HOME_REASON
    return ""


def _same_win(left: str, right: str) -> bool:
    return _norm_win(left).casefold() == _norm_win(right).casefold()
