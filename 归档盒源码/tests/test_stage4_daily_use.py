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

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMessageBox, QAbstractItemView, QRadioButton,
)
from PySide6.QtGui import QAccessible, QAccessibleActionInterface  # noqa: E402

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


def test_cache_save_boundary():
    """S407：.organizer 在打开后、临时文件落盘前被换成空间外符号链接。"""
    root = fresh("cache_boundary")
    organizer = os.path.join(root, ".organizer")
    os.makedirs(organizer)
    outside = os.path.join(BASE, "cache_boundary_outside")
    os.makedirs(outside, exist_ok=True)
    victim = os.path.join(outside, scan_cache.CACHE_FILENAME)
    with open(victim, "wb") as handle:
        handle.write(b"other user file outside workspace\n")
    before = sha256(victim)

    original_open = scan_cache._open_organizer

    def open_then_swap(root_arg):
        fd, org = original_open(root_arg)
        os.rename(organizer, organizer + ".original")
        os.symlink(outside, organizer, target_is_directory=True)
        return fd, org

    scan_cache._open_organizer = open_then_swap
    failed = False
    try:
        try:
            scan_cache.save(root, {})
        except OSError:
            failed = True
    finally:
        scan_cache._open_organizer = original_open
        if os.path.islink(organizer):
            os.unlink(organizer)
        if os.path.isdir(organizer + ".original"):
            os.rename(organizer + ".original", organizer)

    after = sha256(victim)
    outside_intact = before == after

    # 恢复后正常目录仍能写回、读回。
    scan_cache.save(root, {})
    loaded, note = scan_cache.load(root)
    normal_ok = (loaded == {} and note == ""
                 and os.path.isfile(os.path.join(organizer,
                                                 scan_cache.CACHE_FILENAME)))

    # 缓存文件本身是符号链接时也要拒绝。
    link_victim = os.path.join(outside, "linked-cache.json")
    with open(link_victim, "wb") as handle:
        handle.write(b"linked victim\n")
    link_before = sha256(link_victim)
    cache_file = os.path.join(organizer, scan_cache.CACHE_FILENAME)
    os.unlink(cache_file)
    os.symlink(link_victim, cache_file)
    symlink_failed = False
    try:
        scan_cache.save(root, {})
    except OSError:
        symlink_failed = True
    link_intact = sha256(link_victim) == link_before
    os.unlink(cache_file)
    scan_cache.save(root, {})

    rec("S407", "缓存替换前复核实际目录；空间外文件不覆盖，正常目录仍可读写",
        failed and outside_intact and normal_ok and symlink_failed and link_intact,
        f"save 失败={failed} 空间外 SHA 不变={outside_intact}\n"
        f"正常读写={normal_ok} 缓存文件符号链接被拒={symlink_failed} "
        f"链接目标不变={link_intact}\n"
        f"victim before={before} after={after}")


