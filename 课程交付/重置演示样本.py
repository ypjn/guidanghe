#!/usr/bin/env python3
"""准备阶段三演示用的 3 个文件，并让同一目录可以重新从首次预览开始。

只创建还没有的演示文件。再次运行时，只写回本脚本创建过的普通文件。
已经存在的用户文件、符号链接，以及链接指向的目录外文件，都不改写。
同时清掉该目录里的空间偏好和长期规则镜像，避免上一轮确认过的规则
继续把「开发说明」分进项目。其他文件和操作历史保留。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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


def _read_ledger(root: Path) -> dict:
    path = _ledger_path(root)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def _write_ledger(root: Path, files: dict) -> None:
    path = _ledger_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise Skip("清单路径是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise Skip("清单路径是符号链接")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


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


def place_owned_files(root: Path) -> list[str]:
    notes = []
    ledger = _read_ledger(root)
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
        _write_ledger(root, ledger)
    except Skip as exc:
        notes.append(exc.reason)
    return notes


def clear_space_preferences(root: Path) -> str:
    """去掉本空间偏好和长期规则镜像。不删操作历史，不跟随符号链接。"""
    org = root / ".organizer"
    if org.is_symlink():
        return "跳过：.organizer 是符号链接，未改偏好"
    if not org.exists():
        return "本空间没有已保存的偏好"
    db = org / "preferences.db"
    if db.is_symlink():
        return "跳过：preferences.db 是符号链接，未删除"
    removed = False
    if db.is_file():
        db.unlink()
        removed = True
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.is_file() and not side.is_symlink():
            side.unlink()
    cfg = org / "classification_config.json"
    if cfg.is_symlink():
        return "跳过：分类配置是符号链接，未改写"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "分类配置无法读取，未改写"
        if isinstance(data, dict) and data.get("long_term_rules"):
            data["long_term_rules"] = []
            tmp = cfg.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            os.replace(tmp, cfg)
            removed = True
    if removed:
        return "已清除本空间偏好，下次扫描从首次预览开始"
    return "本空间没有已保存的偏好"


def reset(root: Path) -> Path:
    root = _reject_root(root)
    place_owned_files(root)
    clear_space_preferences(root)
    outside = root.parent / OUTSIDE_NAME
    if not outside.exists():
        outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    return outside


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
    notes = place_owned_files(root)
    preference_note = clear_space_preferences(root)
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
