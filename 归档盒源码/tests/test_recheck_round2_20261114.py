"""归档盒 — 阶段 0 复核第二轮回归（短写/临时文件越界/二次身份核对/权限与 mtime）

对应审计第六节新增的四类反例。全部使用临时目录内的合成文件。
要求在 e8f22e2 上失败，在修复后通过。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_recheck_round2_20261114.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ENV = tempfile.mkdtemp(prefix="ab_recheck2_env_")
os.environ.setdefault("ARCHIVEBOX_SCHEME_DIR", os.path.join(_ENV, "schemes"))
os.environ.setdefault("ARCHIVEBOX_APP_DIR", os.path.join(_ENV, "app"))
os.environ.setdefault("ARCHIVEBOX_RECENT_FILE", os.path.join(_ENV, "recent.json"))

import executor  # noqa: E402
from planner import OrganizePlan, PlanItem  # noqa: E402
from scanner import FileEntry  # noqa: E402
from storage import Storage  # noqa: E402

APPROVED = b"approved content"
OTHER = b"external file content that must survive"

PASS = 0
FAIL = 0


def check(name, cond, evidence=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {evidence}")


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def make_plan(root, src_rel, target_rel, content=APPROVED):
    src_abs = os.path.join(root, src_rel)
    parent = os.path.dirname(src_abs)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(src_abs, "wb") as f:
        f.write(content)
    fe = FileEntry(
        abs_path=src_abs, rel_path=src_rel,
        filename=os.path.basename(src_rel), file_type="文档",
        size=len(content), size_display=f"{len(content)} B",
    )
    item = PlanItem(
        file=fe, course="", file_type="文档", rule_applied="测试",
        target_dir_rel=os.path.dirname(target_rel),
        target_filename=os.path.basename(target_rel),
        target_rel_path=target_rel,
        source_sha256="", selected=True,
    )
    return OrganizePlan(root=root, mode="by_type", rules=[], items=[item])


def action_status(store, batch_id):
    return store.get_actions(batch_id)[0].status


def tmp_leftovers(directory):
    try:
        return sorted(
            n for n in os.listdir(directory)
            if ".abtmp" in n or ".archivetmp" in n)
    except OSError:
        return []


@contextmanager
def force_copy_path():
    """把同卷重命名强制标成不可用，专门回归跨卷复制路径的安全约束。"""
    marker = object()
    original = getattr(executor, "_rename_no_replace", marker)
    executor._rename_no_replace = lambda src, dst: ("unsupported", "test")
    try:
        yield
    finally:
        if original is marker:
            delattr(executor, "_rename_no_replace")
        else:
            executor._rename_no_replace = original


# ======================= 1. 短写与指纹失败 =======================

def test_short_write():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        store = Storage(root)
        real_write = os.write

        def short_write(fd, data):
            # 只写 3 字节并正常返回，模拟合法但不足的写
            return real_write(fd, data[:3])

        try:
            executor.os.write = short_write
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.write = real_write
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R1-短写后源文件字节完整", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R1-短写后目标不存在", not os.path.exists(dst),
              f"dst exists={os.path.exists(dst)}")
        check("R1-历史不是 moved", status != "moved", f"status={status}")
        check("R1-无临时文件残留",
              tmp_leftovers(os.path.dirname(dst)) == [],
              f"leftovers={tmp_leftovers(os.path.dirname(dst))}")


def test_tmp_checksum_unreadable():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        store = Storage(root)
        real_sha = executor.sha256_file

        def sha_none_for_tmp(path):
            if os.path.basename(str(path)) == ".abtmp":
                return None
            return real_sha(path)

        try:
            executor.sha256_file = sha_none_for_tmp
            res = executor.execute_plan(plan, store)
        finally:
            executor.sha256_file = real_sha
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R2a-指纹读不出时源文件字节完整", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R2a-指纹读不出时目标不存在", not os.path.exists(dst))
        check("R2a-历史不是 moved", status != "moved", f"status={status}")
        check("R2a-无临时文件残留",
              tmp_leftovers(os.path.dirname(dst)) == [])


def test_source_changed_after_copy():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        store = Storage(root)

        real_write = os.write
        real_copy2 = shutil.copy2
        progress = {"written": 0, "edited": False}

        def edit_source_once():
            if progress["edited"]:
                return
            progress["edited"] = True
            with open(src, "wb") as f:
                f.write(OTHER)

        def hooked_write(fd, data):
            n = real_write(fd, data)
            progress["written"] += n
            # 已复制完全部源字节后，立刻把源改成新内容
            if progress["written"] >= len(APPROVED):
                edit_source_once()
            return n

        def hooked_copy2(a, b, *args, **kwargs):
            r = real_copy2(a, b, *args, **kwargs)
            if os.path.basename(str(b)) == ".abtmp":
                edit_source_once()
            return r

        try:
            executor.os.write = hooked_write
            executor.shutil.copy2 = hooked_copy2
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.write = real_write
            executor.shutil.copy2 = real_copy2
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R2b-源保留复制后的新内容", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R2b-目标未留下旧副本", not os.path.exists(dst),
              f"dst={read_bytes(dst)!r}")
        check("R2b-历史不是 moved", status != "moved", f"status={status}")
        check("R2b-无临时文件残留",
              tmp_leftovers(os.path.dirname(dst)) == [])


# ======================= 2. 临时文件不得越界 =======================

def test_dangling_tmp_symlink():
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "")
        root = os.path.join(d, "root")
        os.makedirs(root)
        outside = os.path.join(d, "outside")
        os.makedirs(outside)
        leaked = os.path.join(outside, "leaked")

        src = os.path.join(root, "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        dst_dir = os.path.join(root, "_已整理")
        os.makedirs(dst_dir, exist_ok=True)
        planted = os.path.join(dst_dir, ".abtmp")
        os.symlink(leaked, planted)

        store = Storage(root)
        res = executor.execute_plan(plan, store)
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R3-空间外未出现源内容", not os.path.exists(leaked),
              f"leaked exists={os.path.exists(leaked)}")
        check("R3-源文件字节完整", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R3-历史不是 moved", status != "moved", f"status={status}")
        check("R3-预置链接未被删除（不是本次创建的 inode）",
              os.path.islink(planted))
        _ = base


def test_dir_swapped_before_tmp_create():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        outside = os.path.join(d, "outside")
        os.makedirs(outside)
        outside_tmp = os.path.join(outside, ".abtmp")
        with open(outside_tmp, "wb") as f:
            f.write(OTHER)

        src = os.path.join(root, "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        dst_dir = os.path.join(root, "_已整理")
        os.makedirs(dst_dir, exist_ok=True)
        saved = dst_dir + "-saved"

        real_open = os.open
        real_copy2 = shutil.copy2
        state = {"done": False}

        def swap_dir():
            if state["done"]:
                return
            state["done"] = True
            os.rename(dst_dir, saved)
            os.symlink(outside, dst_dir, target_is_directory=True)

        def hooked_open(path, flags, *args, **kwargs):
            if (flags & os.O_CREAT) and (flags & os.O_EXCL) and \
                    os.path.basename(str(path)) == ".abtmp":
                swap_dir()
            return real_open(path, flags, *args, **kwargs)

        def hooked_copy2(a, b, *args, **kwargs):
            if os.path.basename(str(b)) == ".abtmp":
                swap_dir()
            return real_copy2(a, b, *args, **kwargs)

        store = Storage(root)
        try:
            executor.os.open = hooked_open
            executor.shutil.copy2 = hooked_copy2
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.open = real_open
            executor.shutil.copy2 = real_copy2
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R4-注入生效", state["done"])
        check("R4-空间外原有 .abtmp 字节未被覆盖",
              read_bytes(outside_tmp) == OTHER,
              f"outside={read_bytes(outside_tmp)!r}")
        check("R4-源文件字节完整", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R4-历史不是 moved", status != "moved", f"status={status}")


# ======================= 3. 身份核对之后再核对 =======================

def test_target_swapped_after_sha_execute():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        store = Storage(root)
        real_sha = executor.sha256_file
        state = {"swapped": False}

        def sha_then_replace(path):
            digest = real_sha(path)
            if (os.path.abspath(str(path)) == os.path.abspath(src)
                    and os.path.isfile(dst) and not state["swapped"]):
                state["swapped"] = True
                os.unlink(dst)
                with open(dst, "wb") as f:
                    f.write(OTHER)
            return digest

        try:
            executor.sha256_file = sha_then_replace
            res = executor.execute_plan(plan, store)
        finally:
            executor.sha256_file = real_sha
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R5-注入生效（整理）", state["swapped"])
        check("R5-原内容仍有完整副本（源）", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R5-被替换的目标未被覆盖", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R5-历史不是 moved", status != "moved", f"status={status}")


def test_target_swapped_after_sha_undo():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        store = Storage(root)
        moved = executor.execute_plan(plan, store)
        check("R6-前置整理成功", moved.moved == 1, f"moved={moved.moved}")

        real_sha = executor.sha256_file
        state = {"swapped": False}

        def sha_then_replace(path):
            digest = real_sha(path)
            if (os.path.abspath(str(path)) == os.path.abspath(dst)
                    and os.path.isfile(src) and not state["swapped"]):
                state["swapped"] = True
                os.unlink(src)
                with open(src, "wb") as f:
                    f.write(OTHER)
            return digest

        try:
            executor.sha256_file = sha_then_replace
            undo = executor.undo_latest_batch(store)
        finally:
            executor.sha256_file = real_sha
        status = action_status(store, moved.batch_id)
        store.close()

        check("R6-注入生效（撤销）", state["swapped"])
        check("R6-原内容仍有完整副本（整理目录）", read_bytes(dst) == APPROVED,
              f"dst={read_bytes(dst)!r}")
        check("R6-被替换的原路径未被覆盖", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R6-历史不是 undone", status != "undone", f"status={status}")
        check("R6-未报已恢复", undo.restored == 0, f"restored={undo.restored}")


# ======================= 4. 权限与 mtime =======================

def _mode_mtime_case(mode):
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "test.sh")
        dst = os.path.join(root, "_已整理", "test.sh")
        plan = make_plan(root, "test.sh", "_已整理/test.sh", b"MODE-CONTENT")
        os.chmod(src, mode)
        stamp = 1_670_000_000
        os.utime(src, (stamp, stamp))
        store = Storage(root)
        res = executor.execute_plan(plan, store)
        status_moved = action_status(store, res.batch_id) if res.batch_id else ""
        try:
            dst_mode = stat.S_IMODE(os.stat(dst).st_mode)
            dst_mtime = os.stat(dst).st_mtime
        except OSError:
            dst_mode = None
            dst_mtime = None
        undo = executor.undo_latest_batch(store)
        status_undone = action_status(store, res.batch_id) if res.batch_id else ""
        try:
            src_mode = stat.S_IMODE(os.stat(src).st_mode)
            src_mtime = os.stat(src).st_mtime
        except OSError:
            src_mode = None
            src_mtime = None
        store.close()

        label = oct(mode)
        check(f"R7-{label} 整理成功", res.moved == 1 and status_moved == "moved",
              f"moved={res.moved} status={status_moved}")
        check(f"R7-{label} 整理后权限保留", dst_mode == mode,
              f"dst_mode={dst_mode}")
        check(f"R7-{label} 整理后 mtime 保留", dst_mtime == stamp,
              f"dst_mtime={dst_mtime}")
        check(f"R7-{label} 撤销成功", undo.restored == 1 and status_undone == "undone",
              f"restored={undo.restored} status={status_undone}")
        check(f"R7-{label} 撤销后权限保留", src_mode == mode,
              f"src_mode={src_mode}")
        check(f"R7-{label} 撤销后 mtime 保留", src_mtime == stamp,
              f"src_mtime={src_mtime}")


def test_mode_and_mtime():
    _mode_mtime_case(0o600)
    _mode_mtime_case(0o751)


# ============ 5. 删源一开始就把目标换走（D5） ============

def test_target_swapped_at_unlink_execute():
    """第二次 lstat 通过后、unlink(源) 一开始就替换目标：原字节必须仍有副本。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)

        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_unlink(path):
            # 只拦截删除源文件这一次系统调用：先把目标换成别的文件，再真正删源
            if (os.path.abspath(str(path)) == os.path.abspath(src)
                    and not state["swapped"]):
                state["swapped"] = True
                if os.path.lexists(dst):
                    real_unlink(dst)
                with open(dst, "wb") as f:
                    f.write(OTHER)
            return real_unlink(path)

        try:
            executor.os.unlink = hooked_unlink
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.unlink = real_unlink
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R8-注入生效（整理）", state["swapped"])
        check("R8-原字节仍有一份完整副本", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R8-被换上的文件仍是新内容", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R8-动作不是 moved", res.moved == 0 and status != "moved",
              f"moved={res.moved} status={status}")
        check("R8-临时副本不残留",
              tmp_leftovers(os.path.dirname(dst)) == [],
              f"leftovers={tmp_leftovers(os.path.dirname(dst))}")


