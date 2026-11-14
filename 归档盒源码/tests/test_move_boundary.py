"""
归档盒 — 落盘边界回归（符号链接越界 + 不覆盖）

复现并验证：
1. _已整理 若是指向空间外的符号链接，执行不得把文件移出空间，
   也不得顺着链接在空间外建目录。
2. 执行与撤销的真正落盘调用不得覆盖「检查之后才出现」的目标文件。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_move_boundary.py
"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from executor import execute_plan, undo_latest_batch, _copy_then_replace
from planner import OrganizePlan, PlanItem
from scanner import FileEntry
from storage import Storage, STATUS_MOVED, STATUS_SKIPPED, STATUS_CONFLICT

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


def mk_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def make_plan(root, src_rel, target_rel, content="SRC-CONTENT"):
    src_abs = os.path.join(root, src_rel)
    mk_file(src_abs, content)
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


def storage_for(root):
    return Storage(root)


# ===================== Bug 1：符号链接越界 =====================

def test_symlink_escape():
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        outside = os.path.join(base, "outside")
        os.makedirs(outside)

        # _已整理 指向空间外
        organized = os.path.join(root, "_已整理")
        os.symlink(outside, organized)

        plan = make_plan(root, "a.txt", "_已整理/a.txt")
        st = storage_for(root)
        res = execute_plan(plan, st)

        src_abs = os.path.join(root, "a.txt")
        outside_file = os.path.join(outside, "a.txt")

        check("B1-源文件仍在原处", os.path.isfile(src_abs),
              f"src exists={os.path.exists(src_abs)}")
        check("B1-空间外没有该文件", not os.path.isfile(outside_file),
              f"outside file exists={os.path.exists(outside_file)}")
        check("B1-动作不是 moved", res.moved == 0,
              f"moved={res.moved}")
        check("B1-批次不报完整成功",
              not (res.failed == 0 and res.skipped == 0 and res.cancelled == 0),
              f"moved={res.moved} skipped={res.skipped} failed={res.failed}")
        # 没有在空间外建目录
        check("B1-空间外未新建子目录",
              os.listdir(outside) == [],
              f"outside contents={os.listdir(outside)}")


# ===================== Bug 2a：执行不覆盖 =====================

def test_execute_no_overwrite_via_function():
    """直接测落盘函数：目标已存在且内容不同时不得覆盖。"""
    with tempfile.TemporaryDirectory() as base:
        src = os.path.join(base, "src.txt")
        dst = os.path.join(base, "dst.txt")
        mk_file(src, "SRC-CONTENT")
        mk_file(dst, "DST-CONTENT-ORIGINAL")

        ok, err, is_conflict = _copy_then_replace(src, dst, base)

        check("B2a-落盘函数返回失败", not ok, f"ok={ok} err={err}")
        check("B2a-标记为冲突", is_conflict, f"is_conflict={is_conflict}")
        with open(dst, encoding="utf-8") as f:
            check("B2a-目标内容未被覆盖", f.read() == "DST-CONTENT-ORIGINAL")
        check("B2a-源文件仍在", os.path.isfile(src))


def test_execute_no_overwrite_in_flow():
    """执行流：exists 检查通过后、真正落盘前，目标被放入不同内容的文件。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="SRC-CONTENT")
        target_abs = os.path.join(root, "_已整理", "a.txt")
        st = storage_for(root)

        real_exists = os.path.exists

        def fake_exists(p):
            # 让预检查以为目标不存在，但磁盘上目标实际存在
            if os.path.abspath(p) == os.path.abspath(target_abs):
                return False
            return real_exists(p)

        # 在执行前先把目标放进去（内容不同）
        os.makedirs(os.path.dirname(target_abs), exist_ok=True)
        mk_file(target_abs, "INJECTED-CONTENT")

        import executor
        orig = executor.os.path.exists
        executor.os.path.exists = fake_exists
        try:
            res = execute_plan(plan, st)
        finally:
            executor.os.path.exists = orig

        with open(target_abs, encoding="utf-8") as f:
            check("B2b-目标内容未被覆盖（执行流）",
                  f.read() == "INJECTED-CONTENT")
        check("B2b-源文件仍在（执行流）",
              os.path.isfile(os.path.join(root, "a.txt")))
        check("B2b-动作不是 moved（执行流）", res.moved == 0,
              f"moved={res.moved}")


