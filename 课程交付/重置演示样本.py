#!/usr/bin/env python3
"""准备阶段三演示用的 3 个文件，并让同一目录可以重新从首次预览开始。

只创建还没有的演示文件。再次运行时，只写回本脚本创建过的普通文件。
已经存在的用户文件、符号链接，以及链接指向的目录外文件，都不改写。
演示开始前已经存在的空间偏好保留。只有脚本先建过这份演示清单之后
才确认的偏好，会在下一次重置时从本空间拿掉，方便同一演示目录重新
从首次预览开始。操作历史保留。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
DEFAULT = HERE / "演示样本"
LEDGER_NAME = "演示样本清单.json"

OWNED = {
    "下载/归档盒/开发说明.md": "接口说明与排期\n",
    "下载/笔记.txt": "高等数学 极限 导数\n",
    "项目/AIGC/项目说明.md": "项目会议记录\n",
}
OUTSIDE_NAME = "范围外-不要放进样本.md"
OUTSIDE_TEXT = "这是空间外对照，扫描不应包含它。\n"


class Skip(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _reject_root(root: Path) -> Path:
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError(f"拒绝跟随符号链接作为整理空间：{root}")
    root = root.resolve()
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError(f"拒绝把演示文件写进 {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"整理空间必须是目录：{root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _walk(root: Path, rel: str, create_dirs: bool) -> Path:
    """沿相对路径逐段查看。任一节是符号链接就停，不创建、不跟随。"""
    current = root
    parts = [part for part in rel.split("/") if part]
    for index, part in enumerate(parts):
        nxt = current / part
        final = index == len(parts) - 1
        if nxt.is_symlink():
            raise Skip("符号链接未跟随")
        if nxt.exists():
            if not _inside(root, nxt):
                raise Skip("路径越出整理空间")
            if not final and not nxt.is_dir():
                raise Skip("路径中有同名的非目录")
        elif final:
            return nxt
        elif create_dirs:
            nxt.mkdir()
            if nxt.is_symlink() or not nxt.is_dir():
                raise Skip("未能在整理空间内建目录")
        else:
            raise Skip("演示文件不存在")
        current = nxt
    return current


def _identity(path: Path) -> dict:
    st = path.lstat()
    return {"dev": st.st_dev, "ino": st.st_ino}


def _ledger_path(root: Path) -> Path:
    return root / ".organizer" / LEDGER_NAME


def _read_ledger(root: Path) -> tuple[dict, list[str] | None]:
    """返回 (文件身份, 演示前偏好编号)。

    第二个值是 None 表示这份目录还没有被脚本记录过演示前的偏好。
    """
    path = _ledger_path(root)
    if not path.is_file() or path.is_symlink():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    if "preserved_preference_ids" not in data:
        return files, None
    raw = data.get("preserved_preference_ids")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return files, None
    return files, raw


def _atomic_write(path: Path, text: str) -> None:
    """在同一目录创建新的普通文件再替换。不打开已有的临时文件名，也不跟随符号链接。"""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise Skip("清单目录是符号链接")
    if path.is_symlink():
        raise Skip("清单路径是符号链接")
    fd, name = tempfile.mkstemp(prefix=".演示清单-", dir=parent)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if path.is_symlink():
            raise Skip("清单路径是符号链接")
        os.replace(name, path)
        name = ""
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def _write_ledger(root: Path, files: dict, preserved: list[str] | None) -> None:
    path = _ledger_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise Skip("清单路径是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise Skip("清单路径是符号链接")
    payload = {"files": files}
    if preserved is not None:
        payload["preserved_preference_ids"] = preserved
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_regular(path: Path, text: str, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_NOFOLLOW
    if exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def place_owned_files(root: Path, preserved: list[str] | None) -> list[str]:
    notes = []
    ledger, recorded = _read_ledger(root)
    if preserved is None:
        preserved = recorded
    for rel, text in OWNED.items():
        try:
            path = _walk(root, rel, create_dirs=True)
        except Skip as exc:
            notes.append(f"{exc.reason}：{rel}")
            continue
        if path.exists():
            if path.is_symlink() or not path.is_file():
                notes.append(f"已有路径未覆盖：{rel}")
                continue
            recorded = ledger.get(rel)
            current = _identity(path)
            if recorded != current:
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            try:
                _write_regular(path, text, exclusive=False)
            except OSError:
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            notes.append(f"已写回脚本创建的文件：{rel}")
        else:
            try:
                _write_regular(path, text, exclusive=True)
            except OSError:
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            if path.is_symlink() or not path.is_file():
                notes.append(f"已有路径未覆盖：{rel}")
                continue
            ledger[rel] = _identity(path)
            notes.append(f"已新建演示文件：{rel}")
    try:
        _write_ledger(root, ledger, preserved)
    except Skip as exc:
        notes.append(exc.reason)
    return notes


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(path.resolve().as_posix()) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _snapshot_preference_ids(root: Path) -> list[str] | None:
    """读取演示开始前的偏好编号。读不到就返回 None，调用方不得改库。"""
    org = root / ".organizer"
    if not org.exists():
        return []
    if org.is_symlink():
        return None
    found: list[str] = []
    db = org / "preferences.db"
    if db.is_symlink():
        return None
    if db.is_file():
        try:
            conn = _connect_readonly(db)
            try:
                rows = conn.execute("SELECT preference_id FROM preferences").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        found.extend(str(row[0]) for row in rows if row[0])
    cfg = org / "classification_config.json"
    if cfg.is_symlink():
        return None
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        rules = data.get("long_term_rules") or []
        if not isinstance(rules, list):
            return None
        for rule in rules:
            if isinstance(rule, dict) and isinstance(rule.get("rule_id"), str):
                found.append(rule["rule_id"])
    return list(dict.fromkeys(found))


def _filter_config_rules(root: Path, preserved: set[str]) -> str | None:
    org = root / ".organizer"
    cfg = org / "classification_config.json"
    if not cfg.exists():
        return None
    if cfg.is_symlink():
        return "跳过：分类配置是符号链接，未改偏好"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "分类配置无法读取，未改偏好"
    if not isinstance(data, dict):
        return "分类配置无法读取，未改偏好"
    rules = data.get("long_term_rules") or []
    if not isinstance(rules, list):
        return "分类配置无法读取，未改偏好"
    kept = [
        rule for rule in rules
        if isinstance(rule, dict) and rule.get("rule_id") in preserved
    ]
    if kept == rules:
        return None
    data["long_term_rules"] = kept
    try:
        _atomic_write(cfg, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except Skip as exc:
        return f"{exc.reason}，未改偏好"
    return None


def _drop_preferences_added_during_demo(root: Path, preserved: set[str]) -> str:
    """只删除演示清单记下之后新增的偏好。演示前的编号原样保留。"""
    blocked = _filter_config_rules(root, preserved)
    if blocked:
        return blocked
    org = root / ".organizer"
    db = org / "preferences.db"
    if db.is_symlink():
        return "跳过：preferences.db 是符号链接，未删除"
    if not db.is_file():
        return "已保留进入演示前的偏好"
    if not preserved:
        db.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.is_symlink():
                continue
            if side.is_file():
                side.unlink()
        return "已清除演示期间新增的偏好，下次扫描从首次预览开始"
    try:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT preference_id FROM preferences").fetchall()
            extra = [row[0] for row in rows if row[0] not in preserved]
            if extra:
                conn.executemany(
                    "DELETE FROM preferences WHERE preference_id = ?",
                    [(item,) for item in extra])
                conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return "偏好库无法改写，已有偏好保留"
    if extra:
        return "已清除演示期间新增的偏好，下次扫描从首次预览开始"
    return "已保留进入演示前的偏好"


def reset(root: Path) -> Path:
    root = _reject_root(root)
    _files, recorded = _read_ledger(root)
    if recorded is None:
        preserved = _snapshot_preference_ids(root)
        place_owned_files(root, preserved)
        if preserved:
            note = "已保留进入演示前的偏好"
        elif preserved is None:
            note = "偏好库无法安全读取，未改动"
        else:
            note = "本空间没有已保存的偏好"
        reset.last_note = note
    else:
        place_owned_files(root, recorded)
        reset.last_note = _drop_preferences_added_during_demo(root, set(recorded))
    outside = root.parent / OUTSIDE_NAME
    if not outside.exists():
        outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    return outside


reset.last_note = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备归档盒阶段三演示文件")
    parser.add_argument("--root", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    try:
        root = _reject_root(args.root)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    outside = root.parent / OUTSIDE_NAME
    outside_existed = outside.exists()
    _files, recorded = _read_ledger(root)
    if recorded is None:
        preserved = _snapshot_preference_ids(root)
        notes = place_owned_files(root, preserved)
        if preserved:
            preference_note = "已保留进入演示前的偏好"
        elif preserved is None:
            preference_note = "偏好库无法安全读取，未改动"
        else:
            preference_note = "本空间没有已保存的偏好"
    else:
        notes = place_owned_files(root, recorded)
        preference_note = _drop_preferences_added_during_demo(root, set(recorded))
    if not outside.exists():
        outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    print(f"整理空间：{root}")
    print(f"范围外对照：{outside}")
    if outside_existed:
        print("范围外对照已存在，未覆盖")
    else:
        print("已新建范围外对照")
    for line in notes:
        print(line)
    print(preference_note)
    extras = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if rel in OWNED or rel == f".organizer/{LEDGER_NAME}":
            continue
        extras.append(rel)
    if extras:
        print("保留的非演示文件：")
        for rel in extras:
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
