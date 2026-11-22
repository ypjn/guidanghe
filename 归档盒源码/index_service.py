"""文件空间的登记和增量更新。

只读取来源目录。取消、权限失败或根目录消失时，不会把没看到的旧文件判成删除。
整理扫描的跳过规则不在这里放宽；_已整理 可以进入索引。
"""

from __future__ import annotations

import hashlib
import os
import sys
import unicodedata
import uuid
from datetime import datetime
from typing import Callable, Optional

from index_policy import (
    ACCESS_CONTENT, ACCESS_EXCLUDE, ACCESS_METADATA, default_policy,
    index_access, normalize_rel,
)
from index_store import (
    IndexStore, IndexStoreError, IndexUpdateReport, SpaceSpec,
)
from scan_cache import file_identity, identity_matches
from scanner import _is_junction_or_reparse, _looks_like_project_root, classify_type
from volume_identity import identity_mismatch_message, probe_volume, volumes_match

PARSER_VERSION = 1
Progress = Optional[Callable[[int, int, str], None]]


def register_space(root: str, name: str = "", policy: Optional[dict] = None,
                   store: Optional[IndexStore] = None) -> SpaceSpec:
    """登记一个文件空间。这不是移动文件的授权，也不会读取最近空间列表。"""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
    if not os.path.isdir(root):
        raise IndexStoreError(f"无法登记，目录不存在：{root}")
    own = store is None
    store = store or IndexStore()
    try:
        found = store.find_space_by_root(root)
        if found is not None:
            return found
        info = probe_volume(root)
        if info is None:
            raise IndexStoreError(f"无法登记，读不到卷身份：{root}")
        spec = SpaceSpec(
            space_id="sp-" + uuid.uuid4().hex[:16],
            root=root,
            name=(name or os.path.basename(root) or root).strip(),
            policy=_stored_policy(policy),
            enabled=True,
            volume_id=info.volume_id,
            filesystem=info.filesystem,
            online=True,
        )
        store.upsert_space(spec, _now())
        return spec
    finally:
        if own:
            store.close()


def update_space(space_id: str, cancel_event=None, progress_cb: Progress = None,
                 store: Optional[IndexStore] = None, read_content: bool = True,
                 limits=None, before_body=None) -> IndexUpdateReport:
    own = store is None
    store = store or IndexStore()
    report = IndexUpdateReport(space_id=space_id)
    try:
        spec = store.get_space(space_id)
        if spec is None:
            report.errors.append("空间不存在")
            report.coverage = "unavailable"
            return report
        if not spec.enabled:
            report.coverage = "paused"
            report.errors.append("空间已暂停，没有刷新索引")
            return report
        now = _now()
        generation = store.bump_generation(space_id)
        if not os.path.isdir(spec.root):
            report.coverage = "offline"
            report.errors.append(
                _offline_note(spec, "根目录现在不存在，已保留上次索引"))
            _set_space_offline(store, spec, now)
            store.set_status(space_id, report.coverage, now)
            return report
        current = probe_volume(spec.root)
        if not volumes_match(spec.volume_id, current):
            report.coverage = "identity_mismatch"
            report.errors.append(identity_mismatch_message(
                spec.volume_id, current, spec.root))
            store.set_status(space_id, report.coverage, now)
            return report
        if not spec.online:
            spec.online = True
            spec.filesystem = current.filesystem if current else spec.filesystem
            store.upsert_space(spec, now)
        listed: set[str] = set()
        seen: set[str] = set()
        excluded_prefixes: list[tuple[str, str]] = []
        _scan(
            store, spec, generation, report, listed, seen, excluded_prefixes,
            cancel_event, progress_cb)
        if report.superseded:
            report.coverage = "partial"
            return report
        _apply_exclusions(store, spec, generation, excluded_prefixes, report)
        if report.superseded:
            return report
        _apply_deletions(store, spec, generation, listed, seen, report)
        if not report.cancelled and not report.superseded:
            store.prune_text(space_id)
            if read_content:
                _index_contents(
                    store, spec, generation, report, cancel_event, limits,
                    before_body, progress_cb)
        if report.cancelled or report.errors:
            report.completed = False
            if report.coverage not in {"offline", "identity_mismatch", "paused"}:
                report.coverage = "partial"
        else:
            report.completed = True
            report.coverage = "complete"
            store.set_last_indexed_at(space_id, now)
            spec = store.get_space(space_id)
            if spec is not None:
                spec.online = True
                if current is not None:
                    spec.volume_id = current.volume_id
                    spec.filesystem = current.filesystem
                store.upsert_space(spec, now)
        store.set_status(space_id, report.coverage, now)
        return report
    finally:
        if own:
            store.close()


