"""S7-3 筛选、勾选与执行确认（真实 Qt 控件）。

覆盖：隐藏已选项不被静默改变 / 批量勾选只作用于可见 / 隐藏勾选提醒与确认统计
与全部实际选择一致 / 取消不执行 / 目标目录树与逐文件目标对应 / 执行与撤销清单。
"""
from __future__ import annotations

import json
import os
import re
import shutil

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       all_texts, finish, mkfile, row_by_source, open_root,
                       close_app, RESULT, OUT, BASE, visible_rows)
from PySide6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox

import windows_harness as H

_TAG = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    """去掉 QLabel 里的 HTML 标签，便于断言用户实际看到的文案。"""
    return _TAG.sub("", str(text or "")).replace("\xa0", " ")

app = make_app()
ROOT = os.path.join(BASE, "select_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT, exist_ok=True)
FILES = {
    "大二上/数据结构/讲义.pdf": b"lecture",
    "大二上/数据结构/试卷.pdf": b"exam",
    "竞赛/蓝桥杯/报名表.pdf": b"signup",
    "其他/说明.txt": b"readme",
    "其他/笔记.md": b"notes",
}
for rel, data in FILES.items():
    mkfile(ROOT, rel, data)

DSP = Dispatcher(app)
CONFIRM = {"action": "ok", "texts": [], "record": []}


def h_confirm(w):
    CONFIRM["texts"] = all_texts(w)
    CONFIRM["record"].append({"texts": CONFIRM["texts"]})
    bb = w.findChild(QDialogButtonBox)
    b = (bb.button(QDialogButtonBox.Ok) if CONFIRM["action"] == "ok"
         else bb.button(QDialogButtonBox.Cancel))
    b.click()


TREE = {"leaves": [], "info": "", "all_count": None}


def collect_tree_leaves(tree):
    leaves = []

    def walk(item):
        if item.childCount() == 0:
            leaves.append({"text": item.text(0), "tip": item.toolTip(0)})
        for i in range(item.childCount()):
            walk(item.child(i))

    root = tree.invisibleRootItem()
    for i in range(root.childCount()):
        walk(root.child(i))
    return leaves


def count_tree_leaves(tree):
    n = 0
    root = tree.invisibleRootItem()
    for i in range(root.childCount()):
        n += _count_leaves(root.child(i))
    return n


def _count_leaves(item):
    if item.childCount() == 0:
        return 1
    return sum(_count_leaves(item.child(i)) for i in range(item.childCount()))


DSP.on("确认整理计划", h_confirm)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402
w = MainWindow()
w.show()
pump(app)
ok("C0-OPEN", open_root(app, w, ROOT), w.path_edit.text())
w.on_scan()
ok("C0-SCAN", wait(app, lambda: w._plan_valid and w.current_plan is not None),
   f"total={w.current_plan.total if w.current_plan else None}")


def check(r, on=True):
    cb = w.table.cellWidget(r, 0).findChild(QCheckBox)
    cb.setChecked(on)
    pump(app, 0.05)


def sel(rel):
    i = row_by_source(w, rel)
    return w.current_plan.items[i]


before = os.path.join(OUT, "manifest_before.json")
H.save_manifest(before, H.full_manifest(ROOT))

# ---------------- C1 隐藏已选：不勾选状态不被静默改变 ----------------
w.plan_search.setText("")
pump(app, 0.3)
w.btn_deselect_visible.click()      # 清空全部勾选，建立干净前置状态
pump(app, 0.2)
ok("C1-START-CLEARED", w.current_plan.selected_count == 0,
   f"selected={w.current_plan.selected_count}")
r_lect = row_by_source(w, "大二上/数据结构/讲义.pdf")
check(r_lect, True)
ok("C1-CHECK-ONE", sel("大二上/数据结构/讲义.pdf").selected,
   f"selected={w.current_plan.selected_count}")
w.plan_search.setText("试卷")
pump(app, 0.3)
hidden_now = w.table.isRowHidden(r_lect)
counts = w.plan_counts_label.text()
ok("C1-HIDDEN-KEEPS-CHECK",
   hidden_now and sel("大二上/数据结构/讲义.pdf").selected
   and "隐藏但仍勾选 1" in counts,
   f"hidden={hidden_now} selected={sel('大二上/数据结构/讲义.pdf').selected} "
   f"counts={counts}")

# ---------------- C2 改变类型/来源筛选后仍不改变 ----------------
w.type_filter.setCurrentIndex(min(1, w.type_filter.count() - 1))
pump(app, 0.3)
w.source_filter.setCurrentIndex(2)     # 长期规则
pump(app, 0.3)
w.source_filter.setCurrentIndex(0)
pump(app, 0.3)
still = sel("大二上/数据结构/讲义.pdf")
ok("C2-FILTER-CHANGES-DONT-MUTATE",
   still.selected and w.table.isRowHidden(r_lect)
   and "隐藏但仍勾选 1" in w.plan_counts_label.text(),
   f"selected={still.selected} hidden={w.table.isRowHidden(r_lect)} "
   f"counts={w.plan_counts_label.text()}")

# ---------------- C3 批量勾选只作用于可见范围 ----------------
w.type_filter.setCurrentIndex(0)
w.plan_search.setText("")
pump(app, 0.3)
w.btn_deselect_visible.click()
pump(app, 0.2)
ok("C3-ALL-CLEARED", w.current_plan.selected_count == 0,
   f"selected={w.current_plan.selected_count}")
w.plan_search.setText("数据结构")
pump(app, 0.3)
vis = visible_rows(w)
w.btn_select_visible.click()
pump(app, 0.2)
ok("C3-SELECT-VISIBLE-ONLY",
   len(vis) == 2 and w.current_plan.selected_count == 2
   and not sel("其他/说明.txt").selected
   and not sel("竞赛/蓝桥杯/报名表.pdf").selected,
   f"vis={len(vis)} selected={w.current_plan.selected_count}")
shot(w, "20_select_visible.png")
w.btn_invert_visible.click()
pump(app, 0.2)
ok("C3-INVERT-VISIBLE", w.current_plan.selected_count == 0,
   f"selected={w.current_plan.selected_count}")

# ---------------- C4 隐藏勾选提醒 + 确认统计一致 ----------------
w.plan_search.setText("")
pump(app, 0.3)
w.btn_deselect_visible.click()
pump(app, 0.2)
check(r_lect, True)
w.plan_search.setText("试卷")         # 讲义 重新被搜索条件隐藏
pump(app, 0.3)
ok("C4-PRECONDITION", w.table.isRowHidden(r_lect)
   and sel("大二上/数据结构/讲义.pdf").selected,
   f"hidden={w.table.isRowHidden(r_lect)} "
   f"selected={w.current_plan.selected_count}")
CONFIRM["action"] = "cancel"
n_before_exec = len(w._storage.list_batches()) if w._storage else 0
w.btn_execute.click()
pump(app, 0.8)
texts = _strip(" | ".join(CONFIRM["texts"]))
total = w.current_plan.selected_count
ok("C4-CONFIRM-STATS",
   f"已选文件：{total} / {w.current_plan.total}" in texts
   and "隐藏了 1 个已勾选文件" in texts,
   f"total={total} texts={texts[:300]}")
ok("C4-CANCEL-NO-EXEC",
   (w._storage is None or len(w._storage.list_batches()) == n_before_exec)
   and H.diff_manifests(H.load_manifest(before),
                        H.full_manifest(ROOT)) == {"missing": [], "extra": [],
                                                   "changed": []},
   f"batches={len(w._storage.list_batches()) if w._storage else 0}")
shot(w, "21_confirm_dialog.png")

# ---------------- C5 目标目录树与逐文件目标对应 ----------------
vis_count = len(visible_rows(w))
w.btn_target_tree.click()
pump(app, 0.6)
tree_dlg = getattr(w, "_tree_dialog", None)
ok("C5-DIALOG-OPEN", tree_dlg is not None and tree_dlg.isVisible(),
   f"dialog={type(tree_dlg).__name__ if tree_dlg else None}")
if tree_dlg is not None:
    TREE["leaves"] = collect_tree_leaves(tree_dlg.tree)
    TREE["info"] = _strip(tree_dlg.info.text())
    shot(tree_dlg, "22_target_tree.png")
    tree_dlg.scope_check.setChecked(False)   # 切到「整份计划」
    pump(app, 0.3)
    tree_dlg.btn_refresh.click()
    pump(app, 0.3)
    TREE["all_count"] = count_tree_leaves(tree_dlg.tree)
    TREE["info_all"] = _strip(tree_dlg.info.text())
    tree_dlg.close()
    pump(app, 0.2)
plan_abs = {os.path.normcase(os.path.abspath(os.path.join(ROOT, it.target_rel_path)))
            for it in w.current_plan.items}
leaf_abs = {os.path.normcase(l["tip"].split("完整目标路径：", 1)[-1])
            for l in TREE["leaves"]}
ok("C5-TREE-MATCHES-VISIBLE",
   len(TREE["leaves"]) == vis_count and leaf_abs <= plan_abs
   and bool(TREE["leaves"])
   and all(l["tip"].startswith("完整目标路径：") for l in TREE["leaves"]),
   f"leaves={len(TREE['leaves'])} visible={vis_count} info={TREE['info'][:90]}")
ok("C5-TREE-SCOPE-OFF-COUNTS-ALL", TREE.get("all_count") == w.current_plan.total,
   f"all={TREE.get('all_count')} total={w.current_plan.total} "
   f"info={TREE.get('info_all', '')[:90]}")

# ---------------- C6 执行统计与实际移动一致（2 个可见项） ----------------
w.plan_search.setText("")
pump(app, 0.3)
w.btn_deselect_visible.click()
pump(app, 0.2)
targets = ["大二上/数据结构/讲义.pdf", "其他/说明.txt"]
for rel in targets:
    check(row_by_source(w, rel), True)
pump(app, 0.2)
ok("C6-EXEC-PRECONDITION", w.current_plan.selected_count == 2
   and sum(1 for r in range(w.table.rowCount())
           if w.table.isRowHidden(r) and w.current_plan.items[r].selected) == 0,
   f"selected={w.current_plan.selected_count}")
CONFIRM["action"] = "ok"
CONFIRM["texts"] = []
w.btn_execute.click()
ok("C6-EXEC-DONE", wait(app, lambda: not w._working, 90), f"working={w._working}")
batch = w._storage.get_latest_batch()
acts = w._storage.get_actions(batch.batch_id) if batch else []
moved = [a for a in acts if a.status == "moved"]
ok("C6-EXEC-COUNT", len(acts) == 2 and len(moved) == 2,
   f"actions={len(acts)} moved={len(moved)} statuses={[a.status for a in acts]}")
ok("C6-EXEC-FILES", all(os.path.isfile(os.path.join(ROOT, a.target_rel)) for a in moved)
   and os.path.isfile(os.path.join(ROOT, "竞赛", "蓝桥杯", "报名表.pdf"))
   and os.path.isfile(os.path.join(ROOT, "其他", "笔记.md")),
   f"targets={[a.target_rel for a in moved]}")
shot(w, "23_after_execute.png")
mid = os.path.join(OUT, "manifest_after_move.json")
H.save_manifest(mid, H.full_manifest(ROOT))
RESULT["artifacts"]["manifest_after_move"] = mid
w.btn_undo.click()
ok("C6-UNDO-DONE", wait(app, lambda: not w._working, 90), "")
after = H.full_manifest(ROOT)
diff = H.diff_manifests(H.load_manifest(before), after)
ok("C6-UNDO-MANIFEST", diff == {"missing": [], "extra": [], "changed": []},
   f"diff={json.dumps(diff, ensure_ascii=False)} count={len(after)}")
RESULT["artifacts"]["manifest_before"] = before

close_app(app, w)
DSP.stop()
finish(app)