def test_stale_cache_same_size_mtime():
    """S408：内容变了但 inode/大小/mtime 不变时，不能复用旧指纹和正文。"""
    root = fresh("stale_cache")
    path = write(root, "下载/笔记.txt", "高等数学\n")
    original = os.stat(path)
    _sr, first = run_plan(root)
    entries, note = scan_cache.load(root)
    before_hits = first["plan"].cache_stats.get("cache_hits")

    with open(path, "r+b") as handle:
        handle.write("数据结构\n".encode("utf-8"))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    after_stat = os.stat(path)
    same_identity = (
        after_stat.st_ino == original.st_ino
        and after_stat.st_size == original.st_size
        and after_stat.st_mtime_ns == original.st_mtime_ns)

    _sr2, second = run_plan(root, entries, note)
    second_plan = second["plan"]
    target_after_change = second_plan.items[0].target_rel_path
    sha_after_change = second_plan.items[0].source_sha256
    disk_sha = sha256(path)
    content_changed = sha_after_change == disk_sha
    classified_new = "未分类" in target_after_change
    cache_missed = (
        second_plan.cache_stats.get("cache_hits") == 0
        and second_plan.cache_stats.get("sha_computed") == 1
        and second_plan.cache_stats.get("sha_reused", 0) == 0)

    entries2, note2 = scan_cache.load(root)
    _sr3, third = run_plan(root, entries2, note2)
    third_plan = third["plan"]
    second_scan_reused = (
        third_plan.cache_stats.get("cache_hits") == 1
        and third_plan.cache_stats.get("sha_reused") == 1
        and third_plan.items[0].target_rel_path == target_after_change
        and third_plan.items[0].source_sha256 == disk_sha)

    for item in third_plan.items:
        item.selected = True
    result = execute_plan(third_plan, Storage(root))
    executed_ok = (
        result.moved == 1 and result.skipped == 0
        and not os.path.exists(path)
        and os.path.isfile(os.path.join(root, target_after_change)))

    # 计划后、执行前再次改成同大小内容，执行前校验仍要跳过。
    root2 = fresh("stale_cache_after_plan")
    path2 = write(root2, "下载/第二次.txt", "高等数学\n")
    st2 = os.stat(path2)
    _s, plan2 = run_plan(root2)
    with open(path2, "r+b") as handle:
        handle.write("数据结构\n".encode("utf-8"))
    os.utime(path2, ns=(st2.st_atime_ns, st2.st_mtime_ns))
    for item in plan2["plan"].items:
        item.selected = True
    result2 = execute_plan(plan2["plan"], Storage(root2))
    post_plan_skipped = (
        result2.moved == 0 and result2.skipped == 1
        and any("源文件内容已变化" in line for line in result2.errors))

    rec("S408", "同大小/同 mtime/inode 下正文变化不复用旧缓存，执行不被旧指纹跳过",
        same_identity and before_hits == 0 and content_changed and classified_new
        and cache_missed and second_scan_reused and executed_ok and post_plan_skipped,
        f"inode/大小/mtime 不变={same_identity} 首次命中={before_hits}\n"
        f"变化后分类={target_after_change} SHA与磁盘一致={content_changed} "
        f"缓存未命中={cache_missed}\n"
        f"第二次重扫复用新指纹={second_scan_reused} 执行={result.moved}/"
        f"{result.skipped}\n"
        f"计划后改内容再执行={result2.moved}/{result2.skipped} "
        f"errors={result2.errors}")


def test_open_space_history_undo_ready():
    """S409：打开已有可撤销批次的空间后，不扫描即可看历史、撤销。"""
    root = fresh("reopen_space")
    source = write(root, "下载/笔记.txt", "高等数学 极限 导数\n")
    source_sha = sha256(source)
    _sr, out = run_plan(root)
    plan = out["plan"]
    for item in plan.items:
        item.selected = True
    target_rel = plan.items[0].target_rel_path
    target = os.path.join(root, target_rel)
    executed = execute_plan(plan, Storage(root))
    storage = Storage(root)
    undo_before = storage.list_batches(include_actions=False)
    storage.close()

    window = main_mod.MainWindow()
    opened = window._open_recent_space(root)
    process(0.2)
    history_enabled = window.btn_history.isEnabled()
    undo_enabled = window.btn_undo.isEnabled()

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    window.on_undo()
    ok = wait_until(lambda: (not window._working)
                    and not os.path.exists(target)
                    and os.path.exists(source), timeout=20)
    restored_sha = sha256(source) if os.path.exists(source) else ""
    window._closing = True
    window.hide()
    window.deleteLater()
    process(0.05)

    rec("S409", "打开已有批次的空间后历史/撤销立即可用，撤销恢复原路径与 SHA",
        opened and executed.moved == 1 and bool(undo_before)
        and history_enabled and undo_enabled and ok
        and restored_sha == source_sha and not os.path.exists(target),
        f"打开={opened} 执行={executed.moved} 历史可点={history_enabled} "
        f"撤销可点={undo_enabled}\n"
        f"撤销完成={ok} 原 SHA={source_sha} 恢复 SHA={restored_sha} "
        f"目标仍存在={os.path.exists(target)}")