def remove_text(space_id: str, store: Optional[IndexStore] = None) -> None:
    """只删除这个空间的正文索引，文件登记和来源文件都留着。"""
    own = store is None
    store = store or IndexStore()
    try:
        store.clear_space_text(space_id)
    finally:
        if own:
            store.close()


def rebuild_text(space_id: str, cancel_event=None, progress_cb: Progress = None,
                 store: Optional[IndexStore] = None,
                 limits=None) -> IndexUpdateReport:
    """丢掉正文片段后按当前解析器重读。不删除来源文件。"""
    own = store is None
    store = store or IndexStore()
    try:
        store.clear_space_text(space_id)
        return update_space(
            space_id, cancel_event, progress_cb, store, True, limits)
    finally:
        if own:
            store.close()


def remove_space(space_id: str, store: Optional[IndexStore] = None) -> None:
    """只删除这个空间的索引登记，不删除来源文件，也不动 .organizer。"""
    own = store is None
    store = store or IndexStore()
    try:
        store.remove_space(space_id)
    finally:
        if own:
            store.close()


def set_space_enabled(space_id: str, enabled: bool,
                      store: Optional[IndexStore] = None) -> SpaceSpec:
    own = store is None
    store = store or IndexStore()
    try:
        spec = store.get_space(space_id)
        if spec is None:
            raise IndexStoreError(f"空间不存在：{space_id}")
        spec.enabled = enabled
        store.upsert_space(spec, _now())
        return spec
    finally:
        if own:
            store.close()


def overlap_notes(store: IndexStore) -> dict[str, str]:
    """重叠根目录下的同一文件会分别保留，并写明还出现在哪个空间。"""
    spaces = store.list_spaces()
    by_id = {item.space_id: item for item in spaces}
    notes: dict[str, str] = {}
    for rec in store.present_files():
        spec = by_id.get(rec.space_id)
        if spec is None:
            continue
        absolute = os.path.realpath(os.path.join(spec.root, rec.rel_path))
        others = []
        for other in spaces:
            if other.space_id == rec.space_id:
                continue
            other_root = os.path.realpath(other.root)
            try:
                inside = os.path.commonpath([absolute, other_root]) == other_root
            except ValueError:
                inside = False
            if inside:
                others.append(other.name)
        if others:
            notes[rec.file_id] = "同一路径也在空间：" + "、".join(others)
    return notes


def _scan(store, spec, generation, report, listed, seen, excluded_prefixes,
          cancel_event, progress_cb) -> None:
    protected = _protected_rels(spec.root)
    policy = dict(spec.policy)
    policy["protected_prefixes"] = protected
    scanned = 0

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def walk(rel: str) -> None:
        nonlocal scanned
        if report.superseded or cancelled():
            report.cancelled = cancelled()
            return
        absolute = spec.root if not rel else os.path.join(spec.root, rel)
        try:
            entries = list(os.scandir(absolute))
        except OSError as exc:
            report.errors.append(f"{rel or '.'}：{exc.strerror or exc}")
            return
        if cancelled():
            report.cancelled = True
            return
        finished = True
        for entry in entries:
            if report.superseded or cancelled():
                report.cancelled = cancelled() or report.cancelled
                finished = False
                break
            name = entry.name
            child = name if not rel else f"{rel}/{name}"
            try:
                linked = entry.is_symlink()
            except OSError as exc:
                report.errors.append(f"{child}：{exc.strerror or exc}")
                continue
            # Windows junction 不是 symlink，但 os.walk/scandir 仍会跟进去，必须单独拦住。
            if not linked:
                try:
                    linked = _is_junction_or_reparse(entry.path)
                except OSError as exc:
                    report.errors.append(f"{child}：{exc.strerror or exc}")
                    continue
            if linked:
                _remember_link(store, spec, generation, child, report, seen)
                if report.superseded:
                    finished = False
                    break
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                report.errors.append(f"{child}：{exc.strerror or exc}")
                continue
            if is_dir:
                reason = _looks_like_project_root(entry.path)
                if reason and child not in policy["protected_prefixes"]:
                    policy["protected_prefixes"] = list(
                        policy["protected_prefixes"]) + [child]
                access = index_access(spec.root, child, policy)
                if access == ACCESS_EXCLUDE:
                    excluded_prefixes.append((child, _exclude_reason(child, policy)))
                    report.excluded += 1
                    continue
                walk(child)
                if report.superseded or report.cancelled:
                    finished = False
                    break
                continue
            _remember_file(
                store, spec, generation, child, entry.path, policy, report, seen)
            scanned += 1
            if progress_cb is not None:
                progress_cb(scanned, 0, child)
        if finished and not report.cancelled and not report.superseded:
            listed.add(rel)

    walk("")
    store.commit()