def test_target_swapped_at_unlink_undo():
    """撤销：unlink(整理目录原件) 一开始就替换原路径，原字节必须仍有副本。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        original = b"ORIGINAL-CONTENT"
        plan = make_plan(root, "a.txt", "_已整理/a.txt", original)
        store = Storage(root)
        moved = executor.execute_plan(plan, store)
        check("R9-前置整理成功", moved.moved == 1, f"moved={moved.moved}")
        check("R9-前置目标内容正确", read_bytes(dst) == original)

        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_unlink(path):
            # 撤销时 _copy_then_replace 先删整理目录里的原件；进入时替换原路径
            if (os.path.abspath(str(path)) == os.path.abspath(dst)
                    and not state["swapped"]):
                state["swapped"] = True
                if os.path.lexists(src):
                    real_unlink(src)
                with open(src, "wb") as f:
                    f.write(OTHER)
            return real_unlink(path)

        try:
            executor.os.unlink = hooked_unlink
            undo = executor.undo_latest_batch(store)
        finally:
            executor.os.unlink = real_unlink
        status = action_status(store, moved.batch_id)
        store.close()

        check("R9-注入生效（撤销）", state["swapped"])
        check("R9-原字节仍有一份完整副本", read_bytes(dst) == original,
              f"dst={read_bytes(dst)!r}")
        check("R9-被换上的原路径仍是新内容", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R9-历史不是 undone",
              status != "undone" and undo.restored == 0,
              f"status={status} restored={undo.restored}")


def _replace_file(path, content, real_unlink):
    if os.path.lexists(path):
        real_unlink(path)
    with open(path, "wb") as f:
        f.write(content)


# ============ 6. 清理临时副本或关闭句柄时换掉目标（D6） ============

def test_target_swapped_at_tmp_unlink_execute():
    """删除 .abtmp 时替换目标：原字节必须写回，历史不得是 moved。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)
        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_unlink(path):
            if os.path.basename(str(path)) == ".abtmp" and not state["swapped"]:
                state["swapped"] = True
                _replace_file(dst, OTHER, real_unlink)
            return real_unlink(path)

        try:
            executor.os.unlink = hooked_unlink
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.unlink = real_unlink
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R10-注入生效（整理）", state["swapped"])
        check("R10-原字节仍有一份完整副本", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R10-被换上的文件仍是新内容", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R10-动作不是 moved", res.moved == 0 and status != "moved",
              f"moved={res.moved} status={status}")


