"""
归档盒 V1.7 — 最近整理空间（本机使用状态，纯逻辑）

存储：系统用户配置目录下的 `recent_spaces.json`，与方案库共用稳定应用标识。
- 不写入方案文件、导出包或 Windows 交接包；
- 只记录路径与使用信息，移除记录不删除真实目录、配置或历史；
- 目录不存在时只标记状态，不自动创建。

测试通过环境变量 `ARCHIVEBOX_RECENT_FILE` 指向独立临时文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import scheme_store

ENV_OVERRIDE = "ARCHIVEBOX_RECENT_FILE"
FILENAME = "recent_spaces.json"
VERSION = 1
MAX_RECORDS = 30


def store_path() -> str:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(scheme_store.app_data_dir(), FILENAME)


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recent_spaces.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _normalize(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or "")))


def _key(path: str) -> str:
    """比较用键：Windows 上路径大小写不敏感，避免同一目录记成两条（S2-19）。"""
    real = _normalize(path)
    return os.path.normcase(real) if os.name == "nt" else real


def _quarantine_corrupt(path: str) -> None:
    """损坏的 JSON 不删除、不覆盖：改名留存，便于用户/同步盘恢复（S2-37）。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        os.replace(path, f"{path}.corrupt-{stamp}")
    except OSError:
        pass


def _read() -> dict:
    path = store_path()
    if not os.path.isfile(path):
        return {"version": VERSION, "items": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        _quarantine_corrupt(path)
        return {"version": VERSION, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        _quarantine_corrupt(path)
        return {"version": VERSION, "items": []}
    return data


def _write(data: dict) -> None:
    _atomic_write(store_path(), json.dumps(data, ensure_ascii=False, indent=2))


@dataclass
class SpaceRecord:
    path: str
    name: str
    last_opened: str = ""
    pinned: bool = False
    exists: bool = True
    accessible: bool = True
    status: str = ""
    status_reason: str = ""

    @property
    def usable(self) -> bool:
        return self.exists and self.accessible


def _status_for(path: str):
    if not os.path.exists(path):
        reason = "目录不存在或设备尚未挂载"
        return False, False, "不存在", reason
    if not os.path.isdir(path):
        return True, False, "不是目录", "该路径当前不是目录"
    if not os.access(path, os.R_OK | os.X_OK):
        return True, False, "不可访问", "没有读取/进入权限"
    return True, True, "可用", ""


def _to_record(row: dict) -> SpaceRecord:
    path = _normalize(str(row.get("path", "")))
    exists, accessible, status, reason = _status_for(path)
    return SpaceRecord(
        path=path,
        name=str(row.get("name") or os.path.basename(path) or path),
        last_opened=str(row.get("last_opened", "")),
        pinned=bool(row.get("pinned", False)),
        exists=exists, accessible=accessible,
        status=status, status_reason=reason)


def list_spaces() -> List[SpaceRecord]:
    data = _read()
    records = []
    for row in data.get("items", []):
        if not isinstance(row, dict) or not str(row.get("path", "")).strip():
            continue
        records.append(_to_record(row))
    records.sort(key=lambda r: (not r.pinned, _neg_time(r.last_opened)))
    return records


def _neg_time(value: str):
    # 时间字符串为 ISO，可字典序比较；用负序实现“最近在前”
    return "".join(chr(0x10FFFF - ord(c)) for c in (value or ""))


def touch(path: str, name: str = "") -> None:
    path = _normalize(path)
    if not path:
        return
    data = _read()
    items = [row for row in data.get("items", []) if isinstance(row, dict)]
    found = None
    for row in items:
        if _key(str(row.get("path", ""))) == _key(path):
            found = row
            break
    if found is None:
        found = {"path": path, "pinned": False}
        items.append(found)
    found["path"] = path
    found["name"] = (name or os.path.basename(path) or path)
    found["last_opened"] = datetime.now().isoformat(timespec="microseconds")
    # 超出上限时优先丢弃最早的非固定记录
    if len(items) > MAX_RECORDS:
        items.sort(key=lambda r: bool(r.get("pinned", False)))
        while len(items) > MAX_RECORDS:
            for i, row in enumerate(items):
                if not row.get("pinned", False):
                    items.pop(i)
                    break
            else:
                break
    data["version"] = VERSION
    data["items"] = items
    _write(data)


def set_pinned(path: str, pinned: bool) -> bool:
    target = _key(path)
    data = _read()
    changed = False
    for row in data.get("items", []):
        if isinstance(row, dict) and _key(str(row.get("path", ""))) == target:
            row["pinned"] = bool(pinned)
            changed = True
    if changed:
        data["version"] = VERSION
        _write(data)
    return changed


def remove(path: str) -> bool:
    target = _normalize(path)
    data = _read()
    items = [row for row in data.get("items", [])
             if not (isinstance(row, dict)
                     and _normalize(str(row.get("path", ""))) == target)]
    if len(items) == len(data.get("items", [])):
        return False
    data["version"] = VERSION
    data["items"] = items
    _write(data)
    return True


def clear() -> None:
    data = {"version": VERSION, "items": []}
    _write(data)


def get(path: str) -> Optional[SpaceRecord]:
    target = _normalize(path)
    for rec in list_spaces():
        if rec.path == target:
            return rec
    return None