def test_reveal_stays_inside_space():
    """S410：绝对路径、../、空间外符号链接都不能被历史定位打开。"""
    root = fresh("reveal_boundary")
    write(root, "下载/正常.txt", "正常\n")
    outside = os.path.join(BASE, "reveal_outside.txt")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("空间外\n")
    link = os.path.join(root, "下载", "链接.txt")
    os.symlink(outside, link)
    normal = main_mod.history_reveal_target(root, "下载/正常.txt", "")
    absolute = main_mod.history_reveal_target(root, outside, "")
    parent = main_mod.history_reveal_target(root, "../reveal_outside.txt", "")
    symlink = main_mod.history_reveal_target(root, "下载/链接.txt", "")
    outside_bytes = open(outside, encoding="utf-8").read()
    rec("S410", "历史定位只返回空间内解析结果；绝对路径/../符号链接越界返回空",
        normal == os.path.join(root, "下载", "正常.txt")
        and absolute == "" and parent == "" and symlink == ""
        and outside_bytes == "空间外\n",
        f"正常={normal!r}\n绝对={absolute!r} ../={parent!r} "
        f"符号链接={symlink!r}\n空间外内容={outside_bytes!r}")


def _ax_row_pick(dlg, name):
    """在对话框辅助功能子树里找到指定名称的行选择按钮接口。"""
    iface = QAccessible.queryAccessibleInterface(dlg)
    if iface is None:
        return None
    for i in range(iface.childCount()):
        ch = iface.child(i)
        try:
            if ch.text(QAccessible.Text.Name) == name:
                return ch
        except Exception:
            continue
    return None


def _ax_press(iface):
    """按辅助功能实际的 Press/Toggle 动作触发控件，返回是否触发成功。"""
    if iface is None:
        return False
    ai = iface.actionInterface()
    if ai is None:
        return False
    actions = ai.actionNames()
    for action in (QAccessibleActionInterface.pressAction(),
                   QAccessibleActionInterface.toggleAction()):
        if action in actions:
            ai.doAction(action)
            return True
    return False


