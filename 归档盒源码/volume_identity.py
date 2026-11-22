"""外置盘可靠卷身份。

盘符或挂载名不能单独证明是同一卷；优先使用 UUID/卷序列号，辅以 st_dev。
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from typing import Callable, Optional

# 测试夹具可注入：(规范化根路径) -> VolumeInfo
_PROBE_OVERRIDE: Optional[Callable[[str], Optional["VolumeInfo"]]] = None


@dataclass(frozen=True)
class VolumeInfo:
    volume_id: str
    filesystem: str
    dev: int
    label: str = ""


def set_probe_override(handler: Optional[Callable[[str], Optional[VolumeInfo]]]) -> None:
    global _PROBE_OVERRIDE
    _PROBE_OVERRIDE = handler


def probe_volume(path: str) -> Optional[VolumeInfo]:
    """读取路径所在卷的可靠身份。路径不存在时返回 None。"""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if _PROBE_OVERRIDE is not None:
        custom = _PROBE_OVERRIDE(root)
        if custom is not None:
            return custom
    try:
        st = os.stat(root)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        try:
            st = os.stat(os.path.dirname(root) or root)
        except OSError:
            return None
    dev = int(st.st_dev)
    filesystem = _filesystem_type(root)
    label = _volume_label(root)
    volume_id = _volume_id(root, dev, filesystem, label)
    if not volume_id:
        volume_id = f"dev:{dev}"
    return VolumeInfo(volume_id=volume_id, filesystem=filesystem or "", dev=dev, label=label)


def volumes_match(expected: str, current: Optional[VolumeInfo]) -> bool:
    if not expected:
        return current is not None
    if current is None:
        return False
    return expected == current.volume_id


def identity_mismatch_message(expected: str, current: Optional[VolumeInfo], root: str) -> str:
    label = (current.label if current else "") or os.path.basename(root.rstrip(os.sep))
    return (
        f"路径仍指向「{label}」，但卷身份与登记时不一致（登记 {expected or '未知'}，"
        f"当前 {current.volume_id if current else '读不到'}）。"
        "不能当作同一外置盘打开，请核对后重新登记或改回正确路径。"
    )


def _volume_id(root: str, dev: int, filesystem: str, label: str) -> str:
    if os.name == "nt":
        serial, fs = _windows_volume_serial(root)
        if serial:
            return f"win:{serial}"
        if fs:
            filesystem = fs
    if sys.platform == "darwin":
        uuid = _macos_disk_uuid(root, dev)
        if uuid:
            return f"uuid:{uuid.lower()}"
    uuid = _linux_mount_uuid(dev)
    if uuid:
        return f"uuid:{uuid.lower()}"
    if label and dev:
        return f"label-dev:{label.casefold()}:{dev}"
    return f"dev:{dev}"


def _filesystem_type(path: str) -> str:
    if os.name == "nt":
        _, fs = _windows_volume_serial(path)
        return fs or ""
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(
                ["stat", "-f", "%T", path],
                capture_output=True, text=True, timeout=2, check=False)
            text = (out.stdout or "").strip()
            return text if out.returncode == 0 else ""
        except Exception:
            return ""
    return _linux_filesystem_type(path)


def _windows_volume_root(path: str) -> str:
    """GetVolumeInformationW 需要盘符根，例如 G:\\，不能用子目录。"""
    abs_path = os.path.abspath(path)
    drive = os.path.splitdrive(abs_path)[0]
    if drive:
        return drive + "\\"
    if abs_path.endswith("\\"):
        return abs_path
    return abs_path + "\\"


def _volume_label(root: str) -> str:
    if os.name == "nt":
        try:
            import ctypes
            rootp = _windows_volume_root(root)
            buf = ctypes.create_unicode_buffer(261)
            fs = ctypes.create_unicode_buffer(261)
            serial = ctypes.c_uint32()
            max_len = ctypes.c_uint32()
            flags = ctypes.c_uint32()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                rootp, buf, len(buf), ctypes.byref(serial),
                ctypes.byref(max_len), ctypes.byref(flags), fs, len(fs))
            if ok:
                return (buf.value or "").strip()
        except Exception:
            pass
        return ""
    return os.path.basename(root.rstrip(os.sep))


def _windows_volume_serial(path: str) -> tuple[str, str]:
    try:
        import ctypes
        root = _windows_volume_root(path)
        serial = ctypes.c_uint32()
        fs = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, ctypes.byref(serial), None, None, fs, len(fs))
        if not ok:
            return "", ""
        return f"{serial.value:08X}", (fs.value or "").strip()
    except Exception:
        return "", ""


def _macos_disk_uuid(root: str, dev: int) -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["diskutil", "info", root],
            capture_output=True, text=True, timeout=3, check=False)
        if out.returncode != 0:
            return ""
        for line in (out.stdout or "").splitlines():
            if "Volume UUID" in line or "Disk / Partition UUID" in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    return parts[1].strip()
    except Exception:
        pass
    return _linux_mount_uuid(dev)


def _linux_mount_uuid(dev: int) -> str:
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # mountinfo: ... optional_fields - mount_point ... - source ...
                parts = line.split()
                if len(parts) < 5:
                    continue
                try:
                    line_dev = int(parts[2].split(":")[0], 16) if ":" in parts[2] else 0
                except ValueError:
                    line_dev = 0
                if line_dev and line_dev != dev:
                    continue
                uuid = _uuid_from_mountinfo_parts(parts)
                if uuid:
                    return uuid
    except OSError:
        pass
    try:
        with open("/etc/mtab", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                source = parts[0]
                m = re.search(r"UUID=([0-9a-fA-F-]+)", source)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def _uuid_from_mountinfo_parts(parts: list[str]) -> str:
    for token in parts:
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", token):
            return token
        if token.startswith("UUID="):
            return token.split("=", 1)[1]
    dash = parts.index("-") if "-" in parts else -1
    if dash >= 0 and dash + 1 < len(parts):
        source = parts[dash + 1]
        m = re.search(r"UUID=([0-9a-fA-F-]+)", source)
        if m:
            return m.group(1)
    return ""


def _linux_filesystem_type(path: str) -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["stat", "-f", "-c", "%T", path],
            capture_output=True, text=True, timeout=2, check=False)
        if out.returncode == 0:
            return (out.stdout or "").strip()
    except Exception:
        pass
    try:
        st = os.statvfs(path)
        # Linux statvfs has f_fsid — not exposed in Python use mountinfo
        del st
    except OSError:
        pass
    dev = None
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return ""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as handle:
            best = ""
            for line in handle:
                parts = line.split()
                if len(parts) < 10:
                    continue
                mount_point = parts[4]
                try:
                    if os.path.samefile(mount_point, path):
                        best = parts[-3] if len(parts) > 8 else ""
                except OSError:
                    try:
                        if path.startswith(mount_point.rstrip("/") + "/") or path == mount_point:
                            best = parts[-3] if len(parts) > 8 else best
                    except Exception:
                        pass
                if dev is not None:
                    try:
                        major_minor = parts[2]
                        major = int(major_minor.split(":")[0], 16)
                        minor = int(major_minor.split(":")[1], 16)
                        import os as _os
                        line_dev = _os.makedev(major, minor) if hasattr(_os, "makedev") else 0
                    except (ValueError, IndexError):
                        line_dev = 0
                    if line_dev == dev and len(parts) > 8:
                        best = parts[-3]
            return best
    except OSError:
        return ""
    return ""