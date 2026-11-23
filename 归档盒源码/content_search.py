"""在本地索引里按文件名、路径和正文查找。

三个及以上字符走 SQLite FTS5 的 trigram，能匹配连续的中文、英文和数字。
一到两个字符 trigram 对不上，改为带转义的连续匹配，并受分页上限约束。
查询文字只作为参数，不拼进 SQL。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from index_store import SearchHit, SearchQuery

MAX_QUERY_CHARS = 200


@dataclass
class SearchPage:
    hits: list[SearchHit] = field(default_factory=list)
    note: str = ""
    elapsed_ms: float = 0.0
    truncated_query: bool = False


def search_files(store, query: SearchQuery) -> SearchPage:
    started = time.perf_counter()
    text = (query.text or "").strip()
    notes = []
    limit = max(1, min(int(query.limit or 50), 200))
    offset = max(0, int(query.offset or 0))
    if not text and not _has_constraint(query):
        return SearchPage(note="请输入要查找的文字")
    if not text:
        # 多取一些行，按文件去重后再应用 offset/limit，排除词不会在上层空耗页容量。
        rows = _filtered_files(store, query, offset + limit)
        hits = _dedupe_file_hits([_hit_from_name(row, "") for row in rows])
        page = SearchPage(
            hits=hits[offset:offset + limit],
            note="没有关键词，只按空间、类型和时间筛选",
            elapsed_ms=(time.perf_counter() - started) * 1000)
        _attach_space_status(store, page.hits)
        return page
    if len(text) > MAX_QUERY_CHARS:
        text = text[:MAX_QUERY_CHARS]
        notes.append(f"查询已截断到 {MAX_QUERY_CHARS} 个字符")
    scope = getattr(query, "scope", "all") or "all"
    # 正文可能一文件多块，先按文件聚合再分页，因此多取候选。
    need = min(max((offset + limit) * 8, 200), 20_000)
    body_rows: list = []
    if scope in {"all", "body"}:
        if len(text) < 3:
            body_rows = _body_like(store, text, query, need)
            notes.append("不足三个字符，按连续文字查找，结果受分页上限约束")
        else:
            try:
                body_rows = _body_fts(store, _phrase(text), query, need)
            except sqlite3.OperationalError:
                body_rows = _body_like(store, text, query, need)
                notes.append("这次按连续文字查找")
    name_rows = _names(store, text, query, need) if scope in {"all", "name"} else []
    hits = [_hit_from_body(row, text) for row in body_rows]
    hits.extend(_hit_from_name(row, text) for row in name_rows)
    hits = _dedupe_file_hits(hits)
    hits.sort(key=lambda item: (0 if item.field == "body" else 1, item.file_id, item.rank, item.location))
    page = SearchPage(
        hits=hits[offset:offset + limit],
        note="；".join(notes),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        truncated_query=len((query.text or "").strip()) > MAX_QUERY_CHARS,
    )
    _attach_space_status(store, page.hits)
    return page


def _attach_space_status(store, hits: list[SearchHit]) -> None:
    status = store.space_online_map()
    for hit in hits:
        online, indexed = status.get(hit.space_id, (True, ""))
        hit.space_online = online
        hit.last_indexed_at = indexed
        if not online and hit.availability == "online":
            hit.availability = "offline"


def _dedupe_file_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """同一文件多个命中时只保留一条，优先正文。"""
    best: dict[str, SearchHit] = {}
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


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _has_constraint(query: SearchQuery) -> bool:
    return bool(
        query.space_ids or query.kinds
        or query.modified_after_ns or query.modified_before_ns
        or query.created_after_ns or query.created_before_ns)


def _filtered_files(store, query: SearchQuery, need: int):
    where, params = _filters(query, "f")
    return store.conn.execute(
        f"""
        SELECT f.file_id, f.space_id, f.rel_path, f.name, f.availability,
               f.index_state,
               COALESCE(t.status, '') AS content_status,
               COALESCE(t.reason, '') AS content_reason
        FROM files f
        LEFT JOIN file_text t ON t.file_id = f.file_id
        WHERE {where}
        ORDER BY f.file_id
        LIMIT ?
        """,
        (*params, need)).fetchall()


def _filters(query: SearchQuery, alias: str) -> tuple[str, list]:
    clauses = [f"{alias}.present=1"]
    params: list = []
    if query.space_ids:
        marks = ",".join("?" for _ in query.space_ids)
        clauses.append(f"{alias}.space_id IN ({marks})")
        params.extend(query.space_ids)
    if query.kinds:
        marks = ",".join("?" for _ in query.kinds)
        clauses.append(f"{alias}.file_type IN ({marks})")
        params.extend(query.kinds)
    after = int(getattr(query, "modified_after_ns", 0) or 0)
    before = int(getattr(query, "modified_before_ns", 0) or 0)
    if after:
        clauses.append(f"{alias}.mtime_ns>=?")
        params.append(after)
    if before:
        clauses.append(f"{alias}.mtime_ns<=?")
        params.append(before)
    created_after = int(getattr(query, "created_after_ns", 0) or 0)
    created_before = int(getattr(query, "created_before_ns", 0) or 0)
    if created_after:
        clauses.append(f"{alias}.ctime_ns>=?")
        params.append(created_after)
    if created_before:
        clauses.append(f"{alias}.ctime_ns<=?")
        params.append(created_before)
    if not query.include_offline:
        clauses.append(f"{alias}.availability='online'")
    return " AND ".join(clauses), params


def _body_fts(store, phrase: str, query: SearchQuery, need: int):
    where, params = _filters(query, "f")
    return store.conn.execute(
        f"""
        SELECT f.file_id, f.space_id, f.rel_path, f.name, f.availability,
               f.index_state, c.location, c.body, c.position,
               COALESCE(t.status, '') AS content_status,
               COALESCE(t.reason, '') AS content_reason,
               bm25(chunks_fts) AS score
        FROM chunks_fts
        JOIN chunks c ON c.chunk_key = chunks_fts.chunk_key
        JOIN files f ON f.file_id = c.file_id
        LEFT JOIN file_text t ON t.file_id = f.file_id
        WHERE chunks_fts MATCH ? AND {where}
        ORDER BY score, f.file_id, c.position
        LIMIT ?
        """,
        (phrase, *params, need)).fetchall()


def _body_like(store, text: str, query: SearchQuery, need: int):
    where, params = _filters(query, "f")
    return store.conn.execute(
        f"""
        SELECT f.file_id, f.space_id, f.rel_path, f.name, f.availability,
               f.index_state, c.location, c.body, c.position,
               COALESCE(t.status, '') AS content_status,
               COALESCE(t.reason, '') AS content_reason,
               0 AS score
        FROM chunks c
        JOIN files f ON f.file_id = c.file_id
        LEFT JOIN file_text t ON t.file_id = f.file_id
        WHERE c.body LIKE ? ESCAPE '\\' AND {where}
        ORDER BY f.file_id, c.position
        LIMIT ?
        """,
        (_like(text), *params, need)).fetchall()


def _names(store, text: str, query: SearchQuery, need: int):
    where, params = _filters(query, "f")
    pattern = _like(text)
    return store.conn.execute(
        f"""
        SELECT f.file_id, f.space_id, f.rel_path, f.name, f.availability,
               f.index_state,
               COALESCE(t.status, '') AS content_status,
               COALESCE(t.reason, '') AS content_reason
        FROM files f
        LEFT JOIN file_text t ON t.file_id = f.file_id
        WHERE (f.name LIKE ? ESCAPE '\\' OR f.rel_path LIKE ? ESCAPE '\\')
          AND {where}
        ORDER BY f.file_id
        LIMIT ?
        """,
        (pattern, pattern, *params, need)).fetchall()


def _hit_from_body(row, text: str) -> SearchHit:
    status = row["content_status"] or ""
    reason = row["content_reason"] or ""
    prefix = ""
    if status == "partial":
        prefix = "部分索引：" + (reason or "正文未完整覆盖") + "。"
    return SearchHit(
        file_id=row["file_id"],
        space_id=row["space_id"],
        rel_path=row["rel_path"],
        evidence=prefix + _snippet(row["body"] or "", text),
        availability=row["availability"],
        location=row["location"] or "",
        field="body",
        content_status=status,
        name=row["name"] or "",
        rank=int(row["position"] or 0),
    )


def _hit_from_name(row, text: str) -> SearchHit:
    del text
    return SearchHit(
        file_id=row["file_id"],
        space_id=row["space_id"],
        rel_path=row["rel_path"],
        evidence=row["rel_path"] or "",
        availability=row["availability"],
        location="文件名",
        field="name",
        content_status=row["content_status"] or "",
        name=row["name"] or "",
        rank=0,
    )


def _snippet(text: str, query: str, width: int = 80) -> str:
    found = text.find(query)
    if found < 0:
        found = text.lower().find(query.lower())
    if found < 0:
        return text[:width]
    start = max(0, found - 16)
    end = min(len(text), found + len(query) + 24)
    return text[start:end]
