"""S7-2 方案导入导出与回退（真实 Qt 控件）。

覆盖：从当前配置保存方案 / 导出当前配置 / 导入方案文件 / 替换与合并差异预览 /
应用后备份 / 备份失败阻断 / 回退 / 非法方案标识不得写出方案库。
"""
from __future__ import annotations

import copy
import json
import os
import shutil

from s7_common import (make_app, ok, pump, wait, shot, Universal, finish,
                       mkfile, open_root, close_app, FakeFileDialog, RESULT,
                       OUT, BASE)

from PySide6.QtWidgets import QApplication

import main as mainmod
import transfer_dialogs as td
import scheme_store as ss
import config_transfer as ct
import classification_config as cc

# 本地方案库是本轮隔离目录；清掉上一轮遗留，保证行数可预期
_lib0 = ss.library_dir()
if os.path.isdir(_lib0):
    shutil.rmtree(_lib0)
os.makedirs(_lib0, exist_ok=True)

app = make_app()
ROOT = os.path.join(BASE, "scheme_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT, exist_ok=True)
mkfile(ROOT, "大二上/数据结构/讲义.pdf", "lecture")

td.QFileDialog = FakeFileDialog
U = Universal(app)

from main import MainWindow  # noqa: E402
w = MainWindow()
w.show()
pump(app)
ok("S0-OPEN", open_root(app, w, ROOT), w.path_edit.text())
w.on_scan()
ok("S0-SCAN", wait(app, lambda: w._plan_valid and w.current_plan is not None),
   f"total={w.current_plan.total if w.current_plan else None}")

from transfer_dialogs import SchemeCenterDialog  # noqa: E402
dlg = SchemeCenterDialog(
    root=ROOT, current_config=copy.deepcopy(w.classification_config),
    apply_callback=w._apply_scheme_config,
    restore_callback=lambda: w._restore_last_config(),
    scan_result=w.scan_result, worker_registry=w, parent=w)
dlg.show()
pump(app, 0.4)
lib = ss.library_dir()
lib_before = sorted(os.listdir(lib)) if os.path.isdir(lib) else []
ok("S0-DIALOG", dlg.table.rowCount() >= 2, f"rows={dlg.table.rowCount()} lib={lib}")
shot(dlg, "10_scheme_center.png")


def rows_named(name):
    for r in range(dlg.table.rowCount()):
        if dlg.table.item(r, 0).text() == name:
            return r
    return -1


def read_cfg():
    return cc.load_config(ROOT)[0]


def cfg_rules(cfg):
    return [r.name for r in cfg.long_term_rules]


# ---------------- S1 从当前配置保存为方案 ----------------
U.plan["inputs"] = ["我的方案A", "本地方案库用例"]
dlg.btn_save_current.click()
pump(app, 0.8)
lib_now = sorted(os.listdir(lib))
r = rows_named("我的方案A")
saved_rec = ss.get_scheme(dlg.table.item(r, 0).data(0x0100)) if r >= 0 else None
ok("S1-SAVE-SCHEME", r >= 0 and len(lib_now) == len(lib_before) + 1
   and saved_rec is not None and saved_rec.name == "我的方案A",
   f"row={r} lib_delta={len(lib_now)-len(lib_before)}")
shot(dlg, "11_saved_scheme.png")

# ---------------- S2 导出当前配置 ----------------
EXP_CUR = os.path.join(OUT, "export_current.json")
FakeFileDialog._save = (EXP_CUR, "归档盒方案 (*.json)")
U.plan["inputs"] = ["导出方案A"]
dlg.btn_export_current.click()
pump(app, 0.6)
exp_ok = os.path.isfile(EXP_CUR)
exp_data = json.load(open(EXP_CUR, encoding="utf-8")) if exp_ok else {}
ok("S2-EXPORT-CURRENT",
   exp_ok and exp_data.get("format") and exp_data.get("config")
   and exp_data.get("name") == "导出方案A"
   and len(exp_data["config"].get("long_term_rules", []))
   == len(w.classification_config.long_term_rules),
   f"path={EXP_CUR} keys={sorted(exp_data)[:8]} name={exp_data.get('name')}")

# ---------------- S3 导入方案文件 ----------------
incoming = json.load(open(EXP_CUR, encoding="utf-8"))
incoming["scheme_id"] = "imported-a-7f31"
incoming["name"] = "导入方案A"
incoming["config"]["long_term_rules"] = [{
    "rule_id": "lt-imp-a1", "name": "导入规则A", "match_type": "exact_path",
    "source_path": "大二上\\数据结构\\讲义.pdf", "include_subdirs": True,
    "keywords": [], "direction": "项目", "semester": "", "category": "导入类别A",
    "inner_mode": "keep", "inner_path": "", "enabled": True, "reason": "导入用例"}]
EXP_IN1 = os.path.join(OUT, "incoming_a.json")
with open(EXP_IN1, "w", encoding="utf-8") as f:
    json.dump(incoming, f, ensure_ascii=False, indent=2)

FakeFileDialog._open = EXP_IN1
dlg.btn_import.click()
pump(app, 1.0)
rA = rows_named("导入方案A")
ok("S3-IMPORT-FILE", rA >= 0 and not dlg.error_label.isVisible(),
   f"row={rA} err={dlg.error_label.text()!r}")
shot(dlg, "12_imported.png")

# ---------------- S4 替换应用（差异预览 + 备份 + 计划失效） ----------------
before_cfg = read_cfg()
dlg.table.selectRow(rA)
pump(app, 0.2)
ok("S4-APPLY-ENABLED", dlg.btn_apply.isEnabled(), "选中导入方案后应用按钮可用")
U.plan["choice"] = "replace"
n_bak0 = len(ct.list_backups(ROOT))
dlg.btn_apply.click()
pump(app, 1.2)
cfg_after = read_cfg()
choice_rec = U.records["choice"][-1] if U.records["choice"] else {}
prev_rec = U.records["preview"][-1] if U.records["preview"] else {}
ok("S4-DIFF-PREVIEW",
   "新增 1 条" in choice_rec.get("diff", "") and choice_rec.get("ok") is True
   and "长期规则顺序" in choice_rec.get("diff", ""),
   f"diff={choice_rec.get('diff')}")
ok("S4-IMPACT", str(prev_rec.get("status", "")).startswith("命中")
   and ROOT in prev_rec.get("impact", ""),
   f"status={prev_rec.get('status')} impact={prev_rec.get('impact','')[:100]}")
ok("S4-REPLACE-APPLIED", cfg_rules(cfg_after) == ["导入规则A"],
   f"rules={cfg_rules(cfg_after)}")
ok("S4-BACKUP-CREATED", len(ct.list_backups(ROOT)) == n_bak0 + 1,
   f"backups={len(ct.list_backups(ROOT))} summary={dlg.backup_label.text()}")
ok("S4-PLAN-INVALIDATED", (not w._plan_valid) and w.current_plan is None
   and "方案已应用" in w.statusBar().currentMessage(),
   f"valid={w._plan_valid} msg={w.statusBar().currentMessage()}")
shot(dlg, "13_applied_replace.png")

# ---------------- S5 合并应用 ----------------
incoming2 = copy.deepcopy(incoming)
incoming2["scheme_id"] = "imported-b-7f32"
incoming2["name"] = "导入方案B"
incoming2["config"]["long_term_rules"] = [{
    "rule_id": "lt-imp-b1", "name": "合并规则B", "match_type": "exact_path",
    "source_path": "其他\\说明.txt", "include_subdirs": True,
    "keywords": [], "direction": "校园事务", "semester": "", "category": "合并类别B",
    "inner_mode": "keep", "inner_path": "", "enabled": True, "reason": "合并用例"}]
EXP_IN2 = os.path.join(OUT, "incoming_b.json")
with open(EXP_IN2, "w", encoding="utf-8") as f:
    json.dump(incoming2, f, ensure_ascii=False, indent=2)
FakeFileDialog._open = EXP_IN2
dlg.btn_import.click()
pump(app, 1.0)
rB = rows_named("导入方案B")
dlg.table.selectRow(rB)
pump(app, 0.2)
U.plan["choice"] = "merge"
dlg.btn_apply.click()
pump(app, 1.2)
cfg_merge = read_cfg()
ok("S5-MERGE-RESULT",
   set(cfg_rules(cfg_merge)) == {"导入规则A", "合并规则B"}
   and "合并结果" in U.records["choice"][-1].get("diff", ""),
   f"rules={cfg_rules(cfg_merge)} diff={U.records['choice'][-1].get('diff')}")
ok("S5-MERGE-BACKUP", len(ct.list_backups(ROOT)) == n_bak0 + 2,
   f"backups={len(ct.list_backups(ROOT))}")
shot(dlg, "14_applied_merge.png")

# ---------------- S6 备份失败必须阻断应用 ----------------
incoming3 = copy.deepcopy(incoming)
incoming3["scheme_id"] = "imported-c-7f33"
incoming3["name"] = "导入方案C"
incoming3["config"]["long_term_rules"] = [{
    "rule_id": "lt-imp-c1", "name": "阻断规则C", "match_type": "exact_path",
    "source_path": "x\\y.txt", "include_subdirs": True, "keywords": [],
    "direction": "项目", "semester": "", "category": "阻断类别C",
    "inner_mode": "keep", "inner_path": "", "enabled": True, "reason": "阻断用例"}]
EXP_IN3 = os.path.join(OUT, "incoming_c.json")
with open(EXP_IN3, "w", encoding="utf-8") as f:
    json.dump(incoming3, f, ensure_ascii=False, indent=2)
FakeFileDialog._open = EXP_IN3
dlg.btn_import.click()
pump(app, 1.0)
rC = rows_named("导入方案C")
dlg.table.selectRow(rC)
pump(app, 0.2)

cfg_path = os.path.join(ROOT, ".organizer", "classification_config.json")
sha_before = (os.path.getsize(cfg_path), open(cfg_path, "rb").read())
backup_path = ct.backup_dir(ROOT)
held = backup_path + "_hold"
if os.path.isdir(held):
    shutil.rmtree(held)
if os.path.isdir(backup_path):
    os.rename(backup_path, held)      # 保留已有备份，不销毁证据
os.makedirs(os.path.dirname(backup_path), exist_ok=True)
with open(backup_path, "w", encoding="utf-8") as f:
    f.write("blocker")
U.plan["choice"] = "replace"
dlg.btn_apply.click()
pump(app, 1.5)
cfg_after_block = read_cfg()
blocked_msgs = [m["text"] for m in U.records["messages"] if "备份" in m["text"]
                or "应用方案失败" == m["title"]]
ok("S6-BACKUP-FAIL-BLOCKS",
   (os.path.getsize(cfg_path), open(cfg_path, "rb").read()) == sha_before
   and cfg_rules(cfg_after_block) == ["导入规则A", "合并规则B"]
   and any("备份" in t for t in blocked_msgs),
   f"msgs={blocked_msgs[:2]} rules={cfg_rules(cfg_after_block)}")
shot(dlg, "15_backup_blocked.png")
os.remove(backup_path)
os.rename(held, backup_path)

# ---------------- S7 回退 ----------------
n_bak2 = len(ct.list_backups(ROOT))
dlg.btn_restore.click()
pump(app, 1.0)
cfg_restored = read_cfg()
restore_msgs = [m["text"] for m in U.records["messages"] if "已恢复" in m["text"]]
ok("S7-RESTORE",
   cfg_rules(cfg_restored) == ["导入规则A"]
   and len(ct.list_backups(ROOT)) == n_bak2 - 1
   and any("已恢复" in t for t in restore_msgs),
   f"rules={cfg_rules(cfg_restored)} backups={len(ct.list_backups(ROOT))} "
   f"msgs={restore_msgs[-2:]}")
ok("S7-RESTORE-INVALIDATES", (not w._plan_valid)
   and "已恢复" in w.statusBar().currentMessage(),
   f"valid={w._plan_valid} msg={w.statusBar().currentMessage()}")
shot(dlg, "16_restored.png")

# ---------------- S8 非法方案标识不得写出方案库 ----------------
lib_before_bad = sorted(os.listdir(lib))
target = os.path.join(OUT, "evil.json")
if os.path.exists(target):
    os.remove(target)
raised = None
try:
    ss.export_scheme_to("../../evil", target)
except Exception as e:
    raised = e
ok("S8-INVALID-ID",
   raised is not None and not os.path.exists(target)
   and sorted(os.listdir(lib)) == lib_before_bad
   and ss.get_scheme("..") is None,
   f"raised={type(raised).__name__ if raised else None}: {raised}; "
   f"written={os.path.exists(target)}")

RESULT["artifacts"]["export_current"] = EXP_CUR
RESULT["artifacts"]["incoming_a"] = EXP_IN1
RESULT["artifacts"]["incoming_b"] = EXP_IN2
RESULT["artifacts"]["incoming_c"] = EXP_IN3
RESULT["artifacts"]["config_snapshot"] = cfg_path
RESULT["artifacts"]["scheme_library"] = lib
RESULT["artifacts"]["backup_dir"] = backup_path

dlg.close()
close_app(app, w)
U.stop()
finish(app)