def _remember_link(store, spec, generation, rel, report, seen) -> None:
    key = _path_key(rel)
    seen.add(key)
    status = store.write_file(generation, _row(
        spec, rel, key, size=0, mtime_ns=0, ctime_ns=0, dev=0, ino=0,
        digest="", availability="link", index_state=ACCESS_METADATA,
        reason="符号链接或目录联接，默认不跟随"))
    if status == "superseded":
        report.superseded = True
        return
    report.excluded += 1


def _remember_file(store, spec, generation, rel, path, policy, report, seen) -> None:
    key = _path_key(rel)
    seen.add(key)
    access = index_access(spec.root, rel, policy)
    identity = file_identity(path)
    if access == ACCESS_EXCLUDE or identity is None:
        reason = _exclude_reason(rel, policy) if access == ACCESS_EXCLUDE else "不是普通文件或暂时读不到身份"
        status = store.write_file(generation, _row(
            spec, rel, key, size=0, mtime_ns=0, ctime_ns=0, dev=0, ino=0,
            digest="", availability="excluded" if access == ACCESS_EXCLUDE else "unreadable",
            index_state=ACCESS_EXCLUDE, reason=reason, present_state=access))
        if status == "superseded":
            report.superseded = True
        else:
            report.excluded += 1
        return
    previous = store.get_file(spec.space_id, key)
    same_identity = (
        previous is not None
        and identity_matches(_identity_from_row(previous), identity)
        and _reuse_without_reread(identity)
        and previous["content_sha256"]
        and access != ACCESS_METADATA)
    if same_identity:
        digest = previous["content_sha256"]
        report.reused += 1
        added = False
    else:
        if access == ACCESS_METADATA:
            digest = ""
            report.reused += 0
        else:
            try:
                digest = _hash_file(path)
            except OSError as exc:
                status = store.write_file(generation, _row(
                    spec, rel, key, size=identity["size"],
                    mtime_ns=identity["mtime_ns"], ctime_ns=identity["ctime_ns"],
                    dev=identity["dev"], ino=identity["ino"], digest="",
                    availability="unreadable", index_state=ACCESS_METADATA,
                    reason=str(exc)))
                if status == "superseded":
                    report.superseded = True
                else:
                    report.errors.append(f"{rel}：{exc}")
                return
            report.reread += 1
        added = previous is None
    if previous is None:
        report.added += 1
    elif not same_identity:
        report.updated += 1
    status = store.write_file(generation, _row(
        spec, rel, key,
        size=identity["size"], mtime_ns=identity["mtime_ns"],
        ctime_ns=identity["ctime_ns"], dev=identity["dev"], ino=identity["ino"],
        digest=digest,
        availability="online",
        index_state=access,
        reason=""))
    if status == "superseded":
        report.superseded = True
    del added


def _apply_exclusions(store, spec, generation, prefixes, report) -> None:
    if not prefixes or report.superseded:
        return
    rows = store.conn.execute(
        "SELECT path_key, rel_path FROM files WHERE space_id=? AND present=1",
        (spec.space_id,)).fetchall()
    keys = []
    for row in rows:
        rel = row["rel_path"]
        for prefix, _reason in prefixes:
            if rel == prefix or rel.startswith(prefix + "/"):
                keys.append(row["path_key"])
                break
    changed = store.mark_deleted(spec.space_id, generation, keys, _now())
    if changed < 0:
        report.superseded = True
        return
    for key_row in store.conn.execute(
            "SELECT file_id FROM files WHERE space_id=? AND present=0 "
            "AND availability='deleted'", (spec.space_id,)):
        del key_row
    if keys:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            for path_key in keys:
                store.conn.execute(
                    "UPDATE files SET availability='excluded', "
                    "index_state='exclude', exclude_reason=? "
                    "WHERE space_id=? AND path_key=?",
                    ("目录已排除，不再索引", spec.space_id, path_key))
            store.conn.commit()
        except Exception:
            store.conn.rollback()
            raise