# ===================== Bug 2b：撤销不覆盖 =====================

def test_undo_no_overwrite():
    """撤销：原路径已被新文件占用时不得覆盖。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="ORIGINAL")
        st = storage_for(root)

        # 正常执行一次
        res = execute_plan(plan, st)
        check("B2c-前置执行成功", res.moved == 1, f"moved={res.moved}")
        moved_abs = os.path.join(root, "_已整理", "a.txt")
        check("B2c-文件已移入目标", os.path.isfile(moved_abs))

        # 在原路径放入内容不同的新文件
        src_abs = os.path.join(root, "a.txt")
        mk_file(src_abs, "NEW-CONTENT-AT-SOURCE")

        undo_res = undo_latest_batch(st)

        with open(src_abs, encoding="utf-8") as f:
            check("B2c-原路径新文件未被覆盖", f.read() == "NEW-CONTENT-AT-SOURCE")
        check("B2c-目标里的原文件仍在", os.path.isfile(moved_abs))
        check("B2c-该项记为冲突而非已恢复",
              undo_res.conflict > 0 and undo_res.restored == 0,
              f"restored={undo_res.restored} conflict={undo_res.conflict}")


def test_undo_no_overwrite_in_flow():
    """撤销流：exists 检查通过后、真正落盘前，原路径被放入不同内容文件。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="ORIGINAL")
        st = storage_for(root)
        execute_plan(plan, st)
        src_abs = os.path.join(root, "a.txt")
        moved_abs = os.path.join(root, "_已整理", "a.txt")

        real_exists = os.path.exists

        def fake_exists(p):
            if os.path.abspath(p) == os.path.abspath(src_abs):
                return False  # 让撤销以为原路径空闲
            return real_exists(p)

        # 检查前在原路径放入不同内容
        mk_file(src_abs, "RACE-CONTENT")

        import executor
        orig = executor.os.path.exists
        executor.os.path.exists = fake_exists
        try:
            undo_res = undo_latest_batch(st)
        finally:
            executor.os.path.exists = orig

        with open(src_abs, encoding="utf-8") as f:
            check("B2d-原路径文件未被覆盖（撤销流）",
                  f.read() == "RACE-CONTENT")
        check("B2d-目标里的原文件仍在（撤销流）",
              os.path.isfile(moved_abs))
        check("B2d-该项记为冲突（撤销流）",
              undo_res.conflict > 0 and undo_res.restored == 0,
              f"restored={undo_res.restored} conflict={undo_res.conflict}")


# ============ C1：os.replace 仍会覆盖独占创建后的占位文件 ============

def test_os_replace_overwrites_placeholder_execute():
    """独占创建成功后、落盘前，占位文件被换成不同内容文件；不得覆盖。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="SRC-CONTENT")
        dst_abs = os.path.join(root, "_已整理", "a.txt")
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
        st = storage_for(root)

        import executor
        real_open = os.open

        def hooked_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            # 仅在对目标独占创建时注入：把刚创建的占位换成别的内容文件
            if (flags & os.O_EXCL and
                    os.path.normpath(os.path.realpath(path)) ==
                    os.path.normpath(os.path.realpath(dst_abs))):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                with open(path, "w", encoding="utf-8") as f:
                    f.write("SWAPPED-BY-RACE")
            return fd

        executor.os.open = hooked_open
        try:
            res = execute_plan(plan, st)
        finally:
            executor.os.open = real_open

        with open(dst_abs, encoding="utf-8") as f:
            check("C1-占位被换后内容未被覆盖（执行）",
                  f.read() == "SWAPPED-BY-RACE")
        check("C1-源文件仍在且内容不变（执行）",
              os.path.isfile(os.path.join(root, "a.txt")))
        check("C1-动作非 moved（执行）", res.moved == 0,
              f"moved={res.moved}")


def test_os_replace_overwrites_placeholder_undo():
    """撤销：独占创建原路径后、落盘前，原路径被换成不同内容；不得覆盖。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="ORIGINAL")
        st = storage_for(root)
        res = execute_plan(plan, st)
        check("C1u-前置执行成功", res.moved == 1, f"moved={res.moved}")
        src_abs = os.path.join(root, "a.txt")
        moved_abs = os.path.join(root, "_已整理", "a.txt")

        import executor
        real_open = os.open

        def hooked_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if (flags & os.O_EXCL and
                    os.path.normpath(os.path.realpath(path)) ==
                    os.path.normpath(os.path.realpath(src_abs))):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                with open(path, "w", encoding="utf-8") as f:
                    f.write("SWAPPED-AT-SOURCE")
            return fd

        executor.os.open = hooked_open
        try:
            undo_res = undo_latest_batch(st)
        finally:
            executor.os.open = real_open

        with open(src_abs, encoding="utf-8") as f:
            check("C1u-原路径被换后内容未被覆盖（撤销）",
                  f.read() == "SWAPPED-AT-SOURCE")
        check("C1u-目标里原文件仍在（撤销）",
              os.path.isfile(moved_abs))
        check("C1u-非已恢复（撤销）",
              undo_res.restored == 0,
              f"restored={undo_res.restored}")


