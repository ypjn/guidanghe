"""本地文件空间索引。

数据库放在用户数据目录，可以整库重建。
它不保存整理历史、偏好或撤销记录，重建失败时保留上一份可查询的库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import scheme_store

SCHEMA_VERSION = 3
ENV_INDEX_DB = "ARCHIVEBOX_INDEX_DB"

CONTENT_DDL = """
CREATE TABLE IF NOT EXISTS file_text (
    file_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL DEFAULT '',
    parser_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_key TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    location TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_key UNINDEXED,
    file_id UNINDEXED,
    space_id UNINDEXED,
    location UNINDEXED,
    body,
    tokenize='trigram'
);
"""


@dataclass
class SpaceSpec:
    space_id: str
    root: str
    name: str
    policy: dict
    enabled: bool = True
    volume_id: str = ""
    filesystem: str = ""
    online: bool = True
    last_indexed_at: str = ""


@dataclass
class IndexUpdateReport:
    space_id: str
    completed: bool = False
    cancelled: bool = False
    coverage: str = "partial"
    errors: list[str] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    deleted: int = 0
    reused: int = 0
    reread: int = 0
    excluded: int = 0
    superseded: bool = False

    @property
    def ok(self) -> bool:
        return self.completed and not self.cancelled and not self.superseded


@dataclass
class SearchQuery:
    text: str = ""
    space_ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    include_offline: bool = True
    limit: int = 100
    offset: int = 0
    scope: str = "all"
    modified_after_ns: int = 0
    modified_before_ns: int = 0
    created_after_ns: int = 0
    created_before_ns: int = 0


@dataclass
class SearchHit:
    file_id: str
    space_id: str
    rel_path: str
    evidence: str = ""
    availability: str = "online"
    space_online: bool = True
    last_indexed_at: str = ""
    index_version: int = SCHEMA_VERSION
    location: str = ""
    field: str = ""
    content_status: str = ""
    name: str = ""
    rank: int = 0


@dataclass
class FileRecord:
    file_id: str
    space_id: str
    rel_path: str
    name: str
    file_type: str
    size: int
    mtime_ns: int
    ctime_ns: int
    dev: int
    ino: int
    content_sha256: str
    availability: str
    index_state: str
    present: bool
    exclude_reason: str = ""
    parser_version: int = 0


def default_index_path() -> str:
    override = os.environ.get(ENV_INDEX_DB, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(scheme_store.app_data_dir(), "index", "index.db")


class IndexStoreError(RuntimeError):
    pass


class IndexStore:
    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(path or default_index_path())
        self.diagnostic = ""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._open()

    def _open(self) -> None:
        if os.path.exists(self.path) and not os.path.isfile(self.path):
            raise IndexStoreError(f"索引路径不是文件：{self.path}")
        self.conn = sqlite3.connect(self.path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._ensure_schema()
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            self._quarantine(str(exc))
            self.conn = sqlite3.connect(self.path, timeout=5)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _quarantine(self, reason: str) -> None:
        stamp = uuid.uuid4().hex[:8]
        dest = f"{self.path}.corrupt-{stamp}"
        os.replace(self.path, dest)
        self.diagnostic = f"索引库无法读取，已改名保留为 {dest}。原因：{reason}"

    def _ensure_schema(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._create_schema()
            return
        version = int(row["value"])
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise IndexStoreError(
                f"索引库版本 {version} 比程序新，没有清空它。")
        self._migrate(version)

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spaces (
                space_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                policy_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '',
                volume_id TEXT NOT NULL DEFAULT '',
                filesystem TEXT NOT NULL DEFAULT '',
                online INTEGER NOT NULL DEFAULT 1,
                last_indexed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                path_key TEXT NOT NULL,
                name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL DEFAULT '',
                availability TEXT NOT NULL,
                index_state TEXT NOT NULL,
                parser_version INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL,
                exclude_reason TEXT NOT NULL DEFAULT '',
                present INTEGER NOT NULL DEFAULT 1,
                UNIQUE(space_id, path_key)
            );
            CREATE INDEX IF NOT EXISTS idx_files_space
                ON files(space_id, present);
            """
        )
        self.conn.executescript(CONTENT_DDL)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        self.conn.commit()

    def _migrate(self, version: int) -> None:
        """版本升级在事务里完成，保留空间、索引、偏好与历史相关数据。"""
        if version == 1:
            self.conn.executescript(CONTENT_DDL)
            self.conn.execute(
                "UPDATE meta SET value='2' WHERE key='schema_version'")
            self.conn.commit()
            version = 2
        if version == 2 and SCHEMA_VERSION >= 3:
            self._migrate_v2_to_v3()
            return
        if version != SCHEMA_VERSION:
            raise IndexStoreError(
                f"没有从版本 {version} 升级的步骤，已保留原库。")

    def _migrate_v2_to_v3(self) -> None:
        from volume_identity import probe_volume

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(spaces)")}
            additions = [
                ("volume_id", "TEXT NOT NULL DEFAULT ''"),
                ("filesystem", "TEXT NOT NULL DEFAULT ''"),
                ("online", "INTEGER NOT NULL DEFAULT 1"),
                ("last_indexed_at", "TEXT NOT NULL DEFAULT ''"),
            ]
            for name, decl in additions:
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE spaces ADD COLUMN {name} {decl}")
            rows = self.conn.execute("SELECT space_id, root_path FROM spaces").fetchall()
            now = datetime.now().isoformat(timespec="seconds")
            for row in rows:
                root = row["root_path"]
                online = 1
                volume_id = ""
                filesystem = ""
                if os.path.isdir(root):
                    info = probe_volume(root)
                    if info is not None:
                        volume_id = info.volume_id
                        filesystem = info.filesystem
                else:
                    online = 0
                self.conn.execute(
                    """
                    UPDATE spaces SET volume_id=?, filesystem=?, online=?, updated_at=?
                    WHERE space_id=?
                    """,
                    (volume_id, filesystem, online, now, row["space_id"]))
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def rebuild(self, *, fail: bool = False) -> None:
        """写到旁边的新库，成功后才替换。失败时原来的库还能查询。"""
        dest = self.path + ".rebuilding"
        if os.path.exists(dest):
            os.remove(dest)
        fresh = None
        try:
            fresh = sqlite3.connect(dest)
            fresh.row_factory = sqlite3.Row
            source_spaces = self.conn.execute("SELECT * FROM spaces").fetchall()
            source_files = self.conn.execute("SELECT * FROM files").fetchall()
            if fail:
                raise RuntimeError("注入的重建失败")
            fresh.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE spaces (
                    space_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT '',
                    volume_id TEXT NOT NULL DEFAULT '',
                    filesystem TEXT NOT NULL DEFAULT '',
                    online INTEGER NOT NULL DEFAULT 1,
                    last_indexed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE files (
                    file_id TEXT PRIMARY KEY,
                    space_id TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    path_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    dev INTEGER NOT NULL,
                    ino INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL DEFAULT '',
                    availability TEXT NOT NULL,
                    index_state TEXT NOT NULL,
                    parser_version INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL,
                    exclude_reason TEXT NOT NULL DEFAULT '',
                    present INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(space_id, path_key)
                );
                """
            )
            fresh.executescript(CONTENT_DDL)
            fresh.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            source_texts = self.conn.execute("SELECT * FROM file_text").fetchall()
            source_chunks = self.conn.execute("SELECT * FROM chunks").fetchall()
            for row in source_spaces:
                values = _space_row_tuple(row)
                fresh.execute(
                    "INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values)
            for row in source_files:
                fresh.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row))
            for row in source_texts:
                fresh.execute(
                    "INSERT INTO file_text(file_id, space_id, content_sha256, "
                    "parser_version, status, reason) VALUES (?,?,?,?,?,?)",
                    (row["file_id"], row["space_id"], row["content_sha256"],
                     row["parser_version"], row["status"], row["reason"]))
            for row in source_chunks:
                fresh.execute(
                    "INSERT INTO chunks(chunk_key, file_id, space_id, position, "
                    "location, body) VALUES (?,?,?,?,?,?)",
                    (row["chunk_key"], row["file_id"], row["space_id"],
                     row["position"], row["location"], row["body"]))
                fresh.execute(
                    "INSERT INTO chunks_fts(chunk_key, file_id, space_id, "
                    "location, body) VALUES (?,?,?,?,?)",
                    (row["chunk_key"], row["file_id"], row["space_id"],
                     row["location"], row["body"]))
            fresh.commit()
            fresh.close()
            fresh = None
            self.conn.close()
            os.replace(dest, self.path)
            self._open()
        except Exception:
            if fresh is not None:
                fresh.close()
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            if not self._connection_open():
                self._open()
            raise

    def _connection_open(self) -> bool:
        try:
            self.conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def generation(self, space_id: str) -> int:
        row = self.conn.execute(
            "SELECT generation FROM spaces WHERE space_id=?",
            (space_id,)).fetchone()
        if row is None:
            raise IndexStoreError(f"空间不存在：{space_id}")
        return int(row["generation"])

    def bump_generation(self, space_id: str) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.generation(space_id)
            new_value = current + 1
            self.conn.execute(
                "UPDATE spaces SET generation=? WHERE space_id=?",
                (new_value, space_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return new_value

    def generation_is_current(self, space_id: str, generation: int) -> bool:
        return self.generation(space_id) == generation

    def upsert_space(self, spec: SpaceSpec, now: str) -> None:
        existing = self.get_space(spec.space_id)
        policy = json.dumps(spec.policy, ensure_ascii=False, sort_keys=True)
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO spaces(
                    space_id, root_path, display_name, enabled, policy_json,
                    created_at, updated_at, generation, last_status,
                    volume_id, filesystem, online, last_indexed_at)
                VALUES (?,?,?,?,?,?,?,0,'',?,?,?,?)
                """,
                (spec.space_id, spec.root, spec.name, 1 if spec.enabled else 0,
                 policy, now, now, spec.volume_id, spec.filesystem,
                 1 if spec.online else 0, spec.last_indexed_at))
        else:
            self.conn.execute(
                """
                UPDATE spaces SET root_path=?, display_name=?, enabled=?,
                    policy_json=?, updated_at=?, volume_id=?, filesystem=?,
                    online=?, last_indexed_at=?
                WHERE space_id=?
                """,
                (spec.root, spec.name, 1 if spec.enabled else 0, policy, now,
                 spec.volume_id, spec.filesystem, 1 if spec.online else 0,
                 spec.last_indexed_at, spec.space_id))
        self.conn.commit()

    def get_space(self, space_id: str) -> Optional[SpaceSpec]:
        row = self.conn.execute(
            "SELECT * FROM spaces WHERE space_id=?", (space_id,)).fetchone()
        return None if row is None else _space_from_row(row)

    def find_space_by_root(self, root: str) -> Optional[SpaceSpec]:
        key = os.path.realpath(os.path.abspath(root))
        for spec in self.list_spaces():
            if os.path.realpath(spec.root) == key:
                return spec
        return None

    def list_spaces(self) -> list[SpaceSpec]:
        rows = self.conn.execute(
            "SELECT * FROM spaces ORDER BY display_name, space_id").fetchall()
        return [_space_from_row(row) for row in rows]

    def set_status(self, space_id: str, status: str, now: str) -> None:
        self.conn.execute(
            "UPDATE spaces SET last_status=?, updated_at=? WHERE space_id=?",
            (status, now, space_id))
        self.conn.commit()

    def set_last_indexed_at(self, space_id: str, when: str) -> None:
        self.conn.execute(
            "UPDATE spaces SET last_indexed_at=?, updated_at=? WHERE space_id=?",
            (when, when, space_id))
        self.conn.commit()

    def mark_files_offline(self, space_id: str, now: str) -> None:
        self.conn.execute(
            """
            UPDATE files SET availability='offline', indexed_at=?
            WHERE space_id=? AND present=1 AND availability NOT IN ('excluded','deleted','link')
            """,
            (now, space_id))
        self.conn.commit()

    def space_online_map(self) -> dict[str, tuple[bool, str]]:
        rows = self.conn.execute(
            "SELECT space_id, online, last_indexed_at FROM spaces").fetchall()
        return {
            row["space_id"]: (bool(row["online"]), row["last_indexed_at"] or "")
            for row in rows
        }

    def remove_space(self, space_id: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._delete_space_text(space_id)
            self.conn.execute("DELETE FROM files WHERE space_id=?", (space_id,))
            self.conn.execute("DELETE FROM spaces WHERE space_id=?", (space_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_file(self, space_id: str, path_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE space_id=? AND path_key=?",
            (space_id, path_key)).fetchone()

    def present_files(self, space_id: Optional[str] = None) -> list[FileRecord]:
        if space_id:
            rows = self.conn.execute(
                "SELECT * FROM files WHERE space_id=? AND present=1 "
                "ORDER BY rel_path", (space_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM files WHERE present=1 ORDER BY space_id, rel_path"
            ).fetchall()
        return [_file_from_row(row) for row in rows]

    def write_file(self, generation: int, row: dict) -> str:
        """写入一条文件记录。代次已经过期时返回 superseded，不覆盖新任务。"""
        started = not self.conn.in_transaction
        if started:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.generation_is_current(row["space_id"], generation):
                if started:
                    self.conn.rollback()
                return "superseded"
            self._insert_file(row)
            if started:
                self.conn.commit()
        except Exception:
            if started:
                self.conn.rollback()
            raise
        return "written"

    def _insert_file(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO files(
                file_id, space_id, rel_path, path_key, name, file_type, size,
                mtime_ns, ctime_ns, dev, ino, content_sha256, availability,
                index_state, parser_version,                 indexed_at, exclude_reason, present)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(space_id, path_key) DO UPDATE SET
                file_id=excluded.file_id,
                rel_path=excluded.rel_path,
                name=excluded.name,
                file_type=excluded.file_type,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                ctime_ns=excluded.ctime_ns,
                dev=excluded.dev,
                ino=excluded.ino,
                content_sha256=excluded.content_sha256,
                availability=excluded.availability,
                index_state=excluded.index_state,
                parser_version=excluded.parser_version,
                indexed_at=excluded.indexed_at,
                exclude_reason=excluded.exclude_reason,
                present=excluded.present
            """,
            (
                row["file_id"], row["space_id"], row["rel_path"], row["path_key"],
                row["name"], row["file_type"], row["size"], row["mtime_ns"],
                row["ctime_ns"], row["dev"], row["ino"], row["content_sha256"],
                row["availability"], row["index_state"], row["parser_version"],
                row["indexed_at"], row["exclude_reason"], row["present"],
            ))

    def commit(self) -> None:
        self.conn.commit()

    def mark_deleted(self, space_id: str, generation: int, path_keys: list[str],
                     now: str) -> int:
        if not path_keys:
            return 0
        if not self.generation_is_current(space_id, generation):
            return -1
        count = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.generation_is_current(space_id, generation):
                self.conn.rollback()
                return -1
            for key in path_keys:
                cur = self.conn.execute(
                    "UPDATE files SET present=0, availability='deleted', "
                    "indexed_at=? WHERE space_id=? AND path_key=? AND present=1",
                    (now, space_id, key))
                count += cur.rowcount if cur.rowcount > 0 else 0
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return count

    def get_file_text(self, file_id: str):
        return self.conn.execute(
            "SELECT * FROM file_text WHERE file_id=?", (file_id,)).fetchone()

    def replace_text(self, generation: int, space_id: str, file_id: str,
                     content_sha256: str, parser_version: int, status: str,
                     reason: str, chunks: list) -> str:
        """用一次提取结果替换该文件的正文。代次过期时不写。"""
        if not self.generation_is_current(space_id, generation):
            return "superseded"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.generation_is_current(space_id, generation):
                self.conn.rollback()
                return "superseded"
            self._delete_file_text(file_id)
            self.conn.execute(
                "INSERT INTO file_text(file_id, space_id, content_sha256, "
                "parser_version, status, reason) VALUES (?,?,?,?,?,?)",
                (file_id, space_id, content_sha256, parser_version, status,
                 reason or ""))
            for chunk in chunks:
                key = f"{file_id}:{chunk.position}"
                self.conn.execute(
                    "INSERT INTO chunks(chunk_key, file_id, space_id, position, "
                    "location, body) VALUES (?,?,?,?,?,?)",
                    (key, file_id, space_id, chunk.position, chunk.location,
                     chunk.text))
                self.conn.execute(
                    "INSERT INTO chunks_fts(chunk_key, file_id, space_id, "
                    "location, body) VALUES (?,?,?,?,?)",
                    (key, file_id, space_id, chunk.location, chunk.text))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return "written"

    def clear_space_text(self, space_id: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._delete_space_text(space_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def prune_text(self, space_id: str) -> None:
        """删掉已不在当前内容索引里的正文，不碰来源文件。"""
        rows = self.conn.execute(
            """
            SELECT t.file_id FROM file_text t
            LEFT JOIN files f ON f.file_id=t.file_id
            WHERE t.space_id=? AND (
                f.file_id IS NULL OR f.present=0 OR f.index_state!='content')
            """,
            (space_id,)).fetchall()
        if not rows:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                self._delete_file_text(row["file_id"])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _delete_space_text(self, space_id: str) -> None:
        self.conn.execute("DELETE FROM chunks_fts WHERE space_id=?", (space_id,))
        self.conn.execute("DELETE FROM chunks WHERE space_id=?", (space_id,))
        self.conn.execute("DELETE FROM file_text WHERE space_id=?", (space_id,))

    def _delete_file_text(self, file_id: str) -> None:
        self.conn.execute("DELETE FROM chunks_fts WHERE file_id=?", (file_id,))
        self.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        self.conn.execute("DELETE FROM file_text WHERE file_id=?", (file_id,))


def _space_from_row(row: sqlite3.Row) -> SpaceSpec:
    try:
        policy = json.loads(row["policy_json"])
    except (TypeError, ValueError):
        policy = {}
    if not isinstance(policy, dict):
        policy = {}
    keys = row.keys()
    return SpaceSpec(
        space_id=row["space_id"],
        root=row["root_path"],
        name=row["display_name"],
        policy=policy,
        enabled=bool(row["enabled"]),
        volume_id=row["volume_id"] if "volume_id" in keys else "",
        filesystem=row["filesystem"] if "filesystem" in keys else "",
        online=bool(row["online"]) if "online" in keys else True,
        last_indexed_at=row["last_indexed_at"] if "last_indexed_at" in keys else "",
    )


def _space_row_tuple(row: sqlite3.Row) -> tuple:
    keys = row.keys()
    defaults = {
        "volume_id": "",
        "filesystem": "",
        "online": 1,
        "last_indexed_at": "",
    }
    values = []
    for key in (
        "space_id", "root_path", "display_name", "enabled", "policy_json",
        "created_at", "updated_at", "generation", "last_status",
        "volume_id", "filesystem", "online", "last_indexed_at",
    ):
        if key in keys:
            values.append(row[key])
        else:
            values.append(defaults.get(key, ""))
    return tuple(values)


def _file_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        file_id=row["file_id"],
        space_id=row["space_id"],
        rel_path=row["rel_path"],
        name=row["name"],
        file_type=row["file_type"],
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        ctime_ns=int(row["ctime_ns"]),
        dev=int(row["dev"]),
        ino=int(row["ino"]),
        content_sha256=row["content_sha256"] or "",
        availability=row["availability"],
        index_state=row["index_state"],
        present=bool(row["present"]),
        exclude_reason=row["exclude_reason"] or "",
        parser_version=int(row["parser_version"] or 0),
    )