def test_history_selection_accessibility():
    """S411：辅助功能能按下「选择第 N 行明细」，进程不崩溃，定位目标/原路径。"""
    root = fresh("history_accessibility")
    source = write(root, "下载/笔记.txt", "高等数学 极限 导数\n")
    source_sha = sha256(source)
    _sr, out = run_plan(root)
    plan = out["plan"]
    for item in plan.items:
        item.selected = True
    target_rel = plan.items[0].target_rel_path
    target = os.path.join(root, target_rel)
    execute_plan(plan, Storage(root))

    window = main_mod.MainWindow()
    window._open_recent_space(root)
    window.on_history()
    process(0.3)
    dlg = window._history_dialog
    rows = dlg.item_table.rowCount() if dlg is not None else 0
    no_selection = (dlg is not None
                    and dlg.item_table.selectionMode()
                    == QAbstractItemView.NoSelection)
    wrappers_kept = bool(dlg is not None and dlg._item_wrappers)

    ax_names = []
    ax_pressed = False
    button_ready = False
    located_target = False
    counts = None
    if dlg is not None:
        iface = QAccessible.queryAccessibleInterface(dlg.item_table)
        if iface is not None:
            counts = (iface.role().name, iface.childCount())
        pick_iface = _ax_row_pick(dlg, "选择第 1 行明细")
        tree = QAccessible.queryAccessibleInterface(dlg)
        if tree is not None:
            for i in range(tree.childCount()):
                ch = tree.child(i)
                try:
                    nm = ch.text(QAccessible.Text.Name)
                except Exception:
                    continue
                if nm.startswith("选择第 ") and nm.endswith(" 行明细"):
                    ax_names.append(nm)
        if _ax_press(pick_iface):
            wait_until(lambda: dlg.item_table.currentRow() == 0, timeout=3)
            ax_pressed = dlg.item_table.currentRow() == 0
        button_ready = dlg.btn_reveal.isEnabled()
        if ax_pressed:
            captured = []
            original_reveal = main_mod._reveal_in_file_manager
            main_mod._reveal_in_file_manager = (
                lambda path: (captured.append(path), "")[1])
            try:
                dlg._on_reveal()
            finally:
                main_mod._reveal_in_file_manager = original_reveal
            located_target = bool(captured and os.path.abspath(captured[-1])
                                  == os.path.abspath(target)
                                  and sha256(captured[-1]) == sha256(target))

    if dlg is not None:
        dlg.accept()
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    window.on_undo()
    undone = wait_until(lambda: (not window._working)
                        and not os.path.exists(target)
                        and os.path.exists(source), timeout=20)
    window.on_history()
    process(0.3)
    dlg2 = window._history_dialog
    located_source = False
    ax_pressed_after_undo = False
    if dlg2 is not None and dlg2.item_table.rowCount():
        pick2 = _ax_row_pick(dlg2, "选择第 1 行明细")
        if _ax_press(pick2):
            wait_until(lambda: dlg2.item_table.currentRow() == 0, timeout=3)
            ax_pressed_after_undo = dlg2.item_table.currentRow() == 0
        if ax_pressed_after_undo:
            captured2 = []
            original_reveal = main_mod._reveal_in_file_manager
            main_mod._reveal_in_file_manager = (
                lambda path: (captured2.append(path), "")[1])
            try:
                dlg2._on_reveal()
            finally:
                main_mod._reveal_in_file_manager = original_reveal
            located_source = bool(captured2 and os.path.abspath(captured2[-1])
                                  == os.path.abspath(source)
                                  and sha256(source) == source_sha)
        dlg2.accept()
    window._closing = True
    window.hide()
    window.deleteLater()
    process(0.05)

    rec("S411", "辅助功能可按下「选择第 N 行明细」；进程存活，定位目标/原路径",
        no_selection and wrappers_kept and button_ready and ax_pressed
        and located_target and undone and ax_pressed_after_undo
        and located_source,
        f"明细行={rows} NoSelection={no_selection} 保留 item wrapper={wrappers_kept}\n"
        f"辅助按钮={ax_names} 辅助接口计数={counts}\n"
        f"整理后辅助按下={ax_pressed} 定位按钮可点={button_ready}\n"
        f"整理后定位目标={located_target} 撤销={undone} "
        f"撤销后辅助按下={ax_pressed_after_undo} 撤销后定位原路径={located_source}")


def _ax_collect(iface, out, depth=0):
    if iface is None or depth > 6:
        return
    try:
        out.append((iface.role().name,
                    iface.text(QAccessible.Text.Name)))
    except Exception:
        return
    for i in range(iface.childCount()):
        _ax_collect(iface.child(i), out, depth + 1)


