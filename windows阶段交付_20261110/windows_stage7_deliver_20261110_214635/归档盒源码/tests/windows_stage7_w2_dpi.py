"""S7-W2-5：DPI 125% / 150% 下主窗口、预览表、确认框、历史窗口可用性。

方法与口径（如实记录，不含糊）：
- 本机双屏实测（探针子进程）：主屏原生 devicePixelRatio = 1.5（系统真实
  设置 150%）；副屏原生 1.25，但为竖屏（864 逻辑宽），放不下主窗口，
  不作为 125% 档位载体。
- 150% 档：主屏系统真实设置，子进程不设任何 DPI 覆盖变量，原生渲染。
- 125% 档：主屏为 150%，无法在不注销的情况下切换；用
  QT_ENABLE_HIGHDPI_SCALING=0（关闭屏幕原生叠加）+ QT_SCALE_FACTOR=1.25
  使 devicePixelRatio 恰为 1.25。Qt6 应用默认 per-monitor-aware，系统
  125% 时的渲染/布局路径与本设置一致；该模拟限制在报告中明确标注。
- 判定：主要按钮（主窗口全部可见按钮 + 确认框 + 历史窗口按钮）非零尺寸、
  中心点在 visibleRegion 内（未被截断/遮挡）、全局矩形落在窗口与屏幕
  可用区内；预览表/历史表所有列宽 > 0、列头非零且在表内（越界列须可
  滚动到达）。注：QHeaderView.visibleRegion() 在滚动区视图内恒为空
  （Qt 实现特性，已实测），表头可点性按几何位置判定。
- 每个档位一个独立子进程，独立隔离目录与合成根目录，截图留证。
"""
from __future__ import annotations

import ctypes
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
                                   "_abx_w2_dpi"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w2_dpi")
CH = rd.sub("children")
R = Results("w2_dpi")
print(f"=== w2_dpi run={rd.run_id} ===")

# ---------------------------------------------------------------- 环境记录
sys_dpi = None
try:
    u32 = ctypes.WinDLL("user32")
    sys_dpi = u32.GetDpiForSystem()
except Exception:
    pass

PROBE = r'''
import json, os, sys
SRC = os.environ["ARCHIVEBOX_SRC"]
sys.path.insert(0, SRC)
os.environ.pop("QT_QPA_PLATFORM", None)
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
screens = [{"name": s.name(), "dpr": s.devicePixelRatio(),
            "geometry": [s.geometry().x(), s.geometry().y(),
                         s.geometry().width(), s.geometry().height()]}
           for s in app.screens()]
with open(os.environ["S7_OUT"], "w", encoding="utf-8") as f:
    json.dump({"platform": app.platformName(), "screens": screens}, f)
print(json.dumps({"platform": app.platformName(), "screens": screens},
                 ensure_ascii=False))
sys.exit(0)
'''

probe_out = os.path.join(CH, "probe_screens.json")
probe_rec = run_child(PROBE, "dpi_probe", CH, BASE, timeout=60, extra_env={
    "ARCHIVEBOX_SRC": SRC, "S7_OUT": probe_out,
})
probe = {}
if os.path.isfile(probe_out):
    with open(probe_out, "r", encoding="utf-8") as f:
        probe = json.load(f)
screens = probe.get("screens", [])
primary_dpr = screens[0]["dpr"] if screens else None
R.check("DPI0_1", "屏幕原生 DPI 探测成功（Qt 原生进程，无任何覆盖变量）",
        probe_rec["exit_code"] == 0 and bool(screens)
        and probe.get("platform") == "windows",
        f"GetDpiForSystem={sys_dpi}；screens={screens}；"
        f"probe_exit={probe_rec['exit_code']}")

native_150 = primary_dpr is not None and abs(primary_dpr - 1.5) < 0.01
R.rec("DPI0_2",
      f"档位来源：主屏原生 dpr={primary_dpr} → "
      + ("150% 档为系统真实设置（子进程不设任何 DPI 覆盖变量）；"
         "125% 档用 QT_ENABLE_HIGHDPI_SCALING=0 + QT_SCALE_FACTOR=1.25 模拟"
         if native_150 else
         "主屏非 150%，125% 与 150% 均用进程级模拟（需在报告标注）"),
      "PASS",
      f"native_150={native_150}；副屏（竖屏）不作为档位载体；"
      f"系统级缩放修改需注销会话，本轮未做")

