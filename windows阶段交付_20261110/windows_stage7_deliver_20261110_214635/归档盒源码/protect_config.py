"""
归档盒 V1.1 — 手动保护目录配置

职责：
- 校验/规范化用户手动添加的保护目录（必须位于当前根目录内、真实目录、非符号链接）
- 按根目录持久化到 {root}/.organizer/protect_config.json
- 重启后按根目录恢复，切换根目录不会串用

保护语义与 scanner 一致：被保护目录及其全部后代都不进入整理计划。
本模块只负责“配置”，不负责扫描；扫描时由 main.py 把 manual 路径传给
scan_directory(protect_dirs=[(path, "manual", "用户手动保护"), ...])。
"""

from __future__ import annotations

import json
import os
from typing import Iterable, List, Tuple

CONFIG_DIR = ".organizer"
CONFIG_FILENAME = "protect_config.json"
CONFIG_VERSION = 1

# 三类保护来源的统一显示名
KIND_LABELS = {
    "default": "默认保护",
    "auto": "自动识别",
    "manual": "用户手动保护",
}

# 这些是程序自身产物，不允许作为手动保护目录（否则会隐藏整理记录）
FORBIDDEN_DIR_NAMES = {CONFIG_DIR, "_已整理"}


class ProtectConfigError(ValueError):
    """手动保护目录不合法。"""


def _norm_key(path: str) -> str:
    """用于去重的路径键（macOS 默认大小写不敏感，这里统一按小写比较）。"""
    return os.path.normcase(os.path.abspath(path))


def normalize_manual_path(root: str, path: str) -> str:
    """校验并把用户选择/输入的目录规范化为绝对路径。

    只接受：
    - 当前根目录内部（不含根目录自身）的已存在目录
    - 不是符号链接（扫描本来就不跟随符号链接）
    - 不是 .organizer / _已整理 等程序产物目录
    """
    root_abs = os.path.abspath(root)
    if not path:
        raise ProtectConfigError("目录为空")
    # 先展开 ~（基线语义），再判断绝对/相对：
    # 相对路径相对整理 root 解析，不依赖当前 cwd（Windows 子进程 cwd 不可控）
    raw = os.path.expanduser(str(path).strip())
    if not raw:
        raise ProtectConfigError("目录为空")
    if os.path.isabs(raw):
        p = os.path.abspath(raw)
    else:
        p = os.path.abspath(os.path.join(root_abs, raw))
    if p == root_abs:
        raise ProtectConfigError("不能把整个根目录设为保护目录（这样没有文件可整理）")
    if not p.startswith(root_abs + os.sep):
        raise ProtectConfigError(f"目录不在当前根目录内：{p}")
    if os.path.islink(p):
        raise ProtectConfigError(f"符号链接目录不参与整理，不能作为保护目录：{p}")
    if not os.path.isdir(p):
        raise ProtectConfigError(f"目录不存在或不是文件夹：{p}")
    rel_parts = os.path.relpath(p, root_abs).split(os.sep)
    if rel_parts and rel_parts[0] in FORBIDDEN_DIR_NAMES:
        raise ProtectConfigError(f"程序自身目录不能作为保护目录：{rel_parts[0]}")
    return p


def dedupe_manual_paths(root: str, paths: Iterable[str]) -> List[str]:
    """规范化 + 去重（保持先出现的顺序）。"""
    seen = set()
    out: List[str] = []
    for p in paths:
        try:
            ap = normalize_manual_path(root, p)
        except ProtectConfigError:
            continue
        key = _norm_key(ap)
        if key in seen:
            continue
        seen.add(key)
        out.append(ap)
    return out


def config_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), CONFIG_DIR, CONFIG_FILENAME)


def load_manual_protect(root: str) -> List[str]:
    """读取该根目录的手动保护配置。

    文件不存在、损坏、根目录不匹配、条目已不存在时都安全降级：
    返回仍在当前根目录内的有效目录列表，不抛异常。
    """
    path = config_path(root)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    stored_root = data.get("root")
    if stored_root and _norm_key(stored_root) != _norm_key(root):
        return []  # 防止切换根目录后串用别人的配置
    manual = data.get("manual_paths") or []
    if not isinstance(manual, list):
        return []
    abs_paths = []
    root_abs = os.path.abspath(root)
    for rel in manual:
        if not isinstance(rel, str) or not rel:
            continue
        abs_paths.append(os.path.join(root_abs, rel))
    return dedupe_manual_paths(root, abs_paths)


def save_manual_protect(root: str, paths: Iterable[str]) -> None:
    """按根目录保存手动保护配置（原子替换，避免写一半）。

    保存前会重新校验；非法条目直接丢弃，保证磁盘上的配置永远可用。
    """
    root_abs = os.path.abspath(root)
    valid = dedupe_manual_paths(root_abs, paths)
    payload = {
        "version": CONFIG_VERSION,
        "root": root_abs,
        "manual_paths": sorted(os.path.relpath(p, root_abs) for p in valid),
    }
    target = config_path(root_abs)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def manual_scan_specs(root: str, paths: Iterable[str]) -> List[Tuple[str, str, str]]:
    """转成 scan_directory 接受的 (路径, 类别, 显示原因) 列表。"""
    valid = dedupe_manual_paths(root, paths)
    return [(p, "manual", KIND_LABELS["manual"]) for p in valid]