def _apply_deletions(store, spec, generation, listed, seen, report) -> None:
    if report.superseded:
        return
    rows = store.conn.execute(
        "SELECT path_key, rel_path, availability FROM files "
        "WHERE space_id=? AND present=1",
        (spec.space_id,)).fetchall()
    stale = []
    for row in rows:
        if row["path_key"] in seen:
            continue
        parent = row["rel_path"].rsplit("/", 1)[0] if "/" in row["rel_path"] else ""
        if parent in listed:
            stale.append(row["path_key"])
    changed = store.mark_deleted(spec.space_id, generation, stale, _now())
    if changed < 0:
        report.superseded = True
        report.deleted = 0
        return
    report.deleted = changed


def _index_contents(store, spec, generation, report, cancel_event, limits,
                    before_body, progress_cb) -> None:
    from index_content import CONTENT_PARSER_VERSION, ContentLimits, extract_document

    limits = limits or ContentLimits()
    rows = store.conn.execute(
        "SELECT file_id, rel_path, content_sha256 FROM files "
        "WHERE space_id=? AND present=1 AND index_state=? ORDER BY rel_path",
        (spec.space_id, ACCESS_CONTENT)).fetchall()
    total = len(rows)
    kept = {"complete", "partial", "unsupported"}
    for index, row in enumerate(rows, start=1):
        if cancel_event is not None and cancel_event.is_set():
            report.cancelled = True
            return
        if not store.generation_is_current(spec.space_id, generation):
            report.superseded = True
            return
        digest = row["content_sha256"] or ""
        existing = store.get_file_text(row["file_id"])
        if (existing is not None and existing["content_sha256"] == digest
                and int(existing["parser_version"]) == CONTENT_PARSER_VERSION
                and existing["status"] in kept):
            continue
        path = os.path.join(spec.root, row["rel_path"])
        extracted = extract_document(
            path, spec.root, limits, cancel_event, before_body)
        if extracted.status == "cancelled":
            report.cancelled = True
            return
        chunks = extracted.chunks if extracted.status in {"complete", "partial"} else []
        status = store.replace_text(
            generation, spec.space_id, row["file_id"], digest,
            CONTENT_PARSER_VERSION, extracted.status, extracted.reason, chunks)
        if status == "superseded":
            report.superseded = True
            return
        if extracted.status == "failed":
            report.errors.append(f"{row['rel_path']}：{extracted.reason}")
        if progress_cb is not None:
            progress_cb(index, total, row["rel_path"])


def _protected_rels(root: str) -> list[str]:
    rels = []
    try:
        from protect_config import load_manual_protect
        manuals = load_manual_protect(root)
    except Exception:
        manuals = []
    for path in manuals:
        if os.path.commonpath([
                os.path.realpath(path), os.path.realpath(root)]) != os.path.realpath(root):
            continue
        rel = normalize_rel(os.path.relpath(path, root))
        if rel and rel != ".":
            rels.append(rel)
    return rels


def _stored_policy(policy: Optional[dict]) -> dict:
    merged = default_policy()
    for key, value in (policy or {}).items():
        if key == "protected_prefixes":
            continue
        merged[key] = value
    merged.pop("protected_prefixes", None)
    return merged


def _exclude_reason(rel: str, policy: dict) -> str:
    if any(normalize_rel(rel) == normalize_rel(item)
           or normalize_rel(rel).startswith(normalize_rel(item) + "/")
           for item in policy.get("deny_read") or []):
        return "明确禁止读取"
    for prefix in policy.get("protected_prefixes") or []:
        prefix = normalize_rel(prefix)
        if prefix and (rel == prefix or rel.startswith(prefix + "/")):
            return "受保护项目默认不索引；只读纳入不会解除移动保护"
    return "命中排除规则"


