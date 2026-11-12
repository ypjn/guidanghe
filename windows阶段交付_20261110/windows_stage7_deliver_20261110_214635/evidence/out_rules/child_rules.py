"""S7-1 长期规则与临时修正（真实 Qt 控件）。

覆盖：临时修改 / 恢复自动分类 / 保存长期规则（试运行+确认）/ 规则缓存失效 /
优先级与排序 / 启停 / 删除 / 历史规则依据；实际移动 + 撤销后清单与 SHA 比对。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       form_field, all_texts, button_of, btn_by_text, defer_click,
                       finish, mkfile, row_by_source, open_root, close_app,
                       RESULT, OUT)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialogButtonBox,
                               QTextEdit)

import main as mainmod
import classification_config as cc

app = make_app()
ROOT = os.path.join(os.environ["ARCHIVEBOX_TEST_BASE"], "rules_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT, exist_ok=True)
F1 = "大二上/数据结构/讲义.pdf"
F2 = "大二上/数据结构/试卷.pdf"
F3 = "竞赛/蓝桥杯/报名表.pdf"
F4 = "其他/说明.txt"
for rel, data in ((F1, "lecture-1"), (F2, "exam-1"),
                  (F3, "signup-1"), (F4, "readme-1")):
    mkfile(ROOT, rel, data)

DSP = Dispatcher(app)
DONE = set()
SC = {"override": None}
RULE_ST = {}


def h_override(w):
    if id(w) in DONE:
        return
    DONE.add(id(w))
    act = SC["override"]
    if act is None:
        w.reject()
        return
    act(w)


def h_rule_draft(w):
    st = RULE_ST.setdefault(id(w), {"step": 0})
    views = w.findChildren(QTextEdit)
    if st["step"] == 0:
        b = btn_by_text(w, "试运行")
        if b is not None and b.isEnabled():
            b.click()
            st["step"] = 1
        return
    if st["step"] == 1:
        text = max((v.toPlainText() for v in views), key=len) if views else ""
        if text.strip():
            st["impact"] = text
            RESULT["artifacts"]["rule_impact_text"] = text
            bb = w.findChild(QDialogButtonBox)
            defer_click(bb.button(QDialogButtonBox.Save))
            st["step"] = 2
        return


DSP.on("修正分类", h_override)
DSP.on("保存为长期规则", h_rule_draft)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402
w = MainWindow()
w.show()
pump(app)
ok("R0-OPEN", open_root(app, w, ROOT), f"path={w.path_edit.text()}")

w.on_scan()
ok("R0-SCAN", wait(app, lambda: w._plan_valid and w.current_plan is not None),
   f"files={len(w.scan_result.files) if w.scan_result else None} "
   f"plan_total={w.current_plan.total if w.current_plan else None}")
shot(w, "01_plan.png")

# ---------------- R1 临时修改（真实界面） ----------------
r1 = row_by_source(w, F1)
item = w.current_plan.items[r1]
auto_target = item.target_rel_path
w.table.setCurrentCell(r1, 1)
w.table.selectRow(r1)
w.table.setFocus()
pump(app, 0.3)
selrows = {i.row() for i in w.table.selectedIndexes()}
ok("R1-SELECT-STATE", w.btn_override_selected.isEnabled(),
   f"row={r1} enabled={w.btn_override_selected.isEnabled()} idle="
   f"{not w._working and not w._plan_working} valid={w._plan_valid} "
   f"mode={w.current_plan.mode} selrows={selrows} hidden={w.table.isRowHidden(r1)}")


def do_temp_override(dlg):
    d = form_field(dlg, "方向")
    sem = form_field(dlg, "学期")
    cat = form_field(dlg, "类别")
    note = form_field(dlg, "修正说明")
    if not all((d, sem, cat, note)):
        raise RuntimeError("修正对话框字段缺失")
    d.setCurrentText("竞赛")
    sem.setText("")
    cat.setText("蓝桥杯")
    note.setText("界面临时修正")
    bb = dlg.findChild(QDialogButtonBox)
    bb.button(QDialogButtonBox.Ok).click()


SC["override"] = do_temp_override
w.btn_override_selected.click()
pump(app, 0.5)
it = w.current_plan.items[r1]
r1_ok = ok("R1-TEMP-OVERRIDE",
   it.is_manual_overridden and it.direction == "竞赛" and it.category == "蓝桥杯"
   and it.target_rel_path.startswith("_已整理/竞赛/蓝桥杯")
   and it.target_rel_path != auto_target,
   f"auto={auto_target} now={it.target_rel_path} dir={it.direction}")
ok("R1-TABLE-HIGHLIGHT",
   w.table.item(r1, 1).text().startswith("✎ ")
   and "本次计划共 1 个手动修正文件" in w.statusBar().currentMessage(),
   f"cell={w.table.item(r1,1).text()} msg={w.statusBar().currentMessage()}")
shot(w, "02_temp_override.png")

# ---------------- R2 恢复自动分类 ----------------
w.table.selectRow(r1)
pump(app, 0.2)


def do_revert(dlg):
    btn_by_text(dlg, "恢复自动分类").click()


SC["override"] = do_revert
w.btn_override_selected.click()
pump(app, 0.5)
it = w.current_plan.items[r1]
ok("R2-REVERT-AUTO",
   r1_ok and (not it.is_manual_overridden) and it.target_rel_path == auto_target
   and not w.table.item(r1, 1).text().startswith("✎ "),
   f"target={it.target_rel_path} auto={auto_target}")

# ---------------- R7 规则草稿缓存失效（先于保存验证） ----------------
from plan_patch import (RuleDraftDialog, OverrideParams,  # noqa: E402
                        MATCH_EXACT_PATH, MATCH_DIR_SCOPE)
sys.path.insert(0, os.path.join(os.environ["ARCHIVEBOX_SRC"], "tests"))
from windows_harness import sha256_file  # noqa: E402

rd = RuleDraftDialog(
    mode=w.current_plan.mode, plan=w.current_plan, source_rel=F3,
    params=OverrideParams(direction="竞赛", semester="",
                          category="蓝桥杯", inner_path="",
                          file_type="pdf", override_note=""),
    scan_result=w.scan_result, config=w.config if hasattr(w, "config") else
    w.classification_config,
    save_callback=lambda cfg: cc.save_config(ROOT, cfg),
    worker_registry=w, parent=None)
rd.dlg.show()
pump(app, 0.3)
rd.btn_preview.click()
ok("R7-PREVIEW-RUN", wait(app, lambda: rd._impact is not None, 20),
   f"impact error={getattr(rd._impact, 'error', None)}")
before_view = rd.impact_view.toPlainText()
rd.match_combo.setCurrentIndex(1)   # 改为目录范围
pump(app, 0.2)
ok("R7-CACHE-INVALIDATED",
   rd.impact_view.toPlainText() == "" and "重新试运行" in rd.preview_hint.text()
   and rd._impact is None,
   f"hint={rd.preview_hint.text()!r} view_empty={rd.impact_view.toPlainText()==''}")
shot(rd.dlg, "03_rule_preview_invalidated.png")
rd.dlg.reject()
pump(app, 0.2)

# ---------------- R3 保存长期规则（嵌套对话框 + 试运行 + 确认） ----------------
r3 = row_by_source(w, F1)
w.table.selectRow(r3)
pump(app, 0.2)


def do_save_rule(dlg):
    form_field(dlg, "方向").setCurrentText("项目")
    form_field(dlg, "学期").setText("")
    form_field(dlg, "类别").setText("课程项目A")
    form_field(dlg, "内部路径").setText("资料")
    defer_click(btn_by_text(dlg, "保存为长期规则"))


SC["override"] = do_save_rule
cfg_path = os.path.join(ROOT, ".organizer", "classification_config.json")
w.btn_override_selected.click()
pump(app, 1.0)
saved = None
if os.path.isfile(cfg_path):
    with open(cfg_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
rules = (saved or {}).get("long_term_rules", [])
ok("R3-SAVE-LONGTERM",
   len(rules) == 1 and rules[0].get("enabled") is True
   and rules[0].get("direction") == "项目"
   and rules[0].get("category") == "课程项目A"
   and rules[0].get("match_type") == MATCH_EXACT_PATH,
   f"rules={json.dumps(rules, ensure_ascii=False)[:300]}")
ok("R3-IMPACT-SHOWN", "命中" in RESULT["artifacts"].get("rule_impact_text", ""),
   RESULT["artifacts"].get("rule_impact_text", "")[:120])
ok("R3-PLAN-INVALIDATED",
   (not w._plan_valid) and w.current_plan is None
   and w.invalid_label.isVisible() and w.table.rowCount() == 0,
   f"valid={w._plan_valid} plan={w.current_plan} label={w.invalid_label.isVisible()}")
shot(w, "04_saved_longterm.png")

# 重新预览：新规则生效
w.btn_regenerate.click()
ok("R4-RULE-EFFECT", wait(app, lambda: w._plan_valid and w.current_plan is not None),
   f"total={w.current_plan.total if w.current_plan else None}")
it = w.current_plan.items[row_by_source(w, F1)]
ok("R4-RULE-TARGET",
   it.target_rel_path.startswith("_已整理/项目/课程项目A")
   and "资料" in it.target_rel_path.split(os.sep)[-2:][0],
   f"target={it.target_rel_path} rule={it.rule_applied} key={it.rule_key}")
ok("R4-RULE-BASIS", (it.rule_key or "").startswith("long_term:"),
   f"rule_key={it.rule_key} rule_applied={it.rule_applied}")
shot(w, "05_rule_applied.png")

# ---------------- R5 优先级与排序（配置编辑器真实控件） ----------------
from config_dialog import ClassificationConfigDialog  # noqa: E402

cfg, _src = cc.load_config(ROOT)
second = cc.LongTermRule(
    rule_id=cc.new_rule_id(), name="二号规则", match_type=MATCH_EXACT_PATH,
    source_path=F1, direction="校园事务", semester="", category="事务B")
cfg.long_term_rules.insert(0, second)   # 二号规则优先级最高
cc.save_config(ROOT, cfg)
w._load_root_config(ROOT)
w._invalidate_for_config_change()
w.btn_regenerate.click()
wait(app, lambda: w._plan_valid and w.current_plan is not None)
it = w.current_plan.items[row_by_source(w, F1)]
ok("R5-PRIORITY-FIRST",
   it.target_rel_path.startswith("_已整理/校园事务/事务B")
   and it.rule_key == "long_term:" + second.rule_id,
   f"target={it.target_rel_path} key={it.rule_key}")

saved_cfg = {}


def save_cb(cfg2):
    cc.save_config(ROOT, cfg2)
    saved_cfg["cfg"] = cfg2


cd = ClassificationConfigDialog(
    root=ROOT, config=cc.load_config(ROOT)[0], using_defaults=False,
    config_error="", scan_result=w.scan_result,
    base_config=cc.load_config(ROOT)[0], save_callback=save_cb,
    worker_registry=w, parent=None)
cd.show()
pump(app, 0.3)
tbl = cd.table_longterm
ok("R5-EDITOR-ROWS", tbl.rowCount() == 2, f"rows={tbl.rowCount()}")
tbl.selectRow(0)
pump(app, 0.1)
btn_by_text(cd, "下移（降低优先级）").click()
pump(app, 0.2)
cd.buttons.button(QDialogButtonBox.Save).click()
pump(app, 0.8)
order_cfg = json.load(open(cfg_path, encoding="utf-8"))["long_term_rules"]
order = [r.get("name") for r in order_cfg]
first_id = order_cfg[0]["rule_id"]
ok("R5-REORDER-SAVED", order[0].endswith("修正规则") and order[1] == "二号规则",
   f"order={order}")
w._load_root_config(ROOT)
w._invalidate_for_config_change()
w.btn_regenerate.click()
wait(app, lambda: w._plan_valid and w.current_plan is not None)
it = w.current_plan.items[row_by_source(w, F1)]
ok("R5-ORDER-TAKES-EFFECT",
   it.target_rel_path.startswith("_已整理/项目/课程项目A")
   and it.rule_key == "long_term:" + first_id,
   f"target={it.target_rel_path} key={it.rule_key} first={first_id}")
shot(w, "06_reordered.png")

# ---------------- R6 启停 ----------------
saved_cfg.clear()
cd2 = ClassificationConfigDialog(
    root=ROOT, config=cc.load_config(ROOT)[0], using_defaults=False,
    config_error="", scan_result=w.scan_result,
    base_config=cc.load_config(ROOT)[0], save_callback=save_cb,
    worker_registry=w, parent=None)
cd2.show()
pump(app, 0.3)
cell = cd2.table_longterm.item(0, 0)
cell.setCheckState(Qt.Unchecked)
pump(app, 0.1)
cd2.buttons.button(QDialogButtonBox.Save).click()
pump(app, 0.8)
en = [r.get("enabled") for r in json.load(open(cfg_path, encoding="utf-8"))["long_term_rules"]]
ok("R6-DISABLED-SAVED", en[0] is False, f"enabled={en}")
w._load_root_config(ROOT)
w._invalidate_for_config_change()
w.btn_regenerate.click()
wait(app, lambda: w._plan_valid and w.current_plan is not None)
it = w.current_plan.items[row_by_source(w, F1)]
ok("R6-DISABLED-NO-MATCH",
   it.target_rel_path.startswith("_已整理/校园事务/事务B")
   and it.rule_key == "long_term:" + second.rule_id,
   f"target={it.target_rel_path} key={it.rule_key}")

# ---------------- R7 删除 ----------------
saved_cfg.clear()
cd3 = ClassificationConfigDialog(
    root=ROOT, config=cc.load_config(ROOT)[0], using_defaults=False,
    config_error="", scan_result=w.scan_result,
    base_config=cc.load_config(ROOT)[0], save_callback=save_cb,
    worker_registry=w, parent=None)
cd3.show()
pump(app, 0.3)
for _ in range(cd3.table_longterm.rowCount()):
    cd3.table_longterm.selectRow(0)
    pump(app, 0.05)
    btn_by_text(cd3, "删除选中").click()
    pump(app, 0.05)
cd3.buttons.button(QDialogButtonBox.Save).click()
pump(app, 0.8)
left = (json.load(open(cfg_path, encoding="utf-8"))["long_term_rules"]
        if os.path.isfile(cfg_path) else [])
ok("R7-DELETE-SAVED", left == [],
   f"left={left} cfg_exists={os.path.isfile(cfg_path)}")
w._load_root_config(ROOT)
w._invalidate_for_config_change()
w.btn_regenerate.click()
wait(app, lambda: w._plan_valid and w.current_plan is not None)
it = w.current_plan.items[row_by_source(w, F1)]
ok("R7-DELETE-BACK-TO-AUTO", it.target_rel_path == auto_target
   and not (it.rule_key or "").startswith("long_term:"),
   f"target={it.target_rel_path} auto={auto_target} key={it.rule_key}")
shot(w, "07_rules_deleted.png")

# ---------------- R8 实际移动 + 历史依据 + 撤销清单比对 ----------------
before = os.path.join(OUT, "manifest_before_move.json")
import windows_harness as H  # noqa: E402
H.save_manifest(before, H.full_manifest(ROOT))


def h_confirm(wd):
    RESULT.setdefault("artifacts", {}).setdefault("confirm_texts", []).append(all_texts(wd))
    bb = wd.findChild(QDialogButtonBox)
    bb.button(QDialogButtonBox.Ok).click()


DSP.on("确认整理计划", h_confirm)
for r in range(w.table.rowCount()):
    cb = w.table.cellWidget(r, 0).findChild(QCheckBox)
    if cb is not None:
        cb.setChecked(r == row_by_source(w, F1))
pump(app, 0.2)
ok("R8-ONE-SELECTED", w.current_plan.selected_count == 1,
   f"selected={w.current_plan.selected_count}")
w.btn_execute.click()
ok("R8-EXEC-DONE", wait(app, lambda: not w._working, 60),
   f"working={w._working}")
batch = w._storage.get_latest_batch()
acts = w._storage.get_actions(batch.batch_id) if batch else []
a = acts[0] if acts else None
moved_abs = os.path.join(ROOT, a.target_rel) if a else ""
ok("R8-HISTORY-BASIS", a is not None and a.status == "moved"
   and a.rule_key == it.rule_key and a.rule_applied == it.rule_applied
   and a.target_rel == it.target_rel_path
   and a.classification_source == "自动",
   f"status={getattr(a,'status',None)} key={getattr(a,'rule_key',None)} "
   f"target={getattr(a,'target_rel',None)} src={getattr(a,'classification_source',None)}")
ok("R8-FILE-MOVED", os.path.isfile(moved_abs), moved_abs)
shot(w, "08_after_execute.png")

after_move = os.path.join(OUT, "manifest_after_move.json")
H.save_manifest(after_move, H.full_manifest(ROOT))
RESULT["artifacts"]["manifest_before_move"] = before
RESULT["artifacts"]["manifest_after_move"] = after_move

w.btn_undo.click()
ok("R8-UNDO-DONE", wait(app, lambda: (not w._working), 40))
after_undo = os.path.join(OUT, "manifest_after_undo.json")
m_undo = H.full_manifest(ROOT)
H.save_manifest(after_undo, m_undo)
RESULT["artifacts"]["manifest_after_undo"] = after_undo
diff = H.diff_manifests(H.load_manifest(before), m_undo)
ok("R8-UNDO-MANIFEST", diff == {"missing": [], "extra": [], "changed": []},
   f"diff={json.dumps(diff, ensure_ascii=False)} count={len(m_undo)}")
ok("R8-UNDO-SHA", F1 in m_undo
   and m_undo[F1]["sha256"] == H.load_manifest(before)[F1]["sha256"],
   "SHA-256 与移动前一致")

# 撤销后历史仍可查且记录撤销状态
b2 = w._storage.get_latest_batch()
a2 = w._storage.get_actions(b2.batch_id)[0]
ok("R8-HISTORY-AFTER-UNDO", a2.status == "undone" and b2.status in
   ("undone", "partial_undone"), f"action={a2.status} batch={b2.status}")

shot(w, "09_after_undo.png")
close_app(app, w)
DSP.stop()
finish(app)
