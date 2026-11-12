"""S5：范围失效 / 生产后台配置快照 / 确定性迟到结果 / 计划取消 / 生命周期证据。

全部通过真实控件与产品入口驱动（QTest + 模态对话框显式应答），
不使用「测试自己构造 PlanWorker 再直接调 generate_plan」的方式，
也不以搜索源码字符串代替行为验证。

只在后台线程入口注入一个**同步闸门**（gated wrapper），
真实的 scan_directory / generate_plan 仍然执行，用于把
「任务运行中」这一时刻变成确定性可观测点。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, run_child, kv,  # noqa: E402
                             full_manifest, save_manifest)

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_s5p"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "s5p")
CH = rd.sub("children")
SHOTS = rd.sub("shots")
R = Results("s5_plan_scope")
print(f"=== S5 run={rd.run_id} ===")


def mkfile(root, rel, data="x"):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data.encode("utf-8"))
    return p


HEAD = r'''
import json, os, sys, threading, time
sys.path.insert(0, r"__SRC__")
sys.path.insert(0, r"__TESTS__")
os.environ.pop("QT_QPA_PLATFORM", None)
BASE = r"__BASE__"
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(BASE, "recent.json")
os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(BASE, "appdata")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(BASE, "appdata", "schemes")
from PySide6.QtWidgets import (QApplication, QMessageBox, QFileDialog,
                               QDialogButtonBox)
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
import main, recent_spaces
from windows_harness import AutoOk, arm_modal, dialog_text, pump, wait_until, shot

EV = []
def mark(name, extra=None):
    d = {"t": time.perf_counter(), "ev": name}
    if extra:
        d.update(extra)
    EV.append(d)

def snap(w, tag):
    plan = w.current_plan
    return {
        "tag": tag,
        "path": w.path_edit.text().replace("\\", "/"),
        "rows": w.table.rowCount(),
        "plan_items": (0 if plan is None else len(plan.items)),
        "selected": (0 if plan is None else plan.selected_count),
        "sources": ([] if plan is None else
                    [i.file.rel_path.replace("\\", "/") for i in plan.items]),
        "targets": ([] if plan is None else
                    [i.target_rel_path.replace("\\", "/") for i in plan.items]),
        "plan_valid": bool(w._plan_valid),
        "invalid_visible": bool(w.invalid_label.isVisible()),
        "invalid_text": w.invalid_label.text(),
        "execute_enabled": bool(w.btn_execute.isEnabled()),
        "regenerate_enabled": bool(w.btn_regenerate.isEnabled()),
        "scan_result_none": w.scan_result is None,
        "scope": getattr(w, "_plan_scope", "?"),
        "seq": w._current_plan_seq,
        "status": w.statusBar().currentMessage(),
    }
'''

# ---------------------------------------------------------------- S1 范围失效
root1 = rd.sub("s1root")
mkfile(root1, "大二上/数据结构/讲义.pdf", "S1-A")
mkfile(root1, "图片素材/logo.png", "S1-B")
mkfile(root1, "其他/散件.txt", "S1-C")

S1 = (HEAD + r'''
ROOT = r"__ROOT__"
OUTJ = r"__OUTJ__"
recent_spaces.touch(ROOT)
w = main.MainWindow(); w.show()
auto = AutoOk(app)
wait_until(app, lambda: w.isVisible(), 20, "visible")
w._open_recent_space(ROOT)
pump(app, 0.4)
obs = {"dialogs": auto.seen, "events": EV}

QTest.mouseClick(w.btn_intake, Qt.LeftButton)
wait_until(app, lambda: w._intake_dialog is not None
           and w._intake_dialog.isVisible(), 10, "intake dialog")
dlg = w._intake_dialog
obs["intake_dialog_visible"] = bool(dlg is not None and dlg.isVisible())

picks = [os.path.join(ROOT, "大二上", "数据结构"),
         os.path.join(ROOT, "图片素材")]
QFileDialog.getExistingDirectory = staticmethod(
    lambda *a, **k: picks.pop(0) if picks else "")
QTest.mouseClick(dlg.btn_add_dir, Qt.LeftButton)
pump(app, 0.4)
obs["after_add1"] = {"list_count": dlg.list_widget.count(),
                     "intake_len": len(w.intake),
                     "list_texts": [dlg.list_widget.item(i).text()
                                    for i in range(dlg.list_widget.count())]}

QTest.mouseClick(w.btn_scan_intake, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 60, "scan1")
pump(app, 0.4)
obs["after_scan1"] = snap(w, "after_scan1")
mark("scope_change_start")
QTest.mouseClick(dlg.btn_add_dir, Qt.LeftButton)
pump(app, 0.5)
obs["after_add2"] = {"list_count": dlg.list_widget.count(),
                     "intake_len": len(w.intake)}
obs["after_invalidate"] = snap(w, "after_invalidate")
obs["invalidate_status"] = w.statusBar().currentMessage()

if w.btn_regenerate.isEnabled():
    QTest.mouseClick(w.btn_regenerate, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 60, "regen")
pump(app, 0.5)
obs["after_regen"] = snap(w, "after_regen")

# 真实确认界面：读取根目录/范围/已选统计后取消（不执行移动）
st = arm_modal(lambda d: d.findChild(QDialogButtonBox).button(
    QDialogButtonBox.Cancel), timeout=10)
QTest.mouseClick(w.btn_execute, Qt.LeftButton)
pump(app, 0.5)
obs["confirm"] = {"captured": st["captured"], "error": st["error"],
                  "handled": st["handled"]}
obs["events"] = EV
with open(OUTJ, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
auto.stop()
w.close(); pump(app, 0.3)
print("S1_DONE")
''').replace("__SRC__", SRC).replace("__TESTS__", TESTS).replace(
    "__BASE__", BASE).replace("__ROOT__", root1).replace(
    "__OUTJ__", os.path.join(CH, "s1.json"))

s1_before = full_manifest(root1)
rec = run_child(S1, "s1_scope", CH, BASE, timeout=300)
try:
    with open(os.path.join(CH, "s1.json"), "r", encoding="utf-8") as f:
        o1 = json.load(f)
except Exception:
    o1 = {}


def g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


R.check("S1_1", "真实“添加文件夹…”控件把来源目录加入待整理清单",
        g(o1, "intake_dialog_visible") is True
        and g(o1, "after_add1", "list_count") == 1
        and g(o1, "after_add1", "intake_len") == 1,
        f"after_add1={g(o1, 'after_add1')}")
R.check("S1_2", "旧计划本来可执行（清单范围扫描后计划有效、执行按钮可用）",
        g(o1, "after_scan1", "plan_items") == 1
        and g(o1, "after_scan1", "plan_valid") is True
        and g(o1, "after_scan1", "execute_enabled") is True
        and g(o1, "after_scan1", "scope") == "intake",
        f"after_scan1={g(o1, 'after_scan1')}")
R.check("S1_3", "通过产品范围入口改变来源范围后，旧计划立即不可执行",
        g(o1, "after_add2", "list_count") == 2
        and g(o1, "after_add2", "intake_len") == 2
        and g(o1, "after_invalidate", "plan_items") == 0
        and g(o1, "after_invalidate", "plan_valid") is False
        and g(o1, "after_invalidate", "execute_enabled") is False
        and g(o1, "after_invalidate", "rows") == 0
        and g(o1, "after_invalidate", "invalid_visible") is True
        and "范围已变化" in (g(o1, "after_invalidate", "invalid_text") or ""),
        f"after_add2={g(o1, 'after_add2')} "
        f"after_invalidate={g(o1, 'after_invalidate')}")
R.check("S1_4", "按新范围重新生成：条目数与来源数与新范围一致、计划可执行",
        g(o1, "after_regen", "plan_items") == 2
        and len(g(o1, "after_regen", "sources") or []) == 2
        and all(s.startswith(("大二上/数据结构/", "图片素材/"))
                for s in (g(o1, "after_regen", "sources") or []))
        and g(o1, "after_regen", "plan_valid") is True
        and g(o1, "after_regen", "execute_enabled") is True,
        f"after_regen={g(o1, 'after_regen')}")
cf = g(o1, "confirm", "captured") or {}
import re as _re
cftext = _re.sub(r"<[^>]+>", "", (cf.get("text") or "")).replace("\\", "/")
R.check("S1_5", "执行确认界面显示的根目录与已选统计与新范围一致（随后取消，不移动文件）",
        g(o1, "confirm", "handled") is True
        and cf.get("class") == "ExecuteConfirmDialog"
        and "已选文件：2 / 2" in cftext
        and root1.replace("\\", "/") in cftext,
        f"class={cf.get('class')} text={cftext[:400]}")
R.check("S1_6", "确认界面取消后磁盘无任何移动（清单与整理前一致）",
        full_manifest(root1) == s1_before,
        f"diff={sorted(set(full_manifest(root1)) ^ set(s1_before))}")

# ------------------------------------------------------- S2 生产后台配置快照
root2 = rd.sub("s2root")
mkfile(root2, "大二上/数据结构/讲义.pdf", "S2-A")
mkfile(root2, "大二上/数据结构/实验.pdf", "S2-B")

S2 = (HEAD + r'''
ROOT = r"__ROOT__"
OUTJ = r"__OUTJ__"
from classification_config import save_config
import copy
recent_spaces.touch(ROOT)
w = main.MainWindow(); w.show()
auto = AutoOk(app)
wait_until(app, lambda: w.isVisible(), 20, "visible")
w._open_recent_space(ROOT)
pump(app, 0.4)

GATE = {"entered": threading.Event(), "release": threading.Event()}
rear = {"real": main.generate_plan}
def gated(*a, **kw):
    GATE["entered"].set()
    GATE["release"].wait(60)
    return rear["real"](*a, **kw)
main.generate_plan = gated

obs = {"dialogs": auto.seen}

# --- 阶段 A：任务启动时快照，界面对象中途被改也不影响本次结果
QTest.mouseClick(w.btn_scan, Qt.LeftButton)
entered = wait_until(app, lambda: GATE["entered"].is_set(), 60, "plan gate A")
obs["A_gate_entered"] = bool(entered)
mark("A_plan_task_running")
before_cfg = sorted({r.direction for r in w.classification_config.semester_dir_rules
                     if r.semester == "大二上" and r.source == "数据结构"})
n_mut = 0
for r in w.classification_config.semester_dir_rules:
    if r.semester == "大二上" and r.source == "数据结构":
        r.direction = "项目"
        n_mut += 1
obs["A_mutated_ui_object"] = {"n": n_mut, "before": before_cfg,
    "after": sorted({r.direction for r in w.classification_config.semester_dir_rules
                     if r.semester == "大二上" and r.source == "数据结构"})}
mark("A_config_object_mutated")
GATE["release"].set()
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "plan A done")
pump(app, 0.4)
obs["A_after"] = snap(w, "A_after")

# --- 阶段 B：计划任务进行中经产品入口保存配置 → 按失效机制丢弃旧结果
GATE["entered"].clear(); GATE["release"].clear()
QTest.mouseClick(w.btn_regenerate, Qt.LeftButton)
wait_until(app, lambda: GATE["entered"].is_set(), 60, "plan gate B")
obs["B_gate_entered"] = bool(GATE["entered"].is_set())
seq_before = w._current_plan_seq
cfg2 = copy.deepcopy(w.classification_config)
w._save_classification_config(ROOT, cfg2)
pump(app, 0.5)
seq_after = w._current_plan_seq
mark("B_config_saved_via_product_entry")
obs["B_seq"] = {"before": seq_before, "after": seq_after}
obs["B_after_save"] = snap(w, "B_after_save")
GATE["release"].set()
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "plan B done")
pump(app, 0.6)
obs["B_after_release"] = snap(w, "B_after_release")
mark("B_late_result_settled")

# --- 阶段 C：重新生成 → 新配置生效，且计划可执行
if w.btn_regenerate.isEnabled():
    QTest.mouseClick(w.btn_regenerate, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "plan C")
pump(app, 0.4)
obs["C_after"] = snap(w, "C_after")
obs["events"] = EV
with open(OUTJ, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
auto.stop()
w.close(); pump(app, 0.3)
print("S2_DONE")
''').replace("__SRC__", SRC).replace("__TESTS__", TESTS).replace(
    "__BASE__", BASE).replace("__ROOT__", root2).replace(
    "__OUTJ__", os.path.join(CH, "s2.json"))

rec2 = run_child(S2, "s2_snapshot", CH, BASE, timeout=420)
try:
    with open(os.path.join(CH, "s2.json"), "r", encoding="utf-8") as f:
        o2 = json.load(f)
except Exception:
    o2 = {}

R.check("S2_1", "经界面启动的计划任务确实处于运行中（同步闸门被后台线程命中）",
        g(o2, "A_gate_entered") is True,
        f"A_gate_entered={g(o2, 'A_gate_entered')}")
A = g(o2, "A_after") or {}
R.check("S2_2", "任务运行中修改的是界面配置对象本身（实测已改为 项目）",
        g(o2, "A_mutated_ui_object", "n", default=0) >= 1
        and g(o2, "A_mutated_ui_object", "after") == ["项目"],
        f"ui_object={g(o2, 'A_mutated_ui_object')}")
R.check("S2_3", "该任务按启动快照产出结果（目标仍按旧配置 课程，而非 项目）",
        A.get("plan_items") == 2 and A.get("plan_valid") is True
        and all(t.startswith("_已整理/课程/") for t in (A.get("targets") or []))
        and not any(t.startswith("_已整理/项目/") for t in (A.get("targets") or [])),
        f"A_targets={A.get('targets')}")
R.check("S2_4", "任务进行中经产品入口保存配置 → 任务标识(seq)递增，旧结果失效",
        g(o2, "B_gate_entered") is True
        and g(o2, "B_seq", "after") == g(o2, "B_seq", "before") + 1,
        f"B_seq={g(o2, 'B_seq')}")
B = g(o2, "B_after_release") or {}
R.check("S2_5", "放行后旧任务结果被丢弃：无可执行计划、无预览行、执行按钮禁用",
        B.get("plan_items") == 0 and B.get("plan_valid") is False
        and B.get("rows") == 0 and B.get("execute_enabled") is False
        and B.get("invalid_visible") is True,
        f"B_after_release={B}")
C = g(o2, "C_after") or {}
R.check("S2_6", "重新生成后新配置生效（目标改到 项目）且计划可执行",
        C.get("plan_items") == 2 and C.get("plan_valid") is True
        and C.get("execute_enabled") is True
        and all(t.startswith("_已整理/项目/") for t in (C.get("targets") or [])),
        f"C_targets={C.get('targets')} status={C.get('status')}")

# ------------------------------------------------- S3 确定性迟到结果（扫描）
rootA = rd.sub("s3A")
rootB = rd.sub("s3B")
mkfile(rootA, "大二上/数据结构/A_ONLY.pdf", "A")
mkfile(rootA, "大二上/数据结构/A2.pdf", "A2")
mkfile(rootB, "大三下/计算机网络/B_ONLY.pdf", "B")

S3 = (HEAD + r'''
ROOTA = r"__ROOTA__"; ROOTB = r"__ROOTB__"
OUTJ = r"__OUTJ__"
recent_spaces.touch(ROOTA); recent_spaces.touch(ROOTB)
w = main.MainWindow(); w.show()
auto = AutoOk(app)
wait_until(app, lambda: w.isVisible(), 20, "visible")
w._open_recent_space(ROOTA)
pump(app, 0.4)
obs = {"dialogs": auto.seen}

GATE = {"entered": threading.Event(), "release": threading.Event()}
real_scan = main.scan_directory
def gated_scan(*a, **kw):
    GATE["entered"].set()
    GATE["release"].wait(60)
    return real_scan(*a, **kw)
main.scan_directory = gated_scan

QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: GATE["entered"].is_set(), 60, "scan gate")
mark("A_scan_worker_blocked")
obs["scan_running"] = bool(w._working)
obs["cancel_visible_during_scan"] = bool(w.btn_cancel.isVisible())
obs["cancel_enabled_during_scan"] = bool(w.btn_cancel.isEnabled())
obs["storage"] = None if w._storage is None else w._storage.root
w._open_recent_space(ROOTB)
pump(app, 0.5)
mark("switched_to_B")
obs["after_switch"] = snap(w, "after_switch")
GATE["release"].set()
mark("scan_gate_released")
got = wait_until(app, lambda: "已忽略旧目录的扫描结果"
                 in w.statusBar().currentMessage(), 60, "late scan result")
mark("late_result_delivered", {"observed": bool(got)})
obs["late_observed"] = bool(got)
obs["late_status"] = w.statusBar().currentMessage()
pump(app, 0.5)
obs["after_late"] = snap(w, "after_late")

QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "scan B")
pump(app, 0.4)
obs["after_scan_B"] = snap(w, "after_scan_B")
obs["events"] = EV
with open(OUTJ, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
auto.stop()
w.close(); pump(app, 0.3)
print("S3_DONE")
''').replace("__SRC__", SRC).replace("__TESTS__", TESTS).replace(
    "__BASE__", BASE).replace("__ROOTA__", rootA).replace(
    "__ROOTB__", rootB).replace("__OUTJ__", os.path.join(CH, "s3.json"))

a_before = full_manifest(rootA)
rec3 = run_child(S3, "s3_late", CH, BASE, timeout=300)
try:
    with open(os.path.join(CH, "s3.json"), "r", encoding="utf-8") as f:
        o3 = json.load(f)
except Exception:
    o3 = {}

order = [e["ev"] for e in (o3.get("events") or [])]
R.check("S3_1", "扫描进行中：任务运行态为真，且产品没有提供扫描取消入口（按钮不可见/禁用）",
        g(o3, "scan_running") is True
        and g(o3, "cancel_visible_during_scan") is False
        and g(o3, "cancel_enabled_during_scan") is False,
        f"running={g(o3, 'scan_running')} visible={g(o3, 'cancel_visible_during_scan')} "
        f"enabled={g(o3, 'cancel_enabled_during_scan')}（记录为产品未提供扫描取消）")
R.rec("S3_2", "扫描中用户主动取消：产品未提供该入口，本轮未验收"
      "（不以求证式的执行中取消替代）", "UNTESTED",
      f"cancel_button_visible={g(o3, 'cancel_visible_during_scan')}")
R.check("S3_3", "顺序可证：A 扫描阻塞 → 切到 B → 放行 → 迟到结果才送达",
        order.index("A_scan_worker_blocked") < order.index("switched_to_B")
        < order.index("scan_gate_released") < order.index("late_result_delivered")
        if all(k in order for k in ("A_scan_worker_blocked", "switched_to_B",
                                    "scan_gate_released", "late_result_delivered"))
        else False,
        f"order={order}")
L = g(o3, "after_late") or {}
R.check("S3_4", "迟到扫描结果未污染 B：界面仍是 B、无计划、无预览行、无 A 的来源",
        L.get("path", "").replace("\\", "/") == rootB.replace("\\", "/")
        and L.get("plan_items") == 0 and L.get("rows") == 0
        and L.get("scan_result_none") is True
        and not any("A_ONLY" in s for s in (L.get("sources") or []))
        and "已忽略旧目录的扫描结果" in (g(o3, "late_status") or ""),
        f"after_late={L} late_status={g(o3, 'late_status')}")
R.check("S3_5", "迟到结果未移动任何文件（A 目录清单不变）",
        full_manifest(rootA) == a_before,
        f"diff={sorted(set(full_manifest(rootA)) ^ set(a_before))}")
B2 = g(o3, "after_scan_B") or {}
R.check("S3_6", "切到 B 后可正常扫描并生成有效可执行计划（含 B 的文件）",
        B2.get("plan_items") == 1 and B2.get("plan_valid") is True
        and B2.get("execute_enabled") is True
        and any("B_ONLY" in s for s in (B2.get("sources") or [])),
        f"after_scan_B={B2}")

# --------------------------------------------------------- S4 计划取消
root4 = rd.sub("s4root")
for i in (1, 2, 3):
    mkfile(root4, f"大二上/数据结构/讲义{i}.pdf", f"S4-{i}")

S4 = (HEAD + r'''
ROOT = r"__ROOT__"
OUTJ = r"__OUTJ__"
recent_spaces.touch(ROOT)
w = main.MainWindow(); w.show()
auto = AutoOk(app)
wait_until(app, lambda: w.isVisible(), 20, "visible")
w._open_recent_space(ROOT)
pump(app, 0.4)
obs = {"dialogs": auto.seen}
GATE = {"entered": threading.Event(), "release": threading.Event()}
real_gen = main.generate_plan
def gated(*a, **kw):
    GATE["entered"].set()
    GATE["release"].wait(60)
    return real_gen(*a, **kw)
main.generate_plan = gated

QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: GATE["entered"].is_set(), 60, "gate")
mark("plan_task_running")
obs["cancel_visible"] = bool(w.btn_cancel.isVisible())
obs["cancel_enabled"] = bool(w.btn_cancel.isEnabled())
QTest.mouseClick(w.btn_cancel, Qt.LeftButton)
pump(app, 0.3)
mark("cancel_clicked")
obs["cancel_state"] = {
    "cancel_seq": w._plan_cancel_seq, "cur_seq": w._current_plan_seq,
    "requested": bool(w._plan_cancel_requested),
    "status": w.statusBar().currentMessage(),
    "cancel_btn_enabled_after": bool(w.btn_cancel.isEnabled()),
}
GATE["release"].set()
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "cancelled")
pump(app, 0.5)
obs["after_cancel"] = snap(w, "after_cancel")
obs["batches_after_cancel"] = (None if w._storage is None else
                               len(w._storage.list_batches()))
_pb = None if w._storage is None else w._storage.get_pending_batch()
obs["stray_iter_batches"] = None if _pb is None else _pb.batch_id
if w.btn_regenerate.isEnabled():
    QTest.mouseClick(w.btn_regenerate, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "regen")
pump(app, 0.4)
obs["after_regen"] = snap(w, "after_regen")
obs["events"] = EV
with open(OUTJ, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
auto.stop()
w.close(); pump(app, 0.3)
print("S4_DONE")
''').replace("__SRC__", SRC).replace("__TESTS__", TESTS).replace(
    "__BASE__", BASE).replace("__ROOT__", root4).replace(
    "__OUTJ__", os.path.join(CH, "s4.json"))

r4_before = full_manifest(root4)
rec4 = run_child(S4, "s4_plan_cancel", CH, BASE, timeout=300)
try:
    with open(os.path.join(CH, "s4.json"), "r", encoding="utf-8") as f:
        o4 = json.load(f)
except Exception:
    o4 = {}
CS = g(o4, "cancel_state") or {}
AC = g(o4, "after_cancel") or {}
RG = g(o4, "after_regen") or {}
R.check("S4_1", "计划生成中取消按钮可见可用，点击后进入取消相位（seq 记录一致）",
        g(o4, "cancel_visible") is True and g(o4, "cancel_enabled") is True
        and CS.get("cancel_seq") == CS.get("cur_seq")
        and CS.get("requested") is True
        and "正在取消计划生成" in (CS.get("status") or "")
        and CS.get("cancel_btn_enabled_after") is False,
        f"cancel_state={CS}")
R.check("S4_2", "取消后不发布可执行半成品：无计划、无预览行、执行按钮禁用、提示取消",
        AC.get("plan_items") == 0 and AC.get("plan_valid") is False
        and AC.get("rows") == 0 and AC.get("execute_enabled") is False
        and AC.get("invalid_visible") is True
        and "取消" in (AC.get("invalid_text") or ""),
        f"after_cancel={AC}")
R.check("S4_3", "取消不移动文件、不写入任何批次历史",
        full_manifest(root4) == r4_before
        and g(o4, "batches_after_cancel") == 0
        and g(o4, "stray_iter_batches") is None,
        f"batches={g(o4, 'batches_after_cancel')} "
        f"pending={g(o4, 'stray_iter_batches')}")
R.check("S4_4", "取消后可重新生成有效计划并恢复可执行",
        RG.get("plan_items") == 3 and RG.get("plan_valid") is True
        and RG.get("execute_enabled") is True,
        f"after_regen={RG}")

# --------------------------------------------- S5 生命周期证据（连接/线程/顺序）
root5 = rd.sub("s5root")
mkfile(root5, "大二上/数据结构/讲义.pdf", "S5-A")
mkfile(root5, "大二上/数据结构/作业.pdf", "S5-B")

S5 = (HEAD + r'''
ROOT = r"__ROOT__"
OUTJ = r"__OUTJ__"
import sqlite3
recent_spaces.touch(ROOT)
EVENTS = []
class LoggedConn(sqlite3.Connection):
    def close(self):
        EVENTS.append({"t": time.perf_counter(), "ev": "conn_close",
                       "id": id(self), "thread": threading.get_ident(),
                       "thread_name": threading.current_thread().name})
        return super().close()
_real = sqlite3.connect
def _connect(*a, **kw):
    kw.setdefault("factory", LoggedConn)
    c = _real(*a, **kw)
    EVENTS.append({"t": time.perf_counter(), "ev": "conn_connect", "id": id(c),
                   "db": (a[0] if a else kw.get("database")),
                   "thread": threading.get_ident(),
                   "thread_name": threading.current_thread().name})
    return c
sqlite3.connect = _connect

w = main.MainWindow(); w.show()
auto = AutoOk(app)
wait_until(app, lambda: w.isVisible(), 20, "visible")
w._open_recent_space(ROOT)
pump(app, 0.4)
obs = {"main_thread": threading.main_thread().name,
       "main_thread_ident": threading.main_thread().ident}
QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: (not w._working and not w._plan_working), 90, "scan")
pump(app, 0.4)
obs["plan_items"] = (0 if w.current_plan is None else len(w.current_plan.items))
ew_holder = {}
st = arm_modal(lambda d: d.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok),
               timeout=10)
QTest.mouseClick(w.btn_execute, Qt.LeftButton)
wait_until(app, lambda: (not w._working), 120, "execute")
pump(app, 0.6)
obs["exec_confirm"] = st["captured"] is not None
obs["after_exec_status"] = w.statusBar().currentMessage()
EVENTS.append({"t": time.perf_counter(), "ev": "exec_thread_finished_observed",
               "thread": threading.get_ident(),
               "thread_name": threading.current_thread().name})

st2 = arm_modal(lambda d: d.button(QMessageBox.Yes), timeout=10)
QTest.mouseClick(w.btn_undo, Qt.LeftButton)
wait_until(app, lambda: (not w._working), 120, "undo")
pump(app, 0.6)
obs["undo_confirm"] = st2["captured"] is not None
obs["after_undo_status"] = w.statusBar().currentMessage()

EVENTS.append({"t": time.perf_counter(), "ev": "close_requested"})
w.close()
pump(app, 0.8)
EVENTS.append({"t": time.perf_counter(), "ev": "close_returned",
               "storage_is_none": w._storage is None,
               "closing": bool(w._closing), "close_pending": bool(w._close_pending)})
obs["events"] = EVENTS
obs["dialogs"] = auto.seen
with open(OUTJ, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
auto.stop()
print("S5_DONE")
''').replace("__SRC__", SRC).replace("__TESTS__", TESTS).replace(
    "__BASE__", BASE).replace("__ROOT__", root5).replace(
    "__OUTJ__", os.path.join(CH, "s5.json"))

rec5 = run_child(S5, "s5_lifecycle", CH, BASE, timeout=360)
try:
    with open(os.path.join(CH, "s5.json"), "r", encoding="utf-8") as f:
        o5 = json.load(f)
except Exception:
    o5 = {}
evs = o5.get("events") or []
conns = [e for e in evs if e.get("ev") == "conn_connect"]
closes = [e for e in evs if e.get("ev") == "conn_close"]
main_ident = o5.get("main_thread_ident")
worker_conns = [c for c in conns if c.get("thread") != main_ident]
main_conns = [c for c in conns if c.get("thread") == main_ident]
open_ids = {c["id"] for c in conns}
closed_ids = {c["id"] for c in closes}
R.check("S5_1", "实测 SQLite 连接身份与创建线程：主线程与后台线程各用自己的连接",
        len(main_conns) >= 1 and len(worker_conns) >= 2
        and all(c.get("thread_name") for c in conns),
        f"main_conns={[(c['thread_name'], c['db']) for c in main_conns]} "
        f"worker_conns={[(c['thread_name'], c['db']) for c in worker_conns]}")
exec_mark = next((e for e in evs if e.get("ev") == "exec_thread_finished_observed"), None)
exec_close = None
if exec_mark:
    cand = [c for c in closes if c.get("thread") != main_ident
            and c["t"] <= exec_mark["t"]]
    exec_close = cand[-1] if cand else None
R.check("S5_2", "后台执行的连接在该线程内关闭，且关闭早于线程结束被主线程观察到",
        exec_close is not None and exec_mark is not None,
        f"exec_close={exec_close} observed_at={exec_mark}")
R.check("S5_3", "所有打开过的连接都已关闭（无遗留连接），窗口关闭后主连接为 None 语义",
        open_ids == closed_ids and g(o5, "events") is not None
        and any(e.get("ev") == "close_returned" and e.get("storage_is_none")
                for e in evs),
        f"opened={len(open_ids)} closed={len(closed_ids)} "
        f"missing={sorted(open_ids - closed_ids)}")
close_ret = next((e for e in evs if e.get("ev") == "close_returned"), None)
R.check("S5_4", "连接关闭顺序：窗口真正关闭（返回）之前，工作线程连接已全部关闭",
        bool(close_ret) and bool(closes)
        and max(c["t"] for c in closes) <= close_ret["t"],
        f"last_close_t={max((c['t'] for c in closes), default=None)} "
        f"close_returned_t={close_ret['t'] if close_ret else None}")
R.check("S5_5", "生产界面完整闭环：扫描→确认执行→从界面撤销，状态栏均有结果",
        g(o5, "plan_items") == 2 and g(o5, "exec_confirm") is True
        and g(o5, "undo_confirm") is True,
        f"plan={g(o5, 'plan_items')} exec_status={g(o5, 'after_exec_status')} "
        f"undo_status={g(o5, 'after_undo_status')}")

R.check_child_exits("S_EXIT", title_extra="本套件全部 GUI 子进程")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["S1_1", "S1_2", "S1_3", "S1_4", "S1_5",
                                  "S1_6", "S2_1", "S2_2", "S2_3", "S2_4",
                                  "S2_5", "S2_6", "S3_1", "S3_3", "S3_4",
                                  "S3_5", "S3_6", "S4_1", "S4_2", "S4_3",
                                  "S4_4", "S5_1", "S5_2", "S5_3", "S5_4",
                                  "S5_5", "S_EXIT"]))
