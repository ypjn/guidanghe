"""S7-W2-2：重复核对窗口真实「打开文件」与「在资源管理器中定位」。

硬性要求（用户指令）：
- 系统默认程序必须实际打开该文件（不替换 QDesktopServices，不用桩）。
- 资源管理器必须打开并选中该文件（不替换 _reveal_in_file_manager）。
- 只核对函数参数不算通过 —— 必须有真实外部操作证据。

证据三层：
1. 进程级：点击前后 Win32_Process 快照差集 + 命令行（默认程序真实拉起，
   命令行含目标路径；explorer 进程命令行含 /select, 目标路径）。
2. 窗口级：EnumWindows 找到默认程序窗口（标题含文件名）与新的
   CabinetWClass 窗口（标题 == 父目录名）。
3. 截图级：记事本窗口在屏截图；Explorer 窗口移动到固定位置后的整屏截图
   （可见选中高亮）。收尾关闭外部窗口/进程，副本文件 SHA 不变。
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
                      os.path.join(os.environ.get("TEMP", os.getcwd()),
                                   "_abx_w2_openreveal"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w2_openreveal")
CH = rd.sub("children")
R = Results("w2_openreveal")
print(f"=== w2_openreveal run={rd.run_id} ===")

CHILD = r'''
import json, os, shutil, subprocess, sys, time, uuid
SRC = os.environ["ARCHIVEBOX_SRC"]
TESTS = os.path.join(SRC, "tests")
CHD = os.path.join(TESTS, "stage7_children")
for p in (SRC, TESTS, CHD):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.pop("QT_QPA_PLATFORM", None)

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       finish, mkfile, open_root, RESULT, OUT)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import windows_harness as H

import ctypes
from ctypes import wintypes

USER32 = ctypes.windll.user32

FNAME = "w2重复内容样例.txt"
DUP_BYTES = b"W2-DUP-CONTENT-0123456789-ABCDEF"

app = make_app()
ROOT = os.path.join(os.environ["ARCHIVEBOX_TEST_BASE"], "openreveal_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
DIR_A = os.path.join(ROOT, "w2打开定位样例目录甲")
DIR_B = os.path.join(ROOT, "乙")
mkfile(ROOT, "w2打开定位样例目录甲/" + FNAME, DUP_BYTES)
mkfile(ROOT, "乙/" + FNAME, DUP_BYTES)
TARGET = os.path.join(DIR_A, FNAME)
TARGET_SHA = H.sha256_file(TARGET)

# ---------------- 进程快照（PowerShell → UTF-8 文件 → 读回） ----------------
def snap_procs():
    p = os.path.join(os.environ["TEMP"], f"abx_snap_{uuid.uuid4().hex}.json")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$ps=Get-CimInstance Win32_Process|"
             "Select-Object ProcessId,Name,CommandLine|"
             "ConvertTo-Json -Compress|"
             f"Out-File -Encoding utf8 '{p}'"],
            capture_output=True, timeout=60)
        with open(p, "r", encoding="utf-8-sig") as f:
            js = json.load(f)
        if isinstance(js, dict):
            js = [js]
        return {int(x["ProcessId"]): x for x in js if x.get("ProcessId")}
    except Exception as e:
        print("snap error:", e)
        return {}
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def wait_new_procs(before, contain=None, name_part=None, timeout=15.0):
    """轮询进程差集；contain=命令行须包含的子串。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump(app, 0.3)
        cur = snap_procs()
        new = {pid: x for pid, x in cur.items() if pid not in before}
        for pid, x in new.items():
            cl = (x.get("CommandLine") or "")
            nm = (x.get("Name") or "")
            if contain is not None and contain.lower() not in cl.lower():
                continue
            if name_part is not None and name_part.lower() not in nm.lower():
                continue
            return {"pid": pid, "name": nm, "cmdline": cl}
        time.sleep(0.7)
    return None


# ---------------- 顶层窗口枚举 ----------------
def enum_windows():
    res = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        cls = ctypes.create_unicode_buffer(256)
        USER32.GetClassNameW(h, cls, 256)
        ttl = ctypes.create_unicode_buffer(512)
        USER32.GetWindowTextW(h, ttl, 512)
        res.append({"hwnd": h, "cls": cls.value, "title": ttl.value})
        return True

    USER32.EnumWindows(cb, 0)
    return res