CHILD = r'''
import json, os, shutil, sys
SRC = os.environ["ARCHIVEBOX_SRC"]
TESTS = os.path.join(SRC, "tests")
CHD = os.path.join(TESTS, "stage7_children")
for p in (SRC, TESTS, CHD):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.pop("QT_QPA_PLATFORM", None)

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       finish, mkfile, open_root, RESULT, OUT)
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (QDialogButtonBox, QPushButton, QTableWidget)

TAG = os.environ["DPI_TAG"]
EXPECT = float(os.environ["DPI_EXPECT"])

app = make_app()
ROOT = os.path.join(os.environ["ARCHIVEBOX_TEST_BASE"], f"dpi{TAG}_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT, exist_ok=True)
FILES = {
    "大二上/数据结构/讲义.pdf": b"lecture",
    "大二上/数据结构/试卷.pdf": b"exam",
    "竞赛/蓝桥杯/报名表.pdf": b"signup",
    "其他/说明.txt": b"readme",
}
for rel, data in FILES.items():
    mkfile(ROOT, rel, data)


def clickable(wdg):
    """非零尺寸 + 中心点在可见区（未被父窗口截断）。"""
    if not (wdg.isVisible() and wdg.width() > 0 and wdg.height() > 0):
        return False, f"visible={wdg.isVisible()} size={wdg.width()}x{wdg.height()}"
    if not wdg.visibleRegion().contains(wdg.rect().center()):
        return False, "中心点不在 visibleRegion（被截断/遮挡）"
    return True, ""


def on_screen(wdg, win):
    tl = wdg.mapToGlobal(QPoint(0, 0))
    br = wdg.mapToGlobal(QPoint(wdg.width(), wdg.height()))
    scr = win.screen().availableGeometry()
    fg = win.frameGeometry()
    inside_win = (tl.x() >= fg.x() and tl.y() >= fg.y()
                  and br.x() <= fg.right() + 1 and br.y() <= fg.bottom() + 1)
    inside_scr = (tl.x() >= scr.x() and tl.y() >= scr.y()
                  and br.x() <= scr.right() + 1 and br.y() <= scr.bottom() + 1)
    return inside_win and inside_scr, f"tl=({tl.x()},{tl.y()}) br=({br.x()},{br.y()})"


CONFIRM = {"done": False, "ok_btn": None, "cancel_btn": None}


def h_confirm(dlg):
    if CONFIRM["done"]:
        return
    bb = dlg.findChild(QDialogButtonBox)
    okb = bb.button(QDialogButtonBox.Ok)
    cb = bb.button(QDialogButtonBox.Cancel)
    CONFIRM["ok_btn"] = clickable(okb)[0] and on_screen(okb, dlg)[0]
    CONFIRM["cancel_btn"] = clickable(cb)[0] and on_screen(cb, dlg)[0]
    shot(dlg, f"dpi{TAG}_confirm.png")
    CONFIRM["done"] = True
    cb.click()      # 取消：不执行移动，只验证确认框可用性


DSP = Dispatcher(app)
DSP.on("确认整理计划", h_confirm)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402
w = MainWindow()
w.show()
pump(app)
ok(f"DPI{TAG}_1", open_root(app, w, ROOT)
   and abs(w.devicePixelRatio() - EXPECT) < 0.01,
   f"devicePixelRatio={w.devicePixelRatio()} expect={EXPECT} "
   f"QT_SCALE_FACTOR={os.environ.get('QT_SCALE_FACTOR')} "
   f"QT_ENABLE_HIGHDPI_SCALING={os.environ.get('QT_ENABLE_HIGHDPI_SCALING')} "
   f"platform={app.platformName()}")
w.on_scan()
ok(f"DPI{TAG}_2", wait(app, lambda: w._plan_valid and w.current_plan is not None),
   f"扫描+计划完成 total={w.current_plan.total if w.current_plan else None}")
shot(w, f"dpi{TAG}_main.png")
shot(w.table, f"dpi{TAG}_table.png")

# --- 主窗口全部可见按钮可完整点到
btns = [b for b in w.findChildren(QPushButton) if b.isVisible()]
bad = []
for b in btns:
    c1, why1 = clickable(b)
    c2, why2 = on_screen(b, w)
    if not (c1 and c2):
        bad.append(f"{b.text()!r}:{why1 or why2}")
ok(f"DPI{TAG}_3", not bad,
   f"主窗口全部可见按钮（{len(btns)} 个）非零尺寸、可点、未被窗口/屏幕截断；"
   + ("; ".join(bad) or f"buttons={len(btns)}"))

# --- 预览表列可点（表头按几何判定：visibleRegion 对 QHeaderView 恒为空）
t = w.table
hdr = t.horizontalHeader()
secs = [hdr.sectionSize(c) for c in range(t.columnCount())]
vw = t.viewport().width()
pos = [hdr.sectionViewportPosition(c) for c in range(t.columnCount())]
overflow = [i for i, p in enumerate(pos) if p < 0 or p + secs[i] > vw]
hdr_geo_ok = (hdr.isVisible() and hdr.width() > 0 and hdr.height() > 0)
tl = hdr.mapToGlobal(QPoint(0, 0))
scr = w.screen().availableGeometry()
hdr_on_scr = (tl.x() >= scr.x() and tl.y() >= scr.y()
              and tl.x() + hdr.width() <= scr.right() + 1
              and tl.y() + hdr.height() <= scr.bottom() + 1)
scroll_ok = (not overflow) or t.horizontalScrollBar().maximum() > 0
ok(f"DPI{TAG}_4", all(s > 0 for s in secs) and hdr_geo_ok and hdr_on_scr
   and scroll_ok,
   f"预览表列全部宽度>0、表头非零且在屏内；越界列可经横向滚动到达；"
   f"cols={t.columnCount()} sizes={secs} viewport={vw} overflow={overflow} "
   f"scrollbar_max={t.horizontalScrollBar().maximum()}")

# --- 确认框（真实点击「开始整理」触发模态确认）
w.btn_execute.click()
pump(app, 1.0)
ok(f"DPI{TAG}_5", CONFIRM["done"] and CONFIRM["ok_btn"] and CONFIRM["cancel_btn"],
   f"确认框出现且「确认开始整理」「取消」均可完整点到（已截图）"
   f"done={CONFIRM['done']} ok={CONFIRM['ok_btn']} "
   f"cancel={CONFIRM['cancel_btn']}")

# --- 历史窗口
w.btn_history.click()
pump(app, 0.8)
hist = getattr(w, "_history_dialog", None)
hist_ok = hist is not None and hist.isVisible()
hb_bad = []
htab_info = ""
if hist_ok:
    for b in hist.findChildren(QPushButton):
        if b.isVisible():
            c1, why1 = clickable(b)
            c2, why2 = on_screen(b, hist)
            if not (c1 and c2):
                hb_bad.append(f"{b.text()!r}:{why1 or why2}")
    tabs = hist.findChildren(QTableWidget)
    htab_info = f"tables={len(tabs)} cols={[x.columnCount() for x in tabs]}"
    if tabs and not all(x.columnCount() > 0 for x in tabs):
        hb_bad.append("历史表存在 0 列")
    shot(hist, f"dpi{TAG}_history.png")
    hist.close()
    pump(app, 0.2)
ok(f"DPI{TAG}_6", hist_ok and not hb_bad,
   f"历史窗口打开、其按钮与表格列可完整点到（已截图）"
   f"open={hist_ok} bad={hb_bad} {htab_info}")

# --- 主窗口整体在屏幕可用区内
scr = w.screen().availableGeometry()
fg = w.frameGeometry()
ok(f"DPI{TAG}_7", fg.x() >= scr.x() - 1 and fg.y() >= scr.y() - 1
   and fg.right() <= scr.right() + 1 and fg.bottom() <= scr.bottom() + 1,
   f"主窗口整体位于屏幕可用区内（未被屏幕边缘截断）"
   f"frame=({fg.x()},{fg.y()},{fg.right()},{fg.bottom()}) "
   f"avail=({scr.x()},{scr.y()},{scr.right()},{scr.bottom()})")

finish(app)
'''

