"""本地版本备份与安全恢复服务。

依赖 backup_store 的对象库与清单。备份不改源、不删源；恢复默认写入新的空目录。
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

from backup_store import (
    BackupStoreError, FileEntry, VersionManifest,
    commit_manifest, detect_case_conflicts, init_vault, is_empty_directory,
    list_recoverable_versions, list_versions, load_manifest,
    metadata_support_matrix, next_version_id, normalize_backup_rel,
    object_exists, object_path, validate_restore_rel_path,
    validate_vault_placement, vault_paths, verify_objects, write_object,
)
from backup_store import _utc_now  # noqa: SLC001 — 与仓库时间格式一致
from utils import sha256_file

Progress = Optional[Callable[[int, int, str], None]]

DEFAULT_SKIP_DIR_NAMES = {
    ".organizer", ".git", ".svn", "__pycache__", ".archivebox-backup",
}


@dataclass
class PreviewReport:
    ok: bool = True
    file_count: int = 0
    total_bytes: int = 0
    estimated_new_bytes: int = 0
    exclusions: list = field(default_factory=list)
    metadata_support: Dict[str, Any] = field(default_factory=dict)
    errors: list = field(default_factory=list)
    placement_errors: list = field(default_factory=list)


@dataclass
class BackupRunReport:
    version_id: str = ""
    state: str = "incomplete"
    recoverable: bool = False
    cancelled: bool = False
    errors: list = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    new_bytes: int = 0
    reused_objects: int = 0


@dataclass
class RestoreRunReport:
    ok: bool = False
    restored: int = 0
    errors: list = field(default_factory=list)
    version_id: str = ""
    target: str = ""


@dataclass
class VerifyReport:
    ok: bool = False
    errors: list = field(default_factory=list)


def preview_backup(source: str, vault: str,
                   policy: Optional[dict] = None,
                   protected_allow: Optional[Set[str]] = None) -> PreviewReport:
    report = PreviewReport(metadata_support=metadata_support_matrix())
    placement = validate_vault_placement(source, vault)
    if placement:
        report.ok = False
        report.placement_errors = placement
        return report
    if not os.path.isdir(source):
        report.ok = False
        report.errors.append(f"来源目录不存在：{source}")
        return report
    files, excluded = _collect_files(source, policy, protected_allow or set())
    report.exclusions = excluded
    report.file_count = len(files)
    report.total_bytes = sum(item["size"] for item in files)
    existing = set()
    if os.path.isdir(vault):
        paths = vault_paths(vault)
        obj_root = paths["objects"]
        if os.path.isdir(obj_root):
            for dirpath, _, names in os.walk(obj_root):
                for name in names:
                    if len(name) == 64:
                        existing.add(name)
    new_bytes = 0
    for item in files:
        digest = sha256_file(item["path"]) or ""
        if digest and digest not in existing:
            new_bytes += item["size"]
    report.estimated_new_bytes = new_bytes
    return report


def run_backup(source: str, vault: str, *,
               policy: Optional[dict] = None,
               protected_allow: Optional[Set[str]] = None,
               cancel_event=None,
               progress_cb: Progress = None,
               note: str = "") -> BackupRunReport:
    report = BackupRunReport()
    placement = validate_vault_placement(source, vault)
    if placement:
        report.errors.extend(placement)
        return report
    source_root = os.path.realpath(os.path.abspath(source))
    try:
        st = os.stat(source_root)
        source_identity = {
            "dev": int(st.st_dev),
            "ino": int(st.st_ino),
        }
    except OSError as exc:
        report.errors.append(f"无法读取来源身份：{exc}")
        return report
    try:
        paths = init_vault(vault, source_root, source_identity)
    except BackupStoreError as exc:
        report.errors.append(str(exc))
        return report
    version_id = next_version_id(paths)
    report.version_id = version_id
    files, excluded = _collect_files(source_root, policy, protected_allow or set())
    conflicts = detect_case_conflicts([item["rel"] for item in files])
    if conflicts:
        report.errors.append(
            "来源存在大小写冲突：" + "；".join(f"{a} / {b}" for a, b in conflicts[:5]))
        return report
    manifest = VersionManifest(
        version_id=version_id,
        state="incomplete",
        source_root=source_root,
        source_identity=source_identity,
        created_at=_utc_now(),
        excluded=excluded,
        metadata_support=metadata_support_matrix(),
    )
    try:
        total = len(files)
        for index, item in enumerate(files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                raise BackupStoreError("备份已取消")
            if progress_cb:
                progress_cb(index, total, item["rel"])
            path = item["path"]
            rel = normalize_backup_rel(item["rel"])
            path_errors = validate_restore_rel_path(rel)
            if path_errors:
                raise BackupStoreError(f"{rel}：" + "；".join(path_errors))
            if os.path.islink(path):
                entry = FileEntry(
                    rel_path=rel,
                    size=0,
                    sha256="",
                    mtime_ns=item["mtime_ns"],
                    mode=item["mode"],
                    is_symlink=True,
                    link_target=os.readlink(path),
                )
                manifest.files.append(entry)
                continue
            before = _file_stat(path)
            digest = sha256_file(path)
            if not digest:
                raise BackupStoreError(f"无法计算哈希：{rel}")
            after = _file_stat(path)
            if before != after:
                raise BackupStoreError(f"复制过程中来源发生变化：{rel}")
            if object_exists(paths, digest):
                report.reused_objects += 1
                ref = f"{digest[:2]}/{digest}"
            else:
                ref = write_object(paths, path, digest)
                report.new_bytes += item["size"]
            manifest.files.append(FileEntry(
                rel_path=rel,
                size=item["size"],
                sha256=digest,
                mtime_ns=item["mtime_ns"],
                mode=item["mode"],
                object_ref=ref,
            ))
        manifest.file_count = len(manifest.files)
        manifest.total_bytes = sum(e.size for e in manifest.files)
        manifest.new_bytes = report.new_bytes
        commit_manifest(paths, version_id, manifest, complete=True)
        report.state = "complete"
        report.recoverable = True
        report.file_count = manifest.file_count
        report.total_bytes = manifest.total_bytes
        return report
    except Exception as exc:
        report.errors.append(str(exc))
        report.state = "incomplete"
        report.recoverable = False
        manifest.errors = list(report.errors)
        manifest.file_count = len(manifest.files)
        manifest.total_bytes = sum(e.size for e in manifest.files)
        manifest.new_bytes = report.new_bytes
        try:
            commit_manifest(paths, version_id, manifest, complete=False)
        except Exception as mark_exc:
            report.errors.append(f"登记未完成清单失败：{mark_exc}")
        return report


def restore_version(vault: str, version_id: str, target: str, *,
                    cancel_event=None,
                    progress_cb: Progress = None) -> RestoreRunReport:
    report = RestoreRunReport(version_id=version_id, target=target)
    paths = vault_paths(vault)
    manifest = load_manifest(paths, version_id)
    if manifest is None:
        report.errors.append("版本不存在")
        return report
    if not manifest.recoverable:
        report.errors.append(
            f"版本状态为 {manifest.state}，不能作为可恢复完成版本")
        return report
    target_abs = os.path.abspath(target)
    if os.path.exists(target_abs):
        if not os.path.isdir(target_abs):
            report.errors.append("恢复目标不是目录")
            return report
        if not is_empty_directory(target_abs):
            report.errors.append("恢复目标必须是新的空目录（非空）")
            return report
    else:
        os.makedirs(target_abs, exist_ok=True)
    target_real = os.path.realpath(target_abs)
    try:
        total = len(manifest.files)
        for index, entry in enumerate(manifest.files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise BackupStoreError("恢复已取消")
            if progress_cb:
                progress_cb(index, total, entry.rel_path)
            path_errors = validate_restore_rel_path(entry.rel_path)
            if path_errors:
                raise BackupStoreError(
                    f"路径逃逸或非法：{entry.rel_path}（{'；'.join(path_errors)}）")
            rel = normalize_backup_rel(entry.rel_path)
            dest = os.path.join(target_abs, *rel.split("/"))
            parent = os.path.dirname(dest)
            os.makedirs(parent, exist_ok=True)
            parent_real = os.path.realpath(parent)
            try:
                inside = os.path.commonpath([parent_real, target_real]) == target_real
            except ValueError:
                inside = False
            if not inside:
                raise BackupStoreError(f"恢复路径越界：{entry.rel_path}")
            if os.path.lexists(dest):
                raise BackupStoreError(f"恢复目标已被占用，拒绝覆盖：{entry.rel_path}")
            if entry.is_symlink:
                os.symlink(entry.link_target, dest)
                report.restored += 1
                continue
            if not entry.sha256 or not object_exists(paths, entry.sha256):
                raise BackupStoreError(f"备份对象缺失或损坏：{entry.rel_path}")
            obj = object_path(paths, entry.sha256)
            tmp = dest + ".restoring"
            if os.path.lexists(tmp):
                if os.path.isdir(tmp) and not os.path.islink(tmp):
                    shutil.rmtree(tmp)
                else:
                    os.remove(tmp)
            shutil.copyfile(obj, tmp)
            if sha256_file(tmp) != entry.sha256:
                raise BackupStoreError(f"恢复写入后校验失败：{entry.rel_path}")
            if entry.mode and os.name != "nt":
                try:
                    os.chmod(tmp, entry.mode & 0o777)
                except OSError:
                    pass
            if os.path.lexists(dest):
                raise BackupStoreError(f"恢复过程中目标被替换：{entry.rel_path}")
            os.replace(tmp, dest)
            report.restored += 1
        report.ok = True
        return report
    except Exception as exc:
        report.errors.append(str(exc))
        report.ok = False
        return report


def verify_version(vault: str, version_id: str) -> VerifyReport:
    paths = vault_paths(vault)
    manifest = load_manifest(paths, version_id)
    if manifest is None:
        return VerifyReport(ok=False, errors=["版本不存在"])
    if not manifest.recoverable:
        return VerifyReport(
            ok=False, errors=[f"版本状态为 {manifest.state}"])
    ok, errors = verify_objects(paths, manifest)
    return VerifyReport(ok=ok, errors=errors)


def list_backup_versions(vault: str, recoverable_only: bool = False):
    paths = vault_paths(vault)
    if recoverable_only:
        return list_recoverable_versions(paths)
    return list_versions(paths)


def _collect_files(source: str, policy: Optional[dict],
                   protected_allow: Set[str]):
    files = []
    excluded = []
    protected = set()
    if policy:
        protected = {
            normalize_backup_rel(p) for p in (policy.get("protected_prefixes") or [])
        }
    allow = {normalize_backup_rel(p) for p in protected_allow}
    skip_dirs = set(DEFAULT_SKIP_DIR_NAMES)
    if policy:
        skip_dirs.update(
            name.casefold() for name in (policy.get("exclude_dir_names") or []))
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name.casefold() not in {n.casefold() for n in skip_dirs}
            and not name.startswith(".演示")
        ]
        for name in filenames:
            if name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                continue
            path = os.path.join(dirpath, name)
            rel = normalize_backup_rel(os.path.relpath(path, source))
            blocked = None
            for prefix in protected:
                if rel == prefix or rel.startswith(prefix + "/"):
                    if prefix not in allow and not any(
                            rel == a or rel.startswith(a + "/") for a in allow):
                        blocked = prefix
                    break
            if blocked:
                excluded.append({
                    "path": rel,
                    "reason": f"受保护前缀 {blocked}，未显式授权备份",
                })
                continue
            try:
                st = os.lstat(path)
            except OSError as exc:
                excluded.append({"path": rel, "reason": str(exc)})
                continue
            files.append({
                "path": path,
                "rel": rel,
                "size": int(st.st_size) if not os.path.islink(path) else 0,
                "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
                "mode": int(stat.S_IMODE(st.st_mode)),
            })
    return files, excluded


def _file_stat(path: str) -> tuple:
    st = os.lstat(path)
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
    )
