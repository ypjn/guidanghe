"""审查报告实测问题的回归。全部在临时目录里做，不碰真实资料。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from classification_config import (  # noqa: E402
    classify_context, compile_config, default_config, looks_like_semester,
)
from config_transfer import (  # noqa: E402
    BACKUP_PREFIX, backup_dir, create_backup, restore_latest_backup,
)
from executor import undo_latest_batch  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, classify_entries, detect_duplicates,
    generate_plan,
)
from scanner import FileEntry, ScanResult, scan_directory  # noqa: E402
from storage import Storage  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name}\n{detail}")
    print(f"PASS {name}")


def write(root, rel, content=b"abc"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def compiled():
    return compile_config(default_config())


def t_semester_names():
    check("2024-2025-1", looks_like_semester("2024-2025-1"))
    check("2023-2024第一学期", looks_like_semester("2023-2024第一学期"))
    check("2023秋季学期", looks_like_semester("2023秋季学期"))
    check("2024秋", looks_like_semester("2024秋"))
    check("大二上", looks_like_semester("大二上"))
    check("普通目录不是学期", not looks_like_semester("高等数学"))


def t_directory_beats_filename():
    cfg = compiled()
    paper = classify_context("高等数学/往年题/2019/期末试卷.pdf", cfg)
    check("往年题锚点", paper.anchor_index == 0 and paper.category == "高等数学",
          paper.rule_desc)
    essay = classify_context("高等数学/英语作业.pdf", cfg)
    check("目录盖过英语作业", essay.category == "高等数学"
          and essay.direction == "课程", essay.rule_desc)
    thesis = classify_context("高等数学/课程论文.docx", cfg)
    check("课程论文留在课程", thesis.direction == "课程"
          and thesis.category == "高等数学", thesis.rule_desc)
    senior = classify_context("2023-2024-2/计组/实验报告.pdf", cfg)
    check("学长学年仍借用", senior.semester == "2023-2024-2"
          and senior.category == "计算机组成原理"
          and senior.rule_key.startswith("semester_dir_borrowed:"),
          senior.rule_desc)
    own = classify_context("大二上/数据结构/作业.pdf", cfg)
    check("本人学期目录", own.rule_key.startswith("semester_dir:")
          and own.semester == "大二上", own.rule_desc)

    root = tempfile.mkdtemp(prefix="review_cls_")
    write(root, "高等数学/往年题/2019/期末试卷.pdf")
    write(root, "2023-2024第一学期/高等数学/a.pdf")
    write(root, "2023秋季学期/计组/b.pdf")
    items, _ = classify_entries(
        scan_directory(root, detect_project_roots=False),
        MODE_BY_DIRECTION_SEMESTER, [])
    by = {item.file.rel_path: item for item in items}
    kept = by["高等数学/往年题/2019/期末试卷.pdf"]
    check("内部路径保留", "往年题" in (kept.inner_path or "")
          and "2019" in (kept.inner_path or ""),
          f"{kept.inner_path} {kept.target_rel_path}")
    sem = by["2023-2024第一学期/高等数学/a.pdf"]
    check("第一学期标签", sem.semester == "2023-2024第一学期", sem.rule_applied)
    autumn = by["2023秋季学期/计组/b.pdf"]
    check("秋季学期计组", autumn.semester == "2023秋季学期"
          and autumn.category == "计算机组成原理", autumn.rule_applied)


def t_neighbor_generic_unchecked():
    root = tempfile.mkdtemp(prefix="review_nb_")
    for rel in (
        "高等数学/习题解答.pdf",
        "新建文件夹/实验报告2.docx",
        "大二上/数据结构/实验一.pdf",
        "images/实验一.png",
    ):
        write(root, rel)
    items, _ = classify_entries(
        scan_directory(root, detect_project_roots=False),
        MODE_BY_DIRECTION_SEMESTER, [])
    by = {item.file.rel_path: item for item in items}
    stray = by["新建文件夹/实验报告2.docx"]
    check("实验报告不并进高数", not stray.rule_key.startswith("neighbor:"),
          stray.rule_applied)
    adopted = by["images/实验一.png"]
    check("同名主干仍可建议", adopted.rule_key.startswith("neighbor:")
          and adopted.category == "数据结构" and not adopted.selected,
          f"{adopted.rule_applied} selected={adopted.selected}")


def t_root_project_marker():
    root = tempfile.mkdtemp(prefix="review_root_")
    write(root, "requirements.txt", b"py\n")
    write(root, "高数/作业.pdf")
    nested = os.path.join(root, "课程设计")
    os.makedirs(os.path.join(nested, ".git"))
    write(nested, "main.py", b"print(1)\n")
    result = scan_directory(root)
    rels = [entry.rel_path for entry in result.files]
    check("根上的课程文件仍在", any(rel.endswith("作业.pdf") for rel in rels), rels)
    check("根标记有说明", "requirements.txt" in result.root_project_marker,
          result.root_project_marker)
    check("根不在保护名单",
          all(os.path.normcase(os.path.abspath(path))
              != os.path.normcase(os.path.abspath(root))
              for path in result.protected_roots),
          str(result.protected_roots))
    check("子项目仍跳过", not any(rel.endswith("main.py") for rel in rels), rels)


def t_zero_byte_and_temp():
    root = tempfile.mkdtemp(prefix="review_dup_")
    write(root, "a.txt", b"")
    write(root, "b.txt", b"")
    write(root, "keep.txt", b"hello")
    old = write(root, "left.archivetmp", b"tmp")
    old_time = 1_700_000_000
    os.utime(old, (old_time, old_time))
    fresh = write(root, "now.archivetmp", b"tmp")
    result = scan_directory(root, detect_project_roots=False)
    names = [entry.filename for entry in result.files]
    check("临时副本不进计划", "left.archivetmp" not in names
          and "now.archivetmp" not in names, names)
    check("过期临时副本已删除", not os.path.exists(old))
    check("当天临时副本先留着", os.path.exists(fresh))
    groups, _sha = detect_duplicates(result)
    check("0 字节不成重复组", groups == {}, str(groups))


def t_undo_cancel():
    root = tempfile.mkdtemp(prefix="review_undo_")
    for name in ("a.txt", "b.txt", "c.txt"):
        write(root, name, name.encode())
    scan = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(scan, MODE_BY_DIRECTION_SEMESTER, [])
    for item in plan.items:
        item.selected = True
    storage = Storage(root)
    from executor import execute_plan
    executed = execute_plan(plan, storage)
    check("三份都移动了", executed.moved == 3, executed.summary())
    cancel = threading.Event()

    def progress(index, _total, _name):
        if index >= 1:
            cancel.set()

    undone = undo_latest_batch(storage, progress_cb=progress, cancel_event=cancel)
    check("停下不算完整成功", undone.was_cancelled and not undone.is_complete
          and undone.restored == 1 and undone.remaining == 2,
          undone.summary())
    batch = storage.get_batch(undone.batch_id)
    check("批次还能继续撤", batch.status == "partial_undone", batch.status)
    moved_left = [
        action for action in storage.get_actions(undone.batch_id)
        if action.status == "moved"
    ]
    check("还剩两份", len(moved_left) == 2, str(len(moved_left)))
    again = undo_latest_batch(storage)
    check("继续撤销能完成", again.is_complete and again.restored == 2,
          again.summary())


def t_backup_keeps_readable():
    root = tempfile.mkdtemp(prefix="review_bak_")
    folder = backup_dir(root)
    os.makedirs(folder, exist_ok=True)
    good = os.path.join(folder, f"{BACKUP_PREFIX}20200101-000000-000001.json")
    with open(good, "w", encoding="utf-8") as handle:
        handle.write('{"state":"default","created_at":"2020"}')
    for index in range(5):
        path = os.path.join(
            folder, f"{BACKUP_PREFIX}2024010{index}-000000-00000{index}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
    state, message = restore_latest_backup(root)
    check("恢复用的是那份好备份", state == "default" and os.path.exists(good),
          message)
    check("说明里留着原备份", os.path.basename(good) in message, message)


def t_headline_helpers_exist():
    # 空计划文案在界面方法上，这里只锁住扫描根标记字段存在
    result = ScanResult(root="/tmp")
    check("扫描结果有根标记字段", result.root_project_marker == "")
    entry = FileEntry("/tmp/a", "a", "a", "文档", 0, "0 B")
    check("文件条目可构造", entry.size == 0)


def main() -> int:
    t_semester_names()
    t_directory_beats_filename()
    t_neighbor_generic_unchecked()
    t_root_project_marker()
    t_zero_byte_and_temp()
    t_undo_cancel()
    t_backup_keeps_readable()
    t_headline_helpers_exist()
    print("review followup checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
