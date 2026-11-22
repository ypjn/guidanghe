"""搜索结果的代次、否定条件和整理资格。

查找不产生移动计划。整理只接受同一个空间里、当前仍在线、
且不在保护目录中的选择。
"""

from __future__ import annotations

import os

from content_extract import _is_within_root
from content_search import search_files
from index_service import _path_key
from scan_cache import file_identity, identity_matches
from scanner import _looks_like_project_root


class SearchSession:
    def __init__(self):
        self.generation = 0

    def begin(self) -> int:
        self.generation += 1
        return self.generation

    def deliver(self, token: int, payload, sink) -> bool:
        if token != self.generation:
            return False
        sink(payload)
        return True


def run_lookup(store, understood, space_ids=None, limit=50, offset=0):
    from dataclasses import replace

    tokens = [part for part in understood.text.split() if part]
    if len(tokens) <= 1:
        page = search_files(store, understood.to_search_query(
            space_ids=list(space_ids or []), scope="all", limit=limit, offset=offset))
    else:
        pages = []
        for token in tokens:
            one = replace(understood, text=token)
            pages.append(search_files(store, one.to_search_query(
                space_ids=list(space_ids or []), scope="all",
                limit=limit, offset=0)))
        common = set.intersection(*({hit.file_id for hit in page.hits} for page in pages))
        page = pages[0]
        page.hits = [hit for hit in page.hits if hit.file_id in common][:limit]
    page.hits = [hit for hit in page.hits if _keeps(hit, understood.excluded)]
    return page


def locate_hit(store, hit) -> tuple[str, str]:
    """按空间和相对路径核对当前文件。同名文件不会互相代替。"""
    spec = store.get_space(hit.space_id)
    if spec is None:
        return "", "空间已移除，这条结果已失效"
    path = os.path.join(spec.root, hit.rel_path)
    if os.path.islink(path) or not os.path.isfile(path):
        return "", "文件已不在索引记录的位置"
    if not _is_within_root(path, spec.root):
        return "", "位置已离开该空间"
    row = store.get_file(hit.space_id, _path_key(hit.rel_path))
    current = file_identity(path)
    if row is None or current is None or not identity_matches(_identity(row), current):
        return "", "文件已变化或索引已过期，请先刷新"
    if row["availability"] != "online":
        return "", "文件当前不在线，不能按同名猜测路径"
    return path, ""


def organize_block_reason(root: str, rel: str) -> str:
    from index_policy import normalize_rel
    rel = normalize_rel(rel)
    cursor = root
    for part in rel.split("/"):
        cursor = os.path.join(cursor, part)
        if os.path.isdir(cursor):
            reason = _looks_like_project_root(cursor)
            if reason:
                return "受保护项目可以搜索，不能进入移动计划"
    try:
        from protect_config import load_manual_protect
        manuals = load_manual_protect(root)
    except Exception:
        manuals = []
    target = os.path.realpath(os.path.join(root, rel))
    for manual in manuals:
        manual_real = os.path.realpath(manual)
        try:
            inside = os.path.commonpath([target, manual_real]) == manual_real
        except ValueError:
            inside = False
        if inside:
            return "受保护目录可以搜索，不能进入移动计划"
    return ""


def selection_plan(root: str, plan, chosen: set[str], space_id: str,
                   hits_by_path: dict):
    """只保留用户选中的、仍可移动的条目。跨空间的选择被分开拒绝。"""
    kept = []
    notes = []
    for item in plan.items:
        rel = item.file.rel_path
        if rel not in chosen:
            item.selected = False
            continue
        hit = hits_by_path.get(rel)
        if hit is None or hit.space_id != space_id:
            item.selected = False
            notes.append(f"{rel}：不属于当前要整理的空间")
            continue
        if hit.availability != "online":
            item.selected = False
            notes.append(f"{rel}：不在线，没有放进移动计划")
            continue
        reason = organize_block_reason(root, rel)
        if reason:
            item.selected = False
            notes.append(f"{rel}：{reason}")
            continue
        item.selected = True
        kept.append(item)
    return kept, notes


def _keeps(hit, excluded: list[str]) -> bool:
    haystack = " ".join((hit.name, hit.rel_path, hit.evidence, hit.location))
    return not any(term and term in haystack for term in excluded)


def _identity(row) -> dict:
    return {
        "dev": int(row["dev"]),
        "ino": int(row["ino"]),
        "size": int(row["size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "ctime_ns": int(row["ctime_ns"]),
    }

