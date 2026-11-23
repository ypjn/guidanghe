"""AI 整理顾问：厂商目录快照 + 咨询会话本地 SQLite。

厂商清单仍以源码里的 PROVIDERS 为准（随版本更新）；这里写入一份只读
快照方便排查，并把多轮对话落盘，方便同一整理空间继续调整。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from ai_fallback import PROVIDERS, _restrict_windows_acl
from scheme_store import app_data_dir

_DB_NAME = "ai_consult.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    title TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    snapshot_text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_root
    ON sessions(root, updated_at DESC);
"""


@dataclass
class ConsultMessage:
    role: str
    content: str
    created_at: float = 0.0


@dataclass
class ConsultSession:
    id: str
    root: str
    title: str
    provider_id: str
    model: str
    snapshot_text: str
    created_at: float
    updated_at: float
    messages: list[ConsultMessage]


def _db_path() -> str:
    return os.path.join(app_data_dir(), _DB_NAME)


def _connect() -> sqlite3.Connection:
    directory = app_data_dir()
    os.makedirs(directory, exist_ok=True)
    path = _db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def sync_provider_catalog() -> None:
    """把内置厂商目录写入本地库，便于查看与排障。"""
    now = time.time()
    with _connect() as conn:
        for spec in PROVIDERS:
            if spec.id == "custom":
                continue
            conn.execute(
                "INSERT INTO providers(id, name, base_url, model, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, base_url=excluded.base_url, "
                "model=excluded.model, updated_at=excluded.updated_at",
                (spec.id, spec.name, spec.base_url, spec.model, now),
            )
        conn.commit()
    try:
        _restrict_windows_acl(_db_path())
    except Exception:
        pass


def list_providers() -> list[dict]:
    sync_provider_catalog()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, base_url, model FROM providers ORDER BY name"
        ).fetchall()
    return [dict(row) for row in rows]


def create_session(*, root: str, title: str, provider_id: str, model: str,
                   snapshot_text: str,
                   messages: Iterable[ConsultMessage] | None = None
                   ) -> ConsultSession:
    sync_provider_catalog()
    session_id = "acs-" + uuid.uuid4().hex[:12]
    now = time.time()
    msg_list = list(messages or [])
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, root, title, provider_id, model, "
            "snapshot_text, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, root, title, provider_id, model, snapshot_text,
             now, now),
        )
        for message in msg_list:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) "
                "VALUES(?,?,?,?)",
                (session_id, message.role, message.content,
                 message.created_at or now),
            )
        conn.commit()
    return get_session(session_id)


def append_message(session_id: str, role: str, content: str) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages(session_id, role, content, created_at) "
            "VALUES(?,?,?,?)",
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (now, session_id),
        )
        conn.commit()


def get_session(session_id: str) -> ConsultSession | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        msg_rows = conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    messages = [
        ConsultMessage(role=m["role"], content=m["content"],
                       created_at=float(m["created_at"]))
        for m in msg_rows
    ]
    return ConsultSession(
        id=row["id"],
        root=row["root"],
        title=row["title"],
        provider_id=row["provider_id"],
        model=row["model"],
        snapshot_text=row["snapshot_text"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        messages=messages,
    )


def latest_session_for_root(root: str) -> ConsultSession | None:
    root_norm = os.path.normcase(os.path.abspath(root))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, root FROM sessions ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    for row in rows:
        if os.path.normcase(os.path.abspath(row["root"])) == root_norm:
            return get_session(row["id"])
    return None


def export_session_json(session_id: str) -> str:
    session = get_session(session_id)
    if session is None:
        raise KeyError(session_id)
    payload = {
        "id": session.id,
        "root": session.root,
        "title": session.title,
        "provider_id": session.provider_id,
        "model": session.model,
        "snapshot_text": session.snapshot_text,
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at}
            for m in session.messages
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
