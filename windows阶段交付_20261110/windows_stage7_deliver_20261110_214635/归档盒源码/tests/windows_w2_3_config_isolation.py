"""W2-3：配置隔离必须验证实际行为。

同一批同名同层级文件放在 A、B 两个根目录，A 使用自定义推荐映射
（「数据结构」→ 方向“资料”）并手动保护 `大二上/高等数学`，B 使用内置
默认配置且不保护。全部结论都来自真实控件（扫描按钮/预览表格/执行按钮/
失效提示）、实际计划、磁盘或数据库，不以“保存函数调用成功”代替。

覆盖：
  Q1 两端实际预览目标不同，重启（新进程）后仍各自恢复
  Q2 A 手动保护的文件不进入计划，B 同路径文件正常进入
  Q3 临时修正 > 长期规则 > 推荐映射（用真实 apply_override_to_plan）
  Q4 修改配置 / 切根目录 / 改变来源范围后旧计划不可执行
  Q5 后台任务使用启动时快照（配置深拷贝）
  Q6 旧任务迟到结果不覆盖新根目录界面
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import Results, RunDir, run_child  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_w23"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w23")
CH = rd.sub("children")
R = Results("w2_3_config_isolation")
print(f"=== W2-3 run={rd.run_id} ===")

FILES = ["大二上/数据结构/讲义.pdf",
         "大二上/高等数学/期末复习.pdf",
         "大二上/高等数学/作业/hw.docx"]
A_ONLY = "大二上/数据结构/A_ONLY_标记.txt"

ROOT_A = rd.sub("root_a")
ROOT_B = rd.sub("root_b")
for rel in FILES + [A_ONLY]:
    p = os.path.join(ROOT_A, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(("A::" + rel).encode("utf-8"))
for rel in FILES:
    p = os.path.join(ROOT_B, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(("B::" + rel).encode("utf-8"))

SETUP = r'''
import json, os, sys
sys.path.insert(0, r"__SRC__")
ROOT_A = r"__ROOT_A__"
ROOT_B = r"__ROOT_B__"
from classification_config import default_config, save_config, load_config
from protect_config import save_manual_protect, load_manual_protect

cfg_a = default_config()
n_changed = 0
for r in cfg_a.semester_dir_rules:
    if r.semester == "大二上" and r.source == "数据结构":
        r.direction = "项目"
        n_changed += 1
save_config(ROOT_A, cfg_a)
save_config(ROOT_B, default_config())
save_manual_protect(ROOT_A, [os.path.join(ROOT_A, "大二上", "高等数学")])

back_a, src_a = load_config(ROOT_A)
back_b, src_b = load_config(ROOT_B)
prot_a = load_manual_protect(ROOT_A)
prot_b = load_manual_protect(ROOT_B)
print("SETUP_JSON=" + json.dumps({
    "changed": n_changed,
    "a_direction": sorted({r.direction for r in back_a.semester_dir_rules
                           if r.semester == "大二上" and r.source == "数据结构"}),
    "b_direction": sorted({r.direction for r in back_b.semester_dir_rules
                           if r.semester == "大二上" and r.source == "数据结构"}),
    "protect_a": prot_a, "protect_b": prot_b,
    "source_a": src_a, "source_b": src_b,
}, ensure_ascii=False))
'''

GUI = r'''
import json, os, sys, time
sys.path.insert(0, r"__SRC__")
sys.path.insert(0, r"__TESTS__")
os.environ.pop("QT_QPA_PLATFORM", None)
ROOT = r"__ROOT__"
ROOT_A = ROOT
ROOT_B = r"__ROOT_B__"
OUTJSON = r"__OUTJSON__"
BASE = r"__BASE__"
STEPS = __STEPS__
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(BASE, "recent.json")
os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(BASE, "appdata")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(BASE, "appdata", "schemes")

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
import main, recent_spaces
from windows_harness import AutoOk, arm_modal, dialog_text, pump, wait_until

dialogs = []

def open_space(path):
    def on_q(dlg):
        dialogs.append(dialog_text(dlg))
        try:
            return dlg.button(QMessageBox.No)
        except Exception:
            return None
    arm_modal(on_q, timeout=6.0)
    w._open_recent_space(path)
    pump(app, 0.3)

def rows():
    out = []
    for r in range(w.table.rowCount()):
        cells = []
        for c in range(w.table.columnCount()):
            it = w.table.item(r, c)
            cells.append((it.text() if it else "").replace("\\", "/"))
        out.append(cells)
    return out

def snap(tag):
    plan = w.current_plan
    return {
        "tag": tag,
        "path": w.path_edit.text().replace("\\", "/"),
        "rows": rows(),
        "plan_items": (0 if plan is None else len(plan.items)),
        "targets": ([] if plan is None else
                    [i.target_rel_path.replace("\\", "/") for i in plan.items]),
        "sources": ([] if plan is None else
                    [i.file.rel_path.replace("\\", "/") for i in plan.items]),
        "invalid_visible": w.invalid_label.isVisible(),
        "execute_enabled": w.btn_execute.isEnabled(),
        "scope": getattr(w, "_plan_scope", "?"),
        "status": w.statusBar().currentMessage(),
    }

w = main.MainWindow()
w.show()
auto = AutoOk(app)   # 只自动确认“只有 Ok”的提示框（如配置变更提示），并记录全文
wait_until(app, lambda: w.isVisible(), 20, "visible")
recent_spaces.touch(ROOT)
open_space(ROOT)

obs = {"steps": STEPS, "first": None, "after_scan": None, "invalidate": {},
       "late": {}, "dialogs": dialogs}

QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: (w.scan_result is not None and not w._working
                        and not w._plan_working), 60, "scan+plan")
pump(app, 0.5)
obs["after_scan"] = snap("after_scan")

if "invalidate_config" in STEPS:
    # 通过产品自身的保存配置入口修改配置 → 旧计划必须失效
    before = snap("before_cfg_change")
    cfg = w.classification_config
    from classification_config import GenericDirRule
    cfg.generic_dir_rules.append(
        GenericDirRule(prefix="讲座", direction="校园事务", category="讲座"))
    w._save_classification_config(ROOT, cfg)
    pump(app, 0.4)
    obs["invalidate"]["config"] = {"before": before, "after": snap("after_cfg")}
    # 重新生成，再做“切根目录”
    QTest.mouseClick(w.btn_scan, Qt.LeftButton)
    wait_until(app, lambda: (not w._working and not w._plan_working), 60, "rescan")
    pump(app, 0.4)
    obs["invalidate"]["rescan"] = snap("after_rescan")
    open_space(ROOT_B)
    pump(app, 0.4)
    obs["invalidate"]["switch_root"] = snap("after_switch_root")

if "late_result" in STEPS:
    # 在 A 的计划生成过程中切到 B，A 的迟到结果不得覆盖 B 的界面
    open_space(ROOT_A)
    pump(app, 0.2)
    QTest.mouseClick(w.btn_scan, Qt.LeftButton)
    started = wait_until(app, lambda: w._working, 10, "A scan started")
    trace_started = bool(started)
    open_space(ROOT_B)
    wait_until(app, lambda: (not w._working and not w._plan_working), 60, "late settle")
    pump(app, 0.6)
    obs["late"] = snap("after_late")
    obs["late"]["a_scan_started"] = trace_started

auto.stop()
with open(OUTJSON, "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False, indent=2)
w.close()
pump(app, 0.3)
print("GUI_JSON_DONE")
'''

PROBE = r'''
import copy, json, os, sys
sys.path.insert(0, r"__SRC__")
ROOT = r"__ROOT__"
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER
from classification_config import (default_config, ClassificationConfig,
                                   SemesterDirRule, save_config,
                                   LongTermRule, MATCH_DIR_SCOPE)
from plan_patch import OverrideParams, apply_override_to_plan
import main
from main import PlanWorker
from storage import Storage

sr = scan_directory(ROOT, detect_project_roots=False)
cfg = default_config()
save_config(ROOT, cfg)

# 推荐映射基线：数据结构 → 课程
base_plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
base_t = {i.file.rel_path.replace("\\", "/"): i.target_rel_path.replace("\\", "/")
          for i in base_plan.items}

# 长期规则：大二上/数据结构 → 方向“长期”
cfg2 = copy.deepcopy(cfg)
cfg2.long_term_rules.append(LongTermRule(
    rule_id="lt-test-0001", name="数据结构长期规则",
    match_type=MATCH_DIR_SCOPE, source_path="大二上/数据结构",
    include_subdirs=True, direction="项目", semester="大二上",
    category="数据结构"))
lt_plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg2)
lt_t = {i.file.rel_path.replace("\\", "/"): i.target_rel_path.replace("\\", "/")
        for i in lt_plan.items}
lt_key = {i.file.rel_path.replace("\\", "/"): i.rule_key for i in lt_plan.items}

# 临时修正（在长期规则命中的计划上）→ 必须压过长期规则
ov_t = ""
ov_note = ""
ov_auto = ""
for src_rel, item in [(i.file.rel_path, i) for i in lt_plan.items]:
    if "数据结构" in src_rel.replace("\\", "/"):
        auto_before = item.target_rel_path.replace("\\", "/")
        apply_override_to_plan(lt_plan, src_rel, OverrideParams(
            direction="竞赛", semester="大二上", category="临时修正课",
            inner_path="", override_note="测试临时修正"))
        ov_t = item.target_rel_path.replace("\\", "/")
        ov_note = item.override_note
        ov_auto = auto_before
        break

# 后台任务快照：产品在把配置交给 PlanWorker 前做深拷贝
import inspect
_src = inspect.getsource(main.MainWindow)
caller_deepcopies = "plan_config = copy.deepcopy(self.classification_config)" in _src
pw = PlanWorker(0, sr, MODE_BY_DIRECTION_SEMESTER, [], config=copy.deepcopy(cfg))
snapshot_copy = copy.deepcopy(pw.config)
cfg.semester_dir_rules.append(SemesterDirRule(
    semester="大二上", source="数据结构", direction="被改坏", category=""))
snap_plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=pw.config)
snap_t = {i.file.rel_path.replace("\\", "/"): i.target_rel_path.replace("\\", "/")
          for i in snap_plan.items}

print("PROBE_JSON=" + json.dumps({
    "base": base_t, "long_term": lt_t, "long_term_keys": lt_key,
    "override_target": ov_t, "override_auto": ov_auto, "override_note": ov_note,
    "worker_same_object": (pw.config is cfg),
    "caller_deepcopies": caller_deepcopies,
    "snapshot_targets": snap_t, "snapshot_is_deep": snapshot_copy is not cfg,
}, ensure_ascii=False))
'''


def child_gui(root: str, label: str, steps: str) -> dict:
    out_json = os.path.join(CH, f"{label}.json")
    src = (GUI.replace("__SRC__", SRC).replace("__TESTS__", TESTS)
           .replace("__ROOT__", root).replace("__ROOT_B__", ROOT_B)
           .replace("__OUTJSON__", out_json).replace("__BASE__", BASE)
           .replace("__STEPS__", steps))
    rec = run_child(src, label, CH, BASE, timeout=180)
    obs = {}
    if os.path.isfile(out_json):
        with open(out_json, "r", encoding="utf-8") as f:
            obs = json.load(f)
    obs["_exit"] = rec["exit_code"]
    return obs


# ======================= 夹具 =======================
setup_rec = run_child(SETUP.replace("__SRC__", SRC).replace("__ROOT_A__", ROOT_A)
                      .replace("__ROOT_B__", ROOT_B), "setup", CH, BASE, timeout=90)
setup = {}
for line in setup_rec["stdout"].splitlines():
    if line.startswith("SETUP_JSON="):
        setup = json.loads(line[len("SETUP_JSON="):])
print("  夹具:", json.dumps(setup, ensure_ascii=False))
R.check("Q0_1", "夹具落盘：A 自定义方向=项目，B=课程，A 手动保护=高等数学，B 无保护",
        setup.get("a_direction") == ["项目"] and setup.get("b_direction") == ["课程"]
        and setup.get("source_a") == "custom" and setup.get("source_b") == "default"
        and len(setup.get("protect_a") or []) == 1
        and (setup.get("protect_b") or []) == [],
        f"{setup}")

# ======================= Q1/Q2：两端预览与重启 =======================
print("\n--- Q1/Q2 两端实际预览 + 重启恢复 + 手动保护 ---")
a1 = child_gui(ROOT_A, "gui_a", "[]")
b1 = child_gui(ROOT_B, "gui_b", "[]")
a2 = child_gui(ROOT_A, "restart_a", "[]")
b2 = child_gui(ROOT_B, "restart_b", "[]")


def targets(obs):
    return (obs.get("after_scan") or {}).get("targets") or []


ta1, tb1, ta2, tb2 = targets(a1), targets(b1), targets(a2), targets(b2)
print("  A 首轮目标:", ta1)
print("  B 首轮目标:", tb1)

R.check("Q1_1", "A 的预览目标走自定义方向「项目」（不再落到默认课程目录）",
        any(t.startswith("_已整理/项目/") for t in ta1),
        f"a_targets={ta1}")
R.check("Q1_2", "B 同路径同名文件的预览目标走默认方向「课程」",
        any(t.startswith("_已整理/课程/大二上/数据结构/") for t in tb1),
        f"b_targets={tb1}")
R.check("Q1_3", "两端对同名同层级文件的实际目标不同",
        bool(ta1) and bool(tb1) and set(ta1).isdisjoint(set(tb1)),
        f"a={ta1} b={tb1}")
R.check("Q1_4", "重启（新进程）后 A 仍按自定义配置恢复同一组目标",
        ta2 == ta1 and bool(ta1), f"first={ta1} restart={ta2}")
R.check("Q1_5", "重启（新进程）后 B 仍按默认配置恢复同一组目标",
        tb2 == tb1 and bool(tb1), f"first={tb1} restart={tb2}")

sa = (a1.get("after_scan") or {}).get("sources") or []
sb = (b1.get("after_scan") or {}).get("sources") or []
R.check("Q2_1", "A 手动保护的「大二上/高等数学」完全不进入计划",
        bool(sa) and not any("高等数学" in s for s in sa), f"a_sources={sa}")
R.check("Q2_2", "B 同路径文件正常进入计划（保护没有被错误套用到 B）",
        any("高等数学/期末复习.pdf" in s for s in sb), f"b_sources={sb}")
R.check("Q2_3", "A 计划只剩 2 个未保护文件、B 为 3 个，且演练未移动任何文件",
        len(sa) == 2 and len(sb) == 3
        and all(os.path.isfile(os.path.join(ROOT_A, s.replace("/", os.sep)))
                for s in sa),
        f"a={len(sa)} b={len(sb)}")

# ======================= Q3/Q5：优先级与快照 =======================
print("\n--- Q3 优先级 / Q5 启动快照 ---")
probe_rec = run_child(PROBE.replace("__SRC__", SRC).replace("__ROOT__", ROOT_B),
                      "probe", CH, BASE, timeout=120)
probe = {}
for line in probe_rec["stdout"].splitlines():
    if line.startswith("PROBE_JSON="):
        probe = json.loads(line[len("PROBE_JSON="):])
print("  probe:", json.dumps({k: probe.get(k) for k in
                              ("base", "long_term", "override_target")},
                             ensure_ascii=False))
key = "大二上/数据结构/讲义.pdf"
R.check("Q3_1", "推荐映射基线：数据结构 → 课程",
        (probe.get("base") or {}).get(key, "").startswith("_已整理/课程/"),
        f"base={probe.get('base')}")
R.check("Q3_2", "长期规则压过推荐映射：数据结构 → 项目",
        (probe.get("long_term") or {}).get(key, "").startswith("_已整理/项目/")
        and "long_term" in (probe.get("long_term_keys") or {}).get(key, ""),
        f"lt={probe.get('long_term')} keys={probe.get('long_term_keys')}")
R.check("Q3_3", "临时修正压过长期规则：目标改为竞赛方向并记录说明",
        (probe.get("override_target") or "").startswith("_已整理/竞赛/")
        and probe.get("override_note") == "测试临时修正"
        and (probe.get("override_auto") or "").startswith("_已整理/项目/"),
        f"t={probe.get('override_target')} auto={probe.get('override_auto')}")
R.check("Q5_1", "产品在启动计划任务前对配置做深拷贝（调用方实现，非 PlanWorker 内部引用）",
        probe.get("caller_deepcopies") is True
        and probe.get("snapshot_is_deep") is True, f"{probe.get('caller_deepcopies')}")
R.check("Q5_2", "改坏界面配置对象后，已提交快照生成的目标不变（启动快照生效）",
        (probe.get("snapshot_targets") or {}).get(key, "").startswith("_已整理/课程/"),
        f"snapshot={probe.get('snapshot_targets')}")

# ======================= Q4：旧计划失效 =======================
print("\n--- Q4 修改配置/切根目录后旧计划不可执行 ---")
a3 = child_gui(ROOT_A, "gui_invalidate", '["invalidate_config"]')
inv = a3.get("invalidate") or {}
cfg_delta = inv.get("config") or {}
cfg_before = cfg_delta.get("before") or {}
cfg_after = cfg_delta.get("after") or {}
sw = inv.get("switch_root") or {}
print("  配置变更前 execute_enabled=", cfg_before.get("execute_enabled"),
      "变更后=", cfg_after.get("execute_enabled"),
      "失效提示=", cfg_after.get("invalid_visible"))
R.check("Q4_1", "修改配置后旧计划被标记失效（失效提示可见且执行按钮禁用）",
        cfg_after.get("invalid_visible") is True
        and cfg_after.get("execute_enabled") is False,
        f"invalid={cfg_after.get('invalid_visible')} exec={cfg_after.get('execute_enabled')}")
R.check("Q4_2", "失效前该计划本来可执行（对照，避免“永远禁用”的假通过）",
        cfg_before.get("execute_enabled") is True
        and cfg_before.get("plan_items") == 2,
        f"before={cfg_before}")
R.check("Q4_3", "切换到另一根目录后旧计划被清空、界面换根",
        sw.get("path", "").replace("\\", "/") == ROOT_B.replace("\\", "/")
        and sw.get("plan_items") == 0 and sw.get("execute_enabled") is False,
        f"path={sw.get('path')} items={sw.get('plan_items')} exec={sw.get('execute_enabled')}")

# ======================= Q6：迟到结果 =======================
print("\n--- Q6 旧任务迟到结果不覆盖新根目录 ---")
a4 = child_gui(ROOT_A, "gui_late", '["late_result"]')
late = a4.get("late") or {}
print("  late:", json.dumps({"path": late.get("path"),
                             "sources": late.get("sources")}, ensure_ascii=False))
R.check("Q6_1", "迟到结果落地后界面仍停在 B 根目录",
        (late.get("path") or "").replace("\\", "/") == ROOT_B.replace("\\", "/"),
        f"path={late.get('path')}")
R.check("Q6_2", "界面中没有 A 根目录独有的条目（迟到计划未污染表格）",
        late.get("a_scan_started") is True
        and not any("A_ONLY_标记" in c
                    for row in (late.get("rows") or []) for c in row)
        and not any("A_ONLY_标记" in s for s in (late.get("sources") or [])),
        f"sources={late.get('sources')} started={late.get('a_scan_started')}")
R.check("Q6_3", "A 的迟到计划被丢弃：B 界面没有出现任何计划（未被替换成 A 的计划）",
        late.get("a_scan_started") is True and late.get("plan_items") == 0
        and (late.get("targets") or []) == [] and (late.get("rows") or []) == [],
        f"items={late.get('plan_items')} rows={len(late.get('rows') or [])}")

R.check_child_exits("Q_EXIT", title_extra="本套件全部 GUI/夹具子进程")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["Q0_1"]
                    + [f"Q1_{i}" for i in range(1, 6)]
                    + [f"Q2_{i}" for i in range(1, 4)]
                    + ["Q3_1", "Q3_2", "Q3_3", "Q5_1", "Q5_2"]
                    + ["Q4_1", "Q4_2", "Q4_3"]
                    + [f"Q6_{i}" for i in range(1, 4)]
                    + ["Q_EXIT"]))
