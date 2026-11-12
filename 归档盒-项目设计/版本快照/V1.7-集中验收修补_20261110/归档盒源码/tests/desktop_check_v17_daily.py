"""
归档盒 V1.7 — 重复文件与日常整理入口 真实桌面检查（cocoa，截图）

V0 真实屏幕
V1 主窗口：最近空间 / 待整理清单 / 扫描清单范围 / 重复文件入口与范围标签
V2 待整理清单窗口（含范围说明）
V3 最近整理空间窗口（固定/缺失状态）
V4 重复文件窗口（内容重复 + 同名不同内容 + 待核对）
V5 重复文件搜索/类型筛选
V6 清单范围扫描：计划只包含指定文件

程序化点击与截图，不等同真人手工试用；合成样本。
运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v17_daily.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox

import recent_spaces
import scheme_store
from classification_config import default_config
from duplicate_report import assemble_report, entries_from_plan, verify_entries
from main import MainWindow
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory

BASE = "/tmp/_v17_desktop"
SHOT_DIR = os.environ.get(
    "ARCHIVEBOX_SHOT_DIR",
    "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
    "验收截图_20261110_V17重复与日常")
RESULTS = []


def rec(tid, title, ok, evidence=""):
    RESULTS.append({"id": tid, "status": "通过" if ok else "未通过",
                    "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def wait_until(app, cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(cond())


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
    return p


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.environ[recent_spaces.ENV_OVERRIDE] = os.path.join(BASE, "recent.json")
    os.environ[scheme_store.ENV_APP_DIR] = os.path.join(BASE, "appdata")

    app = QApplication([])
    screen = app.primaryScreen()
    platform = app.platformName()
    rec("V0", "可获取真实屏幕", screen is not None and platform == "cocoa",
        f"platform={platform}")

    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    root = os.path.join(BASE, "space")
    mk(root, "included/a.txt", "DUP")
    mk(root, "included/sub/b.txt", "B")
    mk(root, "outside/c.txt", "DUP")
    mk(root, "same/dir1/n.txt", "AAAA")
    mk(root, "same/dir2/n.txt", "BBBB")
    untouched = mk(root, "untouched/d.txt", "KEEP")

    recent_spaces.clear()
    recent_spaces.touch(root)
    missing = os.path.join(BASE, "not_mounted")
    recent_spaces.touch(missing)
    recent_spaces.set_pinned(root, True)

    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
    finally:
        QMessageBox.question = real_q
    w.show()
    pump(app)

    v1 = (w.btn_recent.isVisible() and w.btn_intake.isVisible()
          and w.btn_scan_intake.isVisible() and w.btn_duplicates.isVisible()
          and w.scope_label.isVisible())
    shot(w, "01_主窗口_日常入口与范围.png")
    rec("V1", "主窗口显示最近空间/待整理清单/扫描清单范围/重复文件入口", v1,
        f"范围标签={w.scope_label.text()!r}")

    # V2 待整理清单
    w.intake.add(os.path.join(root, "included"), root)
    w.intake.add(os.path.join(root, "outside", "c.txt"), root)
    w._update_scope_label()
    w.on_intake_dialog()
    pump(app)
    idlg = w._intake_dialog
    shot(idlg, "02_待整理清单.png")
    rec("V2", "待整理清单窗口显示登记项与范围说明", idlg is not None and
        idlg.list_widget.count() == 2,
        f"清单行数={idlg.list_widget.count() if idlg else '-'}")
    idlg.close()
    pump(app)

    # V3 最近空间
    from daily_entry_dialogs import RecentSpacesDialog
    rdlg = RecentSpacesDialog(lambda p: False, None)
    rdlg.show()
    pump(app)
    missing_marked = any("不存在" in rdlg.table.item(r, 4).text()
                         for r in range(rdlg.table.rowCount()))
    shot(rdlg, "03_最近整理空间.png")
    rec("V3", "最近空间窗口显示记录、固定状态与缺失目录标记", missing_marked,
        f"行数={rdlg.table.rowCount()} 缺失标记={missing_marked}")
    rdlg.close()

    # V4/V5 重复文件
    raw = scan_directory(root, detect_project_roots=False)
    plan = build_plan(raw, MODE_BY_DIRECTION_SEMESTER, [],
                      config=default_config(), compute_duplicates=True)
    entries = entries_from_plan(plan, raw)
    # 人为制造一个核对失败条目，展示“待核对”
    entries.append(entries[0].__class__(
        rel_path="missing/gone.txt", abs_path=os.path.join(root, "missing/gone.txt"),
        filename="gone.txt", size=1, size_display="1 B"))
    verify_entries(entries)
    report = assemble_report(root, entries, scope_note="桌面检查", source="plan")
    from duplicate_dialog import DuplicateDialog
    dlg = DuplicateDialog(root, "桌面检查", w)
    dlg.show_report(report)
    dlg.show()
    pump(app)
    shot(dlg, "04_重复文件窗口.png")
    kinds = {g.kind for g in report.groups}
    rec("V4", "重复窗口展示内容重复、同名不同内容与待核对分组",
        {"content", "same_name_diff", "unverified"} <= kinds,
        f"分组={[(g.kind, g.count) for g in report.groups]}")
    dlg.search.setText("a.txt")
    pump(app)
    shot(dlg, "05_重复文件搜索筛选.png")
    visible = sum(1 for i in range(dlg.tree.topLevelItemCount())
                  if not dlg.tree.topLevelItem(i).isHidden())
    rec("V5", "重复窗口搜索/筛选只改变视图", visible >= 1,
        f"搜索 a.txt 后可见组={visible}")
    dlg.close()
    pump(app)

    # V6 清单范围扫描
    w._plan_scope = "intake"
    w._update_scope_label()
    w.on_scan_intake()
    done = wait_until(app, lambda: (not w._working and not w._plan_working
                                    and w.current_plan is not None))
    plan_rels = ({i.file.rel_path for i in w.current_plan.items}
                 if w.current_plan else set())
    scope_ok = plan_rels == {"included/a.txt", "included/sub/b.txt",
                             "outside/c.txt"}
    untouched_ok = os.path.isfile(untouched)
    shot(w, "06_清单范围扫描结果.png")
    rec("V6", "按清单范围扫描后计划只包含指定文件，范围外文件保持原位",
        done and scope_ok and untouched_ok,
        f"完成={done} 计划={sorted(plan_rels)} 范围外文件={untouched_ok}")

    w.close()
    pump(app)
    try:
        from main import shutdown_widgets
        shutdown_widgets()
    except Exception:
        pass

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 else 1
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_desktop_check_v17_daily.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"suite": "desktop_check_v17_daily",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "platform": platform, "screenshot_dir": SHOT_DIR,
                   "counts": {"total": len(RESULTS), "pass": passed,
                              "fail": failed},
                   "exit_code": exit_code, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