def _reuse_without_reread(identity: dict) -> bool:
    """Windows 的 ctime 是创建时间，不能用来证明内容没变。"""
    if os.name == "nt":
        return False
    return bool(identity.get("ctime_ns"))


def _identity_from_row(row) -> dict:
    return {
        "dev": int(row["dev"]),
        "ino": int(row["ino"]),
        "size": int(row["size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "ctime_ns": int(row["ctime_ns"]),
    }


def _path_key(rel: str) -> str:
    text = unicodedata.normalize("NFC", normalize_rel(rel))
    if os.name == "nt" or sys.platform == "darwin":
        return text.casefold()
    return text


def _file_id(space_id: str, path_key: str) -> str:
    return hashlib.sha256(f"{space_id}\n{path_key}".encode()).hexdigest()[:32]


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row(spec, rel, key, *, size, mtime_ns, ctime_ns, dev, ino, digest,
         availability, index_state, reason, present_state=None) -> dict:
    del present_state
    return {
        "file_id": _file_id(spec.space_id, key),
        "space_id": spec.space_id,
        "rel_path": normalize_rel(rel),
        "path_key": key,
        "name": os.path.basename(normalize_rel(rel)),
        "file_type": classify_type(os.path.basename(rel)),
        "size": int(size),
        "mtime_ns": int(mtime_ns),
        "ctime_ns": int(ctime_ns),
        "dev": int(dev),
        "ino": int(ino),
        "content_sha256": digest,
        "availability": availability,
        "index_state": index_state,
        "parser_version": PARSER_VERSION if digest else 0,
        "indexed_at": _now(),
        "exclude_reason": reason,
        "present": 0 if index_state == ACCESS_EXCLUDE else 1,
    }


def refresh_space_volume(space_id: str, store: Optional[IndexStore] = None) -> SpaceSpec:
    """路径恢复后核对卷身份并更新在线状态，不扫描文件。"""
    own = store is None
    store = store or IndexStore()
    try:
        spec = store.get_space(space_id)
        if spec is None:
            raise IndexStoreError(f"空间不存在：{space_id}")
        now = _now()
        if not os.path.isdir(spec.root):
            _set_space_offline(store, spec, now)
            raise IndexStoreError(_offline_note(spec, "根目录不存在"))
        current = probe_volume(spec.root)
        if not volumes_match(spec.volume_id, current):
            raise IndexStoreError(identity_mismatch_message(
                spec.volume_id, current, spec.root))
        spec.online = True
        if current is not None:
            spec.filesystem = current.filesystem
        store.upsert_space(spec, now)
        return spec
    finally:
        if own:
            store.close()


def reassociate_space(space_id: str, store: Optional[IndexStore] = None) -> SpaceSpec:
    """在卷身份无法核对时，经用户确认后把当前路径重新绑定为该空间。

    不删除旧索引正文；下一次刷新会按新身份增量更新。
    盘符或卷名变化但序列号一致时不需要调用本函数。
    """
    own = store is None
    store = store or IndexStore()
    try:
        spec = store.get_space(space_id)
        if spec is None:
            raise IndexStoreError(f"空间不存在：{space_id}")
        if not os.path.isdir(spec.root):
            raise IndexStoreError(_offline_note(spec, "根目录不存在，不能重新关联"))
        current = probe_volume(spec.root)
        if current is None:
            raise IndexStoreError(f"读不到卷身份，不能重新关联：{spec.root}")
        if volumes_match(spec.volume_id, current):
            spec.online = True
            spec.filesystem = current.filesystem
            store.upsert_space(spec, _now())
            return spec
        now = _now()
        spec.volume_id = current.volume_id
        spec.filesystem = current.filesystem
        spec.online = True
        store.upsert_space(spec, now)
        store.set_status(space_id, "reassociated", now)
        return spec
    finally:
        if own:
            store.close()


def _set_space_offline(store: IndexStore, spec: SpaceSpec, now: str) -> None:
    spec.online = False
    store.upsert_space(spec, now)
    store.mark_files_offline(spec.space_id, now)


def _offline_note(spec: SpaceSpec, reason: str) -> str:
    indexed = spec.last_indexed_at or "未知"
    return f"{reason}（设备离线，最近成功索引：{indexed}）"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
