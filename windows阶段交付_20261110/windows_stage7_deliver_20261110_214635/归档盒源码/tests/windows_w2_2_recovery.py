"""W2-2：中断恢复必须覆盖真实记账窗口。

executing 批次 × 4 种磁盘状态（先按 executor.recover_batch 源码确定期望）：
  F1 src_here     源在、目标不在        → A：保持 planned；批次 cancelled_clean
  F2 moved_only   目标已在、sha 一致    → B：补记 moved；批次 done
  F3 both         源与目标同时存在      → E/F：paused；两份都不得被覆盖或删除
  F4 neither      源与目标都不存在      → D：paused，且必须明确记录“不可恢复”

恢复入口不是直接调函数：每个夹具都由**新进程**打开归档盒主窗口，
走产品真实的「检测到被中断批次 → 是否核对恢复」对话框并选「是」，
随后读取界面背后的持久化记录与磁盘实际状态。

F5 受控中断：测试专属子进程在「第 2 个文件已 os.rename 成功、但
update_action(moved) 尚未落库」的同步点 os._exit(7) 硬中断。
该非零退出属**故障注入**，单独写入 fault_injection.json，
不计入正常通过率。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, full_manifest, run_child,
                             save_manifest, sha256_file)  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_w22"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w22")
CH = rd.sub("children")
R = Results("w2_2_recovery")
print(f"=== W2-2 run={rd.run_id} ===")

SETUP = r'''
import hashlib, json, os, shutil, sys
sys.path.insert(0, r"__SRC__")
ROOT = r"__ROOT__"
CASE = "__CASE__"
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER
from storage import Storage, BATCH_EXECUTING

def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

sr = scan_directory(ROOT, detect_project_roots=False)
plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
items = [i for i in plan.items if i.selected]
st = Storage(ROOT)
bid = st.create_batch(plan, "中断夹具 " + CASE)
for i, item in enumerate(items):
    st.create_action(bid, item, i,
                     sha(os.path.join(ROOT, item.file.rel_path)))
st.update_batch_status(bid, BATCH_EXECUTING)

first = items[0]
src = os.path.join(ROOT, first.file.rel_path)
tgt = os.path.join(ROOT, first.target_rel_path)
os.makedirs(os.path.dirname(tgt), exist_ok=True)
orig_sha = sha(src)
if CASE == "moved_only":
    os.rename(src, tgt)
elif CASE == "both":
    shutil.copy2(src, tgt)
elif CASE == "neither":
    os.remove(src)

out = {"batch_id": bid, "count": len(items),
       "src_rel": first.file.rel_path.replace("\\", "/"),
       "tgt_rel": first.target_rel_path.replace("\\", "/"),
       "orig_sha": orig_sha,
       "src_after_setup": os.path.isfile(src),
       "tgt_after_setup": os.path.isfile(tgt),
       "db_status_after_setup": st.get_batch(bid).status,
       "actions_after_setup": [a.status for a in st.get_actions(bid)]}
st.close()
print("SETUP_JSON=" + json.dumps(out, ensure_ascii=False))
'''

RECOVER = r'''
import json, os, sys, time
sys.path.insert(0, r"__SRC__")
sys.path.insert(0, r"__TESTS__")
os.environ.pop("QT_QPA_PLATFORM", None)
ROOT = r"__ROOT__"
OUTJSON = r"__OUTJSON__"
BASE = r"__BASE__"
BID = __BID__
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(BASE, "recent.json")
os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(BASE, "appdata")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(BASE, "appdata", "schemes")

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
app = QApplication([])
import main, recent_spaces
from windows_harness import (AutoOk, arm_modal, dialog_text, full_manifest,
                             pump, sha256_file, wait_until)

auto = AutoOk(app)          # 只自动确认“只有 Ok”的结果提示框
dialogs = []

w = main.MainWindow()
w.show()
wait_until(app, lambda: w.isVisible(), 20, "visible")
recent_spaces.touch(ROOT)

def on_interrupted(dlg):
    dialogs.append(dialog_text(dlg))
    return dlg.button(QMessageBox.Yes)

st_modal = arm_modal(on_interrupted, timeout=10.0)
w._open_recent_space(ROOT)
pump(app, 0.5)
done = wait_until(app, lambda: (not w._working and not w._plan_working),
                  120, "recover finished")
pump(app, 0.8)
auto.stop()

b = w._storage.get_batch(BID) if w._storage is not None else None
acts = w._storage.get_actions(BID) if w._storage is not None else []
actions = [{"index": a.action_index, "source_rel": a.source_rel,
            "target_rel": a.target_rel, "status": a.status,
            "error": a.error, "sha": a.source_sha256} for a in acts]
src_rel = actions[0]["source_rel"] if actions else ""
tgt_rel = actions[0]["target_rel"] if actions else ""
src_abs = os.path.join(ROOT, src_rel.replace("/", os.sep)) if src_rel else ""
tgt_abs = os.path.join(ROOT, tgt_rel.replace("/", os.sep)) if tgt_rel else ""
disk = {"source": 0, "target": 0}
for dp, dn, fn in os.walk(ROOT):
    rel = os.path.relpath(dp, ROOT)
    head = rel.split(os.sep)[0]
    if head == ".organizer" or head.startswith(".abx_owned"):
        continue
    if head == "_已整理":
        disk["target"] += len(fn)
    else:
        disk["source"] += len(fn)

payload = {"handled": st_modal.get("handled"), "modal_error": st_modal.get("error"),
           "done": done, "dialogs": dialogs,
           "batch_status": getattr(b, "status", None),
           "batch_summary": getattr(b, "summary", ""),
           "actions": actions,
           "src_exists": os.path.isfile(src_abs) if src_abs else None,
           "tgt_exists": os.path.isfile(tgt_abs) if tgt_abs else None,
           "src_sha": sha256_file(src_abs) if src_abs and os.path.isfile(src_abs) else None,
           "tgt_sha": sha256_file(tgt_abs) if tgt_abs and os.path.isfile(tgt_abs) else None,
           "disk": disk}
with open(OUTJSON, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
w.close()
pump(app, 0.3)
print("RECOVER_JSON=" + json.dumps(payload, ensure_ascii=False))
sys.exit(0 if (payload["done"] and payload["handled"]) else 1)
'''

FILES_1 = ["大二上/数据结构/讲义.pdf"]
FILES_3 = ["大二上/数据结构/讲义.pdf", "大二上/数据结构/作业/hw1.docx",
           "大二上/高等数学/期末复习.pdf"]


def make_root(name: str, files):
    root = rd.sub(name)
    for rel in files:
        p = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(("payload::" + rel).encode("utf-8"))
    return root


def run_setup(root: str, case: str, label: str):
    src = (SETUP.replace("__SRC__", SRC).replace("__ROOT__", root)
           .replace("__CASE__", case))
    rec = run_child(src, label, CH, BASE, timeout=120)
    info = {}
    for line in rec["stdout"].splitlines():
        if line.startswith("SETUP_JSON="):
            info = json.loads(line[len("SETUP_JSON="):])
    return rec, info


def run_recover(root: str, bid: int, label: str, timeout: int = 180) -> dict:
    out_json = os.path.join(CH, f"{label}_recover.json")
    src = (RECOVER.replace("__SRC__", SRC).replace("__TESTS__", TESTS)
           .replace("__ROOT__", root).replace("__OUTJSON__", out_json)
           .replace("__BASE__", BASE).replace("__BID__", str(bid)))
    rec = run_child(src, label, CH, BASE, timeout=timeout)
    obs = {}
    if os.path.isfile(out_json):
        with open(out_json, "r", encoding="utf-8") as f:
            obs = json.load(f)
    obs["_exit"] = rec["exit_code"]
    obs["_timed_out"] = rec["timed_out"]
    return obs


def one(act) -> str:
    return act["status"] if act else "缺失"


# ======================= F1：源在、目标不在 =======================
print("\n--- F1 src_here：动作已登记但文件未移动 ---")
r1 = make_root("f1_src_here", FILES_1)
before1 = full_manifest(r1)
cfg1, info1 = run_setup(r1, "src_here", "f1_setup")
bid1 = info1.get("batch_id")
obs1 = run_recover(r1, bid1, "f1") if bid1 else {}
a1 = (obs1.get("actions") or [{}])[0]
print("  夹具期望：A 分支 → planned / cancelled_clean")
R.check("F1_1", "夹具构造：DB 中 executing + 1 条 planned，磁盘源在目标不在",
        cfg1["exit_code"] == 0 and info1.get("db_status_after_setup") == "executing"
        and info1.get("actions_after_setup") == ["planned"]
        and info1.get("src_after_setup") is True
        and info1.get("tgt_after_setup") is False,
        f"exit={cfg1['exit_code']} info={info1}")
R.check("F1_2", "新进程走真实入口：出现「被中断批次」核对对话框并选「是」",
        obs1.get("handled") is True
        and any("被中断" in d for d in (obs1.get("dialogs") or [])),
        f"handled={obs1.get('handled')} dialogs={obs1.get('dialogs')}")
R.check("F1_3", "恢复后动作保持 planned（未执行的文件不被补记 moved）",
        one(a1) == "planned", f"status={one(a1)} error={a1.get('error')}")
R.check("F1_4", "批次状态为 cancelled_clean（从未真正执行）",
        obs1.get("batch_status") == "cancelled_clean",
        f"status={obs1.get('batch_status')} summary={obs1.get('batch_summary')}")
R.check("F1_5", "磁盘未被改动：源文件仍在、归档目录 0 文件、清单不变",
        obs1.get("src_exists") is True and obs1.get("tgt_exists") is False
        and obs1.get("disk", {}).get("target", -1) == 0
        and full_manifest(r1) == before1, f"disk={obs1.get('disk')}")

# ======================= F2：已移动但状态未持久化 =======================
print("\n--- F2 moved_only：文件已在目标、状态仍是 planned ---")
r2 = make_root("f2_moved_only", FILES_1)
before2 = full_manifest(r2)
cfg2, info2 = run_setup(r2, "moved_only", "f2_setup")
bid2 = info2.get("batch_id")
obs2 = run_recover(r2, bid2, "f2") if bid2 else {}
a2 = (obs2.get("actions") or [{}])[0]
print("  夹具期望：B 分支 → 补记 moved / done")
R.check("F2_1", "夹具构造：状态 planned，磁盘上文件已在目标（sha 一致）",
        cfg2["exit_code"] == 0 and info2.get("actions_after_setup") == ["planned"]
        and info2.get("src_after_setup") is False
        and info2.get("tgt_after_setup") is True,
        f"info={info2}")
R.check("F2_2", "恢复后补记 moved，并写明是中断核对补记",
        one(a2) == "moved" and "恢复补记" in (a2.get("error") or ""),
        f"status={one(a2)} error={a2.get('error')}")
R.check("F2_3", "批次状态为 done（唯一动作已确认为移动成功）",
        obs2.get("batch_status") == "done",
        f"status={obs2.get('batch_status')} summary={obs2.get('batch_summary')}")
R.check("F2_4", "磁盘未二次移动：源不存在、目标存在且哈希等于原始内容",
        obs2.get("src_exists") is False and obs2.get("tgt_exists") is True
        and obs2.get("tgt_sha") == info2.get("orig_sha"),
        f"src={obs2.get('src_exists')} tgt={obs2.get('tgt_exists')} sha={obs2.get('tgt_sha')}")

# ======================= F3：源与目标同时存在 =======================
print("\n--- F3 both：源与目标同时存在 ---")
r3 = make_root("f3_both", FILES_1)
before3 = full_manifest(r3)
cfg3, info3 = run_setup(r3, "both", "f3_setup")
bid3 = info3.get("batch_id")
obs3 = run_recover(r3, bid3, "f3") if bid3 else {}
a3 = (obs3.get("actions") or [{}])[0]
print("  夹具期望：E/F 分支 → paused，两份都保留")
R.check("F3_1", "夹具构造：状态 planned，源与目标同时存在",
        cfg3["exit_code"] == 0 and info3.get("src_after_setup") is True
        and info3.get("tgt_after_setup") is True,
        f"info={info3}")
R.check("F3_2", "恢复后为 paused，并说明双份共存需人工确认",
        one(a3) == "paused" and "同时存在" in (a3.get("error") or ""),
        f"status={one(a3)} error={a3.get('error')}")
R.check("F3_3", "批次状态为 recovered_partial（存在未定项）",
        obs3.get("batch_status") == "recovered_partial",
        f"status={obs3.get('batch_status')}")
R.check("F3_4", "两份都没有被覆盖或删除（源与目标都存在且哈希均为原始内容）",
        obs3.get("src_exists") is True and obs3.get("tgt_exists") is True
        and obs3.get("src_sha") == info3.get("orig_sha")
        and obs3.get("tgt_sha") == info3.get("orig_sha"),
        f"src={obs3.get('src_sha')} tgt={obs3.get('tgt_sha')} orig={info3.get('orig_sha')}")

# ======================= F4：源与目标都不存在 =======================
print("\n--- F4 neither：源与目标都不存在 ---")
r4 = make_root("f4_neither", FILES_1)
cfg4, info4 = run_setup(r4, "neither", "f4_setup")
bid4 = info4.get("batch_id")
obs4 = run_recover(r4, bid4, "f4") if bid4 else {}
a4 = (obs4.get("actions") or [{}])[0]
print("  夹具期望：D 分支 → paused + 明确不可恢复记录")
R.check("F4_1", "夹具构造：DB 有 planned 动作，磁盘双空",
        cfg4["exit_code"] == 0 and info4.get("src_after_setup") is False
        and info4.get("tgt_after_setup") is False,
        f"info={info4}")
R.check("F4_2", "恢复后为 paused，错误信息明确写“都不存在”（不是仅不抛异常）",
        one(a4) == "paused" and "都不存在" in (a4.get("error") or ""),
        f"status={one(a4)} error={a4.get('error')}")
R.check("F4_3", "批次状态为 recovered_partial，且汇总记录 paused 数量",
        obs4.get("batch_status") == "recovered_partial"
        and "paused=1" in (obs4.get("batch_summary") or ""),
        f"status={obs4.get('batch_status')} summary={obs4.get('batch_summary')}")
R.check("F4_4", "确认磁盘没有被凭空补出文件（仍双空）",
        obs4.get("src_exists") is False and obs4.get("tgt_exists") is False,
        f"src={obs4.get('src_exists')} tgt={obs4.get('tgt_exists')}")

# ======================= F5：受控中断（故障注入，单列） =======================
print("\n--- F5 受控中断：rename 成功但 moved 未落库 ---")
r5 = make_root("f5_interrupt", FILES_3)
before5 = full_manifest(r5)
save_manifest(os.path.join(rd.sub("manifests"), "f5_before.json"), before5)

INTERRUPT = r'''
import json, os, sys
sys.path.insert(0, r"__SRC__")
ROOT = r"__ROOT__"
OUTJSON = r"__OUTJSON__"
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER
from storage import Storage, STATUS_MOVED
from executor import execute_plan

orig = Storage.update_action
def patched(self, batch_id, action_index, status, error=""):
    if status == STATUS_MOVED and action_index == 1:
        with open(OUTJSON, "w", encoding="utf-8") as f:
            json.dump({"interrupted_before_persist_index": action_index,
                       "note": "os.rename 已成功，moved 状态尚未写库"},
                      f, ensure_ascii=False)
        os._exit(7)
    return orig(self, batch_id, action_index, status, error)
Storage.update_action = patched

sr = scan_directory(ROOT, detect_project_roots=False)
plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
st = Storage(ROOT)
execute_plan(plan, st)
st.close()
print("SHOULD_NOT_REACH_END")
'''
f5_json = os.path.join(CH, "f5_interrupt.json")
rec5 = run_child(INTERRUPT.replace("__SRC__", SRC).replace("__ROOT__", r5)
                 .replace("__OUTJSON__", f5_json), "f5_interrupt", CH, BASE,
                 timeout=120)
interrupted_info = {}
if os.path.isfile(f5_json):
    with open(f5_json, "r", encoding="utf-8") as f:
        interrupted_info = json.load(f)

fi = {"case": "controlled_interruption",
      "intent": "第 2 个文件 os.rename 成功后、update_action(moved) 前硬中断",
      "child_exit_code": rec5["exit_code"],
      "expected_nonzero": True,
      "nonzero_as_expected": rec5["exit_code"] == 7,
      "timed_out": rec5["timed_out"],
      "sync_point_payload": interrupted_info,
      "stdout_log": rec5.get("stdout_log"),
      "stderr_log": rec5.get("stderr_log"),
      "note": "故障注入单列，不计入正常通过率"}
with open(os.path.join(OUT, "fault_injection.json"), "w", encoding="utf-8") as f:
    json.dump(fi, f, ensure_ascii=False, indent=2)
print("  故障注入：", json.dumps(fi, ensure_ascii=False))

obs5 = {}
pre5 = {}
if rec5["exit_code"] == 7:
    from storage import Storage  # noqa: E402  仅用于取 batch_id 与中断后状态
    _s = Storage(r5)
    bids = [b.batch_id for b in _s.list_batches()]
    if bids:
        _b = _s.get_batch(bids[0])
        _acts = _s.get_actions(bids[0])
        pre5 = {"batch_status": _b.status,
                "statuses": [a.status for a in _acts],
                "summary": _b.summary}
        _s.close()
        obs5 = run_recover(r5, bids[0], "f5")
    else:
        _s.close()
acts5 = obs5.get("actions") or []
statuses5 = {}
for a in acts5:
    statuses5[a["status"]] = statuses5.get(a["status"], 0) + 1
pre_counts = {}
for st in pre5.get("statuses") or []:
    pre_counts[st] = pre_counts.get(st, 0) + 1
print("  F5 中断后（恢复前）观测:", json.dumps(pre5, ensure_ascii=False))
print("  F5 恢复观测:", json.dumps({"batch_status": obs5.get("batch_status"),
                                   "statuses": statuses5,
                                   "disk": obs5.get("disk")}, ensure_ascii=False))

R.check("F5_1", "受控中断已发生：子进程在同步点以非零退出（故障注入记录见 fault_injection.json）",
        rec5["exit_code"] == 7 and not rec5["timed_out"]
        and interrupted_info.get("interrupted_before_persist_index") == 1,
        f"exit={rec5['exit_code']} payload={interrupted_info}")
R.check("F5_2", "中断后进程没来得及收尾：批次仍 executing，第 2 项仍是 planned",
        pre5.get("batch_status") == "executing"
        and pre_counts.get("moved") == 1 and pre_counts.get("planned") == 2,
        f"pre={pre5}")
R.check("F5_3", "新进程恢复把“已移动但未落库”的那项补记为 moved",
        statuses5.get("moved") == 2, f"statuses={statuses5}")
R.check("F5_4", "未执行的第 3 项保持 planned（不被误记为成功）",
        statuses5.get("planned") == 1, f"statuses={statuses5}")
R.check("F5_5", "批次为 recovered_partial，磁盘 2 个在目标、1 个在源，且未被再次移动",
        obs5.get("batch_status") == "recovered_partial"
        and obs5.get("disk", {}).get("target") == 2
        and obs5.get("disk", {}).get("source") == 1,
        f"status={obs5.get('batch_status')} disk={obs5.get('disk')}")

R.check_child_exits("F_EXIT", allow_labels=("f5_interrupt",),
                    title_extra="f5_interrupt 是预期非零的受控中断故障注入")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"F1_{i}" for i in range(1, 6)]
                    + [f"F2_{i}" for i in range(1, 5)]
                    + [f"F3_{i}" for i in range(1, 5)]
                    + [f"F4_{i}" for i in range(1, 5)]
                    + [f"F5_{i}" for i in range(1, 6)]
                    + ["F_EXIT"]))
