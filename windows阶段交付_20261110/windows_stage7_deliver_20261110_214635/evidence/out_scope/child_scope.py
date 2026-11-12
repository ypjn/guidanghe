"""S7-4 最近空间、待整理清单及范围（真实 Qt 控件）。

覆盖：最近空间重启恢复 / 固定 / 移除 / 不存在目录不自动创建；
待整理清单文件、目录、重复项、父子包含、根目录外来源；
指定范围实际计划正确；范围变化使旧计划失效。
拖放只做 Qt 事件注入检查，明确不代表 Windows 资源管理器原生拖放已通过。
"""
from __future__ import annotations

import json
import os
import re
import shutil

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       all_texts, finish, mkfile, open_root, close_app,
                       RESULT, OUT, BASE, FakeFileDialog, defer_click)
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QDialogButtonBox

import windows_harness as H

_TAG = re.compile(r"<[^>]+>")


def _strip(text):
    return _TAG.sub("", str(text or "")).replace("\xa0", " ")


app = make_app()
ROOT = os.path.join(BASE, "scope_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
FILES = {
    "竞赛/蓝桥杯/报名表.pdf": b"signup",
    "大二上/数据结构/讲义.pdf": b"lecture",
    "大二上/数据结构/试卷.pdf": b"exam",
    "其他/说明.txt": b"readme",
    "其他/笔记.md": b"notes",
}
for rel, data in FILES.items():
    mkfile(ROOT, rel, data)
os.makedirs(os.path.join(ROOT, "待整理", "空目录"), exist_ok=True)

OUTSIDE = os.path.join(BASE, "scope_outside")
if os.path.exists(OUTSIDE):
    shutil.rmtree(OUTSIDE)
mkfile(OUTSIDE, "外部.pdf", b"outside")

MISSING = os.path.join(BASE, "scope_missing_space")
if os.path.exists(MISSING):
    shutil.rmtree(MISSING)

# 「上一次会话」写下的最近空间记录：一个可用（将在本轮打开）、一个已不存在
RECENT = os.environ["ARCHIVEBOX_RECENT_FILE"]
os.makedirs(os.path.dirname(RECENT), exist_ok=True)
with open(RECENT, "w", encoding="utf-8") as f:
    json.dump({"version": 1, "items": [
        {"path": MISSING, "name": "已删除空间", "pinned": False,
         "last_opened": "2026-11-09T09:00:00.000000"},
    ]}, f, ensure_ascii=False, indent=2)

DSP = Dispatcher(app)
REC = {"rows": [], "missing_open_enabled": None}


def _cell(table, row, col):
    it = table.item(row, col)
    return it.text() if it is not None else ""


def h_recent(w):
    """最近整理空间（模态 exec）——按真实控件分步操作。

    会再开一个模态窗口的按钮（如「从记录中移除」的确认框）必须延后点击，
    否则内层 exec() 会阻塞当前定时器回调，调度器无法继续处理内层窗口。
    """
    step = REC.get("step", 0)
    if step == 0:
        REC["step"] = 1
        REC["rows"] = [[_cell(w.table, r, c) for c in range(5)]
                       for r in range(w.table.rowCount())]
        shot(w, "30_recent_spaces.png")
        REC["idx_missing"] = next(
            (r for r in range(w.table.rowCount())
             if _cell(w.table, r, 1) == os.path.realpath(MISSING)), -1)
        REC["idx_root"] = next(
            (r for r in range(w.table.rowCount())
             if _cell(w.table, r, 1) == os.path.realpath(ROOT)), -1)
        if REC["idx_missing"] >= 0:
            w.table.selectRow(REC["idx_missing"])
            REC["missing_open_enabled"] = w.btn_open.isEnabled()
            REC["missing_status"] = _cell(w.table, REC["idx_missing"], 4)
        if REC["idx_root"] >= 0:
            w.table.selectRow(REC["idx_root"])
            defer_click(w.btn_pin)                 # 固定
        return
    if step == 1:
        REC["step"] = 2
        REC["after_pin"] = [_cell(w.table, r, 3) for r in range(w.table.rowCount())]
        REC["row0_after_pin"] = _cell(w.table, 0, 1)
        sel_root = next(r for r in range(w.table.rowCount())
                        if _cell(w.table, r, 1) == os.path.realpath(ROOT))
        w.table.selectRow(sel_root)
        defer_click(w.btn_pin)                     # 取消固定
        return
    if step == 2:
        REC["step"] = 3
        REC["after_unpin"] = [_cell(w.table, r, 3) for r in range(w.table.rowCount())]
        if REC["idx_missing"] >= 0:
            w.table.selectRow(REC["idx_missing"])
            defer_click(w.btn_remove)              # 会弹出确认框
        return
    if step == 3:
        REC["step"] = 4
        REC["rows_after_remove"] = w.table.rowCount()
        REC["remaining"] = [_cell(w.table, r, 1) for r in range(w.table.rowCount())]
        shot(w, "31_recent_after_remove.png")
        return
    bb = w.findChild(QDialogButtonBox)
    defer_click(bb.button(QDialogButtonBox.Close))


DSP.on("最近整理空间", h_recent)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402
import daily_entry_dialogs as ded  # noqa: E402

w = MainWindow()
w.show()
pump(app)
ok("Z0-OPEN", open_root(app, w, ROOT), w.path_edit.text())
w.on_scan()
ok("Z0-SCAN", wait(app, lambda: w._plan_valid and w.current_plan is not None, 40),
   f"total={w.current_plan.total if w.current_plan else None}")

before = os.path.join(OUT, "manifest_before.json")
H.save_manifest(before, H.full_manifest(ROOT))
RESULT["artifacts"]["manifest_before"] = before

# ---------------- Z1 最近整理空间：重启恢复 / 固定 / 移除 / 不自动创建 ----------------
w.btn_recent.click()
pump(app, 0.5)
ok("Z1-RESTART-RECOVERED", REC["idx_root"] >= 0 and REC["idx_missing"] >= 0,
   f"rows={REC['rows']}")
ok("Z1-MISSING-STATUS", "不存在" in REC.get("missing_status", ""),
   f"status={REC.get('missing_status')}")
ok("Z1-MISSING-NOT-OPENABLE", REC["missing_open_enabled"] is False,
   f"enabled={REC['missing_open_enabled']}")
ok("Z1-MISSING-NOT-CREATED", not os.path.exists(MISSING),
   f"exists={os.path.exists(MISSING)}")
ok("Z1-PIN", REC.get("after_pin") and "是" in REC["after_pin"]
   and REC.get("row0_after_pin") == os.path.realpath(ROOT),
   f"pins={REC.get('after_pin')} row0={REC.get('row0_after_pin')}")
ok("Z1-UNPIN", REC.get("after_unpin") and all(v == "否" for v in REC["after_unpin"]),
   f"pins={REC.get('after_unpin')}")
ok("Z1-REMOVE-RECORD-ONLY", REC.get("rows_after_remove") == 1
   and os.path.isdir(ROOT) and not os.path.exists(MISSING),
   f"left={REC.get('remaining')} root_exists={os.path.isdir(ROOT)}")
with open(RECENT, "r", encoding="utf-8") as f:
    recent_data = json.load(f)
ok("Z1-RECENT-FILE-CONTENT",
   len(recent_data["items"]) == 1
   and os.path.realpath(recent_data["items"][0]["path"]) == os.path.realpath(ROOT),
   f"items={recent_data['items']}")

# ---------------- Z2 待整理清单：文件 / 目录 / 重复 / 父子包含 / 根外 ----------------
ded.QFileDialog = FakeFileDialog
w.btn_intake.click()
pump(app, 0.4)
dlg = getattr(w, "_intake_dialog", None)
ok("Z2-DIALOG-OPEN", dlg is not None and dlg.isVisible(),
   f"dialog={type(dlg).__name__ if dlg else None}")
msgs = []


def add_dir(path):
    FakeFileDialog._open = path
    dlg.btn_add_dir.click()
    pump(app, 0.2)
    msgs.append(dlg.message.text())


def add_files(paths):
    FakeFileDialog._open_many = list(paths)
    dlg.btn_add_files.click()
    pump(app, 0.2)
    msgs.append(dlg.message.text())


add_files([os.path.join(ROOT, "其他", "说明.txt")])
ok("Z2-ADD-FILE", len(w.intake) == 1 and "已加入" in msgs[-1], f"n={len(w.intake)} m={msgs[-1]}")
add_dir(os.path.join(ROOT, "其他"))              # 子项被父目录合并
ok("Z2-PARENT-COALESCES", len(w.intake) == 1 and "合并" in msgs[-1],
   f"n={len(w.intake)} m={msgs[-1]}")
add_files([os.path.join(ROOT, "大二上", "数据结构", "讲义.pdf")])
ok("Z2-ADD-NESTED-FILE", len(w.intake) == 2, f"n={len(w.intake)}")
add_dir(os.path.join(ROOT, "大二上"))
ok("Z2-NESTED-PARENT-COALESCES", len(w.intake) == 2 and "合并" in msgs[-1],
   f"n={len(w.intake)} m={msgs[-1]}")
add_dir(os.path.join(ROOT, "大二上"))             # 完全重复
ok("Z2-DUPLICATE", len(w.intake) == 2 and "已在清单中" in msgs[-1],
   f"n={len(w.intake)} m={msgs[-1]}")
add_files([os.path.join(OUTSIDE, "外部.pdf")])     # 根目录外
ok("Z2-OUTSIDE-REJECTED", len(w.intake) == 2 and "不在当前整理空间内" in msgs[-1],
   f"n={len(w.intake)} m={msgs[-1]}")
add_dir(os.path.join(ROOT, "待整理", "空目录"))     # 空目录：登记成功但解析为 0
ok("Z2-EMPTY-DIR-REGISTERED", len(w.intake) == 3, f"n={len(w.intake)} m={msgs[-1]}")
ok("Z2-DIALOG-LIST", dlg.list_widget.count() == 3,
   f"list={dlg.list_widget.count()} scope={_strip(dlg.scope_label.text())[:120]}")
shot(dlg, "32_intake_list.png")

# ---------------- Z3 指定范围实际计划 ----------------
w.btn_scan_intake.click()


def intake_plan_ready():
    return (not w._working and not w._plan_working and w._plan_valid
            and w.current_plan is not None
            and w._plan_scope == "intake"
            and "待整理清单" in w._plan_scope_note)


ok("Z3-INTAKE-PLAN", wait(app, intake_plan_ready, 60),
   f"total={w.current_plan.total if w.current_plan else None} "
   f"note={w._plan_scope_note}")
rel_set = {it.file.rel_path.replace("\\", "/") for it in w.current_plan.items}
expected = {"其他/说明.txt", "其他/笔记.md",
            "大二上/数据结构/讲义.pdf", "大二上/数据结构/试卷.pdf"}
ok("Z3-INTAKE-PLAN-EXACT", rel_set == expected,
   f"got={sorted(rel_set)}")
ok("Z3-SCOPE-NOTE", w._plan_scope == "intake"
   and "待整理清单" in w._plan_scope_note
   and "纳入 4 个文件" in w._plan_scope_note
   and "空目录" not in w._plan_scope_note,
   f"scope={w._plan_scope} note={w._plan_scope_note}")
ok("Z3-SCOPE-LABEL", "待整理清单" in w.scope_label.text()
   and w.scope_label.isVisible(), w.scope_label.text())
ok("Z3-EXCLUDED-OUTSIDE", "竞赛/蓝桥杯/报名表.pdf" not in rel_set,
   "范围外文件未进入计划")
shot(w, "33_intake_scope.png")

# ---------------- Z4 通过 Qt 事件注入检查拖放（非原生拖放） ----------------
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(os.path.join(ROOT, "竞赛", "蓝桥杯", "报名表.pdf"))])
evt = QDropEvent(QPointF(12, 12), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
drop_route = "sendEvent(list_widget)"
QApplication.sendEvent(dlg.list_widget, evt)
pump(app, 0.3)
if dlg.list_widget.count() == 3:
    drop_route = "sendEvent(list_widget.viewport)"
    evt2 = QDropEvent(QPointF(12, 12), Qt.CopyAction, mime, Qt.LeftButton,
                      Qt.NoModifier)
    QApplication.sendEvent(dlg.list_widget.viewport(), evt2)
    pump(app, 0.3)
if dlg.list_widget.count() == 3:
    drop_route = "直接调用控件 dropEvent(QDropEvent)"
    evt3 = QDropEvent(QPointF(12, 12), Qt.CopyAction, mime, Qt.LeftButton,
                      Qt.NoModifier)
    dlg.list_widget.dropEvent(evt3)
    pump(app, 0.3)
RESULT["notes"].append(
    "拖放为 Qt 事件注入检查（" + drop_route + "），只验证 dropEvent 的 mime 解析"
    "与清单登记；不代表 Windows 资源管理器原生拖放已通过，原生拖放列未测。")
ok("Z4-DROP-QT-EVENT", dlg.list_widget.count() == 4 and len(w.intake) == 4,
   f"list={dlg.list_widget.count()} n={len(w.intake)} route={drop_route} "
   f"m={_strip(dlg.message.text())[:120]}")

# ---------------- Z5 范围变化使旧计划失效 ----------------
ok("Z5-PLAN-INVALIDATED", not w._plan_valid and w.current_plan is None
   and w.invalid_label.isVisible()
   and "待整理范围已变化" in w.invalid_label.text(),
   f"valid={w._plan_valid} label={w.invalid_label.text()}")
ok("Z5-STATUS-HINT", "待整理清单已更新" in w.statusBar().currentMessage(),
   w.statusBar().currentMessage())
ok("Z5-EXEC-DISABLED", not w.btn_execute.isEnabled(),
   f"enabled={w.btn_execute.isEnabled()}")

# ---------------- Z6 按新范围重新生成计划 ----------------
w.btn_scan_intake.click()
ok("Z6-RESCAN", wait(app, lambda: intake_plan_ready()
                     and w.current_plan.total == 5, 60),
   f"total={w.current_plan.total if w.current_plan else None}")
rel2 = {it.file.rel_path.replace("\\", "/") for it in w.current_plan.items}
ok("Z6-NEW-PLAN-INCLUDES-DROPPED",
   rel2 == expected | {"竞赛/蓝桥杯/报名表.pdf"} and "纳入 5 个文件" in w._plan_scope_note,
   f"got={sorted(rel2)} note={w._plan_scope_note}")

# ---------------- Z7 全套只登记范围，没有任何真实移动 ----------------
dlg.close()
pump(app, 0.3)
after = H.full_manifest(ROOT)
diff = H.diff_manifests(H.load_manifest(before), after)
ok("Z7-NO-MOVE", diff == {"missing": [], "extra": [], "changed": []}
   and not os.path.exists(os.path.join(ROOT, "_已整理")),
   f"diff={json.dumps(diff, ensure_ascii=False)}")

close_app(app, w)
DSP.stop()
finish(app)