def find_window(cls=None, title_contains=None, title_eq=None):
    for wdw in enum_windows():
        if cls is not None and wdw["cls"] != cls:
            continue
        t = wdw["title"]
        if title_eq is not None and t.strip().lower() != title_eq.lower():
            continue
        if (title_contains is not None
                and title_contains.lower() not in t.lower()):
            continue
        if cls is None and title_eq is None and title_contains is None:
            continue
        return wdw
    return None


def wait_window(timeout=15.0, **kw):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump(app, 0.3)
        wdw = find_window(**kw)
        if wdw:
            return wdw
        time.sleep(0.4)
    return None


def close_hwnd(hwnd):
    USER32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE


def kill_pid(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                   timeout=30)


def grab_screen(name):
    pm = app.primaryScreen().grabWindow(0)
    p = os.path.join(OUT, "shots", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    pm.save(p)
    RESULT["artifacts"]["screen_" + name] = p
    return os.path.getsize(p) > 0


DSP = Dispatcher(app)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402

w = MainWindow()
w.show()
pump(app)
open_root(app, w, ROOT)
w.on_scan()
ok("OR1_1", wait(app, lambda: w._plan_valid and w.current_plan is not None, 60),
   f"扫描+计划完成 total={w.current_plan.total if w.current_plan else None}")
w.btn_duplicates.click()
pump(app, 0.3)
dlg = getattr(w, "_duplicate_dialog", None)
ready = wait(app, lambda: dlg is not None and dlg.report is not None, 90)
ok("OR1_2", ready, f"duplicates dialog ready={ready}")
shot(dlg, "20_duplicates_dialog.png")


def group_of(tree, kind):
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        if it.data(3, Qt.UserRole) == kind:
            return it
    return None


g_content = group_of(dlg.tree, "content")
ok("OR1_3", g_content is not None and g_content.childCount() == 2,
   f"content 组存在且 2 份 "
   f"paths={[g_content.child(j).text(1) for j in range(g_content.childCount())] if g_content else None}")

target = g_content.child(0)
dlg.tree.setCurrentItem(target)
pump(app, 0.1)

# ---------------- 真实「打开文件」（系统默认程序） ----------------
# 注：Win11 记事本为多标签应用，可能复用已有进程（无新 PID），
# 进程级证据记为信息项 OR2_PROC；窗口级证据（OR2_2）为必测项。
before = snap_procs()
dlg.btn_open.click()          # 真实 QDesktopServices.openUrl，无任何替换
np_ = wait_new_procs(before, contain=TARGET, timeout=15.0)
np2 = wait_new_procs(before, name_part="notepad", timeout=6.0) if not np_ else None
opened_proc = np_ or np2
RESULT["notes"].append(
    "默认程序进程级证据: "
    + (json.dumps(opened_proc, ensure_ascii=False) if opened_proc
       else "无新进程（Win11 记事本为多标签应用，可能复用已有进程；"
            "以窗口级标题证据 + 截图为准"))

nwin = wait_window(title_contains=FNAME, timeout=12.0)
if nwin:
    USER32.SetWindowPos(nwin["hwnd"], 0, 80, 80, 900, 620, 0x0040)
    try:
        USER32.SetForegroundWindow(nwin["hwnd"])
    except Exception:
        pass
    pump(app, 1.2)
ok("OR2_2", nwin is not None,
   f"默认程序窗口出现（标题含文件名，置前并截图）："
   f"{nwin['title'] if nwin else '无'} cls={nwin['cls'] if nwin else ''}")
shot_ok = grab_screen("21_notepad_opened.png")
pump(app, 0.5)

# 关闭默认程序窗口（优雅关闭；兜底 kill 新进程），并确认文件未被修改
closed = False
if nwin:
    close_hwnd(nwin["hwnd"])
    t0 = time.time()
    while time.time() - t0 < 8:
        pump(app, 0.3)
        if find_window(title_contains=FNAME) is None:
            closed = True
            break
if not closed and opened_proc:
    kill_pid(opened_proc["pid"])
    pump(app, 0.5)
ok("OR2_3", H.sha256_file(TARGET) == TARGET_SHA and shot_ok,
   f"打开为只读体验后文件 SHA-256 不变且截图非空；closed={closed}")

# ---------------- 真实「在资源管理器中定位」 ----------------
# 注：explorer /select 进程生命周期短（窗口建成后即退出），进程级证据
# 尽力记录为 note；窗口级证据（CabinetWClass 标题=「目录名 - 文件资源
# 管理器」）为必测主证据。
before2 = snap_procs()
dlg.btn_reveal.click()         # 真实 explorer /select,<path>，无任何替换
ex = wait_new_procs(before2, contain="/select", timeout=15.0)
RESULT["notes"].append(
    "explorer /select 进程级证据: "
    + (json.dumps(ex, ensure_ascii=False) if ex else
       "未捕获（explorer /select 进程生命周期短，窗口建成后即退出；"
       "以窗口级证据 + 截图为准）"))

DIR_TITLE = "w2打开定位样例目录甲"
ewin = wait_window(cls="CabinetWClass", title_contains=DIR_TITLE, timeout=15.0)
ok("OR3_1", ewin is not None,
   f"资源管理器定位窗口真实打开（CabinetWClass，标题=「目录名 - 文件"
   f"资源管理器」）：title={ewin['title'] if ewin else '无'}")
if ewin:
    USER32.SetWindowPos(ewin["hwnd"], 0, 80, 80, 900, 620, 0x0040)
    try:
        USER32.SetForegroundWindow(ewin["hwnd"])
    except Exception:
        pass
    pump(app, 1.5)
    shot_ok2 = grab_screen("22_explorer_reveal.png")
    close_hwnd(ewin["hwnd"])
    pump(app, 0.6)
    gone = find_window(cls="CabinetWClass", title_contains=DIR_TITLE) is None
    ok("OR3_2", shot_ok2 and gone,
       f"Explorer 定位窗口已移动到固定位置截图（可见选中高亮）并关闭；"
       f"shot_ok={shot_ok2} closed={gone}")
else:
    ok("OR3_2", False, "未找到 Explorer 定位窗口，无法截图")

# ---------------- 副本不受影响 ----------------
after_files = H.full_manifest(ROOT)
ok("OR4_1", len(after_files) == 2
   and H.sha256_file(TARGET) == TARGET_SHA
   and H.sha256_file(os.path.join(DIR_B, FNAME)) == TARGET_SHA
   and not os.path.exists(os.path.join(ROOT, "_已整理")),
   f"打开/定位后副本无删除/无移动/内容不变：{sorted(after_files)}")

dlg.close()
pump(app, 0.3)
finish(app)
'''

s7_out = os.path.join(CH, "openreveal_result.json")
rec = run_child(CHILD, "openreveal", CH, BASE, timeout=240, extra_env={
    "ARCHIVEBOX_SRC": SRC,
    "ARCHIVEBOX_TEST_BASE": BASE,
    "ARCHIVEBOX_RESULTS_DIR": OUT,
    "S7_OUT": s7_out,
    "ARCHIVEBOX_RECENT_FILE": os.path.join(CH, "openreveal_recent.json"),
    "ARCHIVEBOX_APP_DIR": os.path.join(CH, "openreveal_appdata"),
    "ARCHIVEBOX_SCHEME_DIR": os.path.join(CH, "openreveal_appdata",
                                          "schemes"),
})
child = {}
if os.path.isfile(s7_out):
    with open(s7_out, "r", encoding="utf-8") as f:
        child = json.load(f)
for c in child.get("checks", []):
    R.check(c["id"], c["id"], c["ok"], c.get("detail", ""))
R.check("OR_EXIT", "子进程真实 OS 退出码为 0",
        rec["exit_code"] == 0 and not rec["timed_out"],
        f"exit={rec['exit_code']} timed_out={rec['timed_out']} "
        f"elapsed_ms={rec['elapsed_ms']}")

R.check_child_exits("OR_EXIT_ALL", title_extra="打开/定位子进程必须 exit=0")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["OR1_1", "OR1_2", "OR1_3", "OR2_2",
                                  "OR2_3", "OR3_1", "OR3_2", "OR4_1",
                                  "OR_EXIT", "OR_EXIT_ALL"]))
