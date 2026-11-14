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
import sys
import tempfile

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


if __name__ == "__main__":
    test_short_write()
    test_tmp_checksum_unreadable()
    test_source_changed_after_copy()
    test_dangling_tmp_symlink()
    test_dir_swapped_before_tmp_create()
    test_target_swapped_after_sha_execute()
    test_target_swapped_after_sha_undo()
    test_mode_and_mtime()
    print(f"\n合计：通过 {PASS} / 未通过 {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