def test_target_swapped_at_tmp_unlink_undo():
    """撤销：删除 .abtmp 时替换原路径，整理目录里的原字节必须还在。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        original = b"ORIGINAL-CONTENT"
        plan = make_plan(root, "a.txt", "_已整理/a.txt", original)
        store = Storage(root)
        moved = executor.execute_plan(plan, store)
        check("R10u-前置整理成功", moved.moved == 1, f"moved={moved.moved}")
        check("R10u-前置目标内容正确", read_bytes(dst) == original)

        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_unlink(path):
            if os.path.basename(str(path)) == ".abtmp" and not state["swapped"]:
                state["swapped"] = True
                _replace_file(src, OTHER, real_unlink)
            return real_unlink(path)

        try:
            executor.os.unlink = hooked_unlink
            undo = executor.undo_latest_batch(store)
        finally:
            executor.os.unlink = real_unlink
        status = action_status(store, moved.batch_id)
        store.close()

        check("R10u-注入生效（撤销）", state["swapped"])
        check("R10u-原字节仍有一份完整副本", read_bytes(dst) == original,
              f"dst={read_bytes(dst)!r}")
        check("R10u-被换上的原路径仍是新内容", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R10u-历史不是 undone",
              status != "undone" and undo.restored == 0,
              f"status={status} restored={undo.restored}")


def test_target_swapped_at_close_execute():
    """源已删除后，关闭句柄时替换目标：原字节必须写回。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)
        real_close = os.close
        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_close(fd):
            if (not os.path.exists(src)) and not state["swapped"]:
                state["swapped"] = True
                _replace_file(dst, OTHER, real_unlink)
            return real_close(fd)

        try:
            executor.os.close = hooked_close
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.close = real_close
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R11-注入生效（整理）", state["swapped"])
        check("R11-原字节仍有一份完整副本", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R11-被换上的文件仍是新内容", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R11-动作不是 moved", res.moved == 0 and status != "moved",
              f"moved={res.moved} status={status}")


