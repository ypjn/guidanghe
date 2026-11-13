"""
归档盒 V1.7 — 通用工具函数

职责：SHA-256 计算、Windows 长路径前缀等辅助函数。
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional


def win_long_path(path: str) -> str:
    """返回可用于 Windows 长路径 API 的路径（S3 / filesafe F-22）。

    仅在 Windows 且绝对路径接近或超过 260 字符时加 ``\\\\?\\`` 前缀；
    已带前缀的路径原样返回。非 Windows 平台原样返回。
    """
    if os.name != "nt" or not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    if len(p) < 248:
        return p
    if p.startswith("\\\\"):
        # UNC：\\server\share → \\?\UNC\server\share
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def strip_long_prefix(path: str) -> str:
    """去掉 ``\\\\?\\`` / ``\\\\?\\UNC\\`` 前缀，用于展示与相对路径计算。"""
    if not path:
        return path
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path


def sha256_file(file_path: str, follow_symlinks: bool = False) -> Optional[str]:
    """
    计算文件的 SHA-256 哈希。

    返回 64 位小写十六进制字符串，或 None（文件不存在/不可读）。
    """
    if not os.path.exists(win_long_path(file_path)):
        return None
    if os.path.islink(file_path) and not follow_symlinks:
        return None
    try:
        h = hashlib.sha256()
        with open(win_long_path(file_path), "rb") as f:
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
    if not os.path.exists(win_long_path(file_path)):
        return None
    try:
        size = os.path.getsize(win_long_path(file_path))
    except OSError:
        return None
    sha = sha256_file(file_path)
    if sha is None:
        return None
    return (size, sha)
