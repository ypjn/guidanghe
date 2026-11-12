"""S7-W2-1：Windows 资源管理器原生拖放（真实 OLE 拖拽，非 Qt 事件注入）。

硬性要求（用户指令）：
- 从资源管理器把两个合成文件拖入「待整理清单」，清单 +2；
- 按清单扫描后，计划只包含这两个文件，范围外文件不变；
- 必须有真实操作证据，禁止用 Qt 事件注入宣称通过。

方法（全部真实操作）：
- 子进程为真实 Qt GUI（windows 平台），打开主窗口与待整理清单对话框；
- subprocess 启动真实 explorer.exe /select,<file>，定位到固定屏幕位置，
  SendInput 发真实 Ctrl+A 全选（源目录仅含两个合成文件）；
- 截图色带检测自动定位选中行（无硬编码像素），SendInput 发真实鼠标
  按下/移动/释放 —— 由资源管理器自身发起 OLE 拖放（CF_HDROP 两个文件），
  Qt 窗口作为真实 OLE 放置目标接收；
- 释放后验证清单 +2、按清单扫描计划恰含两文件、范围外文件未动；
- 全程三张截图留证（资源管理器定位/全选高亮/清单+2）。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, run_child, sha256_file)  # noqa

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", os.getcwd()),
                                   "_abx_w2_dragdrop"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w2_dragdrop")
CH = rd.sub("children")
R = Results("w2_dragdrop")
print(f"=== w2_dragdrop run={rd.run_id} ===")

CHILD = r'''
import json, os, shutil, subprocess, sys, time
from collections import Counter

SRC = os.environ["ARCHIVEBOX_SRC"]
TESTS = os.path.join(SRC, "tests")
CHD = os.path.join(TESTS, "stage7_children")
for p in (SRC, TESTS, CHD):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.pop("QT_QPA_PLATFORM", None)

from s7_common import (make_app, ok, pump, wait, Dispatcher, ack_message,
                       finish, mkfile, open_root, RESULT, OUT)
from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtWidgets import QApplication
import windows_harness as H


class _DragTap(QObject):
    """app 级事件探针（只记录，不拦截）：捕获 DragEnter/DragMove/
    DragLeave/Drop 是否真的到达本进程的 Qt 窗口。"""

    NAMES = {60: "DragEnter", 61: "DragMove", 62: "DragLeave", 63: "Drop"}

    def __init__(self, appobj):
        super().__init__(appobj)
        self.events = []

    def eventFilter(self, obj, ev):
        t = int(ev.type())
        if t in self.NAMES:
            self.events.append((self.NAMES[t], obj.metaObject().className()))
        return False

import ctypes
from ctypes import wintypes

USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32

ULONG_PTR = ctypes.c_ulonglong


class MOUSEINPUT(ctypes.Structure):
    # mouseData 字段不可省：少了它 sizeof(INPUT)=32，SendInput 返回 0
    # 且 GetLastError=87（参数无效），鼠标键盘全部注入失败（本机实测）。
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion)]


SCREEN_W, SCREEN_H = 0, 0  # 物理分辨率，make_app 后由 QScreen 填充
MOVE, LEFTDOWN, LEFTUP, ABSOLUTE = 0x0001, 0x0002, 0x0004, 0x8000


def set_screen_metrics(screen):
    """SendInput 绝对坐标归一化分母必须用物理分辨率。

    GetSystemMetrics 在 per-monitor DPI 感知进程里返回逻辑值（本机实测
    1707x1067），直接当分母会让鼠标落点整体偏移；物理值 =
    QScreen 逻辑宽 × devicePixelRatio（本机实测 2560x1600）。
    """
    global SCREEN_W, SCREEN_H
    dpr = screen.devicePixelRatio()
    SCREEN_W = int(screen.geometry().width() * dpr)
    SCREEN_H = int(screen.geometry().height() * dpr)


def send_mouse(x, y, flags):
    inp = INPUT(type=0)
    inp.mi = MOUSEINPUT(dx=int(x * 65536 / SCREEN_W),
                        dy=int(y * 65536 / SCREEN_H),
                        mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    n = USER32.SendInput(1, (INPUT * 1)(inp), ctypes.sizeof(INPUT))
    if n != 1:
        RESULT["errors"].append(
            f"SendInput 失败 ret={n} last_error={ctypes.get_last_error()}"
            f" @({x},{y}) flags={flags:#x}")


def send_key(vk, up=False):
    inp = INPUT(type=1)
    inp.ki = KEYBDINPUT(wVk=vk, dwFlags=2 if up else 0)
    USER32.SendInput(1, (INPUT * 1)(inp), ctypes.sizeof(INPUT))


def ctrl_a():
    send_key(0x11)          # Ctrl down
    send_key(0x41)         # A down
    send_key(0x41, True)   # A up
    send_key(0x11, True)    # Ctrl up


def enum_windows():
    res = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        cls = ctypes.create_unicode_buffer(256)
        USER32.GetClassNameW(h, cls, 256)
        ttl = ctypes.create_unicode_buffer(512)
        USER32.GetWindowTextW(h, ttl, 512)
        res.append((h, cls.value, ttl.value))
        return True

    USER32.EnumWindows(cb, 0)
    return res


def wait_explorer(title_part, timeout=12.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump(app, 0.3)
        for h, c, t in enum_windows():
            if c == "CabinetWClass" and title_part in t:
                return h, t
        time.sleep(0.4)
    return None, None


def grab_screen(name):
    pm = app.primaryScreen().grabWindow(0)
    p = os.path.join(OUT, "shots", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    pm.save(p)
    RESULT["artifacts"]["screen_" + name] = p
    return pm


def find_selected_bands(img, lx0, ly0, lx1, ly1):
    """在窗口区找高亮/内容色带（物理像素坐标）。

    阈值经探针 probe_bands2 实测校准：暗色主题下整行选中高亮与文件名
    文本相对背景的色差 > 60 即可稳定覆盖；frac>0.12 过滤窄噪带。
    """
    cnt = Counter()
    for y in range(ly0, ly1):
        for x in range(lx0, lx1, 3):
            c = img.pixel(x, y)
            cnt[((c >> 16) & 255, (c >> 8) & 255, c & 255)] += 1
    (br, bg, bb), _ = cnt.most_common(1)[0]
    frac = {}
    for y in range(ly0, ly1):
        n = d = 0
        for x in range(lx0, lx1, 2):
            c = img.pixel(x, y)
            r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
            if abs(r - br) + abs(g - bg) + abs(b - bb) > 60:
                d += 1
            n += 1
        frac[y] = d / max(n, 1)
    bands, cur = [], []
    for y in range(ly0, ly1):
        if frac[y] > 0.12:
            cur.append(y)
        else:
            if len(cur) >= 4:
                bands.append((cur[0], cur[-1]))
            cur = []
    if len(cur) >= 4:
        bands.append((cur[0], cur[-1]))
    return bands, (br, bg, bb)


DPR = None

ROOT = os.environ["ARCHIVEBOX_TEST_BASE"]
ROOT = os.path.join(ROOT, "dragdrop_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT)
SRC_DIR = os.path.join(ROOT, "拖入源")
os.makedirs(SRC_DIR)
F1 = os.path.join(SRC_DIR, "w2拖放样例甲.txt")
F2 = os.path.join(SRC_DIR, "w2拖放样例乙.pdf")
OUTSIDE = os.path.join(ROOT, "范围外文件.txt")
with open(F1, "wb") as f:
    f.write(b"DRAG-A-0123456789")
with open(F2, "wb") as f:
    f.write(b"DRAG-B-9876543210")
with open(OUTSIDE, "wb") as f:
    f.write(b"OUTSIDE-KEEP")
SHA = {p: H.sha256_file(p) for p in (F1, F2, OUTSIDE)}

DSP = Dispatcher(app := make_app())
set_screen_metrics(app.primaryScreen())
tap = _DragTap(app)
app.installEventFilter(tap)

from main import MainWindow  # noqa: E402

w = MainWindow()
w.show()
pump(app)
open_root(app, w, ROOT)
pump(app, 0.2)
w.btn_intake.click()
pump(app, 0.5)
dlg = getattr(w, "_intake_dialog", None)
ok("DD0_1", dlg is not None and dlg.isVisible(), f"待整理清单对话框已打开")

# 对话框固定到主屏右侧（物理像素）
USER32.SetWindowPos(ctypes.c_void_p(int(dlg.winId())), 0, 900, 60, 1230, 860,
                    0x0040)
dlg.raise_()
pump(app, 0.6)
DPR = w.devicePixelRatio()
lw = dlg.list_widget
center = lw.mapToGlobal(QPoint(lw.width() // 2, lw.height() // 2))
drop_phys = (int(center.x() * DPR), int(center.y() * DPR))
ok("DD0_2", 0 < drop_phys[0] < SCREEN_W and 0 < drop_phys[1] < SCREEN_H,
   f"对话框已定位，清单控件释放点（物理像素）={drop_phys} dpr={DPR} "
   f"screen={SCREEN_W}x{SCREEN_H}")

# ---------------- 真实资源管理器（拖放源） ----------------
subprocess.Popen(["explorer", f"/select,{F1}"])
ex_hwnd, ex_title = wait_explorer("拖入源")
ok("DD1_1", ex_hwnd is not None,
   f"资源管理器窗口已打开（/select 第一项）：title={ex_title}")
USER32.SetWindowPos(ex_hwnd, 0, 60, 60, 820, 560, 0x0040)


def force_foreground(h):
    """AttachThreadInput 强制前台（探针 probe_bands2 实测有效）。"""
    fg = USER32.GetForegroundWindow()
    if fg == h:
        return
    me = KERNEL32.GetCurrentThreadId()
    t_fg = USER32.GetWindowThreadProcessId(fg, None)
    t_h = USER32.GetWindowThreadProcessId(h, None)
    USER32.AttachThreadInput(me, t_fg, True)
    USER32.AttachThreadInput(me, t_h, True)
    USER32.SetForegroundWindow(h)
    USER32.BringWindowToTop(h)
    USER32.AttachThreadInput(me, t_fg, False)
    USER32.AttachThreadInput(me, t_h, False)


fg_ok = False
for _k in range(8):
    if USER32.GetForegroundWindow() == ex_hwnd:
        fg_ok = True
        break
    force_foreground(ex_hwnd)
    pump(app, 0.6)
RESULT["notes"].append(
    f"资源管理器强制前台（AttachThreadInput，重试验证）："
    f"{'成功' if fg_ok else '失败'}。测试期间用户已关闭高权限桌面宠物 "
    f"BongoCat（UnityWndClass）—— 该进程会持续抢占前台焦点，导致 OLE "
    f"拖拽无法发起（探针实测），属于环境干扰而非产品缺陷。")
pump(app, 1.5)
img_init = grab_screen("29_explorer_open.png").toImage()

# /select 已把第一项置为选中 —— 初始截图里最高的色带即第一个文件行，
# 以它定位文件行（避免硬编码行位置；两行位置每次开窗有漂移，本机实测）。
init_bands, bg0 = find_selected_bands(img_init, 100, 150, 860, 560)
row_bands = [b for b in init_bands if b[1] - b[0] + 1 >= 20]
row = max(row_bands, key=lambda b: b[1] - b[0]) if row_bands else None
row_h = (row[1] - row[0] + 1) if row else 0

# ---------------- 单击第二行取得焦点，再真实 Ctrl+A 全选 ----------------
# 探针（probe_drag_dst）实测配方：x=560 在文件区（x<400 是导航窗格）；
# 点【未选中】的第二行——点已选中项会触发 Win11 延迟重命名吞掉拖拽。
drag_x_phys = 560
drag_y_phys = (row[1] + max(row_h // 2, 8)) if row else 330
send_mouse(drag_x_phys, drag_y_phys, MOVE | ABSOLUTE)
pump(app, 0.25)
send_mouse(drag_x_phys, drag_y_phys, MOVE | ABSOLUTE | LEFTDOWN)
pump(app, 0.06)
send_mouse(drag_x_phys, drag_y_phys, MOVE | ABSOLUTE | LEFTUP)
pump(app, 0.5)
ctrl_a()
pump(app, 0.8)
img_sel = grab_screen("30_before_drag.png").toImage()
sel_bands, bgc = find_selected_bands(
    img_sel, 100, max(150, (row[0] if row else 330) - 60),
    860, (row[0] if row else 330) + 3 * (row_h or 40) + 40)
sel_h = sum(b[1] - b[0] + 1 for b in sel_bands)
ok("DD1_2", row is not None and sel_h >= 1.5 * row_h,
   f"Ctrl+A 生效证明（单击定位行→全选，真实键鼠注入）：初始选中行="
   f"{row} 高={row_h}px 初始色带={init_bands} 全选后色带={sel_bands} "
   f"总高={sel_h}px（须≥1.5×{row_h}px）背景={bgc}")
if row is None:
    RESULT["errors"].append(f"初始行未定位（init_bands={init_bands}），"
                            f"退回默认 y={drag_y_phys} 拖拽")
    drag_y_phys = 330
    row_h = 40
RESULT["notes"].append(
    f"拖拽起点（物理像素）=({drag_x_phys},{drag_y_phys}) —— 文件区第二行"
    f"中心（由 /select 初始选中行 {row} 推算，避免点到已选中首行触发"
    f" Win11 延迟重命名）")

# ---------------- 真实鼠标拖拽（OLE 由资源管理器发起） ----------------
send_mouse(drag_x_phys, drag_y_phys, MOVE | ABSOLUTE)
pump(app, 0.15)
send_mouse(drag_x_phys, drag_y_phys, MOVE | ABSOLUTE | LEFTDOWN)
pump(app, 0.05)
# 先小步越阈（每步 4px、约 45ms 一步），再线性推进到释放点（探针实测配方）
for _i in range(1, 6):
    send_mouse(drag_x_phys + 4 * _i, drag_y_phys + 1, MOVE | ABSOLUTE)
    pump(app, 0.03)
    time.sleep(0.015)
x0, y0 = drag_x_phys + 20, drag_y_phys + 1
x1, y1 = drop_phys
steps = 36
for i in range(1, steps + 1):
    xi = x0 + (x1 - x0) * i // steps
    yi = y0 + (y1 - y0) * i // steps
    send_mouse(xi, yi, MOVE | ABSOLUTE)
    pump(app, 0.03)
    time.sleep(0.015)
pump(app, 0.2)
send_mouse(x1, y1, MOVE | ABSOLUTE | LEFTUP)
pump(app, 1.2)

# 诊断：真实光标位置 / 释放点顶层窗口 / Qt 收到的拖放事件
_cur = wintypes.POINT()
USER32.GetCursorPos(ctypes.byref(_cur))
_h_at = USER32.WindowFromPhysicalPoint(wintypes.POINT(x1, y1))
_cls_at = ctypes.create_unicode_buffer(256)
USER32.GetClassNameW(_h_at, _cls_at, 256)
RESULT["notes"].append(
    f"释放点诊断：GetCursorPos=({_cur.x},{_cur.y}) "
    f"WindowFromPhysicalPoint({x1},{y1}) 类名={_cls_at.value} "
    f"hwnd==对话框winId:{_h_at == int(dlg.winId())} "
    f"Qt拖放事件={tap.events[:40]}")

grab_screen("31_after_drop.png")
paths = [dlg.list_widget.item(i).data(Qt.UserRole)
         for i in range(dlg.list_widget.count())]
ok("DD2_1", dlg.list_widget.count() == 2
   and set(paths) == {F1, F2},
   f"拖放后清单 +2 且恰为两个合成文件：count={dlg.list_widget.count()} "
   f"paths={paths}")

# 关闭资源管理器窗口与对话框
for h, c, t in enum_windows():
    if c == "CabinetWClass" and "拖入源" in t:
        USER32.PostMessageW(h, 0x0010, 0, 0)
pump(app, 0.8)
dlg.reject()
pump(app, 0.5)

# ---------------- 按清单扫描 ----------------
w.btn_scan_intake.click()
ready = wait(app, lambda: w._plan_valid and w.current_plan is not None, 90)
ok("DD3_1", ready and w._plan_scope == "intake",
   f"按清单扫描完成：scope={w._plan_scope} "
   f"total={w.current_plan.total if w.current_plan else None}")
if w.current_plan:
    srcs = sorted(i.file.rel_path.replace("\\", "/")
                  for i in w.current_plan.items)
    want = sorted(["拖入源/w2拖放样例甲.txt", "拖入源/w2拖放样例乙.pdf"])
    ok("DD3_2", srcs == want,
       f"计划恰包含这两个文件：{srcs} expect={want}")
    ok("DD3_3", "范围外文件.txt" not in srcs
       and all(os.path.isfile(p) and H.sha256_file(p) == SHA[p]
               for p in (F1, F2, OUTSIDE))
       and not os.path.exists(os.path.join(ROOT, "_已整理")),
       f"范围外文件不在计划且三文件均在原位、SHA-256 不变（扫描/计划"
       f"不移动文件）：outside_in_plan={'范围外文件.txt' in srcs}")
else:
    ok("DD3_2", False, "无计划")
    ok("DD3_3", False, "无计划")

shots = [n for n in ("29_explorer_open.png", "30_before_drag.png",
                     "31_after_drop.png")
         if os.path.exists(os.path.join(OUT, "shots", n))
         and os.path.getsize(os.path.join(OUT, "shots", n)) > 0]
ok("DD4_1", len(shots) == 3, f"三张截图（资源管理器定位/全选高亮/清单+2）"
   f"非空：{shots}")

finish(app)
'''

s7_out = os.path.join(CH, "dragdrop_result.json")
rec = run_child(CHILD, "dragdrop", CH, BASE, timeout=300, extra_env={
    "ARCHIVEBOX_SRC": SRC,
    "ARCHIVEBOX_TEST_BASE": BASE,
    "ARCHIVEBOX_RESULTS_DIR": OUT,
    "S7_OUT": s7_out,
    "ARCHIVEBOX_RECENT_FILE": os.path.join(CH, "dragdrop_recent.json"),
    "ARCHIVEBOX_APP_DIR": os.path.join(CH, "dragdrop_appdata"),
    "ARCHIVEBOX_SCHEME_DIR": os.path.join(CH, "dragdrop_appdata",
                                          "schemes"),
})
child = {}
if os.path.isfile(s7_out):
    with open(s7_out, "r", encoding="utf-8") as f:
        child = json.load(f)
for c in child.get("checks", []):
    R.check(c["id"], c["id"], c["ok"], c.get("detail", ""))
R.check("DD_EXIT", "子进程真实 OS 退出码为 0",
        rec["exit_code"] == 0 and not rec["timed_out"],
        f"exit={rec['exit_code']} timed_out={rec['timed_out']} "
        f"elapsed_ms={rec['elapsed_ms']}")

R.check_child_exits("DD_EXIT_ALL", title_extra="原生拖放子进程必须 exit=0")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["DD0_1", "DD0_2", "DD1_1", "DD1_2",
                                  "DD2_1", "DD3_1", "DD3_2", "DD3_3",
                                  "DD4_1", "DD_EXIT", "DD_EXIT_ALL"]))