# ============ C2：路径边界可在校验之后被换掉 ============

def test_source_parent_swapped_to_outside_execute():
    """整理前把源文件父目录换成指向空间外的链接，外部同内容文件不得被删。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "sub/a.txt", "_已整理/a.txt",
                         content="SAME-CONTENT")
        outside = os.path.join(base, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "a.txt"), "w", encoding="utf-8") as f:
            f.write("SAME-CONTENT")

        # 把源父目录 sub/ 换成指向 outside 的符号链接
        src_parent = os.path.join(root, "sub")
        import shutil as _sh
        _sh.rmtree(src_parent)
        os.symlink(outside, src_parent)

        st = storage_for(root)
        res = execute_plan(plan, st)

        outside_file = os.path.join(outside, "a.txt")
        check("C2a-空间外文件未被删除", os.path.isfile(outside_file))
        with open(outside_file, encoding="utf-8") as f:
            check("C2a-空间外文件内容不变", f.read() == "SAME-CONTENT")
        check("C2a-动作非 moved", res.moved == 0, f"moved={res.moved}")
        check("C2a-空间内目标无该文件",
              not os.path.isfile(os.path.join(root, "_已整理", "a.txt")))


def test_target_parent_swapped_to_outside_undo():
    """撤销前把已整理文件所在目录换成空间外链接，不得删除外部文件。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="ORIGINAL")
        st = storage_for(root)
        res = execute_plan(plan, st)
        check("C2b-前置执行成功", res.moved == 1, f"moved={res.moved}")

        outside = os.path.join(base, "outside")
        os.makedirs(outside)
        # 把一个同内容文件放到 outside
        with open(os.path.join(outside, "a.txt"), "w", encoding="utf-8") as f:
            f.write("ORIGINAL")
        # 把 _已整理 换成指向 outside 的链接
        import shutil as _sh
        _sh.rmtree(os.path.join(root, "_已整理"))
        os.symlink(outside, os.path.join(root, "_已整理"))

        undo_res = undo_latest_batch(st)

        outside_file = os.path.join(outside, "a.txt")
        check("C2b-空间外文件未被删除", os.path.isfile(outside_file))
        with open(outside_file, encoding="utf-8") as f:
            check("C2b-空间外文件内容不变", f.read() == "ORIGINAL")
        check("C2b-未报已恢复", undo_res.restored == 0,
              f"restored={undo_res.restored}")


