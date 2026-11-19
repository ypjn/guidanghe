"""扫描/正文增量缓存。

缓存放在整理空间的 `.organizer/scan_cache.json`，只记录本程序确认过的
文件身份、SHA-256 和正文摘录。再次扫描时，身份（dev/ino/size/mtime_ns）
完全一致的文件可以复用；新增、修改、删除、改名或身份读不出来的文件
一律重新处理。缓存损坏、版本不兼容或路径是符号链接时直接退回全量扫描。

缓存只用于加速扫描和预览，不替代执行前校验：执行器仍会用计划里的
source_sha256 与磁盘当前内容比对，变化或读不出的文件继续跳过。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

CACHE_VERSION = 1
CACHE_FILENAME = "scan_cache.json"
ORGANIZER_DIR = ".organizer"

#: 正文解析规则变化时递增；旧缓存只丢正文，SHA-256 仍可复用。
TEXT_VERSION = 2


def cache_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), ORGANIZER_DIR, CACHE_FILENAME)


def _safe_parent(root: str) -> str:
    org = os.path.join(os.path.abspath(root), ORGANIZER_DIR)
    if os.path.islink(org):
        raise OSError("缓存目录是符号链接，未写入")
    if os.path.exists(org) and not os.path.isdir(org):
        raise OSError("缓存目录不是目录，未写入")
    return org


def file_identity(path: str):
    """读取普通文件的稳定身份；符号链接、目录、读失败都返回 None。"""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def identity_matches(record, current) -> bool:
    if not isinstance(record, dict) or not isinstance(current, dict):
        return False
    for key in ("dev", "ino", "size", "mtime_ns"):
        if record.get(key) != current.get(key):
            return False
    return True


def _valid_record(record) -> bool:
    if not isinstance(record, dict):
        return False
    for key in ("dev", "ino", "size", "mtime_ns"):
        if not isinstance(record.get(key), int):
            return False
    return True


def load(root: str):
    """返回 (files, note)。files 是 rel_path -> 记录；note 是回退原因。"""
    path = cache_path(root)
    if os.path.islink(path):
        return {}, "缓存是符号链接，已回退全量扫描"
    if not os.path.isfile(path):
        return {}, ""
    try:
        if os.path.getsize(path) > 64 * 1024 * 1024:
            return {}, "缓存过大，已回退全量扫描"
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}, "缓存损坏，已回退全量扫描"
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}, "缓存版本不兼容，已回退全量扫描"
    raw = data.get("files")
    if not isinstance(raw, dict):
        return {}, "缓存格式不正确，已回退全量扫描"
    files = {}
    for rel, record in raw.items():
        if not isinstance(rel, str) or not _valid_record(record):
            continue
        item = dict(record)
        if data.get("text_version") != TEXT_VERSION:
            item.pop("text", None)
            item.pop("kind", None)
        files[rel] = item
    return files, ""


def save(root: str, files: dict) -> None:
    """原子写入缓存；失败由调用方决定只记录日志，不影响本次计划。"""
    parent = _safe_parent(root)
    os.makedirs(parent, exist_ok=True)
    path = cache_path(root)
    if os.path.islink(path):
        raise OSError("缓存路径是符号链接，未写入")
    payload = {
        "version": CACHE_VERSION,
        "text_version": TEXT_VERSION,
        "files": files,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fd, name = tempfile.mkstemp(prefix=".scan_cache-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = ""
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def entry(identity: dict, sha256: str = "", text: str | None = None,
          kind: str = "", note: str = "") -> dict:
    record = dict(identity)
    if sha256:
        record["sha256"] = sha256
    if text is not None:
        record["text"] = text
    if kind:
        record["kind"] = kind
    if note:
        record["note"] = note
    return record
