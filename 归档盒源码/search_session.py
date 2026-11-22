"""搜索结果的代次、否定条件和整理资格。

查找不产生移动计划。整理只接受同一个空间里、当前仍在线、
且不在保护目录中的选择。

多词求交、排除词和分页都按「完整结果集 → 按文件聚合 → 稳定排序 →
再切片」的顺序处理，避免先截断再过滤造成空页或漏命中。
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

from content_extract import _is_within_root
from content_search import SearchPage, search_files
from index_service import _path_key
from scan_cache import file_identity, identity_matches
from scanner import _looks_like_project_root

# 单次查找在内存中聚合的上限。超过后结果仍正确分页，但会注明截断。
_LOOKUP_SCAN_CAP = 20_000


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
    """完整应用包含/排除条件后，按文件聚合再分页。"""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    started = time.perf_counter()
    tokens = [part for part in understood.text.split() if part]
    notes: list[str] = []
    truncated = False

    if len(tokens) <= 1:
        page = search_files(store, understood.to_search_query(
            space_ids=list(space_ids or []), scope="all",
            limit=_LOOKUP_SCAN_CAP, offset=0))
        if page.note:
            notes.append(page.note)
        truncated = truncated or page.truncated_query
        if len(page.hits) >= _LOOKUP_SCAN_CAP:
            truncated = True
        hits = _aggregate_by_file(page.hits)
    else:
        id_sets: list[set[str]] = []
        hit_maps: list[dict] = []
        for token in tokens:
            one = replace(understood, text=token)
            page = search_files(store, one.to_search_query(
                space_ids=list(space_ids or []), scope="all",
                limit=_LOOKUP_SCAN_CAP, offset=0))
            if page.note:
                notes.append(page.note)
            truncated = truncated or page.truncated_query
            if len(page.hits) >= _LOOKUP_SCAN_CAP:
                truncated = True
            by_id = _aggregate_by_file(page.hits)
            hit_maps.append({hit.file_id: hit for hit in by_id})
            id_sets.append(set(hit_maps[-1]))
        common = set.intersection(*id_sets) if id_sets else set()
        hits = []
        for file_id in common:
            chosen = None
            for mapping in hit_maps:
                hit = mapping.get(file_id)
                if hit is None:
                    continue
                if chosen is None or (hit.field == "body" and chosen.field != "body"):
                    chosen = hit
            if chosen is not None:
                hits.append(chosen)

    hits = [hit for hit in hits if _keeps(hit, understood.excluded)]
    hits.sort(key=lambda item: (
        0 if item.field == "body" else 1,
        item.file_id,
        item.rank,
        item.location,
    ))
    if truncated:
        notes.append(f"候选超过 {_LOOKUP_SCAN_CAP} 条时只在此范围内求交与排除")
    return SearchPage(
        hits=hits[offset:offset + limit],
        note="；".join(dict.fromkeys(n for n in notes if n)),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        truncated_query=truncated,
    )


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


def _aggregate_by_file(hits: list) -> list:
    """同一文件多个正文块或文件名命中时，保留一条代表结果。"""
    best: dict = {}
    order: list[str] = []
    for hit in hits:
        previous = best.get(hit.file_id)
        if previous is None:
            best[hit.file_id] = hit
            order.append(hit.file_id)
            continue
        prefer_new = (
            (hit.field == "body" and previous.field != "body")
            or (hit.field == previous.field and (
                hit.rank < previous.rank
                or (hit.rank == previous.rank and hit.location < previous.location)
            ))
        )
        if prefer_new:
            best[hit.file_id] = hit
    return [best[file_id] for file_id in order]


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
