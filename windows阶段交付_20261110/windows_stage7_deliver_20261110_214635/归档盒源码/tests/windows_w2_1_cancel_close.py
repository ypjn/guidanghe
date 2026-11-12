"""W2-1：取消与关闭等待（测试专属可控同步点）。

不靠文件数量或 sleep 撞时序：在被测子进程里 patch os.rename，
在第 N 次真实移动成功后进入同步点（阻塞工作线程），
主线程确认“已移动 N 个且 < 总数”后再点「取消整理」/关闭窗口，
然后放行线程。生产语义不变（移动仍然是真实 os.rename）。

子进程内部是本轮唯一目录；观测数据写入 JSON 由本套件断言。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, full_manifest, load_manifest,
                             run_child, save_manifest)  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_w21"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w21")
CH = rd.sub("children")
R = Results("w2_1_cancel_close")
print(f"=== W2-1 run={rd.run_id} ===")

N_FILES = 12
WINDOW = 3

PROLOGUE = r'''
import json, os, sys, threading, time
SRC = r"__SRC__"
TESTS = r"__TESTS__"
ROOT = r"__ROOT__"
OUTJSON = r"__OUTJSON__"
BASE = r"__BASE__"
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(BASE, "recent.json")
os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(BASE, "appdata")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(BASE, "appdata", "schemes")
WINDOW = __WINDOW__
TRACEFILE = r"__TRACEFILE__"
TRACE = []
OPEN_DIALOGS = []
def trace(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    TRACE.append(line)
    print(f"[trace] {msg}", flush=True)
    # 立即落盘：即使父进程超时强杀，也能看到最后观测到的阶段
    try:
        with open(TRACEFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

# 硬看门狗：模态弹窗等阻塞不会无限挂住父套件（退出码 9 可识别）
def _watchdog():
    trace("WATCHDOG_FIRED")
    os._exit(9)
_wd = threading.Timer(__WATCHDOG__, _watchdog)
_wd.daemon = True
_wd.start()

# 测试专属同步点：第 WINDOW 次真实移动完成后阻塞工作线程
real_rename = os.rename
_ren = {"n": 0}
reached = threading.Event()
gate = threading.Event()
def hooked_rename(a, b, **kw):
    real_rename(a, b)
    _ren["n"] += 1
    if WINDOW and _ren["n"] == WINDOW:
        trace("sync_point_hit after rename #%d" % _ren["n"])
        reached.set()
        gate.wait(60)
        trace("sync_point_released")
os.rename = hooked_rename

import storage as storage_mod
_storage_ctor = {"calls": []}
_orig_init = storage_mod.Storage.__init__
def patched_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    _storage_ctor["calls"].append({
        "thread": threading.current_thread().name,
        "id": id(self),
    })
storage_mod.Storage.__init__ = patched_init

from PySide6.QtWidgets import QApplication, QMessageBox, QDialogButtonBox
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
import main, recent_spaces
from windows_harness import AutoOk, arm_modal, pump, wait_until

logs = []
_orig_set_working = main.MainWindow._set_working
def traced_set_working(self, busy, *a, **kw):
    trace(f"_set_working({busy})")
    return _orig_set_working(self, busy, *a, **kw)
main.MainWindow._set_working = traced_set_working

def dump(extra):
    payload = {"trace": TRACE, "renames": _ren["n"],
               "open_dialogs": OPEN_DIALOGS,
               "storage_calls": _storage_ctor["calls"], **extra}
    with open(OUTJSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
'''

CONFIRM = r'''
# 打开根目录时产品可能弹出「未完成批次」询问框。
# 本套件用全新根目录，正常不应出现；若出现则记录并选「否」，
# 由断言把「意外弹窗」暴露出来，而不是让子进程静默挂死。
def open_space(path):
    def on_q(dlg):
        txt = dialog_text(dlg)
        OPEN_DIALOGS.append(txt)
        trace("open_space_dialog: " + txt.splitlines()[0] if txt else "open_space_dialog")
        try:
            return dlg.button(QMessageBox.No)
        except Exception:
            return None
    st = arm_modal(on_q, timeout=6.0)
    w._open_recent_space(path)
    pump(app, 0.3)
    return st

def click_confirm():
    def on_confirm(dlg):
        bb = dlg.findChild(QDialogButtonBox)
        return bb.button(QDialogButtonBox.Ok)
    # 计划刚发布时按钮启用可能落后一个事件轮次：先等按钮可用，
    # 点击后若确认框未出现则重试（不能把“按钮没点动”当成通过）
    wait_until(app, lambda: w.btn_execute.isEnabled(), 30, "btn_execute enabled")
    st = None
    for _ in range(5):
        if st is not None and st.get("handled"):
            break
        st = arm_modal(on_confirm, timeout=8.0)
        QTest.mouseClick(w.btn_execute, Qt.LeftButton)
        pump(app, 0.3)
    return st
'''


def child_source(root: str, out_json: str, window: int, body: str,
                 watchdog: int = 150) -> str:
    pro = (PROLOGUE.replace("__SRC__", SRC).replace("__TESTS__", TESTS)
           .replace("__ROOT__", root)
           .replace("__OUTJSON__", out_json).replace("__BASE__", BASE)
           .replace("__TRACEFILE__", out_json + ".trace")
           .replace("__WATCHDOG__", str(watchdog))
           .replace("__WINDOW__", str(window)))
    return pro + CONFIRM + body


def make_root(path: str, n: int):
    plan = [("大二上/数据结构", f"讲义{i:02d}.pdf") for i in range(n // 2)]
    plan += [("大二上/高等数学", f"复习 资料{i:02d}.docx") for i in range(n - n // 2)]
    for d, name in plan:
        p = os.path.join(path, d.replace("/", os.sep), name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(("data-" + name).encode("utf-8"))


# ======================= 场景 C1：执行中取消 =======================
root1 = rd.sub("c1_root")
make_root(root1, N_FILES)
before1 = full_manifest(root1)
save_manifest(os.path.join(rd.sub("manifests"), "c1_before.json"), before1)

c1_json = os.path.join(CH, "c1_obs.json")
body1 = r'''
name_ok = True
w = main.MainWindow()
w.show()
wait_until(app, lambda: w.isVisible(), 20, "visible")
recent_spaces.touch(ROOT)
open_space(ROOT)
w.path_edit.setText(ROOT)
QTest.mouseClick(w.btn_scan, Qt.LeftButton)
ok = wait_until(app, lambda: (w.scan_result is not None and not w._working
                              and not w._plan_working), 60, "scan")
trace(f"scan_done={ok} items={0 if w.current_plan is None else len(w.current_plan.items)}")
st_confirm = click_confirm()
trace(f"confirm_handled={st_confirm['handled']}")
hit = wait_until(app, reached.is_set, 90, "sync point")
batch_before = None
if w._storage is not None:
    bs = w._storage.list_batches()
    batch_before = bs[0].batch_id if bs else None
trace(f"reached={hit} moved_inflight={_ren['n']} batch={batch_before}")
cancel_enabled = w.btn_cancel.isEnabled() and w.btn_cancel.isVisible()
trace(f"cancel_button_enabled={cancel_enabled}")
QTest.mouseClick(w.btn_cancel, Qt.LeftButton)
trace("cancel_clicked")
pump(app, 0.2)
gate.set()
done = wait_until(app, lambda: (not w._working and not w._plan_working), 120, "cancel done")
pump(app, 0.5)
b = w._storage.get_batch(batch_before) if batch_before else None
acts = w._storage.get_actions(batch_before) if batch_before else []
statuses = {}
for a in acts:
    statuses[a.status] = statuses.get(a.status, 0) + 1
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
trace(f"batch_status={getattr(b,'status',None)} statuses={statuses} disk={disk}")
dump({"phase": "after_cancel", "batch_id": batch_before,
      "batch_status": getattr(b, "status", None), "statuses": statuses,
      "disk": disk, "cancel_enabled": cancel_enabled, "done": done,
      "total_files": len(w.scan_result.files) if w.scan_result else None})
w.close()
wait_until(app, lambda: not w.isVisible(), 30, "closed")
sys.exit(0 if (hit and cancel_enabled and done) else 1)
'''
rec1 = run_child(child_source(root1, c1_json, WINDOW, body1), "c1", CH, BASE, timeout=240)
obs1 = {}
if os.path.isfile(c1_json):
    with open(c1_json, "r", encoding="utf-8") as f:
        obs1 = json.load(f)
print("  C1 观测:", json.dumps({k: obs1.get(k) for k in
                                 ("batch_status", "statuses", "disk", "renames")},
                                ensure_ascii=False))

R.check("C1_0", "打开全新根目录未出现意外弹窗（否则子进程会挂死）",
        obs1.get("open_dialogs") == [],
        f"dialogs={obs1.get('open_dialogs')}")
R.check("C1_1", "同步点在“已移动 3 个且未全部完成”时精确命中",
        0 < WINDOW < N_FILES and obs1.get("renames") == WINDOW,
        f"renames={obs1.get('renames')} window={WINDOW} total={N_FILES}")
R.check("C1_2", "取消按钮在任务进行中可见且可用（真实点击）",
        obs1.get("cancel_enabled") is True, f"enabled={obs1.get('cancel_enabled')}")
st = obs1.get("statuses", {})
R.check("C1_3", "批次状态为 cancelled（部分已移动被取消）",
        obs1.get("batch_status") == "cancelled", f"status={obs1.get('batch_status')}")
R.check("C1_4", "动作状态与同步点一致：moved=3，其余 cancelled",
        st.get("moved") == WINDOW and
        st.get("cancelled") == N_FILES - WINDOW,
        f"statuses={st}")
R.check("C1_5", "磁盘与记录一致：目标 3 个、源剩 9 个",
        obs1.get("disk", {}).get("target") == WINDOW and
        obs1.get("disk", {}).get("source") == N_FILES - WINDOW,
        f"disk={obs1.get('disk')}")
R.check("C1_6", "无半成品可执行计划残留（任务已结束且窗口可正常关闭）",
        obs1.get("done") is True and rec1["exit_code"] == 0,
        f"done={obs1.get('done')} child_exit={rec1['exit_code']}")

# 撤销恢复：新进程从持久化历史撤销
after1 = full_manifest(root1)
undo_json = os.path.join(CH, "c1_undo.json")
body1u = r'''
from executor import undo_is_available, undo_latest_batch
from storage import Storage
s = Storage(ROOT)
avail = undo_is_available(s)
res = undo_latest_batch(s)
after = {"source": 0, "target": 0}
for dp, dn, fn in os.walk(ROOT):
    rel = os.path.relpath(dp, ROOT)
    head = rel.split(os.sep)[0]
    if head == ".organizer" or head.startswith(".abx_owned"):
        continue
    if head == "_已整理":
        after["target"] += len(fn)
    else:
        after["source"] += len(fn)
bs = s.list_batches()
b = bs[0] if bs else None
json.dump({"available": avail, "restored": res.restored,
           "conflict": res.conflict, "errors": res.errors,
           "batch_status": getattr(b, "status", None), "batch_id": getattr(b, "batch_id", None),
           "disk": after},
          open(OUTJSON, "w", encoding="utf-8"), ensure_ascii=False)
s.close()
'''
rec1u = run_child(child_source(root1, undo_json, 999, body1u), "c1u", CH, BASE, timeout=180)
obs1u = {}
if os.path.isfile(undo_json):
    with open(undo_json, "r", encoding="utf-8") as f:
        obs1u = json.load(f)
print("  C1 撤销观测:", json.dumps(obs1u, ensure_ascii=False))

restored_manifest = full_manifest(root1)
R.check("C1_7", "撤销可恢复已完成的 3 项（restored=3，无冲突）",
        obs1u.get("available") is True and obs1u.get("restored") == WINDOW
        and obs1u.get("conflict") == 0,
        f"{obs1u}")
R.check("C1_8", "撤销后完整文件清单与整理前一致（路径/大小/SHA-256）",
        obs1.get("renames") == WINDOW and restored_manifest == before1,
        f"renames={obs1.get('renames')} diff_keys={set(restored_manifest) ^ set(before1)}")

# ======================= 场景 C2：执行中关闭窗口 =======================
root2 = rd.sub("c2_root")
make_root(root2, N_FILES)
before2 = full_manifest(root2)
save_manifest(os.path.join(rd.sub("manifests"), "c2_before.json"), before2)

c2_json = os.path.join(CH, "c2_obs.json")
body2 = r'''
from PySide6.QtCore import QTimer
w = main.MainWindow()
w.show()
wait_until(app, lambda: w.isVisible(), 20, "visible")
recent_spaces.touch(ROOT)
open_space(ROOT)
w.path_edit.setText(ROOT)
QTest.mouseClick(w.btn_scan, Qt.LeftButton)
wait_until(app, lambda: (w.scan_result is not None and not w._working
                        and not w._plan_working), 60, "scan")
click_confirm()
hit = wait_until(app, reached.is_set, 90, "sync point")
bs = w._storage.list_batches()
batch_id = bs[0].batch_id if bs else None
trace(f"reached={hit} in_flight={_ren['n']} batch={batch_id}")

# 关闭窗口：走正常 closeEvent，应进入等待相位而非立刻关闭
w.close()
pump(app, 0.3)
pending = getattr(w, "_close_pending", False)
closing = getattr(w, "_closing", False)
alive_storage = w._storage is not None
db_usable = False
if alive_storage:
    try:
        db_usable = w._storage.conn.execute("select 1").fetchone()[0] == 1
    except Exception as e:
        trace(f"db error {e!r}")
trace(f"close_pending={pending} closing={closing} storage_alive={alive_storage} db_usable={db_usable}")

# 关闭等待期间禁止启动新任务
scan_before = w.scan_result
nb_before = len(w._storage.list_batches()) if w._storage else -1
w.on_scan()
pump(app, 0.3)
blocked = (w.scan_result is scan_before) and (
    (len(w._storage.list_batches()) if w._storage else -1) == nb_before)
trace(f"new_task_blocked={blocked} working={w._working}")

# 各后台线程独立 SQLite 连接
calls = _storage_ctor["calls"]
main_thread_name = threading.main_thread().name
worker_calls = [c for c in calls if c["thread"] != main_thread_name]
distinct = len(set(c["id"] for c in calls)) == len(calls)
trace(f"storage_in_worker_threads={len(worker_calls)} distinct={distinct}")

gate.set()
trace("gate_released")
closed = wait_until(app, lambda: not w.isVisible(), 90, "window closed after worker end")
trace(f"closed={closed} closing_flag={getattr(w,'_closing',None)}")
dump({"phase": "after_close", "batch_id": batch_id, "hit": hit,
      "close_pending": pending, "closing_during_wait": closing,
      "storage_alive_during_wait": alive_storage, "db_usable": db_usable,
      "new_task_blocked": blocked,
      "storage_worker_threads": len(worker_calls),
      "storage_distinct": distinct,
      "closed": closed, "closing_after": getattr(w, "_closing", None),
      "renames": _ren["n"]})
sys.exit(0 if (hit and pending and not closing and closed) else 1)
'''
rec2 = run_child(child_source(root2, c2_json, 2, body2), "c2", CH, BASE, timeout=240)
obs2 = {}
if os.path.isfile(c2_json):
    with open(c2_json, "r", encoding="utf-8") as f:
        obs2 = json.load(f)
print("  C2 观测:", json.dumps({k: obs2.get(k) for k in
                                 ("close_pending", "closing_during_wait",
                                  "storage_alive_during_wait", "db_usable",
                                  "new_task_blocked", "closed", "renames")},
                                ensure_ascii=False))

R.check("C2_0", "打开全新根目录未出现意外弹窗",
        obs2.get("open_dialogs") == [], f"dialogs={obs2.get('open_dialogs')}")
R.check("C2_1", "执行中关闭：进入关闭等待相位而非立即关闭",
        obs2.get("close_pending") is True
        and obs2.get("closing_during_wait") is False,
        f"pending={obs2.get('close_pending')} closing={obs2.get('closing_during_wait')}")
R.check("C2_2", "等待期间数据库对象仍存活且连接可用",
        obs2.get("storage_alive_during_wait") is True and obs2.get("db_usable") is True,
        f"alive={obs2.get('storage_alive_during_wait')} usable={obs2.get('db_usable')}")
R.check("C2_3", "等待期间禁止启动新任务（扫描结果未被覆盖）",
        obs2.get("new_task_blocked") is True, f"blocked={obs2.get('new_task_blocked')}")
R.check("C2_4", "后台线程使用自己的 SQLite 连接（非主线程、实例互不相同）",
        (obs2.get("storage_worker_threads") or 0) >= 1
        and obs2.get("storage_distinct") is True,
        f"worker_conns={obs2.get('storage_worker_threads')} distinct={obs2.get('storage_distinct')}")
R.check("C2_5", "线程真正结束后窗口才关闭（closing 置位）",
        obs2.get("closed") is True and obs2.get("closing_after") is True,
        f"closed={obs2.get('closed')} closing={obs2.get('closing_after')}")
R.check("C2_6", "受控关闭过程子进程正常退出（无异常）",
        rec2["exit_code"] == 0, f"exit={rec2['exit_code']}")

# 关闭后新进程：能读历史、无锁、撤销恢复完整
c2b_json = os.path.join(CH, "c2b_obs.json")
body2b = r'''
from storage import Storage
from executor import undo_is_available, undo_latest_batch
s = Storage(ROOT)
bs = s.list_batches()
b = bs[0] if bs else None
avail = undo_is_available(s)
statuses = {}
for a in s.get_actions(b.batch_id) if b else []:
    statuses[a.status] = statuses.get(a.status, 0) + 1
res = undo_latest_batch(s) if b else None
post = {}
for dp, dn, fn in os.walk(ROOT):
    rel = os.path.relpath(dp, ROOT)
    head = rel.split(os.sep)[0]
    if head == ".organizer" or head.startswith(".abx_owned"):
        continue
    if head == "_已整理":
        post["target"] = post.get("target", 0) + len(fn)
    else:
        post["source"] = post.get("source", 0) + len(fn)
b2 = s.list_batches()[0] if s.list_batches() else None
json.dump({"batch_id": getattr(b, "batch_id", None),
           "batch_status": getattr(b, "status", None),
           "statuses": statuses, "available": avail,
           "restored": getattr(res, "restored", None),
           "conflict": getattr(res, "conflict", None),
           "errors": getattr(res, "errors", None),
           "batch_status_after": getattr(b2, "status", None),
           "disk": post},
          open(OUTJSON, "w", encoding="utf-8"), ensure_ascii=False)
s.close()
'''
rec2b = run_child(child_source(root2, c2b_json, 999, body2b), "c2b", CH, BASE, timeout=180)
obs2b = {}
if os.path.isfile(c2b_json):
    with open(c2b_json, "r", encoding="utf-8") as f:
        obs2b = json.load(f)
print("  C2 新进程观测:", json.dumps(obs2b, ensure_ascii=False))

R.check("C2_7", "关闭后新进程可打开历史库（无数据库锁）",
        rec2b["exit_code"] == 0 and obs2b.get("batch_id") is not None,
        f"exit={rec2b['exit_code']} batch={obs2b.get('batch_id')}")
R.check("C2_8", "被中断批次未被误记为成功（状态非 done）",
        obs2b.get("batch_status") in ("cancelled", "partial", "cancelled_clean",
                                      "cancelled_undone"),
        f"status={obs2b.get('batch_status')} actions={obs2b.get('statuses')}")
R.check("C2_9", "撤销后源文件完整恢复（数量一致）",
        obs2b.get("restored") == 2
        and obs2b.get("disk", {}).get("target", 0) == 0
        and obs2b.get("disk", {}).get("source") == N_FILES,
        f"restored={obs2b.get('restored')} disk={obs2b.get('disk')}")
restored2 = full_manifest(root2)
R.check("C2_10", "撤销后清单与整理前逐文件一致",
        (obs2.get("renames") or 0) > 0 and restored2 == before2,
        f"renames={obs2.get('renames')} diff_keys={set(restored2) ^ set(before2)}")

R.check_child_exits("C_EXIT", title_extra="本套件全部 GUI 子进程")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["C1_0"]
                    + [f"C1_{i}" for i in range(1, 9)]
                    + ["C2_0"] + [f"C2_{i}" for i in range(1, 11)]
                    + ["C_EXIT"]))