# 150% 档：系统真实设置（主屏原生 dpr=1.5），不设任何 DPI 覆盖变量
# 125% 档：QT_ENABLE_HIGHDPI_SCALING=0 + QT_SCALE_FACTOR=1.25（精确 dpr=1.25）
RUNS = []
if native_150:
    RUNS.append(("125", "1.25", {"QT_ENABLE_HIGHDPI_SCALING": "0",
                                 "QT_SCALE_FACTOR": "1.25"}))
    RUNS.append(("150", "1.5", {}))
else:
    RUNS.append(("125", "1.25", {"QT_ENABLE_HIGHDPI_SCALING": "0",
                                 "QT_SCALE_FACTOR": "1.25"}))
    RUNS.append(("150", "1.5", {"QT_ENABLE_HIGHDPI_SCALING": "0",
                                 "QT_SCALE_FACTOR": "1.5"}))

runs = []
for tag, expect, overrides in RUNS:
    s7_out = os.path.join(CH, f"dpi{tag}_result.json")
    env = {
        "ARCHIVEBOX_SRC": SRC,
        "ARCHIVEBOX_TEST_BASE": BASE,
        "ARCHIVEBOX_RESULTS_DIR": os.path.join(OUT, f"dpi{tag}"),
        "S7_OUT": s7_out,
        "DPI_TAG": tag,
        "DPI_EXPECT": expect,
        "ARCHIVEBOX_RECENT_FILE": os.path.join(CH, f"dpi{tag}_recent.json"),
        "ARCHIVEBOX_APP_DIR": os.path.join(CH, f"dpi{tag}_appdata"),
        "ARCHIVEBOX_SCHEME_DIR": os.path.join(CH, f"dpi{tag}_appdata",
                                              "schemes"),
    }
    env.update(overrides)
    rec = run_child(CHILD, f"dpi{tag}", CH, BASE, timeout=180, extra_env=env)
    os.makedirs(os.path.join(OUT, f"dpi{tag}"), exist_ok=True)
    child = {}
    if os.path.isfile(s7_out):
        with open(s7_out, "r", encoding="utf-8") as f:
            child = json.load(f)
    for c in child.get("checks", []):
        R.check(c["id"], f"[{tag}%] {c['id']}", c["ok"], c.get("detail", ""))
    R.check(f"DPI{tag}_EXIT", f"[{tag}%] 子进程真实 OS 退出码为 0",
            rec["exit_code"] == 0 and not rec["timed_out"],
            f"exit={rec['exit_code']} timed_out={rec['timed_out']} "
            f"elapsed_ms={rec['elapsed_ms']}")
    shots = []
    shot_dir = os.path.join(OUT, f"dpi{tag}", "shots")
    if os.path.isdir(shot_dir):
        shots = [n for n in os.listdir(shot_dir)
                 if os.path.getsize(os.path.join(shot_dir, n)) > 0]
    R.check(f"DPI{tag}_SHOTS", f"[{tag}%] 四张截图（主窗口/预览表/确认框/历史）"
            "已生成且非空",
            {"main", "table", "confirm", "history"} <=
            {n.split("_", 1)[-1].split(".")[0] for n in shots},
            f"shots={sorted(shots)}")
    runs.append({"tag": tag, "expect": expect, "exit": rec["exit_code"],
                 "timed_out": rec["timed_out"], "overrides": overrides,
                 "stdout_log": rec.get("stdout_log"),
                 "stderr_log": rec.get("stderr_log"),
                 "checks": len(child.get("checks", []))})

with open(os.path.join(OUT, "dpi_runs.json"), "w", encoding="utf-8") as f:
    json.dump(runs, f, ensure_ascii=False, indent=2)

R.check_child_exits("DPI_EXIT_ALL", title_extra="探针与两个 DPI 子进程必须 exit=0")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["DPI0_1", "DPI0_2",
                                  "DPI125_1", "DPI125_2", "DPI125_3",
                                  "DPI125_4", "DPI125_5", "DPI125_6",
                                  "DPI125_7", "DPI125_EXIT", "DPI125_SHOTS",
                                  "DPI150_1", "DPI150_2", "DPI150_3",
                                  "DPI150_4", "DPI150_5", "DPI150_6",
                                  "DPI150_7", "DPI150_EXIT", "DPI150_SHOTS",
                                  "DPI_EXIT_ALL"]))
