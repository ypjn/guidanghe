"""当前整理空间的文件夹监视。

只判断哪些新路径值得告诉用户去重新扫描。不移动文件，不写预览。
"""
from __future__ import annotations

import os

from scanner import SKIP_DIRS, _is_junction_or_reparse

_ACCEPTED_KINDS = frozenset({"created", "moved_in"})
_SKIP = SKIP_DIRS | {name.lower() for name in SKIP_DIRS}


def accept_watch_path(root: str, path: str, kind: str) -> bool:
    """新文件或移入文件仍在空间内、且不是自身产物或越界链接时返回 True。"""
    if kind not in _ACCEPTED_KINDS or not root or not path:
        return False
    root_real = os.path.realpath(root)
    if not root_real:
        return False
    if _crosses_link_or_junction(root, path):
        return False
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath([root_real, resolved]) != root_real:
            return False
    except ValueError:
        return False
    rel = os.path.relpath(resolved, root_real)
    if rel in (".", os.curdir):
        return False
    for segment in rel.split(os.sep):
        if segment in _SKIP or segment.lower() in _SKIP:
            return False
    return True


def _crosses_link_or_junction(root: str, path: str) -> bool:
    """只检查空间内部的每一段。空间外面的系统目录链接不算越界。"""
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        rel = os.path.relpath(path_abs, root_abs)
    except ValueError:
        return False
    if rel.startswith(".."):
        return False
    cursor = root_abs
    for segment in rel.split(os.sep):
        if segment in ("", os.curdir):
            continue
        cursor = os.path.join(cursor, segment)
        if os.path.islink(cursor):
            return True
        if os.path.lexists(cursor) and _is_junction_or_reparse(cursor):
            return True
    return False
