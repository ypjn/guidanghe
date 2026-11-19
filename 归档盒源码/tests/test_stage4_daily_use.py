"""
归档盒阶段四 · 日常使用版回归（offscreen）。

覆盖：扫描阶段进度与 seq 隔离、增量缓存命中/失效、缓存损坏回退、
预览后源文件变化仍被拒绝、OOXML 正文解析与坏文件原因、批量预览
筛选/勾选一致、历史定位跟随当前路径。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_stage4_daily_use.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage4_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage4_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage4_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import main as main_mod  # noqa: E402
import scan_cache  # noqa: E402
from content_extract import extract_contents  # noqa: E402
from executor import execute_plan  # noqa: E402
from planner import (  # noqa: E402
    DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE,
)
from scanner import scan_directory  # noqa: E402
from storage import Storage  # noqa: E402

APP = QApplication.instance() or QApplication([])
BASE = tempfile.mkdtemp(prefix="stage4_daily_")
CHECKS = []


def rec(tid, title, ok, evidence=""):
    CHECKS.append((tid, title, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    if evidence:
        print(evidence)


def fresh(tag):
    root = os.path.join(BASE, tag)
    os.makedirs(root, exist_ok=True)
    return os.path.realpath(root)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_zip(path, members):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def make_docx(path, text):
    make_zip(path, {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            f"{text}</w:t></w:r></w:p></w:body></w:document>"),
    })


def make_pptx(path, text):
    make_zip(path, {
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="x" xmlns:a="a"><a:t>'
            f"{text}</a:t></p:sld>"),
    })


def make_xlsx(path, text):
    make_zip(path, {
        "xl/sharedStrings.xml": (
            f'<sst xmlns="x"><si><t>{text}</t></si></sst>'),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="x"><sheetData><row><c><v>0</v></c>'
            "</row></sheetData></worksheet>"),
    })


def run_plan(root, entries=None, note=""):
    result = scan_directory(root)
    worker = main_mod.PlanWorker(
        1, result, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES,
        scan_cache_entries=entries or {}, cache_note=note)
    out = {}
    worker.finished_ok.connect(lambda seq, plan: out.update(plan=plan))
    worker.failed.connect(lambda seq, err: out.update(error=err))
    worker.run()
    return result, out


def plan_map(plan):
    return {
        item.file.rel_path: (item.target_rel_path, item.rule_key,
                             item.selected)
        for item in plan.items
    }


def test_scan_progress_and_seq():
    root = fresh("scan_progress")
    for index in range(650):
        write(root, f"目录{index % 7}/文件{index:03d}.txt", "x")

    events = []

    def progress(seen, current):
        events.append((seen, current))

    result = scan_directory(root, progress_cb=progress)
    worker = main_mod.ScanWorker(root, seq=7)
    worker_events = []
    worker.progress.connect(
        lambda seq, seen, current: worker_events.append((seq, seen, current)))
    worker.run()

    monotonic = all(events[i][0] <= events[i + 1][0]
                    for i in range(len(events) - 1))
    ok = (
        len(result.files) == 650
        and events
        and events[0] == (0, "")
        and events[-1][0] == 650
        and monotonic
        and any(current for _seen, current in events)
        and worker_events
        and all(seq == 7 for seq, _seen, _current in worker_events)
        and worker_events[-1][1] == 650
    )

    # 旧扫描的完成信号不能写入新扫描
    window = main_mod.MainWindow()
    window._current_scan_seq = 2
    window._close_pending = True
    old_result = scan_directory(root)
    window._full_scan_result = None
    window._on_scan_done(1, old_result)
    stale_ignored = window._full_scan_result is None
    window._on_scan_done(2, old_result)
    current_written = window._full_scan_result is old_result
    window._closing = True
    window.hide()
    window.deleteLater()
    process(0.05)

    rec("S401", "扫描阶段报告已发现数量与当前路径；旧扫描完成信号不写入新扫描",
        ok and stale_ignored and current_written,
        f"文件数={len(result.files)} 回调={len(events)} "
        f"首={events[:2]} 末={events[-2:]}\n"
        f"worker seq 全为 7={all(seq == 7 for seq, _s, _c in worker_events)} "
        f"worker 末={worker_events[-1:]}\n"
        f"旧 seq 被忽略={stale_ignored} 当前 seq 写入={current_written}")


def test_cache_hits_and_invalidation():
    root = fresh("cache")
    write(root, "课程/高等数学/笔记.txt", "高等数学 极限 导数\n")
    write(root, "课程/高等数学/说明.md", "高等数学 课程说明\n")
    write(root, "下载/手册.pdf", "%PDF-1.4 fake")
    make_docx(os.path.join(root, "下载/Word笔记.docx"), "高等数学 Word 正文")

    _sr1, first = run_plan(root)
    entries, note = scan_cache.load(root)
    _sr2, second = run_plan(root, entries, note)
    first_stats = first["plan"].cache_stats
    second_stats = second["plan"].cache_stats
    same_plan = plan_map(first["plan"]) == plan_map(second["plan"])

    # 修改 1 个、新增 1 个、删除 1 个、改名 1 个
    write(root, "课程/高等数学/笔记.txt", "高等数学 极限 导数 修改\n")
    write(root, "下载/新文件.txt", "新文件正文\n")
    os.unlink(os.path.join(root, "课程/高等数学/说明.md"))
    os.rename(os.path.join(root, "下载/手册.pdf"),
              os.path.join(root, "下载/手册改名.pdf"))
    entries2, note2 = scan_cache.load(root)
    sr3, third = run_plan(root, entries2, note2)
    third_stats = third["plan"].cache_stats

    # 同一批文件走空缓存，分类依据应与增量结果一致
    _sr4, full = run_plan(root, {}, "")
    incremental_map = plan_map(third["plan"])
    full_map = plan_map(full["plan"])
    same_after_change = incremental_map == full_map
    file_set_same = (
        sorted(item.file.rel_path for item in third["plan"].items)
        == sorted(entry.rel_path for entry in sr3.files))

    ok = (
        first_stats.get("cache_hits") == 0
        and second_stats.get("cache_hits") == len(entries)
        and second_stats.get("sha_reused") == len(entries)
        and second_stats.get("content_reused") >= 3
        and same_plan
        and third_stats.get("cache_hits", 0) >= 1
        and third_stats.get("cache_misses", 0) >= 3
        and same_after_change
        and file_set_same
    )
    rec("S402", "增量缓存复用未变化文件；新增/修改/删除/改名正确失效且分类一致",
        ok,
        f"首次={first_stats}\n第二次={second_stats}\n"
        f"变化后={third_stats}\n"
        f"首次与第二次计划一致={same_plan} "
        f"增量与全量一致={same_after_change} 文件集合一致={file_set_same}\n"
        f"增量计划={sorted(incremental_map.items())}")


def test_corrupt_cache_and_execution_validation():
    root = fresh("cache_corrupt")
    source = write(root, "课程/高等数学/笔记.txt", "高等数学 极限 导数\n")
    other = write(root, "下载/其他.txt", "其他内容\n")
    org = os.path.join(root, ".organizer")
    os.makedirs(org, exist_ok=True)
    with open(os.path.join(org, "scan_cache.json"), "w", encoding="utf-8") as handle:
        handle.write("{bad json")

    entries, note = scan_cache.load(root)
    _sr, out = run_plan(root, entries, note)
    plan = out.get("plan")
    fallback = (
        entries == {}
        and "损坏" in note
        and plan is not None
        and plan.cache_stats.get("cache_hits") == 0
        and "损坏" in plan.cache_stats.get("cache_note", "")
    )

    for item in plan.items:
        item.selected = True
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("预览之后被改过的内容\n")
    result = execute_plan(plan, Storage(root))
    skipped = any("源文件内容已变化" in line and "笔记.txt" in line
                  for line in result.errors)
    source_kept = open(source, encoding="utf-8").read() == "预览之后被改过的内容\n"
    other_moved = any("其他.txt" in line for line in result.errors) is False
    rec("S403", "缓存损坏回退全量；预览后源文件变化仍被拒绝，其他项不受影响",
        fallback and result.skipped == 1 and result.moved == 1
        and skipped and source_kept and other_moved,
        f"缓存说明={note!r} 计划缓存={plan.cache_stats if plan else None}\n"
        f"moved={result.moved} skipped={result.skipped} errors={result.errors}\n"
        f"源文件保留新内容={source_kept} 其他项未被误报={other_moved}")


def test_ooxml_parsing_and_bad_files():
    root = fresh("parsing")
    docx = os.path.join(root, "下载/课程笔记.docx")
    pptx = os.path.join(root, "下载/答辩.pptx")
    xlsx = os.path.join(root, "下载/成绩.xlsx")
    bad = os.path.join(root, "下载/坏文件.docx")
    pdf = os.path.join(root, "下载/资料.pdf")
    plain = os.path.join(root, "下载/普通.txt")
    denied = os.path.join(root, "下载/无权限.txt")
    make_docx(docx, "高等数学 课件正文")
    make_pptx(pptx, "项目 答辩 演示")
    make_xlsx(xlsx, "数据结构 成绩")
    with open(bad, "wb") as handle:
        handle.write(b"not a zip")
    with open(pdf, "wb") as handle:
        handle.write(b"%PDF-1.4")
    with open(plain, "w", encoding="utf-8") as handle:
        handle.write("普通正文内容\n")
    with open(denied, "w", encoding="utf-8") as handle:
        handle.write("无权限内容\n")
    result = scan_directory(root)
    os.chmod(denied, 0o000)
    try:
        extraction = extract_contents(result.files, root=root)
    finally:
        os.chmod(denied, 0o644)

    known = {
        "下载/课程笔记.docx": extraction.texts.get("下载/课程笔记.docx", ""),
        "下载/答辩.pptx": extraction.texts.get("下载/答辩.pptx", ""),
        "下载/成绩.xlsx": extraction.texts.get("下载/成绩.xlsx", ""),
    }
    ok = (
        "高等数学" in known["下载/课程笔记.docx"]
        and "答辩" in known["下载/答辩.pptx"]
        and "数据结构" in known["下载/成绩.xlsx"]
        and extraction.kinds.get("下载/课程笔记.docx") == "正文"
        and "损坏" in extraction.errors.get("下载/坏文件.docx", "")
        and "PDF" in extraction.errors.get("下载/资料.pdf", "")
        and "无法读取正文" in extraction.errors.get("下载/无权限.txt", "")
        and extraction.texts.get("下载/普通.txt") == "普通正文内容"
    )
    rec("S404", "docx/pptx/xlsx 正文可读；损坏、无权限、未支持各有原因且不挡其他文件",
        ok,
        f"正文={known}\n"
        f"kinds={extraction.kinds}\n"
        f"errors={extraction.errors}\n"
        f"普通文本={extraction.texts.get('下载/普通.txt')!r}")


def test_preview_filter_and_selection():
    root = fresh("preview")
    for index in range(4):
        write(root, f"下载/项目{index}.txt", f"项目 会议 {index}\n")
    for index in range(4):
        write(root, f"下载/课程{index}.txt", f"高等数学 作业 {index}\n")

    window = main_mod.MainWindow()
    window.path_edit.setText(root)
    window._reset_for_new_dir(root)
    window.show()
    window.on_scan()
    assert wait_until(lambda: (not window._working)
                      and window.scan_result is not None)
    window._generate_and_show_plan()
    assert wait_until(lambda: (not window._plan_working)
                      and window.current_plan is not None and window._plan_valid)

    window._set_visible_checked(True)
    selected_before = {item.file.rel_path
                       for item in window.current_plan.items if item.selected}
    window.plan_search.setText("项目")
    process()
    visible = [flag for flag in window._visible_flags if flag]
    visible_rels = {window.current_plan.items[index].file.rel_path
                    for index, flag in enumerate(window._visible_flags) if flag}
    window._set_visible_checked(False)
    after_deselect = {item.file.rel_path
                      for item in window.current_plan.items if item.selected}
    expected = selected_before - visible_rels
    counts_ok = f"可见 {len(visible)}" in window.plan_counts_label.text()
    counts_text = window.plan_counts_label.text()
    window.close()

    ok = (
        len(selected_before) == 8
        and len(visible_rels) == 4
        and after_deselect == expected
        and counts_ok
    )
    rec("S405", "预览筛选只改视图；取消可见勾选不影响被筛掉的行，计数一致",
        ok,
        f"全部已选={len(selected_before)} 可见={len(visible_rels)} "
        f"筛掉={len(selected_before - visible_rels)}\n"
        f"取消可见勾选后仍选={sorted(after_deselect)}\n"
        f"计数标签={counts_text!r}")


def test_history_reveal_current_path():
    root = fresh("reveal")
    source_rel = "下载/笔记.txt"
    target_rel = os.path.join("_已整理", "课程", "高等数学", "笔记.txt")
    source = write(root, source_rel, "高等数学 极限 导数\n")
    target = os.path.join(root, target_rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(source, target)
    after_move = main_mod.history_reveal_target(root, source_rel, target_rel)
    os.rename(target, source)
    after_undo = main_mod.history_reveal_target(root, source_rel, target_rel)
    os.unlink(source)
    missing = main_mod.history_reveal_target(root, source_rel, target_rel)
    rec("S406", "历史定位跟随文件当前位置；目标/原位置都不在时不返回同名文件",
        after_move == target and after_undo == source and missing == "",
        f"目标存在时={after_move!r} 撤销后={after_undo!r} "
        f"都不存在={missing!r}")


def wait_until(predicate, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        APP.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


def process(seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def main():
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
    test_scan_progress_and_seq()
    test_cache_hits_and_invalidation()
    test_corrupt_cache_and_execution_validation()
    test_ooxml_parsing_and_bad_files()
    test_preview_filter_and_selection()
    test_history_reveal_current_path()
    failed = [row for row in CHECKS if not row[2]]
    print()
    print(f"合计 {len(CHECKS)} 项：通过 {len(CHECKS) - len(failed)} / 未通过 {len(failed)}")
    for tid, title, _ok, evidence in failed:
        print(f"未通过 {tid} {title}\n{evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