def test_target_swapped_at_close_undo():
    """撤销：整理件已删除后，关闭句柄时替换原路径。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        original = b"ORIGINAL-CONTENT"
        plan = make_plan(root, "a.txt", "_已整理/a.txt", original)
        store = Storage(root)
        moved = executor.execute_plan(plan, store)
        check("R11u-前置整理成功", moved.moved == 1, f"moved={moved.moved}")
        check("R11u-前置目标内容正确", read_bytes(dst) == original)

        real_close = os.close
        real_unlink = os.unlink
        state = {"swapped": False}

        def hooked_close(fd):
            if (not os.path.exists(dst)) and not state["swapped"]:
                state["swapped"] = True
                _replace_file(src, OTHER, real_unlink)
            return real_close(fd)

        try:
            executor.os.close = hooked_close
            undo = executor.undo_latest_batch(store)
        finally:
            executor.os.close = real_close
        status = action_status(store, moved.batch_id)
        store.close()

        check("R11u-注入生效（撤销）", state["swapped"])
        check("R11u-原字节仍有一份完整副本", read_bytes(dst) == original,
              f"dst={read_bytes(dst)!r}")
        check("R11u-被换上的原路径仍是新内容", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R11u-历史不是 undone",
              status != "undone" and undo.restored == 0,
              f"status={status} restored={undo.restored}")


# ========== 6. 同卷重命名方案验收（D7） ==========

def test_same_volume_rename_no_second_inode():
    """同卷移动必须是重命名：目标就是源 inode，不存在可被原地改坏的副本。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        src_ino = os.stat(src).st_ino
        store = Storage(root)

        real_sha = executor.sha256_file
        state = {"fired": False}
        replacement = b"X" * len(APPROVED)

        def sha_then_edit(path):
            digest = real_sha(path)
            if (os.path.abspath(str(path)) == os.path.abspath(src)
                    and os.path.isfile(dst) and not state["fired"]):
                # 等长、原地改，inode 不变（模拟 b983a03 上的目标副本被改坏）
                state["fired"] = True
                with open(dst, "r+b") as f:
                    f.write(replacement)
            return digest

        try:
            executor.sha256_file = sha_then_edit
            res = executor.execute_plan(plan, store)
        finally:
            executor.sha256_file = real_sha
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R10-同卷移动不写第二个 inode",
              os.path.isfile(dst) and os.stat(dst).st_ino == src_ino,
              f"src_ino={src_ino} dst_ino="
              f"{os.stat(dst).st_ino if os.path.exists(dst) else None}")
        check("R10-目标字节完整", read_bytes(dst) == APPROVED,
              f"dst={read_bytes(dst)!r}")
        check("R10-历史 moved", res.moved == 1 and status == "moved",
              f"moved={res.moved} status={status} injected={state['fired']}")