def test_target_dir_symlink_race_during_write():
    """目标边界校验通过后、写入前，_已整理 被换成空间外链接。
    文件不得出现在空间外；若已在外部建了自己的 inode，只回滚它，不碰外部原有文件。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="SRC")
        dst_abs = os.path.join(root, "_已整理", "a.txt")
        organized_dir = os.path.join(root, "_已整理")
        os.makedirs(organized_dir, exist_ok=True)
        outside = os.path.join(base, "outside")
        os.makedirs(outside)

        import executor
        real_open = os.open

        def hooked_open(path, flags, *args, **kwargs):
            # 在对目标独占创建之前，把 _已整理 换成指向 outside 的链接
            if (flags & os.O_EXCL and
                    os.path.basename(os.path.dirname(path)) == "_已整理"):
                import shutil as _sh
                _sh.rmtree(organized_dir)
                os.symlink(outside, organized_dir)
            return real_open(path, flags, *args, **kwargs)

        executor.os.open = hooked_open
        try:
            res = execute_plan(plan, st := storage_for(root))
        finally:
            executor.os.open = real_open

        # 空间外不得出现该文件（若出现，必须是本次自己建的，且已回滚）
        check("C2c-空间外无整理出的文件",
              not os.path.isfile(os.path.join(outside, "a.txt")),
              f"outside contents={os.listdir(outside)}")
        check("C2c-动作非 moved", res.moved == 0, f"moved={res.moved}")
        # 源文件仍在
        check("C2c-源文件仍在", os.path.isfile(os.path.join(root, "a.txt")))


# ============ C3：复制完成后源文件被改，新内容不能丢 ============

def test_source_modified_after_copy():
    """复制完成之后、删源之前，源被写成新内容。源必须保留新内容，动作记失败。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        plan = make_plan(root, "a.txt", "_已整理/a.txt", content="OLD")
        st = storage_for(root)
        src_abs = os.path.join(root, "a.txt")

        import executor
        real_write = os.write
        state = {"written": 0, "edited": False}

        def hooked_write(fd, data):
            n = real_write(fd, data)
            state["written"] += n
            # 源字节已全部写入临时副本后，立刻把源文件改成新内容
            if state["written"] >= len("OLD") and not state["edited"]:
                state["edited"] = True
                with open(src_abs, "w", encoding="utf-8") as f:
                    f.write("NEW-CONTENT-AFTER-COPY")
            return n

        executor.os.write = hooked_write
        try:
            res = execute_plan(plan, st)
        finally:
            executor.os.write = real_write

        with open(src_abs, encoding="utf-8") as f:
            check("C3-源文件保留新内容", f.read() == "NEW-CONTENT-AFTER-COPY")
        check("C3-动作非 moved", res.moved == 0, f"moved={res.moved}")
        check("C3-目标未留下旧内容",
              not os.path.isfile(os.path.join(root, "_已整理", "a.txt")))
        check("C3-无临时文件残留",
              not os.path.exists(os.path.join(root, "_已整理", ".abtmp")))


# ============ C4：255 字节合法文件名 ============

def test_255_byte_filename():
    """255 字节文件名必须能整理成功，临时名不得超过同目录 255 字节限制。"""
    with tempfile.TemporaryDirectory() as base:
        root = os.path.join(base, "space")
        os.makedirs(root)
        # 255 字节的文件名（ASCII，每字符 1 字节）
        long_name = "a" * 255
        plan = make_plan(root, long_name, f"_已整理/{long_name}",
                         content="255BYTE-CONTENT")
        st = storage_for(root)
        res = execute_plan(plan, st)
        check("C4-255字节文件名整理成功", res.moved == 1,
              f"moved={res.moved}")
        target = os.path.join(root, "_已整理", long_name)
        check("C4-目标存在", os.path.isfile(target))
        with open(target, encoding="utf-8") as f:
            check("C4-目标内容与源一致", f.read() == "255BYTE-CONTENT")
        check("C4-源文件已移走",
              not os.path.isfile(os.path.join(root, long_name)))


if __name__ == "__main__":
    test_symlink_escape()
    test_execute_no_overwrite_via_function()
    test_execute_no_overwrite_in_flow()
    test_undo_no_overwrite()
    test_undo_no_overwrite_in_flow()
    test_os_replace_overwrites_placeholder_execute()
    test_os_replace_overwrites_placeholder_undo()
    test_source_parent_swapped_to_outside_execute()
    test_target_parent_swapped_to_outside_undo()
    test_target_dir_symlink_race_during_write()
    test_source_modified_after_copy()
    test_255_byte_filename()
    print(f"\n合计：通过 {PASS} / 未通过 {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
