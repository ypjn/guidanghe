"""本地版本备份与安全恢复。

备份不改源、不删源、不传播删除。恢复默认写入新的空目录，复制对象而非硬链接。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from backup_store import (
    BackupError, BackupItem, BackupPreview, BackupVersion,
    commit_manifest, ensure_vault, get_version, list_versions,
    mark_failed, object_path, utc_now, write_draft_manifest,
    write_object_atomic,
)

Progress = Optional[Callable[[int, int, str], None]]

DEFAULT_EXCLUDES = {
    ".organizer", ".git", ".svn", "__pycache__", ".DS_Store",
    "Thumbs.db", "desktop.ini",
}

WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

METADATA_SUPPORT = [
    "相对路径",
    "大小",
    "SHA-256 内容校验",
    "来源 identity（dev/ino/size/mtime_ns/ctime_ns）",
    "POSIX 权限位（mode）——尽力保存，恢复时按目标系统应用",
]
METADATA_NOT_CLAIMED = [
    "不是系统级整盘备份",
    "不保证 ACL / 扩展属性 / 所有者 / Finder 标签完整恢复",
    "同一物理盘上的仓库不具备整盘损坏容灾能力",
]


@dataclass
class BackupReport:
    version_id: str = ""
    completed: bool = False
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    item_count: int = 0
    total_bytes: int = 0
    reused_objects: int = 0
    written_objects: int = 0


@dataclass
class RestoreReport:
    version_id: str = ""
    completed: bool = False
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    restored: int = 0
    target_root: str = ""


def assert_independent_roots(source: str, vault: str) -> None:
    source = os.path.realpath(os.path.abspath(source))
    vault = os.path.realpath(os.path.abspath(vault))
    if not os.path.isdir(source):
        raise BackupError(f"来源目录不存在：{source}")
    if source == vault:
        raise BackupError("备份仓库不能与来源是同一目录")
    try:
        if os.path.commonpath([source, vault]) in {source, vault}:
            raise BackupError("备份仓库不得与来源相互嵌套或重叠")
    except ValueError:
        # 不同盘符/根，允许。
        pass


def preview_backup(source: str, vault: str,
                   extra_excludes: Optional[list[str]] = None) -> BackupPreview:
    assert_independent_roots(source, vault)
    excludes = sorted(DEFAULT_EXCLUDES | set(extra_excludes or []))
    files = list(_iter_source_files(source, excludes))
    total = sum(size for _, size, _ in files)
    existing = _existing_digests(vault) if os.path.isdir(vault) else set()
    estimated_new = 0
    for path, size, _ in files:
        digest = _sha256_file(path)
        if digest not in existing:
            estimated_new += size
    warnings = list(METADATA_NOT_CLAIMED)
    return BackupPreview(
        file_count=len(files),
        total_bytes=total,
        estimated_new_bytes=estimated_new,
        exclusions=excludes,
        metadata_support=list(METADATA_SUPPORT),
        warnings=warnings,
    )


def create_backup(source: str, vault: str, *,
                  extra_excludes: Optional[list[str]] = None,
                  note: str = "",
                  cancel_event=None,
                  progress_cb: Progress = None) -> BackupReport:
    assert_independent_roots(source, vault)
    ensure_vault(vault)
    report = BackupReport()
    version_id = "bv-" + uuid.uuid4().hex[:16]
    report.version_id = version_id
    excludes = sorted(DEFAULT_EXCLUDES | set(extra_excludes or []))
    source_root = os.path.realpath(os.path.abspath(source))
    version = BackupVersion(
        version_id=version_id,
        created_at=utc_now(),
        source_root=source_root,
        source_identity=_path_identity(source_root),
        status="drafting",
        note=note,
    )
    try:
        files = list(_iter_source_files(source_root, excludes))
        total = len(files)
        for index, (path, size, rel) in enumerate(files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                raise BackupError("备份已取消")
            if progress_cb:
                progress_cb(index, total, rel)
            safe = _validate_rel_path(rel)
            identity = _path_identity(path)
            digest = _sha256_file(path)
            # 复制前再看一眼，来源中途变化则失败该项。
            again = _path_identity(path)
            if again != identity:
                raise BackupError(f"复制过程中来源发生变化：{rel}")
            dest = object_path(vault, digest)
            if os.path.isfile(dest):
                if _sha256_file(dest) != digest:
                    raise BackupError(f"对象库中已有损坏对象：{digest}")
                report.reused_objects += 1
            else:
                write_object_atomic(vault, digest, path)
                if _sha256_file(dest) != digest:
                    raise BackupError(f"对象写入后校验失败：{rel}")
                report.written_objects += 1
            version.items.append(BackupItem(
                rel_path=safe,
                size=size,
                sha256=digest,
                source_identity=identity,
                metadata=_collect_metadata(path),
                object_key=digest,
            ))
        write_draft_manifest(vault, version)
        commit_manifest(vault, version)
        report.completed = True
        report.item_count = len(version.items)
        report.total_bytes = sum(item.size for item in version.items)
        return report
    except Exception as exc:
        report.errors.append(str(exc))
        try:
            mark_failed(vault, version, str(exc))
        except Exception as mark_exc:
            report.errors.append(f"登记失败状态时出错：{mark_exc}")
        report.completed = False
        return report


def restore_version(vault: str, version_id: str, target: str, *,
                    cancel_event=None,
                    progress_cb: Progress = None) -> RestoreReport:
    report = RestoreReport(version_id=version_id, target_root=target)
    version = get_version(vault, version_id)
    if version is None:
        report.errors.append("版本不存在")
        return report
    if version.status != "complete":
        report.errors.append(
            f"版本状态为 {version.status}，不能作为可恢复完成版本")
        return report
    target = os.path.abspath(target)
    if os.path.exists(target):
        if not os.path.isdir(target):
            report.errors.append("恢复目标不是目录")
            return report
        if os.listdir(target):
            report.errors.append("恢复目标必须是新的空目录")
            return report
    else:
        os.makedirs(target, exist_ok=True)
    target_real = os.path.realpath(target)
    try:
        total = len(version.items)
        for index, item in enumerate(version.items, start=1):
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                raise BackupError("恢复已取消")
            if progress_cb:
                progress_cb(index, total, item.rel_path)
            rel = _validate_rel_path(item.rel_path)
            dest = os.path.join(target, rel.replace("/", os.sep))
            dest_real_parent = os.path.realpath(os.path.dirname(dest) or target)
            try:
                inside = os.path.commonpath(
                    [dest_real_parent, target_real]) == target_real
            except ValueError:
                inside = False
            if not inside:
                raise BackupError(f"恢复路径越界：{item.rel_path}")
            if os.path.lexists(dest):
                raise BackupError(f"恢复目标已被占用，拒绝覆盖：{item.rel_path}")
            obj = object_path(vault, item.object_key or item.sha256)
            if not os.path.isfile(obj):
                raise BackupError(f"备份对象缺失：{item.rel_path}")
            digest = _sha256_file(obj)
            if digest != item.sha256:
                raise BackupError(f"备份对象校验失败：{item.rel_path}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # 复制到临时名再置换，避免中途目标被换成链接。
            tmp = dest + ".restoring"
            if os.path.lexists(tmp):
                if os.path.isdir(tmp) and not os.path.islink(tmp):
                    shutil.rmtree(tmp)
                else:
                    os.remove(tmp)
            shutil.copyfile(obj, tmp)
            if _sha256_file(tmp) != item.sha256:
                raise BackupError(f"恢复写入后校验失败：{item.rel_path}")
            mode = item.metadata.get("mode")
            if isinstance(mode, int):
                try:
                    os.chmod(tmp, mode & 0o777)
                except OSError:
                    pass
            if os.path.lexists(dest):
                raise BackupError(f"恢复过程中目标被替换：{item.rel_path}")
            os.replace(tmp, dest)
            report.restored += 1
        report.completed = True
        return report
    except Exception as exc:
        report.errors.append(str(exc))
        report.completed = False
        return report


def verify_version(vault: str, version_id: str) -> list[str]:
    version = get_version(vault, version_id)
    if version is None:
        return ["版本不存在"]
    if version.status != "complete":
        return [f"版本状态为 {version.status}"]
    problems = []
    for item in version.items:
        try:
            _validate_rel_path(item.rel_path)
        except BackupError as exc:
            problems.append(str(exc))
            continue
        obj = object_path(vault, item.object_key or item.sha256)
        if not os.path.isfile(obj):
            problems.append(f"缺失对象：{item.rel_path}")
            continue
        digest = _sha256_file(obj)
        if digest != item.sha256:
            problems.append(f"对象损坏：{item.rel_path}")
    return problems


def _iter_source_files(root: str, excludes: list[str]):
    exclude_set = {name.casefold() for name in excludes}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name.casefold() not in exclude_set and not name.startswith(".演示")
        ]
        for name in filenames:
            if name.casefold() in exclude_set:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            yield path, os.path.getsize(path), rel


def _validate_rel_path(rel: str) -> str:
    if not rel or rel.strip() != rel:
        raise BackupError("非法相对路径")
    normalized = rel.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise BackupError(f"拒绝绝对路径：{rel}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise BackupError(f"拒绝越界相对路径：{rel}")
    for part in parts:
        stem = part.split(".")[0].upper()
        if stem in WIN_RESERVED:
            raise BackupError(f"拒绝 Windows 保留名：{rel}")
        if any(ch in part for ch in '<>:"|?*\0'):
            raise BackupError(f"路径含非法字符：{rel}")
    return "/".join(parts)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _path_identity(path: str) -> dict:
    st = os.lstat(path)
    return {
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
        "ctime_ns": int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9))),
    }


def _collect_metadata(path: str) -> dict:
    st = os.lstat(path)
    return {
        "mode": int(stat.S_IMODE(st.st_mode)),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
    }


def _existing_digests(vault: str) -> set[str]:
    root = os.path.join(vault, "objects")
    found: set[str] = set()
    if not os.path.isdir(root):
        return found
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if len(name) == 64 and all(ch in "0123456789abcdef" for ch in name):
                found.add(name)
    return found


# re-export helpers used by tests/UI
__all__ = [
    "BackupError", "BackupPreview", "BackupReport", "RestoreReport",
    "METADATA_SUPPORT", "METADATA_NOT_CLAIMED",
    "assert_independent_roots", "preview_backup", "create_backup",
    "restore_version", "verify_version", "list_versions", "get_version",
]
