"""
归档盒 V1.6 — 整理方案管理与预览增强 真实桌面检查（cocoa，截图）

V0 可获取真实屏幕（cocoa）
V1 主窗口预览：搜索/筛选/批量勾选入口与计数可见
V2 搜索筛选 + 可见批量勾选（截图）
V3 目标目录结构树（截图）
V4 整理方案库：内置方案、导出/应用入口（截图）
V5 应用方案到当前目录后旧计划失效（程序化应用 + 截图）

说明：程序化点击与截图，不等同真人手工观察；合成样本。
运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v16_scheme_preview.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

from classification_config import default_config, LongTermRule
import config_transfer as CT
import scheme_store as SS
from main import MainWindow
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory
from target_tree_dialog import TargetTreeDialog

BASE = os.path.join("/tmp", "_v16_desktop_check")
SHOT_DIR = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
            "验收截图_20261109_V16方案与预览")
RESULTS = []


def rec(tid, title, ok, evidence=""):
    status = "通过" if ok else "未通过"
    RESULTS.append({"id": tid, "status": status, "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def shot(widget, name):
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, name)
    ok = widget.grab().save(path)
    print(f"截图已保存：{path} ({ok})")
    return path


def mk(root, rel, content="x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.environ[SS.ENV_OVERRIDE] = os.path.join(BASE, "_schemes")

    app = QApplication([])
    screen = app.primaryScreen()
    platform = app.platformName()
    rec("V0", "可获取真实屏幕",
        screen is not None and platform == "cocoa",
        f"platform={platform}；screen={bool(screen)}")

    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    root = os.path.join(BASE, "合成资料_V16")
    for rel, text in (("大二上/数据结构/作业/a.pdf", "A"),
                      ("大二上/数据结构/实验/b.docx", "B"),
                      ("大二上/C++程序设计/作业/c.txt", "C"),
                      ("大二上/高数/作业/d.pdf", "D")):
        mk(root, rel, text)

    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
    finally:
        QMessageBox.question = real_q

    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-v16", name="数据结构归项目", match_type="dir_scope",
        source_path="大二上/数据结构", include_subdirs=True,
        direction="项目", category="V16类别"))
    sr = scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                      compute_duplicates=False)
    w.current_plan = plan
    w._plan_valid = True
    w._populate_table(plan, reset_filters=True)
    w._update_button_states()
    w.show()
    pump(app)

    v1 = (w.plan_search.isVisible() and w.btn_target_tree.isVisible()
          and "计划" in w.plan_counts_label.text())
    shot(w, "01_主窗口_预览筛选与计数.png")
    rec("V1", "主窗口显示搜索、筛选、批量勾选与计数", v1,
        f"计数={w.plan_counts_label.text()}")

    w.plan_search.setText("数据结构")
    pump(app)
    visible = sum(1 for f in w._visible_flags if f)
    w._set_visible_checked(False)
    pump(app)
    shot(w, "02_筛选后批量取消勾选.png")
    v2 = (visible == 2 and w.plan_counts_label.text().find("隐藏但仍勾选") >= 0)
    rec("V2", "搜索筛选后仅对可见行批量操作，隐藏行保持勾选", v2,
        f"可见={visible}；计数={w.plan_counts_label.text()}")

    w._clear_plan_filters()
    pump(app)
    tree = TargetTreeDialog(plan, [True] * plan.total, w)
    tree.show()
    pump(app)
    shot(tree, "03_目标目录结构树.png")
    v3 = tree.tree.topLevelItemCount() >= 1
    rec("V3", "目标目录结构树可展示计划层级与文件", v3,
        f"顶层节点={tree.tree.topLevelItemCount()}")

    import transfer_dialogs as TD
    center = TD.SchemeCenterDialog(
        root=root, current_config=cfg,
        apply_callback=lambda c: None, restore_callback=lambda: "已恢复",
        scan_result=sr, worker_registry=w, parent=w)
    center.show()
    pump(app)
    shot(center, "04_整理方案库.png")
    v4 = center.table.rowCount() >= 2
    rec("V4", "整理方案库显示内置方案与导入/导出/应用入口", v4,
        f"方案行数={center.table.rowCount()}")
    center.close()

    old_center_exec = TD.SchemeCenterDialog.exec
    old_choice_exec = TD.ConfigChoiceDialog.exec
    old_preview_exec = TD.ApplyPreviewDialog.exec
    TD.ConfigChoiceDialog.exec = lambda self: QDialog.Accepted
    TD.ApplyPreviewDialog.exec = lambda self: QDialog.Accepted
    try:
        c2 = TD.SchemeCenterDialog(
            root=root, current_config=cfg,
            apply_callback=w._apply_scheme_config,
            restore_callback=w._restore_last_config,
            scan_result=None, worker_registry=w, parent=w)
        incoming = default_config()
        incoming.long_term_rules.append(LongTermRule(
            rule_id="lt-v16b", name="桌面应用规则", match_type="dir_scope",
            source_path="大二上/数据结构", direction="项目", category="桌面类别"))
        c2._confirm_and_apply(incoming, "桌面检查方案")
        c2.close()
    finally:
        TD.SchemeCenterDialog.exec = old_center_exec
        TD.ConfigChoiceDialog.exec = old_choice_exec
        TD.ApplyPreviewDialog.exec = old_preview_exec
    pump(app)
    shot(w, "05_应用方案后主窗口.png")
    v5 = (w.current_plan is None and not w._plan_valid
          and "已失效" in w.invalid_label.text())
    rec("V5", "应用方案后旧预览立即失效并提示重新预览", v5,
        f"计划={w.current_plan}；提示={w.invalid_label.text()!r}")

    tree.close()
    w.close()
    pump(app)

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 else 1
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_desktop_check_v16_scheme_preview.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"suite": "desktop_check_v16_scheme_preview",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "platform": platform,
                   "screenshot_dir": SHOT_DIR,
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


if __name__ == "__main__":
    sys.exit(main())
