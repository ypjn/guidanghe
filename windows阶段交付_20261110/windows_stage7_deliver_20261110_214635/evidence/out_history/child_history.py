"""S7-6 历史导出（真实 Qt 控件 + 持久化记录）。

覆盖：界面查询（批次列表/逐项明细/搜索/状态筛选）、CSV/JSON 实际内容、
源目标路径与规则依据、导出不能按当前规则重算过去目标。
"""
from __future__ import annotations

import csv
import json
import os
import shutil

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       finish, mkfile, open_root, close_app, RESULT, OUT, BASE,
                       FakeFileDialog, defer_click, row_by_source)

import windows_harness as H
from PySide6.QtWidgets import QCheckBox, QPushButton

import main as main_mod
from classification_config import (LongTermRule, load_config, save_config)


app = make_app()
ROOT = os.path.join(BASE, "history_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
mkfile(ROOT, "22级/数据结构/讲义.pdf", b"LECTURE")
mkfile(ROOT, "22级/数据结构/试卷.pdf", b"EXAM")
mkfile(ROOT, "其他/说明.txt", b"README")
mkfile(ROOT, "竞赛/蓝桥杯/报名表.pdf", b"SIGN")

CONF = {"action": "ok"}


def h_confirm(w):
    wanted = ("开始整理",) if CONF["action"] == "ok" else ("取消",)
    for b in w.findChildren(QPushButton):
        if any(t in b.text() for t in wanted):
            defer_click(b)
            return


DSP = Dispatcher(app)
DSP.on("确认整理计划", h_confirm)
DSP.on("*", ack_message)
main_mod.QFileDialog = FakeFileDialog

w = main_mod.MainWindow()
w.show()
pump(app)
ok("H0-OPEN", open_root(app, w, ROOT), w.path_edit.text())
w.on_scan()
ok("H0-PLAN", wait(app, lambda: w._plan_valid and w.current_plan is not None, 45),
   f"total={w.current_plan.total if w.current_plan else None}")

# 写入一条长期规则（走产品配置 API），并让主窗口重新加载配置
cfg, _src = load_config(ROOT)
cfg.long_term_rules = [LongTermRule(
    rule_id="rule-hist-0001", name="历史依据规则", match_type="exact_path",
    source_path="22级/数据结构/讲义.pdf", direction="项目", category="讲义存档",
    inner_path="", reason="用于历史依据导出核验")]
save_config(ROOT, cfg)
w._load_root_config(ROOT)
w.on_scan()
ok("H0-PLAN-WITH-RULE",
   wait(app, lambda: w._plan_valid and w.current_plan is not None
        and any("讲义存档" in it.target_rel_path for it in w.current_plan.items), 45),
   f"targets={[it.target_rel_path for it in (w.current_plan.items if w.current_plan else [])]}")
plan_targets = {it.file.rel_path.replace("\\", "/"): it.target_rel_path
                for it in w.current_plan.items}
hist_target = plan_targets["22级/数据结构/讲义.pdf"]
ok("H0-RULE-TARGET", "项目" in hist_target and "讲义存档" in hist_target,
   f"target={hist_target}")

# 只执行 2 项，其余保留在原位，便于对照“历史只记录实际动作”
w.btn_deselect_visible.click()
pump(app, 0.2)
for rel in ("22级/数据结构/讲义.pdf", "其他/说明.txt"):
    cb = w.table.cellWidget(row_by_source(w, rel), 0).findChild(QCheckBox)
    cb.click()                                   # 真实行内复选框
pump(app, 0.3)
ok("H0-SELECTED-2", w.current_plan.selected_count == 2,
   f"selected={w.current_plan.selected_count}")
CONF["action"] = "ok"
w.btn_execute.click()
ok("H0-EXEC", wait(app, lambda: not w._working, 90), f"working={w._working}")
batch = w._storage.get_latest_batch()
acts = w._storage.get_actions(batch.batch_id)
ok("H0-EXEC-2", len([a for a in acts if a.status == "moved"]) == 2,
   f"actions={[(a.source_rel, a.status) for a in acts]}")

# ---------------- H1 界面查询：批次 + 逐项明细 ----------------
w.btn_history.click()
pump(app, 0.5)
dlg = getattr(w, "_history_dialog", None)
ok("H1-DIALOG", dlg is not None and dlg.isVisible(),
   f"dialog={type(dlg).__name__ if dlg else None}")
ok("H1-BATCH-ROW", dlg.batch_table.rowCount() == 1
   and dlg.batch_table.item(0, 0).text() == f"#{batch.batch_id}"
   and dlg.batch_table.item(0, 3).text() == "已完成"
   and dlg.batch_table.item(0, 4).text() == "2",
   f"row0={[dlg.batch_table.item(0, c).text() for c in range(10)]}")
ok("H1-CONFIG-SNAPSHOT", dlg.batch_config_label.isVisible()
   and "快照" in dlg.batch_config_label.text()
   and "不受后来修改影响" in dlg.batch_config_label.text()
   and "分类配置 v2" in dlg.batch_config_label.text(),
   dlg.batch_config_label.text()[:160])
items = {(dlg.item_table.item(r, 1).text().replace("\\", "/")):
         {"target": dlg.item_table.item(r, 2).text(),
          "status": dlg.item_table.item(r, 4).text(),
          "source": dlg.item_table.item(r, 6).text(),
          "tip": dlg.item_table.item(r, 6).toolTip()}
         for r in range(dlg.item_table.rowCount())}
shot(dlg, "50_history_dialog.png")
ok("H1-ITEM-ROWS", dlg.item_table.rowCount() == 2
   and set(items) == {"22级/数据结构/讲义.pdf", "其他/说明.txt"},
   f"rows={sorted(items)}")
ok("H1-RULE-BASIS",
   items["22级/数据结构/讲义.pdf"]["source"] == "长期规则"
   and "rule-hist-0001" in items["22级/数据结构/讲义.pdf"]["tip"]
   and items["22级/数据结构/讲义.pdf"]["target"] == hist_target
   and items["其他/说明.txt"]["source"] == "自动",
   f"lecture={items['22级/数据结构/讲义.pdf']} other={items['其他/说明.txt']}")

# ---------------- H2 搜索与状态筛选 ----------------
dlg.search_edit.setText("讲义")
pump(app, 0.3)
ok("H2-SEARCH", dlg.item_table.rowCount() == 1
   and "讲义" in dlg.item_table.item(0, 1).text()
   and "共 1 项" in dlg.page_label.text(),
   f"rows={dlg.item_table.rowCount()} page={dlg.page_label.text()}")
dlg.search_edit.setText("")
pump(app, 0.2)
idx = next(i for i in range(dlg.status_filter.count())
           if dlg.status_filter.itemData(i) == "failed")
dlg.status_filter.setCurrentIndex(idx)
pump(app, 0.3)
ok("H2-STATUS-FILTER", dlg.item_table.rowCount() == 0
   and "无匹配项" in dlg.page_label.text(),
   f"rows={dlg.item_table.rowCount()} page={dlg.page_label.text()}")
dlg.status_filter.setCurrentIndex(0)
pump(app, 0.3)
ok("H2-FILTER-RESET", dlg.item_table.rowCount() == 2,
   f"rows={dlg.item_table.rowCount()}")

# ---------------- H3 CSV / JSON 导出内容 ----------------
csv_path = os.path.join(OUT, "history_batch.csv")
FakeFileDialog._save = (csv_path, "CSV 文件 (*.csv)")
dlg.btn_export.click()
pump(app, 0.5)
ok("H3-CSV-FILE", os.path.isfile(csv_path), csv_path)
with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
head = rows[1]                       # rows[0] 是批次摘要行，rows[1] 才是列名
data = [r for r in rows[2:] if len(r) == 15]
csv_by_src = {r[1].replace("\\", "/"): r for r in data}
ok("H3-CSV-CONTENT",
   "规则标识" in head and "命中依据" in head and len(data) == 2
   and csv_by_src["22级/数据结构/讲义.pdf"][2] == hist_target
   and csv_by_src["22级/数据结构/讲义.pdf"][13].endswith("rule-hist-0001")
   and csv_by_src["22级/数据结构/讲义.pdf"][9] == "长期规则"
   and csv_by_src["22级/数据结构/讲义.pdf"][14] != ""
   and csv_by_src["其他/说明.txt"][9] == "自动",
   f"head={head}\nlecture={csv_by_src.get('22级/数据结构/讲义.pdf')}")

json_path = os.path.join(OUT, "history_batch.json")
FakeFileDialog._save = (json_path, "JSON 文件 (*.json)")
dlg.btn_export.click()
pump(app, 0.5)
ok("H3-JSON-FILE", os.path.isfile(json_path), json_path)
with open(json_path, "r", encoding="utf-8") as f:
    payload = json.load(f)
j_by_src = {a["source_rel"].replace("\\", "/"): a for a in payload["actions"]}
ok("H3-JSON-CONTENT",
   payload["batch"]["batch_id"] == batch.batch_id
   and payload["batch"]["root"] == ROOT
   and len(payload["actions"]) == 2
   and j_by_src["22级/数据结构/讲义.pdf"]["target_rel"] == hist_target
   and j_by_src["22级/数据结构/讲义.pdf"]["rule_key"].endswith("rule-hist-0001")
   and j_by_src["22级/数据结构/讲义.pdf"]["classification_source"] == "长期规则"
   and j_by_src["其他/说明.txt"]["classification_source"] == "自动",
   f"batch={payload['batch']['batch_id']} actions={list(j_by_src)}")
RESULT["artifacts"]["history_csv"] = csv_path
RESULT["artifacts"]["history_json"] = json_path

# ---------------- H4 数据库记录与导出一致 ----------------
db_act = {a.source_rel.replace("\\", "/"): a for a in acts}
ok("H4-DB-MATCHES-EXPORT",
   db_act["22级/数据结构/讲义.pdf"].target_rel == hist_target
   and db_act["22级/数据结构/讲义.pdf"].rule_key.endswith("rule-hist-0001")
   and db_act["22级/数据结构/讲义.pdf"].rule_applied
   == csv_by_src["22级/数据结构/讲义.pdf"][14]
   and db_act["其他/说明.txt"].classification_source == "自动",
   f"db={[ (a.source_rel, a.target_rel, a.rule_key) for a in acts ]}")

# ---------------- H5 改配置后历史不重算 ----------------
mkfile(ROOT, "22级/数据结构/讲义.pdf", b"LECTURE")     # 同名源文件重新出现
cfg2, _ = load_config(ROOT)
cfg2.long_term_rules = [LongTermRule(
    rule_id="rule-hist-0002", name="新规则", match_type="exact_path",
    source_path="22级/数据结构/讲义.pdf", direction="校园事务", category="待归档",
    inner_path="", reason="配置已变更")]
save_config(ROOT, cfg2)
w._load_root_config(ROOT)
w.on_scan()
ok("H5-NEW-PLAN-DIFFERS",
   wait(app, lambda: w._plan_valid and w.current_plan is not None
        and any("待归档" in it.target_rel_path for it in w.current_plan.items), 45),
   f"targets={[it.target_rel_path for it in (w.current_plan.items if w.current_plan else [])]}")
new_target = next(it.target_rel_path for it in w.current_plan.items
                  if it.file.rel_path.replace("\\", "/") == "22级/数据结构/讲义.pdf")
ok("H5-TARGET-ACTUALLY-CHANGED", new_target != hist_target,
   f"old={hist_target} new={new_target}")

dlg.btn_refresh.click()
pump(app, 0.5)
items2 = {(dlg.item_table.item(r, 1).text().replace("\\", "/")):
          dlg.item_table.item(r, 2).text() for r in range(dlg.item_table.rowCount())}
ok("H5-HISTORY-NOT-RECOMPUTED",
   items2.get("22级/数据结构/讲义.pdf") == hist_target and len(items2) == 2,
   f"items={items2}")
csv2 = os.path.join(OUT, "history_batch_after_config_change.csv")
FakeFileDialog._save = (csv2, "CSV 文件 (*.csv)")
dlg.btn_export.click()
pump(app, 0.5)
with open(csv2, "r", encoding="utf-8-sig", newline="") as f:
    rows2 = [r for r in list(csv.reader(f))[1:] if len(r) == 15]
by_src2 = {r[1].replace("\\", "/"): r for r in rows2}
ok("H5-EXPORT-STABLE",
   by_src2["22级/数据结构/讲义.pdf"][2] == hist_target
   and by_src2["22级/数据结构/讲义.pdf"][13].endswith("rule-hist-0001"),
   f"exported={by_src2.get('22级/数据结构/讲义.pdf')}")
shot(dlg, "51_history_after_config_change.png")
RESULT["artifacts"]["history_csv_after_config_change"] = csv2

dlg.close()
pump(app, 0.3)
close_app(app, w)
DSP.stop()
finish(app)
