"""索引范围策略。

返回值只有 exclude / metadata / content。
这里不修改整理保护，也不因为“纳入索引”而允许移动文件。
"""

from __future__ import annotations

import os
from typing import Iterable

ACCESS_EXCLUDE = "exclude"
ACCESS_METADATA = "metadata"
ACCESS_CONTENT = "content"

# 整理扫描会跳过 _已整理。索引仍要能找到已经整理好的文件。
DEFAULT_EXCLUDE_DIR_NAMES = (
    ".organizer",
    ".git", ".svn", ".hg",
    ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".cache",
    "node_modules",
    ".gradle", ".idea", ".vscode",
    ".mamba-root", "site-packages",
    "__MACOSX", ".ipynb_checkpoints",
    ".dsh-backups",
    ".archivebox-backup",
)

DEFAULT_EXCLUDE_FILE_NAMES = (
    ".ds_store", "thumbs.db", "desktop.ini", "icon",
)


def default_policy() -> dict:
    return {
        "exclude_dir_names": list(DEFAULT_EXCLUDE_DIR_NAMES),
        "exclude_file_names": list(DEFAULT_EXCLUDE_FILE_NAMES),
        "readonly_include": [],
        "metadata_only": [],
        "deny_read": [],
        "protected_prefixes": [],
    }


def normalize_rel(relative_path: str) -> str:
    text = (relative_path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _under(rel: str, prefix: str) -> bool:
    prefix = normalize_rel(prefix)
    if not prefix:
        return True
    return rel == prefix or rel.startswith(prefix + "/")


def _names(policy: dict, key: str, fallback: Iterable[str]) -> set[str]:
    raw = policy.get(key, None)
    if raw is None:
        raw = fallback
    return {str(item).casefold() for item in raw}


def index_access(root: str, relative_path: str, policy: dict) -> str:
    """决定一个相对路径能不能进入索引。不读取文件正文。"""
    del root  # 保护前缀由调用方算好放进 policy，避免这里改整理配置。
    policy = policy or {}
    rel = normalize_rel(relative_path)
    parts = [part for part in rel.split("/") if part]
    if any(_under(rel, item) for item in policy.get("deny_read") or []):
        return ACCESS_EXCLUDE
    exclude_dirs = _names(policy, "exclude_dir_names", DEFAULT_EXCLUDE_DIR_NAMES)
    if any(part.casefold() in exclude_dirs for part in parts[:-1]):
        return ACCESS_EXCLUDE
    if parts and parts[-1].casefold() in exclude_dirs and not os.path.splitext(parts[-1])[1]:
        return ACCESS_EXCLUDE
    exclude_files = _names(policy, "exclude_file_names", DEFAULT_EXCLUDE_FILE_NAMES)
    if parts and parts[-1].casefold() in exclude_files:
        return ACCESS_EXCLUDE
    protected = [normalize_rel(item) for item in policy.get("protected_prefixes") or []]
    included = [normalize_rel(item) for item in policy.get("readonly_include") or []]
    for prefix in protected:
        if prefix and _under(rel, prefix):
            if any(_under(rel, item) for item in included):
                break
            return ACCESS_EXCLUDE
    if any(_under(rel, item) for item in policy.get("metadata_only") or []):
        return ACCESS_METADATA
    return ACCESS_CONTENT
