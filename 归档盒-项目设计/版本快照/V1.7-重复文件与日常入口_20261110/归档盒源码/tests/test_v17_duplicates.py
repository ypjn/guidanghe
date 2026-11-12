"""
归档盒 V1.7 — 重复文件查看 专项验证（offscreen）

D01 同内容不同名称 → 内容重复组
D02 同名不同内容 → 同名不同内容组，不误判为内容重复
D03 三份以上副本 → 组内份数正确
D04 缺失/不可读文件 → 核对失败，不产生假重复
D05 扫描后文件变化（同大小改内容）→ 旧指纹作废并重新核对
D06 取消核对 → 不生成结论
D07 窗口分组、筛选与 CSV/JSON 导出统计一致
D08 打开/定位/复制路径不改变文件内容与副本数量
D09 主窗口：后台核对、缓存复用、切换目录后失效

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v17_duplicates.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from duplicate_report import (
    DupEntry, assemble_report, entries_from_plan, export_csv_text,
    export_json_text, verify_entries, STATUS_VERIFIED, STATUS_FAILED,
)
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from classification_config import default_config
from scanner import scan_directory
import duplicate_dialog as DD
from duplicate_dialog import DuplicateDialog
from main import MainWindow

RESULTS = []
BASE = "/tmp/_v17_duplicates"


def rec(tid, title, ok, evidence=""):
    RESULTS.append({"id": tid, "status": "通过" if ok else "未通过",
                    "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def wait_until(app, cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(cond())


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))
    return p


def entry_for(root, rel):
    p = os.path.join(root, rel)
    st = os.stat(p)
    return DupEntry(rel_path=rel, abs_path=p,
                    filename=os.path.basename(rel),
                    size=st.st_size, size_display="",
                    expected_mtime_ns=st.st_mtime_ns)


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    app = QApplication.instance() or QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # D01 同内容不同名称
    try:
        root = fresh("d01")
        mk(root, "a/one.txt", "SAME-CONTENT")
        mk(root, "b/two.md", "SAME-CONTENT")
        entries = [entry_for(root, "a/one.txt"), entry_for(root, "b/two.md")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="测试", source="scan")
        ok = (rep.content_groups == 1 and rep.content_files == 2
              and rep.same_name_groups == 0)
        rec("D01", "同内容不同名称归入同一内容重复组", ok,
            f"内容组={rep.content_groups} 文件份数={rep.content_files}")
    except Exception as e:
        rec("D01", "同内容不同名称", False, f"异常：{e}")

    # D02 同名不同内容
    try:
        root = fresh("d02")
        mk(root, "a/x.txt", "AAAA")
        mk(root, "b/x.txt", "BBBB")
        entries = [entry_for(root, "a/x.txt"), entry_for(root, "b/x.txt")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="测试", source="scan")
        kinds = [g.kind for g in rep.groups]
        ok = ("same_name_diff" in kinds and rep.content_groups == 0
              and rep.same_name_groups == 1 and rep.same_name_files == 2)
        rec("D02", "同名但内容不同归入「同名不同内容」，不误判为内容重复", ok,
            f"分组={[(g.kind, g.count) for g in rep.groups]}")
    except Exception as e:
        rec("D02", "同名不同内容", False, f"异常：{e}")

    # D03 三份以上副本
    try:
        root = fresh("d03")
        for rel in ("a/x.bin", "b/y.bin", "c/z.bin"):
            mk(root, rel, "THREE")
        entries = [entry_for(root, r) for r in
                   ("a/x.bin", "b/y.bin", "c/z.bin")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="测试", source="scan")
        ok = (rep.content_groups == 1 and rep.content_files == 3
              and rep.groups[0].count == 3)
        rec("D03", "三份以上副本的组内份数正确", ok,
            f"内容组={rep.content_groups} 份数={rep.content_files}")
    except Exception as e:
        rec("D03", "三份副本", False, f"异常：{e}")

    # D04 缺失/不可读不产生假重复
    try:
        root = fresh("d04")
        mk(root, "a/x.txt", "SAME")
        entries = [entry_for(root, "a/x.txt"),
                   DupEntry(rel_path="a/missing.txt",
                            abs_path=os.path.join(root, "a/missing.txt"),
                            filename="missing.txt", size=4, size_display="")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="测试", source="scan")
        ok = (rep.content_groups == 0 and rep.failed == 1
              and any(g.kind == "unverified" for g in rep.groups))
        rec("D04", "缺失/不可读文件标记核对失败，不被当作重复证据", ok,
            f"失败={rep.failed} 分组={[g.kind for g in rep.groups]}")
    except Exception as e:
        rec("D04", "缺失文件", False, f"异常：{e}")

    # D05 扫描后文件变化
    try:
        root = fresh("d05")
        p = mk(root, "a/x.txt", "AAAA")
        plan = build_plan(scan_directory(root, detect_project_roots=False),
                          MODE_BY_DIRECTION_SEMESTER, [],
                          config=default_config(),
                          compute_duplicates=True)
        entries = entries_from_plan(plan, None)
        entry = entries[0]
        old_sha = entry.sha256
        time.sleep(0.02)
        with open(p, "wb") as f:
            f.write(b"BBBB")     # 同大小、内容不同
        verify_entries(entries)
        ok = (entry.rechecked and entry.status == STATUS_VERIFIED
              and entry.sha256 and entry.sha256 != old_sha)
        rec("D05", "扫描后文件变化时旧指纹作废并重新核对", ok,
            f"重新核对={entry.rechecked} 状态={entry.status} 指纹变化="
            f"{entry.sha256 != old_sha}")
    except Exception as e:
        rec("D05", "文件变化", False, f"异常：{e}")

    # D06 取消
    try:
        import threading
        root = fresh("d06")
        mk(root, "a/big.bin", "x" * 1024)
        e = entry_for(root, "a/big.bin")
        cancel = threading.Event()
        cancel.set()
        ok = verify_entries([e], cancel_event=cancel) is False
        rec("D06", "取消核对时不生成结论", ok, f"返回值={ok}")
    except Exception as e:
        rec("D06", "取消", False, f"异常：{e}")

    # D07 窗口分组、筛选与导出统计
    try:
        root = fresh("d07")
        mk(root, "a/x.txt", "SAME")
        mk(root, "b/y.txt", "SAME")
        mk(root, "c/n.txt", "AAAA")
        mk(root, "d/n.txt", "BBBB")
        entries = [entry_for(root, r) for r in
                   ("a/x.txt", "b/y.txt", "c/n.txt", "d/n.txt")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="D07", source="scan")
        dlg = DuplicateDialog(root, "D07", None)
        dlg.show_report(rep)
        group_count = dlg.tree.topLevelItemCount()
        dlg.kind_filter.setCurrentIndex(1)      # 内容重复
        pump(app)
        visible_groups = sum(1 for i in range(dlg.tree.topLevelItemCount())
                             if not dlg.tree.topLevelItem(i).isHidden())

        class FakeFD:
            queue = []

            @staticmethod
            def getSaveFileName(*a, **k):
                return FakeFD.queue.pop(0)

        out_csv = os.path.join(root, "_dup.csv")
        out_json = os.path.join(root, "_dup.json")
        FakeFD.queue = [(out_csv, ""), (out_json, "")]
        real = DD.QFileDialog
        DD.QFileDialog = FakeFD
        try:
            dlg.on_export("csv")
            dlg.on_export("json")
        finally:
            DD.QFileDialog = real
        with open(out_csv, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        with open(out_json, encoding="utf-8") as f:
            data = json.load(f)
        expected_rows = 1 + sum(len(g.entries) for g in rep.groups)
        ok = (group_count == len(rep.groups)
              and visible_groups == 1
              and len(rows) == expected_rows
              and data["counts"]["content_groups"] == rep.content_groups
              and data["scope"] == "D07")
        rec("D07", "窗口分组、类型筛选与 CSV/JSON 导出统计一致", ok,
            f"分组={group_count} 筛选后内容组={visible_groups} "
            f"CSV行={len(rows)}/{expected_rows}")
        dlg.close()
    except Exception as e:
        rec("D07", "窗口与导出", False, f"异常：{e}")

    # D08 文件操作不改变内容
    try:
        root = fresh("d08")
        p = mk(root, "a/x.txt", "SAME")
        mk(root, "b/y.txt", "SAME")
        entries = [entry_for(root, "a/x.txt"), entry_for(root, "b/y.txt")]
        verify_entries(entries)
        rep = assemble_report(root, entries, scope_note="D08", source="scan")
        dlg = DuplicateDialog(root, "D08", None)
        dlg.show_report(rep)
        child = dlg.tree.topLevelItem(0).child(0)
        dlg.tree.setCurrentItem(child)
        before = open(p, "rb").read()
        mtime_before = os.stat(p).st_mtime_ns
        real_reveal = DD._reveal_in_file_manager
        real_open = DD.QDesktopServices.openUrl
        DD._reveal_in_file_manager = lambda path: ""
        DD.QDesktopServices.openUrl = staticmethod(lambda url: True)
        try:
            dlg.on_reveal()
            dlg.on_copy_path()
            dlg.on_open()
        finally:
            DD._reveal_in_file_manager = real_reveal
            DD.QDesktopServices.openUrl = real_open
        after = open(p, "rb").read()
        ok = (before == after and mtime_before == os.stat(p).st_mtime_ns
              and QApplication.clipboard().text() == p
              and len(files_under(root)) == 2)
        rec("D08", "打开/定位/复制路径不改变文件内容与副本数量", ok,
            f"内容不变={before == after} 副本数={len(files_under(root))}")
        dlg.close()
    except Exception as e:
        rec("D08", "文件操作", False, f"异常：{e}")

    # D09 主窗口后台核对与缓存
    try:
        root = fresh("d09")
        mk(root, "a/x.txt", "SAME")
        mk(root, "b/y.txt", "SAME")
        mk(root, "c/z.txt", "OTHER")
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        raw = scan_directory(root, detect_project_roots=False)
        w._full_scan_result = raw
        w.scan_result = raw
        w._scan_generation = 1
        plan = build_plan(raw, MODE_BY_DIRECTION_SEMESTER, [],
                          config=default_config(), compute_duplicates=True)
        w.current_plan = plan
        w._plan_valid = True
        w.on_show_duplicates()
        done = wait_until(
            app, lambda: (not w._working and w._duplicate_dialog is not None
                          and w._duplicate_dialog.report is not None))
        rep1 = w._duplicate_dialog.report if w._duplicate_dialog else None
        w._duplicate_dialog.close()
        pump(app)
        w.on_show_duplicates()
        pump(app)
        rep2 = w._duplicate_dialog.report if w._duplicate_dialog else None
        cached = (w._duplicate_worker is None and rep1 is not None
                  and rep2 is rep1)
        w._reset_for_new_dir(fresh("d09_new"))
        cleared = (w._duplicate_cache is None and w._duplicate_dialog is None)
        ok = (done and rep1 is not None and rep1.content_groups == 1
              and cached and cleared)
        rec("D09", "主窗口后台核对、缓存复用；切换目录后旧结果失效", ok,
            f"完成={done} 内容组={rep1.content_groups if rep1 else '-'} "
            f"缓存复用={cached} 切换后清理={cleared}")
        w.close()
        pump(app)
    except Exception as e:
        rec("D09", "主窗口集成", False, f"异常：{e}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 and RESULTS else 1
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_v17_duplicates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"suite": "test_v17_duplicates",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "counts": {"total": len(RESULTS), "pass": passed,
                              "fail": failed},
                   "exit_code": exit_code, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    try:
        from main import shutdown_widgets
        shutdown_widgets()
    except Exception:
        pass
    return exit_code


def files_under(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for n in files:
            out.append(os.path.join(base, n))
    return out


if __name__ == "__main__":
    sys.exit(main())
