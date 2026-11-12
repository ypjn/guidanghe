"""退出清理最小复现：主窗口 + 历史窗口 + 规则草稿的组合退出。

背景：V1.5 专项曾出现“断言 15/15、脚本自报 exit_code=0，但进程在 Qt
清理阶段 SIGSEGV、操作系统返回 139”。崩溃栈为
`destroyQCoreApplication → visitAllPyObjects → QPushButtonWrapper 析构`，
属于解释器退出阶段的控件析构顺序问题。

本脚本只构造该组合并退出，不打印业务断言，便于反复运行观察真实退出码：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/exit_cleanup_check.py
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/exit_cleanup_check.py --teardown

`--teardown` 会在退出前显式销毁顶层控件（与生产入口一致），
用于对比“交给 Qt 清理”和“确定性清理”的差异。

所有文件操作只在独立临时目录执行。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox

from classification_config import default_config, save_config
from config_dialog import ClassificationConfigDialog
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory
from storage import Storage
from executor import execute_plan
from main import MainWindow, HistoryDialog
from plan_patch import RuleDraftDialog, OverrideParams

BASE = "/tmp/_exit_cleanup_check"


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))


def pump(app, seconds=0.2):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teardown", action="store_true")
    args = ap.parse_args()

    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    app = QApplication.instance() or QApplication([])
    root = os.path.join(BASE, "root")
    mk(root, "大二上/数据结构/作业/a.pdf", "a")
    mk(root, "大二上/数据结构/实验/b.pdf", "b")

    cfg = default_config()
    save_config(root, cfg)
    sr = scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                      compute_duplicates=True)
    result = execute_plan(plan, Storage(root))

    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        pump(app)
        hist = HistoryDialog(w)
        hist._selected_batch_id = result.batch_id
        hist.load_items(force=True)
        hist.show()
        pump(app)
        conf = ClassificationConfigDialog(
            root=root, config=cfg, using_defaults=False,
            base_config=cfg, save_callback=None)
        pump(app)
        draft = RuleDraftDialog(
            mode=MODE_BY_DIRECTION_SEMESTER, plan=plan,
            source_rel="大二上/数据结构/作业/a.pdf",
            params=OverrideParams(direction="项目", semester="", category="退出"),
            scan_result=sr, config=cfg, save_callback=None)
        pump(app)
    finally:
        QMessageBox.question = real_q

    conf.reject()
    draft.dlg.reject()
    hist.reject()
    w.close()
    pump(app)

    if args.teardown:
        from main import shutdown_widgets
        shutdown_widgets()
        print("已显式销毁顶层控件")
    print("组合构造与关闭完成，准备退出")


if __name__ == "__main__":
    main()