def test_history_row_picker_clipped_to_viewport():
    """S412：明细滚出视口的行按钮必须隐藏，不能挡住刷新/搜索/上一页。"""
    root = fresh("history_picker_clip")
    for i in range(85):
        write(root, f"下载/课程{i:02d}.txt", f"高等数学 第{i}章\n")
    _sr, out = run_plan(root)
    plan = out["plan"]
    for item in plan.items:
        item.selected = True
    execute_plan(plan, Storage(root))

    window = main_mod.MainWindow()
    window.show()
    process(0.2)
    window._open_recent_space(root)
    window.on_history()
    process(0.5)
    dlg = window._history_dialog
    dlg.resize(1080, 720)
    dlg.activateWindow()
    dlg.raise_()
    process(0.3)

    refresh_hits = []
    try:
        dlg.btn_refresh.clicked.disconnect()
    except Exception:
        pass
    dlg.btn_refresh.clicked.connect(
        lambda: (refresh_hits.append(1), dlg.refresh_all()))

    def hit(widget):
        return QApplication.widgetAt(widget.mapToGlobal(widget.rect().center()))

    def clipped_bad():
        vp = dlg.item_table.viewport()
        vp_tl = vp.mapToGlobal(QPoint(0, 0))
        vp_br = vp.mapToGlobal(QPoint(vp.width() - 1, vp.height() - 1))
        shown = 0
        bad = []
        for r, btn in enumerate(dlg._row_pickers):
            if not btn.isVisible():
                continue
            shown += 1
            tl = btn.mapToGlobal(QPoint(0, 0))
            br = btn.mapToGlobal(QPoint(btn.width() - 1, btn.height() - 1))
            inside = (vp_tl.x() <= tl.x() and vp_tl.y() <= tl.y()
                      and br.x() <= vp_br.x() and br.y() <= vp_br.y())
            if not inside:
                bad.append(r)
        return shown, bad

    bar = dlg.item_table.verticalScrollBar()
    positions = [("顶部", bar.minimum()),
                 ("中段", (bar.minimum() + bar.maximum()) // 2),
                 ("底部", bar.maximum())]
    report = []
    ok = True
    for label, value in positions:
        bar.setValue(value)
        process(0.2)
        shown, bad = clipped_bad()
        hit_refresh = hit(dlg.btn_refresh) is dlg.btn_refresh
        hit_search = hit(dlg.search_edit) is dlg.search_edit
        hit_prev = hit(dlg.btn_prev) is dlg.btn_prev
        before_hits = len(refresh_hits)
        QTest.mouseClick(dlg.btn_refresh, Qt.LeftButton)
        process(0.2)
        refreshed = len(refresh_hits) == before_hits + 1
        row_after_refresh = dlg.item_table.currentRow()
        QTest.mouseClick(dlg.search_edit, Qt.LeftButton)
        process(0.05)
        search_focus = dlg.focusWidget() is dlg.search_edit
        row_after_search = dlg.item_table.currentRow()
        QTest.mouseClick(dlg.btn_prev, Qt.LeftButton)
        process(0.05)
        row_after_prev = dlg.item_table.currentRow()
        step_ok = (shown > 0 and not bad and hit_refresh and hit_search
                   and hit_prev and refreshed
                   and row_after_refresh == -1 and search_focus
                   and row_after_search == -1 and row_after_prev == -1)
        ok = ok and step_ok
        report.append(
            f"{label}: 可见按钮={shown} 越界={bad} 命中刷新={hit_refresh} "
            f"命中搜索={hit_search} 命中上一页={hit_prev} 刷新={refreshed} "
            f"行={row_after_refresh}/{row_after_search}/{row_after_prev} "
            f"搜索聚焦={search_focus}")

    # 窄窗口同样不能盖住其他控件
    dlg.resize(820, 490)
    process(0.3)
    bar.setValue((bar.minimum() + bar.maximum()) // 2)
    process(0.2)
    shown_n, bad_n = clipped_bad()
    narrow_ok = (shown_n > 0 and not bad_n
                 and hit(dlg.btn_refresh) is dlg.btn_refresh
                 and hit(dlg.search_edit) is dlg.search_edit
                 and hit(dlg.btn_prev) is dlg.btn_prev)
    report.append(f"820×490: 可见按钮={shown_n} 越界={bad_n} "
                  f"命中刷新={hit(dlg.btn_refresh) is dlg.btn_refresh} "
                  f"命中搜索={hit(dlg.search_edit) is dlg.search_edit} "
                  f"命中上一页={hit(dlg.btn_prev) is dlg.btn_prev}")
    ok = ok and narrow_ok

    # 滚到中段后，只对仍可见的那一行做辅助功能 Press
    dlg.resize(1080, 720)
    process(0.2)
    bar.setValue((bar.minimum() + bar.maximum()) // 2)
    process(0.2)
    visible_rows = [r for r, btn in enumerate(dlg._row_pickers)
                    if btn.isVisible()]
    target_row = visible_rows[len(visible_rows) // 2] if visible_rows else -1
    ax_ok = False
    if target_row >= 0:
        pick = _ax_row_pick(dlg, f"选择第 {target_row + 1} 行明细")
        pressed = _ax_press(pick)
        wait_until(lambda: dlg.item_table.currentRow() == target_row,
                   timeout=3)
        ax_ok = (pressed and dlg.item_table.currentRow() == target_row
                 and dlg.btn_reveal.isEnabled()
                 and dlg._row_pickers[target_row].isVisible())
    report.append(f"中段辅助 Press 行={target_row} 成功={ax_ok} "
                  f"按钮可见={target_row >= 0 and dlg._row_pickers[target_row].isVisible()}")

    dlg.accept()
    window._closing = True
    window.hide()
    window.deleteLater()
    process(0.1)
    rec("S412", "明细滚出视口的行按钮隐藏，刷新/搜索/上一页不被遮挡",
        ok and ax_ok,
        "\n".join(report))


def test_recent_spaces_dialog_accessibility():
    """S413：最近空间不用 item view 选择，辅助功能/键盘选中不崩溃。"""
    from daily_entry_dialogs import RecentSpacesDialog
    import recent_spaces

    first = fresh("recent_first")
    write(first, "笔记.txt", "a\n")
    recent_spaces.touch(first)
    second = fresh("recent_second")
    write(second, "笔记.txt", "b\n")
    recent_spaces.touch(second)

    opened = []
    dlg = RecentSpacesDialog(lambda p: (opened.append(p), True)[1], None)
    dlg.show()
    process(0.3)
    no_item_view = not dlg.findChildren(QAbstractItemView)
    rows = dlg.row_count()
    radios = dlg.findChildren(QRadioButton)
    names = [r.accessibleName() for r in radios]
    radioless = bool(radios) and all(n.startswith("选择空间：") for n in names)

    tree = []
    iface = QAccessible.queryAccessibleInterface(dlg)
    _ax_collect(iface, tree)
    ax_radio = next((nm for role, nm in tree
                     if role == "RadioButton" and nm.startswith("选择空间：")),
                    None)

    ax_pick = None
    # 递归按名称找辅助功能接口
    def find_iface(node, prefix, depth=0):
        if node is None or depth > 6:
            return None
        try:
            if node.text(QAccessible.Text.Name).startswith(prefix):
                return node
        except Exception:
            pass
        for i in range(node.childCount()):
            found = find_iface(node.child(i), prefix, depth + 1)
            if found is not None:
                return found
        return None

    ax_pick = find_iface(iface, "选择空间：")
    ax_pressed = _ax_press(ax_pick)
    process(0.2)
    selected = dlg._selected()
    selected_path = selected.path if selected is not None else ""
    open_ready = dlg.btn_open.isEnabled()
    QTest.mouseClick(dlg.btn_open, Qt.LeftButton)
    process(0.2)
    opened_ok = bool(opened) and opened[-1] == selected_path
    closed = not dlg.isVisible()

    dlg2 = RecentSpacesDialog(lambda p: True, None)
    dlg2.show()
    process(0.2)
    radios2 = dlg2._row_group.buttons() if dlg2._row_group else []
    keyboard_ok = False
    if len(radios2) >= 2:
        radios2[0].setFocus()
        process(0.05)
        QTest.keyClick(radios2[0], Qt.Key_Down)
        process(0.1)
        keyboard_ok = dlg2._row_group.checkedId() == 1
    dlg2.accept()
    process(0.1)

    rec("S413", "最近空间不用 item view；辅助功能/键盘选中并打开不崩溃",
        no_item_view and rows >= 2 and radioless and ax_radio is not None
        and ax_pressed and bool(selected_path) and open_ready
        and opened_ok and closed and keyboard_ok,
        f"行数={rows} 无 item view={no_item_view} 单选按钮={names}\n"
        f"辅助树单选={ax_radio!r} 辅助按下={ax_pressed} "
        f"选中={selected_path} 打开按钮可点={open_ready}\n"
        f"打开回调={opened_ok} 窗口已关={closed} 键盘下移={keyboard_ok}")


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
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    test_scan_progress_and_seq()
    test_cache_hits_and_invalidation()
    test_corrupt_cache_and_execution_validation()
    test_ooxml_parsing_and_bad_files()
    test_preview_filter_and_selection()
    test_history_reveal_current_path()
    test_cache_save_boundary()
    test_stale_cache_same_size_mtime()
    test_open_space_history_undo_ready()
    test_reveal_stays_inside_space()
    test_history_selection_accessibility()
    test_history_row_picker_clipped_to_viewport()
    test_recent_spaces_dialog_accessibility()
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