def test_source_replaced_at_rename():
    """改名一开始替换源目录项：被换文件必须原样留下，不得记成功。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)

        real_rename = getattr(executor, "_rename_no_replace", None)
        real_unlink = os.unlink
        state = {"fired": False}

        def swap_source_once():
            if state["fired"]:
                return
            state["fired"] = True
            real_unlink(src)
            with open(src, "wb") as f:
                f.write(OTHER)

        def hooked_rename(s, dd):
            if os.path.abspath(str(s)) == os.path.abspath(src):
                swap_source_once()
            return real_rename(s, dd)

        def hooked_unlink(path):
            # 兼容复制路径（b983a03）：unlink(源) 一开始就替换源路径
            if os.path.abspath(str(path)) == os.path.abspath(src):
                swap_source_once()
            return real_unlink(path)

        try:
            if real_rename is not None:
                executor._rename_no_replace = hooked_rename
            executor.os.unlink = hooked_unlink
            res = executor.execute_plan(plan, store)
        finally:
            if real_rename is not None:
                executor._rename_no_replace = real_rename
            executor.os.unlink = real_unlink
        status = action_status(store, res.batch_id) if res.batch_id else ""
        store.close()

        check("R11-注入生效", state["fired"])
        check("R11-被换入的文件字节仍在", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R11-原字节仍有一份完整副本", read_bytes(src) == APPROVED,
              f"src={read_bytes(src)!r}")
        check("R11-历史不是 moved", res.moved == 0 and status != "moved",
              f"moved={res.moved} status={status}")


def test_cleanup_enospc_keeps_original():
    """清理时目标被换、原路径被占用、.abkeep 创建 ENOSPC：仍要留下完整原件。"""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)

        real_unlink = os.unlink
        real_link = os.link
        state = {"fired": False}

        def hooked_link(a, b, *args, **kwargs):
            # 清理阶段建立 .abkeep 备份时：目标被换、原路径被占用，并注入 ENOSPC
            if (os.path.basename(str(b)).startswith(".abkeep")
                    and not state["fired"]):
                state["fired"] = True
                if os.path.lexists(dst):
                    real_unlink(dst)
                with open(dst, "wb") as f:
                    f.write(OTHER)
                with open(src, "wb") as f:
                    f.write(b"new source occupant")
                raise OSError(28, "injected disk full")
            return real_link(a, b, *args, **kwargs)

        try:
            with force_copy_path():
                executor.os.link = hooked_link
                try:
                    res = executor.execute_plan(plan, store)
                finally:
                    executor.os.link = real_link
        finally:
            executor.os.link = real_link
        action = store.get_actions(res.batch_id)[0]
        tmp = os.path.join(root, "_已整理", ".abtmp")
        tmp_bytes = read_bytes(tmp)
        store.close()

        check("R12-注入生效", state["fired"])
        check("R12-历史为失败", action.status == "failed",
              f"status={action.status}")
        check("R12-错误里写明临时副本路径", ".abtmp" in action.error,
              f"error={action.error}")
        check("R12-按错误里的路径能读到完整原件", tmp_bytes == APPROVED,
              f"tmp={tmp_bytes!r}")


def _memory_probe() -> tuple[bool, str]:
    code = r"""
