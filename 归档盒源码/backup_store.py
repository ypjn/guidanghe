"""本地版本备份仓库：对象存储与清单读写。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils import sha256_file, win_long_path

VAULT_MARKER = ".archivebox-backup"
VAULT_SCHEMA = 1
MANIFEST_SCHEMA = 1
OBJECT_CHUNK = 1 << 20

_WIN_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


class BackupStoreError(ValueError):
    """仓库或清单不合法、不可读、不可写。"""


@dataclass
class FileEntry:
    rel_path: str
    size: int
    sha256: str
    mtime_ns: int = 0
    mode: int = 0
    object_ref: str = ""
    is_symlink: bool = False
    link_target: str = ""


@dataclass
class VersionManifest:
    version_id: str
    state: str
    source_root: str
    source_identity: Dict[str, Any]
    created_at: str
    files: List[FileEntry] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata_support: Dict[str, Any] = field(default_factory=dict)
    file_count: int = 0
    total_bytes: int = 0
    new_bytes: int = 0
    note: str = ""

    @property
    def recoverable(self) -> bool:
        return self.state == "complete" and not self.errors


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm_root(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _paths_overlap(a: str, b: str) -> bool:
    ra, rb = _norm_root(a), _norm_root(b)
    if ra == rb:
        return True
    try:
        common = os.path.commonpath([ra, rb])
    except ValueError:
        return False
    return common == ra or common == rb


def validate_vault_placement(source_root: str, vault_root: str) -> List[str]:
    errors: List[str] = []
    src = _norm_root(source_root)
    vault = _norm_root(vault_root)
    if not os.path.isdir(src):
        errors.append(f"来源目录不存在：{src}")
    if not vault:
        errors.append("仓库路径为空")
    if _paths_overlap(src, vault):
        errors.append("备份仓库不得与来源目录嵌套或相同")
    for root, label in ((src, "来源"), (vault, "仓库")):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            for name in list(dirnames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    other = vault if root == src else src
                    if _paths_overlap(os.path.realpath(full), other):
                        errors.append(
                            f"{label}内存在指向对方的符号链接："
                            f"{os.path.relpath(full, root)}")
                    dirnames.remove(name)
    return errors


def vault_paths(vault_root: str) -> Dict[str, str]:
    root = _norm_root(vault_root)
    marker = os.path.join(root, VAULT_MARKER)
    return {
        "root": root,
        "marker": marker,
        "objects": os.path.join(marker, "objects"),
        "versions": os.path.join(marker, "versions"),
        "staging": os.path.join(marker, "_staging"),
        "vault_json": os.path.join(marker, "vault.json"),
    }


def init_vault(vault_root: str, source_root: str,
               source_identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    overlap = validate_vault_placement(source_root, vault_root)
    if overlap:
        raise BackupStoreError("; ".join(overlap))
    paths = vault_paths(vault_root)
    os.makedirs(paths["objects"], exist_ok=True)
    os.makedirs(paths["versions"], exist_ok=True)
    meta = {
        "schema": VAULT_SCHEMA,
        "source_root": _norm_root(source_root),
        "source_identity": source_identity or {},
        "created_at": _utc_now(),
    }
    if not os.path.isfile(paths["vault_json"]):
        _atomic_write_json(paths["vault_json"], meta)
    return paths


def _atomic_write_json(path: str, data: Any) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _object_ref(sha256: str) -> str:
    return f"{sha256[:2]}/{sha256}"


def object_path(vault_paths_dict: Dict[str, str], sha256: str) -> str:
    return os.path.join(vault_paths_dict["objects"], _object_ref(sha256))


def object_exists(vault_paths_dict: Dict[str, str], sha256: str) -> bool:
    path = object_path(vault_paths_dict, sha256)
    if not os.path.isfile(path):
        return False
    return sha256_file(path) == sha256


def write_object(vault_paths_dict: Dict[str, str], src_path: str,
                 expected_sha256: str, cancel_event=None) -> str:
    ref = _object_ref(expected_sha256)
    final = os.path.join(vault_paths_dict["objects"], ref)
    if object_exists(vault_paths_dict, expected_sha256):
        return ref
    os.makedirs(os.path.dirname(final), exist_ok=True)
    staging = vault_paths_dict["staging"]
    os.makedirs(staging, exist_ok=True)
    tmp = os.path.join(staging, f"obj-{expected_sha256}.tmp")
    try:
        _copy_file_content(src_path, tmp, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise BackupStoreError("备份已取消")
        actual = sha256_file(tmp, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise BackupStoreError("备份已取消")
        if actual != expected_sha256:
            raise BackupStoreError(
                f"对象校验失败：期望 {expected_sha256[:16]}…，实际 {actual}")
        if not os.path.isfile(final):
            os.replace(tmp, final)
        else:
            os.unlink(tmp)
            if sha256_file(final) != expected_sha256:
                raise BackupStoreError(f"仓库内已有对象哈希不一致：{ref}")
    except OSError as exc:
        raise BackupStoreError(f"写入对象失败：{exc}") from exc
    finally:
        if os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return ref


def _copy_file_content(src: str, dst: str, cancel_event=None) -> None:
    with open(win_long_path(src), "rb") as rin, open(dst, "wb") as rout:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            chunk = rin.read(OBJECT_CHUNK)
            if not chunk:
                break
            rout.write(chunk)


def next_version_id(vault_paths_dict: Dict[str, str]) -> str:
    versions_dir = vault_paths_dict["versions"]
    os.makedirs(versions_dir, exist_ok=True)
    existing = []
    for name in os.listdir(versions_dir):
        if re.fullmatch(r"\d{6}", name):
            existing.append(int(name))
    return f"{max(existing, default=0) + 1:06d}"


def version_dir(vault_paths_dict: Dict[str, str], version_id: str) -> str:
    return os.path.join(vault_paths_dict["versions"], version_id)


def manifest_path(vault_paths_dict: Dict[str, str], version_id: str) -> str:
    return os.path.join(version_dir(vault_paths_dict, version_id), "manifest.json")


def commit_manifest(vault_paths_dict: Dict[str, str],
                    version_id: str, manifest: VersionManifest,
                    complete: bool) -> str:
    vdir = version_dir(vault_paths_dict, version_id)
    os.makedirs(vdir, exist_ok=True)
    mpath = manifest_path(vault_paths_dict, version_id)
    payload = _manifest_to_dict(manifest, complete=complete)
    staging = vault_paths_dict["staging"]
    os.makedirs(staging, exist_ok=True)
    tmp = os.path.join(staging, f"manifest-{version_id}.tmp.json")
    _atomic_write_json(tmp, payload)
    partial_path = mpath + ".incomplete"
    os.replace(tmp, partial_path)
    if complete:
        final_payload = dict(payload)
        final_payload["state"] = "complete"
        _atomic_write_json(mpath, final_payload)
        try:
            os.unlink(partial_path)
        except OSError:
            pass
        return mpath
    return partial_path


def _manifest_to_dict(manifest: VersionManifest, complete: bool) -> Dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "state": "complete" if complete else "incomplete",
        "version_id": manifest.version_id,
        "source_root": manifest.source_root,
        "source_identity": manifest.source_identity,
        "created_at": manifest.created_at,
        "note": manifest.note or "",
        "metadata_support": manifest.metadata_support,
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_bytes,
        "new_bytes": manifest.new_bytes,
        "files": [
            {
                "rel_path": e.rel_path,
                "size": e.size,
                "sha256": e.sha256,
                "mtime_ns": e.mtime_ns,
                "mode": e.mode,
                "object_ref": e.object_ref,
                "is_symlink": e.is_symlink,
                "link_target": e.link_target,
            }
            for e in manifest.files
        ],
        "excluded": manifest.excluded,
        "errors": manifest.errors,
    }


def load_manifest(vault_paths_dict: Dict[str, str],
                  version_id: str) -> Optional[VersionManifest]:
    mpath = manifest_path(vault_paths_dict, version_id)
    if not os.path.isfile(mpath):
        incomplete = mpath + ".incomplete"
        if os.path.isfile(incomplete):
            data = _read_json(incomplete)
            return _dict_to_manifest(data) if data else None
        return None
    data = _read_json(mpath)
    return _dict_to_manifest(data) if data else None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _dict_to_manifest(data: Dict[str, Any]) -> VersionManifest:
    files = []
    for item in data.get("files") or []:
        files.append(FileEntry(
            rel_path=item.get("rel_path", ""),
            size=int(item.get("size", 0)),
            sha256=item.get("sha256", ""),
            mtime_ns=int(item.get("mtime_ns", 0)),
            mode=int(item.get("mode", 0)),
            object_ref=item.get("object_ref", ""),
            is_symlink=bool(item.get("is_symlink")),
            link_target=item.get("link_target", ""),
        ))
    return VersionManifest(
        version_id=str(data.get("version_id", "")),
        state=str(data.get("state", "incomplete")),
        source_root=str(data.get("source_root", "")),
        source_identity=dict(data.get("source_identity") or {}),
        created_at=str(data.get("created_at", "")),
        files=files,
        excluded=list(data.get("excluded") or []),
        errors=list(data.get("errors") or []),
        metadata_support=dict(data.get("metadata_support") or {}),
        file_count=int(data.get("file_count", len(files))),
        total_bytes=int(data.get("total_bytes", 0)),
        new_bytes=int(data.get("new_bytes", 0)),
        note=str(data.get("note") or ""),
    )


def list_versions(vault_paths_dict: Dict[str, str]) -> List[VersionManifest]:
    versions_dir = vault_paths_dict["versions"]
    if not os.path.isdir(versions_dir):
        return []
    out: List[VersionManifest] = []
    for name in sorted(os.listdir(versions_dir)):
        if not re.fullmatch(r"\d{6}", name):
            continue
        m = load_manifest(vault_paths_dict, name)
        if m is not None:
            out.append(m)
    return out


def list_recoverable_versions(vault_paths_dict: Dict[str, str]) -> List[VersionManifest]:
    return [v for v in list_versions(vault_paths_dict) if v.recoverable]


def verify_objects(vault_paths_dict: Dict[str, str],
                   manifest: VersionManifest,
                   cancel_event=None,
                   progress_cb=None) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    total = len(manifest.files)
    for index, entry in enumerate(manifest.files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            errors.append("校验已取消")
            return (False, errors)
        if progress_cb:
            progress_cb(index, total, entry.rel_path)
        if entry.is_symlink:
            continue
        if not entry.sha256:
            errors.append(f"{entry.rel_path}：缺少内容哈希")
            continue
        if not object_exists(vault_paths_dict, entry.sha256):
            errors.append(f"{entry.rel_path}：对象缺失或哈希不符")
    if cancel_event is not None and cancel_event.is_set():
        errors.append("校验已取消")
        return (False, errors)
    return (len(errors) == 0, errors)


def normalize_backup_rel(rel_path: str) -> str:
    text = (rel_path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def validate_restore_rel_path(rel_path: str) -> List[str]:
    errors: List[str] = []
    raw = (rel_path or "").replace("\\", "/")
    # Windows 上 os.path.isabs("/abs") 为 False，必须显式拒绝 POSIX 绝对路径。
    if (
        os.path.isabs(rel_path or "")
        or (rel_path or "").startswith("\\")
        or raw.startswith("/")
        or raw.startswith("~")
    ):
        errors.append(f"绝对或非法路径：{rel_path}")
    rel = normalize_backup_rel(rel_path)
    if not rel:
        errors.append("空相对路径")
        return errors
    if len(rel) >= 2 and rel[1] == ":":
        errors.append(f"盘符路径：{rel_path}")
    parts = rel.split("/")
    if any(not p for p in parts):
        errors.append(f"含空段：{rel_path}")
    for seg in parts:
        if seg in (".", ".."):
            errors.append(f"路径逃逸段：{rel_path}")
        base, _, ext = seg.partition(".")
        check = (base or seg).casefold()
        if not ext and check in _WIN_RESERVED:
            errors.append(f"Windows 保留名：{seg}")
    return errors


def detect_case_conflicts(paths: List[str]) -> List[Tuple[str, str]]:
    seen: Dict[str, str] = {}
    conflicts: List[Tuple[str, str]] = []
    for p in paths:
        key = os.path.normcase(p)
        if key in seen and seen[key] != p:
            conflicts.append((seen[key], p))
        else:
            seen[key] = p
    return conflicts


def is_empty_directory(path: str) -> bool:
    p = _norm_root(path)
    if not os.path.isdir(p):
        return False
    with os.scandir(p) as it:
        return next(it, None) is None


def metadata_support_matrix() -> Dict[str, Any]:
    return {
        "content": True,
        "rel_path": True,
        "size": True,
        "sha256": True,
        "mtime_ns": True,
        "unix_mode": os.name != "nt",
        "symlink_target": True,
        "xattrs": False,
        "acl": False,
        "owner": False,
        "creation_time": False,
        "windows_attrs": False,
        "note": (
            "备份文件内容与可记录的路径、修改时间；"
            "不保证扩展属性、ACL、所有者或 Windows 创建时间。"
        ),
    }