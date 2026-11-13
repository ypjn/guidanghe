"""
归档盒 V1.7 — 手动保护目录配置

职责：
- 校验/规范化用户手动添加的保护目录（必须位于当前根目录内、真实目录、非符号链接）
- 按根目录持久化到 {root}/.organizer/protect_config.json
- 重启后按根目录恢复，切换根目录不会串用

保护语义与 scanner 一致：被保护目录及其全部后代都不进入整理计划。
本模块只负责“配置”，不负责扫描；扫描时由 main.py 把 manual 路径传给
scan_directory(protect_dirs=[(path, "manual", "用户手动保护"), ...])。

数据安全（V1.7）：
- 根目录改名/移动后，配置随目录一起搬走——按当前根目录继续使用，
  不因为记录的旧根路径对不上就返回空列表。
- 保护目录暂时不存在时保留配置项，不静默删除；扫描时才跳过不存在的项。
- 所有“是否在根内”的比较统一 normcase（Windows 大小写不敏感）。
"""

from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional, Tuple

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
    """用于比较/去重的路径键（Windows 路径大小写不敏感）。"""
    return os.path.normcase(os.path.abspath(path))


def _within(root_abs: str, p: str) -> bool:
    """p 是否位于 root 内（含比较前统一 normcase）。"""
    rk = _norm_key(root_abs)
    pk = _norm_key(p)
    return pk == rk or pk.startswith(rk + os.sep)


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
    p = os.path.abspath(os.path.expanduser(str(path).strip()))
    if _norm_key(p) == _norm_key(root_abs):
        raise ProtectConfigError("不能把整个根目录设为保护目录（这样没有文件可整理）")
    if not _within(root_abs, p):
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
    """规范化 + 去重（保持先出现的顺序）。仅保留当前真实存在的目录。"""
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


def _rel_entry(root_abs: str, entry: str) -> Optional[str]:
    """把配置里的一项转成相对当前根的路径；明显越界/非法时返回 None。"""
    if not isinstance(entry, str) or not entry.strip():
        return None
    s = entry.strip()
    if os.path.isabs(s):
        if not _within(root_abs, s):
            return None
        rel = os.path.relpath(os.path.abspath(s), root_abs)
    else:
        rel = s.replace("/", os.sep).replace("\\", os.sep)
    if rel in ("", "."):
        return None
    parts = [p for p in rel.split(os.sep) if p]
    if not parts or parts[0] in (".", "..") or parts[0] in FORBIDDEN_DIR_NAMES:
        return None
    return os.path.join(*parts)


def _kept_rel_entries(root_abs: str, entries: Iterable[str]) -> List[str]:
    """保留配置项（不要求目录当前存在），按 normcase 去重。"""
    seen = set()
    out: List[str] = []
    for entry in entries:
        rel = _rel_entry(root_abs, entry)
        if not rel:
            continue
        key = os.path.normcase(rel)
        if key in seen:
            continue
        seen.add(key)
        out.append(rel)
    return out


def config_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), CONFIG_DIR, CONFIG_FILENAME)


def load_manual_protect_report(root: str) -> Tuple[List[str], List[str]]:
    """读取手动保护配置，返回 (绝对路径列表, 需要告知用户的说明)。

    - 根目录改名/移动：配置里的旧根路径对不上时，按配置随目录搬走处理，
      继续使用这些相对项，并给出说明（不返回空列表）。
    - 目录暂时不存在：条目保留，说明里列出数量。
    - 文件损坏：返回空列表 + 说明（原文件保留不动）。
    """
    root_abs = os.path.abspath(root)
    notes: List[str] = []
    path = config_path(root_abs)
    if not os.path.isfile(path):
        return [], notes
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        notes.append(f"保护目录配置无法读取（原文件保留）：{path}：{e}")
        return [], notes
    if not isinstance(data, dict):
        notes.append(f"保护目录配置格式不正确（原文件保留）：{path}")
        return [], notes
    stored_root = data.get("root")
    if stored_root and _norm_key(stored_root) != _norm_key(root_abs):
        notes.append(
            "保护目录配置记录的根目录是：\n"
            f"{stored_root}\n当前整理空间是：\n{root_abs}\n"
            "（目录可能已改名或移动）配置随目录一起保留，已按当前根目录继续使用。")
    manual = data.get("manual_paths") or []
    if not isinstance(manual, list):
        return [], notes
    rels = _kept_rel_entries(root_abs, manual)
    abs_paths = [os.path.abspath(os.path.join(root_abs, rel)) for rel in rels]
    missing = [p for p in abs_paths if not os.path.isdir(p)]
    if missing:
        notes.append(
            f"有 {len(missing)} 个保护目录当前不存在，配置已保留，不会自动删除：\n"
            + "\n".join(missing[:10])
            + ("…" if len(missing) > 10 else ""))
    return abs_paths, notes


def load_manual_protect(root: str) -> List[str]:
    """读取该根目录的手动保护配置（兼容旧调用，返回绝对路径列表）。"""
    paths, _notes = load_manual_protect_report(root)
    return paths


def save_manual_protect(root: str, paths: Iterable[str]) -> None:
    """按根目录保存手动保护配置（原子替换，避免写一半）。

    只丢弃“明显越界/非法”的条目；暂时不存在的目录仍保留，
    避免一次保存就把用户配置永久清空。
    """
    root_abs = os.path.abspath(root)
    rels = _kept_rel_entries(root_abs, list(paths))
    payload = {
        "version": CONFIG_VERSION,
        "root": root_abs,
        "manual_paths": sorted(rels),
    }
    target = config_path(root_abs)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def manual_scan_specs(root: str, paths: Iterable[str]) -> List[Tuple[str, str, str]]:
    """转成 scan_directory 接受的 (路径, 类别, 显示原因) 列表。

    只返回当前真实存在的目录；不存在的配置项保留在文件里，但扫描时跳过。
    """
    root_abs = os.path.abspath(root)
    valid = [p for p in
             (os.path.abspath(os.path.join(root_abs, r))
              for r in _kept_rel_entries(root_abs, paths))
             if os.path.isdir(p)]
    return [(p, "manual", KIND_LABELS["manual"]) for p in valid]