import os, resource, sys, tempfile
sys.path.insert(0, %r)
import executor
from planner import generate_plan, MODE_BY_TYPE
from scanner import scan_directory
from storage import Storage
d = tempfile.mkdtemp()
root = os.path.join(d, "root")
os.makedirs(root)
p = os.path.join(root, "large.bin")
with open(p, "wb") as f:
    f.truncate(128 * 1024 * 1024)
plan = generate_plan(scan_directory(root, detect_project_roots=False), MODE_BY_TYPE, [])
db = Storage(root)
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
res = executor.execute_plan(plan, db)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(res.moved, round((after - before) / 1024 / 1024, 1))
""" % SRC
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env, timeout=600)
    if proc.returncode != 0:
        return False, f"rc={proc.returncode} {proc.stderr[-300:]}"
    parts = proc.stdout.strip().split()
    if len(parts) != 2:
        return False, f"stdout={proc.stdout!r}"
    moved, delta = int(parts[0]), float(parts[1])
    return moved == 1 and delta <= 80, f"moved={moved} increase={delta} MiB"


def test_memory_increment():
    ok_flag, detail = _memory_probe()
    check("R13-128MiB 正常移动峰值内存增量 <= 80MiB", ok_flag, detail)


# ===== 7. 6.0 口径收尾：换源保留磁盘原件 / temp_rel 不清空 =====

def _reopen_action(root, batch_id):
    db = Storage(root)
    try:
        return db.get_actions(batch_id)[0]
    finally:
        db.close()


def _swap_read_entry_after_open(read_path, other):
    """在源/整理件 fd 打开后立刻替换其目录项（rename 入口之前）。"""
    real_open = os.open
    state = {"done": False}

    def hooked_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if (not state["done"] and not (flags & os.O_CREAT)
                and os.path.abspath(str(path)) == os.path.abspath(read_path)):
            state["done"] = True
            real_unlink = os.unlink
            real_unlink(read_path)
            with open(read_path, "wb") as f:
                f.write(other)
        return fd

    return hooked_open, state


def test_swap_before_rename_execute():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)
        hooked, state = _swap_read_entry_after_open(src, OTHER)
        real_open = os.open
        try:
            executor.os.open = hooked
            res = executor.execute_plan(plan, store)
        finally:
            executor.os.open = real_open
        batch_id = res.batch_id
        store.close()
        action = _reopen_action(root, batch_id)
        saved = (os.path.join(root, action.temp_rel)
                 if action.temp_rel else "")

        check("R14-注入生效（整理）", state["done"])
        check("R14-未调用重命名、目标不存在", not os.path.exists(dst))
        check("R14-换入文件原样留在源目录项", read_bytes(src) == OTHER,
              f"src={read_bytes(src)!r}")
        check("R14-历史为 failed", action.status == "failed",
              f"status={action.status}")
        check("R14-重开后 temp_rel 指向的字节等于原字节",
              bool(saved) and read_bytes(saved) == APPROVED,
              f"temp_rel={action.temp_rel!r} bytes={read_bytes(saved)!r}")
        check("R14-错误里写明另存路径",
              bool(saved) and saved in action.error, f"error={action.error}")


def test_swap_before_rename_undo():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)
        moved = executor.execute_plan(plan, store)
        check("R15-前置整理成功", moved.moved == 1, f"moved={moved.moved}")

        hooked, state = _swap_read_entry_after_open(dst, OTHER)
        real_open = os.open
        try:
            executor.os.open = hooked
            executor.undo_latest_batch(store)
        finally:
            executor.os.open = real_open
        store.close()
        action = _reopen_action(root, moved.batch_id)
        saved = (os.path.join(root, action.temp_rel)
                 if action.temp_rel else "")

        check("R15-注入生效（撤销）", state["done"])
        check("R15-换入文件原样留在整理目录", read_bytes(dst) == OTHER,
              f"dst={read_bytes(dst)!r}")
        check("R15-原路径未被重建", not os.path.exists(src))
        check("R15-历史为 conflict", action.status == "conflict",
              f"status={action.status}")
        check("R15-重开后 temp_rel 指向的字节等于原字节",
              bool(saved) and read_bytes(saved) == APPROVED,
              f"temp_rel={action.temp_rel!r} bytes={read_bytes(saved)!r}")


def _copy_failure_case(mode):
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "root")
        os.makedirs(root)
        src = os.path.join(root, "a.txt")
        dst = os.path.join(root, "_已整理", "a.txt")
        plan = make_plan(root, "a.txt", "_已整理/a.txt", APPROVED)
        store = Storage(root)

        import errno as _errno
        real_open = os.open
        real_write = os.write
        real_unlink = os.unlink
        state = {"done": False, "target_fd": None}

        def hooked_open(path, flags, *args, **kwargs):
            if mode == "target-enospc" and (flags & os.O_EXCL) and \
                    os.path.abspath(str(path)) == os.path.abspath(dst):
                state["done"] = True
                raise OSError(_errno.ENOSPC, "injected create ENOSPC")
            fd = real_open(path, flags, *args, **kwargs)
            if mode == "target-write-enospc" and (flags & os.O_EXCL) and \
                    os.path.abspath(str(path)) == os.path.abspath(dst):
                state["target_fd"] = fd
            return fd

        def hooked_write(fd, data):
            if mode == "target-write-enospc" and fd == state["target_fd"]:
                state["done"] = True
                raise OSError(_errno.ENOSPC, "injected write ENOSPC")
            return real_write(fd, data)

        def hooked_unlink(path):
            if mode == "source-unlink-denied" and \
                    os.path.abspath(str(path)) == os.path.abspath(src):
                state["done"] = True
                raise OSError(_errno.EACCES, "injected unlink EACCES")
            return real_unlink(path)

        try:
            with force_copy_path():
                executor.os.open = hooked_open
                executor.os.write = hooked_write
                executor.os.unlink = hooked_unlink
                try:
                    res = executor.execute_plan(plan, store)
                finally:
                    executor.os.open = real_open
                    executor.os.write = real_write
                    executor.os.unlink = real_unlink
        finally:
            executor.os.open = real_open
            executor.os.write = real_write
            executor.os.unlink = real_unlink
        batch_id = res.batch_id
        store.close()
        action = _reopen_action(root, batch_id)
        saved = (os.path.join(root, action.temp_rel)
                 if action.temp_rel else "")
        # 必须在临时目录销毁前读字节
        return (state, action, saved,
                read_bytes(src), read_bytes(saved),
                os.path.exists(dst))


def test_copy_target_enospc_keeps_temp():
    state, action, saved, src_bytes, saved_bytes, dst_exists = \
        _copy_failure_case("target-enospc")
    check("R16-注入生效（建目标 ENOSPC）", state["done"])
    check("R16-历史为 failed", action.status == "failed", f"status={action.status}")
    check("R16-temp_rel 未清空且字节完整",
          bool(saved) and saved_bytes == APPROVED,
          f"temp_rel={action.temp_rel!r} bytes={saved_bytes!r}")
    check("R16-源文件保持原字节", src_bytes == APPROVED, f"src={src_bytes!r}")
    check("R16-目标未创建", not dst_exists)
    check("R16-错误里写明临时副本路径", bool(saved) and saved in action.error,
          f"error={action.error}")


def test_copy_target_write_enospc_keeps_temp():
    state, action, saved, src_bytes, saved_bytes, dst_exists = \
        _copy_failure_case("target-write-enospc")
    check("R17-注入生效（写目标 ENOSPC）", state["done"])
    check("R17-历史为 failed", action.status == "failed", f"status={action.status}")
    check("R17-temp_rel 未清空且字节完整",
          bool(saved) and saved_bytes == APPROVED,
          f"temp_rel={action.temp_rel!r} bytes={saved_bytes!r}")
    check("R17-源文件保持原字节", src_bytes == APPROVED, f"src={src_bytes!r}")
    check("R17-只回滚本次创建的目标", not dst_exists)
    check("R17-错误里写明临时副本路径", bool(saved) and saved in action.error,
          f"error={action.error}")


def test_copy_unlink_denied_keeps_temp():
    state, action, saved, src_bytes, saved_bytes, dst_exists = \
        _copy_failure_case("source-unlink-denied")
    check("R18-注入生效（删源 EACCES）", state["done"])
    check("R18-历史为 failed", action.status == "failed", f"status={action.status}")
    check("R18-temp_rel 未清空且字节完整",
          bool(saved) and saved_bytes == APPROVED,
          f"temp_rel={action.temp_rel!r} bytes={saved_bytes!r}")
    check("R18-源文件保持原字节", src_bytes == APPROVED, f"src={src_bytes!r}")
    check("R18-只回滚本次创建的目标", not dst_exists)
    check("R18-错误里写明临时副本路径", bool(saved) and saved in action.error,
          f"error={action.error}")


if __name__ == "__main__":
    # 前 11 组回归针对跨卷复制路径：强制 rename 不可用，保证逻辑仍被覆盖
    with force_copy_path():
        test_short_write()
        test_tmp_checksum_unreadable()
        test_source_changed_after_copy()
        test_dangling_tmp_symlink()
        test_dir_swapped_before_tmp_create()
        test_target_swapped_after_sha_execute()
        test_target_swapped_after_sha_undo()
        test_mode_and_mtime()
        test_target_swapped_at_unlink_execute()
        test_target_swapped_at_unlink_undo()
        test_target_swapped_at_tmp_unlink_execute()
        test_target_swapped_at_tmp_unlink_undo()
        test_target_swapped_at_close_execute()
        test_target_swapped_at_close_undo()
    # 同卷重命名方案验收
    test_same_volume_rename_no_second_inode()
    test_source_replaced_at_rename()
    test_cleanup_enospc_keeps_original()
    test_memory_increment()
    # 6.0 口径收尾
    test_swap_before_rename_execute()
    test_swap_before_rename_undo()
    test_copy_target_enospc_keeps_temp()
    test_copy_target_write_enospc_keeps_temp()
    test_copy_unlink_denied_keeps_temp()
    print(f"\n合计：通过 {PASS} / 未通过 {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
