#!/usr/bin/env python3
"""准备阶段三演示用的 3 个文件，并让同一目录可以重新从首次预览开始。

只创建还没有的演示文件。再次运行时，只写回本脚本创建过的普通文件。
已经存在的用户文件、符号链接，以及链接指向的目录外文件，都不改写。
演示开始前已经存在的空间偏好保留，之后在这个空间里新确认的偏好也保留。
只有一开始没有任何偏好的演示目录，才会在下一次重置时去掉演示过程中
新确认的偏好，方便同一演示目录重新从首次预览开始。操作历史保留。
范围外对照若已是符号链接，包括指向尚不存在文件的链接，一律不跟随。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import stat
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


class LinkedInode(Exception):
    """打开之后才发现这个 inode 多出了链接，不截断、不写入。"""


class PathReplaced(Exception):
    """目标路径上的文件已不是清单里的 inode，保留现有文件。"""


_RENAME_SWAP = 0x00000002  # macOS renamex_np 的 RENAME_SWAP 标志


def _rename_swap(from_path: Path, to_path: Path) -> None:
    """在同一卷内原子交换两个路径的目录项。

    成功后 from_path 与 to_path 指向的 inode 互换，失败抛 OSError。
    用 renamex_np 而不是 os.rename：标准库不暴露交换语义，普通 rename
    会覆盖目标路径，正好丢掉对「路径已被换掉」的判断窗口。
    """
    libc = ctypes.CDLL(None, use_errno=True)
    func = getattr(libc, "renamex_np", None)
    if func is None:
        raise OSError("当前平台不支持 renamex_np")
    func.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    rc = func(os.fsencode(str(from_path)), os.fsencode(str(to_path)),
              _RENAME_SWAP)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


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


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _identity_matches(recorded: object, current: dict) -> bool:
    if not isinstance(recorded, dict):
        return False
    return (recorded.get("dev") == current["dev"]
            and recorded.get("ino") == current["ino"])


def _same_volume(root: Path, recorded: dict) -> bool:
    try:
        dev = root.lstat().st_dev
    except OSError:
        return False
    return recorded.get("dev") == dev


def _find_moved(root: Path, recorded: dict) -> list[Path]:
    """在整理空间内按清单记录的设备号和 inode 找回被移走的演示文件。

    不按文件名搜索，也不跟随符号链接；.organizer 是程序自己的库文件，跳过。
    记录里带内容指纹时，指纹不一致的不算匹配（防 inode 复用后认错文件）。
    """
    found: list[Path] = []
    dev = recorded.get("dev")
    ino = recorded.get("ino")
    if not isinstance(dev, int) or not isinstance(ino, int):
        return found
    expected = recorded.get("sha256")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [name for name in sorted(dirnames)
                       if name != ".organizer"
                       and not (here / name).is_symlink()]
        for name in sorted(filenames):
            path = here / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_dev != dev or info.st_ino != ino:
                continue
            if isinstance(expected, str) and _sha256_file(path) != expected:
                continue
            found.append(path)
    return found


def _move_back(root: Path, src: Path, dest: Path,
               recorded: dict) -> tuple[str, str]:
    """把同一 inode 的文件移回原路径，不覆盖、不跟随链接。

    用「硬链接 + 删旧名」代替覆盖语义的重命名：目标已存在时直接失败，
    文件系统不支持时也不会去猜别的文件。返回 (状态, 说明)：
    moved 表示已放到原路径；stop 表示链接后目标路径被换掉，调用方必须保留
    该目录项且不得再新建；blocked 表示没有移回。
    """
    current = dest.parent
    while _inside(root, current) and current != root:
        if current.is_symlink():
            return "blocked", "原路径的上级目录是符号链接"
        current = current.parent
    if not _inside(root, dest):
        return "blocked", "原路径越出整理空间"
    if dest.is_symlink():
        return "blocked", "原路径是符号链接"
    if dest.exists():
        return "blocked", "原路径已被占用"
    try:
        src_info = src.lstat()
    except OSError:
        return "blocked", "旧位置已无法读取"
    if (src_info.st_dev != recorded.get("dev")
            or src_info.st_ino != recorded.get("ino")):
        return "blocked", "旧位置的文件身份已变化"
    if src_info.st_nlink > 1:
        return "blocked", "旧位置是多链接 inode"
    try:
        os.link(src, dest)
    except FileExistsError:
        return "blocked", "原路径已被占用"
    except OSError as exc:
        return "blocked", f"无法移回：{exc.strerror or exc}"
    try:
        info = dest.lstat()
    except OSError:
        return "blocked", "移回后无法核对文件身份"
    if info.st_dev != recorded.get("dev") or info.st_ino != recorded.get("ino"):
        # 身份对不上说明目标路径已被换成另一份文件。保留它，不 unlink。
        return "stop", "移回后身份不一致，未删除"
    dropped, reason = _drop_old_name(src, recorded)
    if not dropped:
        return "moved", reason
    return "moved", ""


def _drop_old_name(src: Path, recorded: dict) -> tuple[bool, str]:
    """只把清单里的那个旧名字从可见路径上移开，不按路径删除。

    先用 O_RDONLY|O_NOFOLLOW 打开旧路径并按描述符核对 inode。再在同一目录
    建一个空的占位文件（新 inode），用 RENAME_SWAP 把旧路径上的 inode 换到
    点文件名上（扫描跳过点开头）。交换是原子的：换完核对点文件名上必须还是
    清单里的 inode，对不上就换回去，用户文件留在原路径。核对通过后把占位
    inode 改名成另一个点文件名，可见旧路径随之消失。全程不 unlink 点文件：
    .演示旧名-* 留着清单 inode 的那一个名字，.演示占位-* 留着占位 inode。
    返回 (旧目录项是否已处理, 说明)。
    """
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True, ""
    except OSError:
        return False, "旧路径无法打开，未删除"
    try:
        info = os.fstat(fd)
        if (info.st_dev != recorded.get("dev")
                or info.st_ino != recorded.get("ino")):
            return False, "旧路径已换成另一份文件，未删除"
        temp = None
        for index in range(1000):
            candidate = src.parent / f".演示旧名-{os.getpid()}-{index}"
            try:
                candidate.lstat()
            except FileNotFoundError:
                temp = candidate
                break
            except OSError:
                continue
        if temp is None:
            return False, "旧路径未能删除，两份都在"
        try:
            pfd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | os.O_NOFOLLOW, 0o666)
        except OSError:
            return False, "旧路径未能删除，两份都在"
        try:
            pinfo = os.fstat(pfd)
        finally:
            os.close(pfd)
        try:
            _rename_swap(src, temp)
        except OSError:
            return False, "旧路径未能删除，两份都在"
        try:
            swapped = temp.lstat()
        except OSError:
            swapped = None
        if (swapped is None
                or swapped.st_dev != recorded.get("dev")
                or swapped.st_ino != recorded.get("ino")):
            # 换下来的不是清单里的 inode：换回去，占位 inode 留在点文件名上
            try:
                _rename_swap(src, temp)
            except OSError:
                return False, "旧路径未能删除，两份都在"
            return False, "旧路径已换成另一份文件，未删除"
        # 可见旧路径上现在是占位 inode。改名到另一个点文件名，可见旧路径消失；
        # 名字被换成别的文件就改回来，不删除任何东西。
        spare = None
        for index in range(1000):
            candidate = src.parent / f".演示占位-{os.getpid()}-{index}"
            try:
                candidate.lstat()
            except FileNotFoundError:
                spare = candidate
                break
            except OSError:
                continue
        if spare is None:
            return False, "旧路径未能删除，两份都在"
        try:
            os.rename(src, spare)
        except OSError:
            return False, "旧路径未能删除，两份都在"
        try:
            moved = spare.lstat()
        except OSError:
            moved = None
        if (moved is None or moved.st_dev != pinfo.st_dev
                or moved.st_ino != pinfo.st_ino):
            try:
                os.rename(spare, src)
            except OSError:
                pass
            return False, "旧路径已换成另一份文件，未删除"
        return True, ""
    finally:
        os.close(fd)


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


def _write_temp_text(parent: Path, text: str) -> Path:
    """在同一目录写一个新的普通文件，返回临时名。

    写入失败时不按路径删除这个点文件：扫描会跳过点开头，留下比误删更安全。
    """
    data = text.encode("utf-8")
    for index in range(1000):
        name = parent / f".演示临时-{os.getpid()}-{index}"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o666)
        except FileExistsError:
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return name
    raise OSError("无法创建临时文件")


def _has_dot_old_link(root: Path, dev: int, ino: int) -> bool:
    """整理空间里是否有本次清理留下的 .演示旧名-* 指向这个 inode。

    扫描会跳过点开头的名字，所以这种多出来的名字不算被别的程序共享。
    """
    for path in _find_moved(root, {"dev": dev, "ino": ino}):
        if path.name.startswith(".演示旧名-"):
            return True
    return False


def _nlink_allowed(root: Path, dev: int, ino: int, nlink: int) -> bool:
    """这个 inode 的链接数是否可以安全写回。

    只有一条名字（写回后旧 inode 只剩临时点文件），或者多出来的那一条
    正是本次清理留下的 .演示旧名-*（nlink 正好为 2），才允许保留路径上的
    新 inode。其它情况一律换回去。
    """
    if nlink == 1:
        return True
    return nlink == 2 and _has_dot_old_link(root, dev, ino)


def _refresh_regular(path: Path, text: str, expected: dict,
                     root: Path) -> None:
    """用新 inode 刷新目标路径，不改写目标原来指向的 inode。

    打开并 fstat 目标，确认还是清单里的文件。内容写到同目录的新文件，再用
    RENAME_SWAP 与目标路径原子交换。交换后核对被换下来的 inode：它只剩点
    文件名，或另一个名字就是本次清理留下的 .演示旧名-* 且 nlink 正好为 2，
    才算没有被别的程序共享；否则立刻换回去。全程不 unlink 点文件：留下的
    .演示临时-*、.演示旧名-* 扫描都会跳过。
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_dev != expected.get("dev")
                or info.st_ino != expected.get("ino")):
            raise PathReplaced(path.name)
        if not _nlink_allowed(root, info.st_dev, info.st_ino, info.st_nlink):
            raise LinkedInode(path.name)
    finally:
        os.close(fd)
    temp = _write_temp_text(path.parent, text)
    try:
        now = path.lstat()
    except OSError:
        raise PathReplaced(path.name)
    if (now.st_dev != expected.get("dev")
            or now.st_ino != expected.get("ino")):
        raise PathReplaced(path.name)
    if not _nlink_allowed(root, now.st_dev, now.st_ino, now.st_nlink):
        raise LinkedInode(path.name)
    _rename_swap(temp, path)
    # 交换后：path 是我们的新 inode，temp 是原路径换下来的 inode
    try:
        swapped = temp.lstat()
    except OSError:
        swapped = None
    if (swapped is None
            or swapped.st_dev != expected.get("dev")
            or swapped.st_ino != expected.get("ino")):
        try:
            _rename_swap(path, temp)
        except OSError:
            raise OSError("安装核对失败且无法换回")
        raise PathReplaced(path.name)
    if not _nlink_allowed(root, swapped.st_dev, swapped.st_ino,
                          swapped.st_nlink):
        try:
            _rename_swap(path, temp)
        except OSError:
            raise OSError("安装核对失败且无法换回")
        raise LinkedInode(path.name)
    # 成功：旧 inode 留在 .演示临时-* 上，不删除；扫描会跳过点开头


