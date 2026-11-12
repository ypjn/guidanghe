"""
归档盒 V0.3 — 通用工具函数

职责：SHA-256 计算等辅助函数。
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional


def sha256_file(file_path: str, follow_symlinks: bool = False) -> Optional[str]:
    """
    计算文件的 SHA-256 哈希。

    返回 64 位小写十六进制字符串，或 None（文件不存在/不可读）。
    """
    if not os.path.exists(file_path):
        return None
    if os.path.islink(file_path) and not follow_symlinks:
        return None
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            # 分块读取，避免大文件占满内存
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def file_fingerprint(file_path: str) -> Optional[tuple]:
    """
    返回 (size, sha256) 元组，或 None（无法读取）。

    用于独立核验用户文件：比对多重集合 (size, sha256)。
    """
    if not os.path.exists(file_path):
        return None
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return None
    sha = sha256_file(file_path)
    if sha is None:
        return None
    return (size, sha)
