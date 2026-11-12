"""
归档盒 V1.1 — UI 状态机验证（offscreen Qt）

针对用户反馈的四处界面缺陷，真实触发后台线程 / 信号 / 关闭流程，
并断言 QMainWindow 的按钮 enabled、visible、字段值。

运行：
  QT_QPA_PLATFORM=offscreen \
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/test_v11_ui_state.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

import main as main_mod
from main import MainWindow, PlanWorker
from planner import generate_plan as real_generate_plan, OrganizePlan
from scanner import scan_directory
from storage import Storage

BASE = "/tmp/_v11_ui_verify"
RESULTS: list[dict] = []
DIALOGS: list[tuple] = []

# ---- 可控的计划生成：用 Event 卡住后台线程，模拟大目录 SHA 计算耗时 ----
GATE = threading.Event()
REAL_GEN = real_generate_plan


def gated_generate_plan(scan_result, mode, rules, cancel_event=None, progress_cb=None):
    total = max(1, len(scan_result.files))
    if progress_cb:
        first = scan_result.files[0].rel_path if scan_result.files else ""
        progress_cb(1, total, first)
    GATE.wait(timeout=30)          # 测试放行后才继续
    return REAL_GEN(scan_result, mode, rules,
                    cancel_event=cancel_event, progress_cb=progress_cb)


def boom_generate_plan(*args, **kwargs):
    raise RuntimeError("模拟计划生成失败：无法读取文件")


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status, "evidence": evidence})
    icon = {"通过": "PASS", "未通过": "FAIL"}[status]
    print(f"[{icon}] {tid} {title}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"        {line}")


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, *args, content=None):
    """创建文件：mk(root, "dir", "file.ext") 或 mk(root, "dir", "file.ext", "内容")。"""
    if not args:
        raise ValueError("mk 至少需要路径")
    if content is None and len(args) > 1:
        last = args[-1]
        if isinstance(last, (bytes, bytearray)):
            content, args = last, args[:-1]
        elif isinstance(last, str) and len(last) >= 20 and "." in args[-2]:
            content, args = last, args[:-1]
    if content is None:
        content = "x"
    full = os.path.join(root, *args)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return full


def pump(app, seconds=0.2):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def wait_until(app, cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


def make_window(root, files=None):
    w = MainWindow()
    w.path_edit.setText(root)
    w.scan_result = scan_directory(root, detect_project_roots=False) if files is None else files
    w.show()                       # offscreen 下也需 show，子控件的 isVisible 才准确
    QApplication.instance().processEvents()
    return w


def cleanup_window(w, app, timeout=20):
    for wk in w._background_workers():
        if wk.isRunning():
            if hasattr(wk, "request_cancel"):
                try:
                    wk.request_cancel()
                except Exception:
                    pass
            wk.wait(int(timeout * 1000))
    if w._storage is not None:
        try:
            w._storage.close()
        except Exception:
            pass
        w._storage = None
    w.close()
    w.deleteLater()
    app.processEvents()


def main():
    # 所有弹窗在 offscreen 下自动关闭，并记录调用
    def _record(kind):
        def _fn(*args, **kwargs):
            DIALOGS.append((kind, args[2] if len(args) > 2 else ""))
            return QMessageBox.Ok
        return staticmethod(_fn)

    QMessageBox.information = _record("information")
    QMessageBox.warning = _record("warning")
    QMessageBox.critical = _record("critical")
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)

    app = QApplication([])
    main_mod.generate_plan = gated_generate_plan

    # 夹具：每场景 160 个文件，保证计划生成既有真实内容又不会太久
    t1 = fresh("scan_vs_plan")
    for i in range(160):
        mk(t1, "课程A", f"doc_{i:03d}.docx", "v" * 2048)
        mk(t1, "课程B", f"doc_{i:03d}.pdf", "p" * 2048)

    t2 = fresh("cancel_scenario")
    for i in range(160):
        mk(t2, "大一下", "高数", f"ch_{i:03d}.pdf", "x" * 2048)
        mk(t2, "大一下", "英语", f"paper_{i:03d}.docx", "y" * 2048)

    windows = []

    # ========== P1: 扫描线程结束的清理，不能覆盖仍在运行的计划线程状态 ==========
    GATE.clear()
    w1 = make_window(t1)
    windows.append(w1)
    w1._set_working(True, "扫描中……")
    w1._generate_and_show_plan()

    started = wait_until(app, lambda: w1._plan_working and w1._plan_worker is not None
                         and w1._plan_worker.isRunning(), timeout=5)
    # 模拟 ScanWorker 结束：生产代码里由 QThread.finished 触发
    w1._on_scan_worker_finished()
    pump(app, 0.05)

    p1_plan_working = w1._plan_working
    p1_cancel_visible = w1.btn_cancel.isVisible()
    p1_cancel_enabled = w1.btn_cancel.isEnabled()
    p1_browse_disabled = not w1.btn_browse.isEnabled()
    p1_scan_disabled = not w1.btn_scan.isEnabled()
    p1_progress_visible = w1.progress.isVisible()
    p1_progress_max = w1.progress.maximum()

    # 放行，等计划真正完成
    GATE.set()
    finished = wait_until(app, lambda: not w1._plan_working, timeout=30)
    pump(app, 0.3)

    p1_after_valid = w1._plan_valid and w1.current_plan is not None
    p1_after_cancel_hidden = not w1.btn_cancel.isVisible()
    p1_after_execute_enabled = w1.btn_execute.isEnabled()
    p1_after_browse_enabled = w1.btn_browse.isEnabled()
    p1_after_scan_enabled = w1.btn_scan.isEnabled()

    p1_ok = all([
        started, p1_plan_working, p1_cancel_visible, p1_cancel_enabled,
        p1_browse_disabled, p1_scan_disabled, p1_progress_visible,
        p1_progress_max > 0,
        finished, p1_after_valid, p1_after_cancel_hidden,
        p1_after_execute_enabled, p1_after_browse_enabled, p1_after_scan_enabled,
    ])
    rec(
        "P1",
        "扫描结束清理不覆盖计划线程：取消按钮保持、浏览/扫描禁用；完成后恢复",
        "通过" if p1_ok else "未通过",
        (
            f"计划线程已启动={started}\n"
            f"模拟扫描结束清理后：\n"
            f"  _plan_working={p1_plan_working}（应 True）\n"
            f"  取消按钮 visible/enabled={p1_cancel_visible}/{p1_cancel_enabled}\n"
            f"  浏览/扫描禁用={p1_browse_disabled}/{p1_scan_disabled}（应 True/True）\n"
            f"  进度条 visible/max={p1_progress_visible}/{p1_progress_max}\n"
            f"计划完成后：valid={p1_after_valid} cancel_hidden={p1_after_cancel_hidden} "
            f"执行/浏览/扫描 enabled={p1_after_execute_enabled}/{p1_after_browse_enabled}/{p1_after_scan_enabled}"
        )
    )
    cleanup_window(w1, app)

    # ========== P2: 取消重新生成后，旧计划必须清空且不可执行 ==========
    GATE.clear()
    w2 = make_window(t2)
    windows.append(w2)
    w2._storage = Storage(t2)
    old_plan = REAL_GEN(w2.scan_result, w2.mode, w2.rules)
    w2.current_plan = old_plan
    w2._plan_valid = True
    w2._populate_table(old_plan)
    w2._update_button_states()
    p2_pre_execute = w2.btn_execute.isEnabled()

    GATE.clear()
    w2.on_regenerate_plan()
    started2 = wait_until(app, lambda: w2._plan_working and w2._plan_worker is not None
                          and w2._plan_worker.isRunning(), timeout=5)
    # 参数变化/重新生成后旧计划立即失效
    p2_invalid_on_start = not w2._plan_valid and not w2.btn_execute.isEnabled()

    # 用户点取消按钮（真实走 on_cancel_execute -> _cancel_plan_generation）
    w2.btn_cancel.click()
    pump(app, 0.05)
    p2_cancel_requested = w2._plan_cancel_requested
    p2_cancel_btn_disabled = not w2.btn_cancel.isEnabled()

    GATE.set()
    stopped = wait_until(app, lambda: not w2._plan_working
                         and (w2._plan_worker is None or not w2._plan_worker.isRunning()),
                         timeout=30)
    pump(app, 0.3)

    p2_current_none = w2.current_plan is None
    p2_valid_false = not w2._plan_valid
    p2_execute_disabled = not w2.btn_execute.isEnabled()
    p2_label_visible = w2.invalid_label.isVisible()
    p2_cancel_hidden = not w2.btn_cancel.isVisible()
    p2_regenerate_enabled = w2.btn_regenerate.isEnabled()

    p2_ok = all([
        p2_pre_execute, started2, p2_invalid_on_start, p2_cancel_requested,
        p2_cancel_btn_disabled, stopped, p2_current_none, p2_valid_false,
        p2_execute_disabled, p2_label_visible, p2_cancel_hidden,
        p2_regenerate_enabled,
    ])
    rec(
        "P2",
        "取消重新生成：旧计划清空、执行禁用、取消按钮恢复隐藏",
        "通过" if p2_ok else "未通过",
        (
            f"取消前旧计划可执行={p2_pre_execute}\n"
            f"二次生成已启动={started2}\n"
            f"启动瞬间旧计划失效={p2_invalid_on_start}\n"
            f"取消请求标记={p2_cancel_requested}，取消按钮禁点={p2_cancel_btn_disabled}\n"
            f"线程已停={stopped}\n"
            f"取消后 current_plan is None={p2_current_none}\n"
            f"  _plan_valid={w2._plan_valid}（应 False）\n"
            f"  执行按钮 enabled={w2.btn_execute.isEnabled()}（应 False）\n"
            f"  无效标签 visible={p2_label_visible}\n"
            f"  取消按钮 visible={w2.btn_cancel.isVisible()}（应 False）\n"
            f"  重新预览 enabled={p2_regenerate_enabled}"
        )
    )
    cleanup_window(w2, app)

    # ========== P3: 过期 seq 的信号必须全部丢弃 ==========
    w3 = make_window(t1)
    windows.append(w3)
    w3._storage = Storage(t1)
    plan_current = REAL_GEN(w3.scan_result, w3.mode, w3.rules)
    plan_stale = REAL_GEN(w3.scan_result, w3.mode, w3.rules)
    w3.current_plan = plan_current
    w3._plan_valid = True

    # 当前任务 seq=102，上一个任务 seq=101（self._plan_worker 指向 102）
    stale = PlanWorker(101, w3.scan_result, w3.mode, w3.rules, parent=w3)
    current = PlanWorker(102, w3.scan_result, w3.mode, w3.rules, parent=w3)
    w3._plan_worker = current
    w3._current_plan_seq = 102
    w3._plan_seq_counter = 102
    w3._plan_working = True
    w3._refresh_busy_ui()   # 与 _generate_and_show_plan 相同的忙碌态刷新

    for wk in (stale, current):
        wk.progress.connect(w3._on_plan_progress)
        wk.finished_ok.connect(w3._on_plan_done)
        wk.cancelled.connect(w3._on_plan_cancelled)
        wk.failed.connect(w3._on_plan_failed)
        wk.finished_seq.connect(w3._on_plan_worker_finished)

    plan_current_id = id(plan_current)
    dialogs_before = len(DIALOGS)
    status_before = w3.statusBar().currentMessage()

    stale.finished_ok.emit(101, plan_stale)
    stale.progress.emit(101, 99, 100, "ignored.txt")
    stale.failed.emit(101, "旧任务失败（不应弹窗）")
    stale.cancelled.emit(101)
    stale.finished_seq.emit(101)
    pump(app, 0.05)

    p3_not_overwritten = id(w3.current_plan) == plan_current_id
    p3_still_valid = w3._plan_valid
    p3_no_dialog = len(DIALOGS) == dialogs_before
    p3_status_untouched = w3.statusBar().currentMessage() == status_before
    p3_plan_still_working = w3._plan_working
    p3_cancel_still_visible = w3.btn_cancel.isVisible()

    # 当前 seq 的成功信号应被接受
    current.finished_ok.emit(102, plan_stale)
    pump(app, 0.05)
    p3_current_accepted = w3._plan_valid and w3.current_plan is not None
    current.finished_seq.emit(102)
    pump(app, 0.05)
    p3_done_working_false = not w3._plan_working
    p3_execute_enabled = w3.btn_execute.isEnabled()

    p3_ok = all([
        p3_not_overwritten, p3_still_valid, p3_no_dialog,
        p3_status_untouched, p3_plan_still_working, p3_cancel_still_visible,
        p3_current_accepted, p3_done_working_false, p3_execute_enabled,
    ])
    rec(
        "P3",
        "旧 seq 的 finished/progress/failed/cancelled/finished_seq 全部被丢弃",
        "通过" if p3_ok else "未通过",
        (
            f"伪造 seq=101 信号后：\n"
            f"  计划未被覆盖={p3_not_overwritten}\n"
            f"  _plan_valid 仍 True={p3_still_valid}\n"
            f"  未弹窗={p3_no_dialog}\n"
            f"  状态栏未被进度污染={p3_status_untouched}\n"
            f"  _plan_working 仍 True={p3_plan_still_working}\n"
            f"  取消按钮仍可见={p3_cancel_still_visible}\n"
            f"seq=102 信号：计划被接受={p3_current_accepted} "
            f"结束后 _plan_working=False={p3_done_working_false} 执行可用={p3_execute_enabled}"
        )
    )
    cleanup_window(w3, app)

    # ========== P4: 计划生成抛出异常 -> 清空计划、禁用执行、提示、可重试 ==========
    w4 = make_window(t1)
    windows.append(w4)
    w4._storage = Storage(t1)
    main_mod.generate_plan = boom_generate_plan
    dialogs_before = len(DIALOGS)
    w4._generate_and_show_plan()
    wait_until(app, lambda: w4._plan_worker is not None and not w4._plan_worker.isRunning()
               and not w4._plan_working, timeout=10)
    pump(app, 0.2)

    p4_current_none = w4.current_plan is None
    p4_valid_false = not w4._plan_valid
    p4_execute_disabled = not w4.btn_execute.isEnabled()
    p4_label_visible = w4.invalid_label.isVisible()
    p4_cancel_hidden = not w4.btn_cancel.isVisible()
    p4_regenerate_enabled = w4.btn_regenerate.isEnabled()
    p4_dialog_shown = any(k == "critical" and "模拟计划生成失败" in str(v)
                          for k, v in DIALOGS[dialogs_before:])

    p4_ok = all([
        p4_current_none, p4_valid_false, p4_execute_disabled, p4_label_visible,
        p4_cancel_hidden, p4_regenerate_enabled, p4_dialog_shown,
    ])
    rec(
        "P4",
        "计划生成失败：清空计划、禁用执行、显示错误、允许重新生成",
        "通过" if p4_ok else "未通过",
        (
            f"current_plan is None={p4_current_none}\n"
            f"_plan_valid={w4._plan_valid}\n"
            f"执行按钮 enabled={w4.btn_execute.isEnabled()}（应 False）\n"
            f"无效标签 visible={p4_label_visible}\n"
            f"取消按钮 visible={w4.btn_cancel.isVisible()}（应 False）\n"
            f"重新预览 enabled={p4_regenerate_enabled}\n"
            f"错误弹窗已触发={p4_dialog_shown}"
        )
    )
    main_mod.generate_plan = gated_generate_plan
    cleanup_window(w4, app)

    # ========== P5: 关闭窗口时线程未结束 -> 窗口与数据库保持存活 ==========
    GATE.clear()
    t5 = fresh("close_scenario")
    for i in range(160):
        mk(t5, "大一下", "高数", f"ch_{i:03d}.pdf", "x" * 2048)
    w5 = make_window(t5)
    windows.append(w5)
    w5._storage = Storage(t5)
    w5.show()
    pump(app, 0.05)
    w5._generate_and_show_plan()
    wait_until(app, lambda: w5._plan_working and w5._plan_worker is not None
               and w5._plan_worker.isRunning(), timeout=5)

    ev = QCloseEvent()
    w5.closeEvent(ev)
    pump(app, 0.05)

    p5_ignored = not ev.isAccepted()
    p5_visible = w5.isVisible()
    p5_storage_alive = w5._storage is not None
    # 数据库仍可正常读写（未被关闭）
    try:
        w5._storage.get_pending_batch()
        p5_db_usable = True
    except Exception:
        p5_db_usable = False

    # 放行后台任务，等待自动关闭
    GATE.set()
    wait_until(app, lambda: not w5._plan_working, timeout=30)
    closed = wait_until(app, lambda: not w5.isVisible() and w5._storage is None, timeout=10)

    p5_ok = all([p5_ignored, p5_visible, p5_storage_alive, p5_db_usable, closed])
    rec(
        "P5",
        "关闭请求被拒绝，窗口与数据库保持到线程结束，随后自动关闭",
        "通过" if p5_ok else "未通过",
        (
            f"closeEvent accepted={ev.isAccepted()}（应 False）\n"
            f"窗口仍可见={p5_visible}\n"
            f"_storage 仍存活={p5_storage_alive}，数据库可读写={p5_db_usable}\n"
            f"线程结束后窗口自动关闭且数据库已正确关闭={closed}"
        )
    )

    # ========== P6: 真实扫描 → 自动生成计划的正常路径回归 ==========
    GATE.set()
    t6 = fresh("normal_flow")
    for i in range(40):
        mk(t6, "大一下", "高数", f"ch_{i:03d}.pdf", "x" * 200)
        mk(t6, "大一下", "英语", f"paper_{i:03d}.docx", "y" * 200)
    w6 = make_window(t6, files=None)
    w6.scan_result = None
    windows.append(w6)
    w6.btn_scan.setEnabled(True)
    w6.btn_scan.click()
    scan_ok = wait_until(app, lambda: not w6._working and w6.scan_result is not None, timeout=10)
    plan_ok = wait_until(app, lambda: not w6._plan_working and w6._plan_valid, timeout=30)
    pump(app, 0.2)

    p6_execute_enabled = w6.btn_execute.isEnabled()
    p6_cancel_hidden = not w6.btn_cancel.isVisible()
    p6_browse_enabled = w6.btn_browse.isEnabled()
    p6_valid = w6._plan_valid and w6.current_plan is not None
    p6_ok = scan_ok and plan_ok and p6_valid and p6_execute_enabled \
        and p6_cancel_hidden and p6_browse_enabled
    rec(
        "P6",
        "正常流程回归：扫描 → 自动生成计划 → 按钮状态正确",
        "通过" if p6_ok else "未通过",
        (
            f"扫描完成={scan_ok}，计划完成={plan_ok}，计划有效={p6_valid}\n"
            f"执行 enabled={p6_execute_enabled}，取消按钮隐藏={p6_cancel_hidden}，"
            f"浏览 enabled={p6_browse_enabled}"
        )
    )
    cleanup_window(w6, app)

    # 清理其余窗口
    for win in windows:
        try:
            cleanup_window(win, app)
        except Exception:
            pass

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    total = len(RESULTS)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_v11_ui_state.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print()
    print(f"合计 {total} 项：通过 {passed} / 未通过 {total - passed}")
    print(f"结果已保存: {out}")

    app.quit()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