def _create_regular(path: Path, text: str) -> None:
    """新建一份演示文件：独占创建，不覆盖、不跟随链接，也不留点文件。"""
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | os.O_NOFOLLOW, 0o666)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _remember(ledger: dict, rel: str, path: Path) -> None:
    ledger[rel] = _identity(path)
    digest = _sha256_file(path)
    if digest:
        ledger[rel]["sha256"] = digest


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
            if not _identity_matches(recorded, current):
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            if path.lstat().st_nlink > 1:
                notes.append(f"硬链接未覆盖：{rel}")
                continue
            try:
                _refresh_regular(path, text, recorded, root)
            except LinkedInode:
                notes.append(f"硬链接未覆盖：{rel}")
                continue
            except PathReplaced:
                notes.append(f"路径已被换成另一份文件，未覆盖：{rel}")
                continue
            except OSError:
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            _remember(ledger, rel, path)
            notes.append(f"已写回脚本创建的文件：{rel}")
        else:
            recorded = ledger.get(rel)
            blocked = ""
            if isinstance(recorded, dict):
                if _same_volume(root, recorded):
                    found = _find_moved(root, recorded)
                    unique = []
                    shared = 0
                    for candidate in found:
                        try:
                            info = candidate.lstat()
                        except OSError:
                            continue
                        if info.st_nlink > 1:
                            shared += 1
                        else:
                            unique.append(candidate)
                    if len(unique) == 1 and not shared:
                        status, reason = _move_back(root, unique[0], path, recorded)
                        if status == "moved":
                            try:
                                _refresh_regular(path, text, recorded, root)
                            except LinkedInode:
                                notes.append(f"硬链接未覆盖：{rel}")
                                continue
                            except PathReplaced:
                                notes.append(f"路径已被换成另一份文件，未覆盖：{rel}")
                                continue
                            except OSError:
                                notes.append(f"已按 inode 移回但未写回内容：{rel}")
                                continue
                            _remember(ledger, rel, path)
                            suffix = f"（{reason}）" if reason else ""
                            notes.append(f"已按 inode 移回演示文件{suffix}：{rel}")
                            continue
                        if status == "stop":
                            notes.append(f"{reason}：{rel}")
                            continue
                        blocked = f"{reason}：{rel}"
                    elif shared:
                        blocked = f"同 inode 的文件带硬链接，未移动，已新建：{rel}"
                    elif len(unique) > 1:
                        blocked = f"找到多个同 inode 的文件，未移动：{rel}"
                    else:
                        blocked = f"未在本卷找到已移动的演示文件，已新建：{rel}"
                else:
                    blocked = f"记录的设备号与当前卷不同，未按 inode 移回：{rel}"
            try:
                _create_regular(path, text)
            except OSError:
                notes.append(f"已有文件未覆盖：{rel}")
                continue
            if path.is_symlink() or not path.is_file():
                notes.append(f"已有路径未覆盖：{rel}")
                continue
            _remember(ledger, rel, path)
            notes.append(f"已新建演示文件：{rel}")
            if blocked:
                notes.append(blocked)
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
    """空演示目录才清掉后来确认的偏好。已有偏好的整理空间一律不删。"""
    if preserved:
        return "已保留该整理空间后来确认的偏好"
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


def _place_outside(outside: Path) -> str:
    """只在对照路径完全不存在时新建普通文件。悬空符号链接也不跟随。"""
    if outside.is_symlink():
        return "范围外对照是符号链接，未跟随"
    if outside.exists():
        return "范围外对照已存在，未覆盖"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(outside, flags, 0o644)
    except FileExistsError:
        return "范围外对照已存在，未覆盖"
    except OSError:
        return "范围外对照是符号链接，未跟随"
    try:
        os.write(fd, OUTSIDE_TEXT.encode("utf-8"))
    finally:
        os.close(fd)
    return "已新建范围外对照"


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
    reset.last_outside_note = _place_outside(outside)
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
    outside_note = _place_outside(outside)
    print(f"整理空间：{root}")
    print(f"范围外对照：{outside}")
    print(outside_note)
    for line in notes:
        print(line)
    print(preference_note)
    extras = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        if path.name.startswith("."):
            # 交换收口留下的点文件，扫描会跳过，不当作要提醒的用户文件
            continue
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
