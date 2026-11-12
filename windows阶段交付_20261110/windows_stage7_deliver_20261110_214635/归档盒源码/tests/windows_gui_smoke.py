"""GUI 冒烟：主窗口 → 扫描 → 预览 → 断言表非空 → 截图 → 关闭 → 退出码。
隔离状态目录，Windows 原生桌面。
"""
from __future__ import annotations
import os, sys, time, shutil, json, subprocess, hashlib

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

# ===== 隔离状态目录 =====
BASE = os.environ.get("ARCHIVEBOX_TEST_BASE", r"C:\Users\Lenovo\AppData\Local\Temp\_abx_gui_smoke")
os.environ.setdefault("ARCHIVEBOX_RECENT_FILE", os.path.join(BASE, "recent.json"))
os.environ.setdefault("ARCHIVEBOX_APP_DIR", os.path.join(BASE, "appdata"))
os.environ.setdefault("ARCHIVEBOX_SCHEME_DIR", os.path.join(BASE, "appdata", "schemes"))
os.environ.setdefault("QT_QPA_PLATFORM", "")  # 强制原生

import recent_spaces
import scheme_store
print(f"recent env: {os.environ.get('ARCHIVEBOX_RECENT_FILE')}")
print(f"scheme  env: {os.environ.get('ARCHIVEBOX_APP_DIR')}")

# ===== patch QMessageBox =====
for n in ("information", "warning", "critical"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

# ===== 合成目录 =====
ROOT = os.path.join(BASE, "root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
os.makedirs(ROOT, exist_ok=True)
for p, c in [("大二上/数据结构/讲义.pdf", "pdf-1"),
             ("大二上/数据结构/作业/hw1.docx", "doc-1"),
             ("大一下/程序设计基础/代码/main.py", "print('x')"),
             ("图片素材/logo.png", "png-bytes")]:
    full = os.path.join(ROOT, p)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f: f.write(c.encode("utf-8"))

SHOT_DIR = os.path.join(BASE, "shots")
os.makedirs(SHOT_DIR, exist_ok=True)
def shot(w, name):
    path = os.path.join(SHOT_DIR, name)
    w.grab().save(path)
    print(f"shot → {path}")
    return path

def pump(app, sec=0.3):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.02)

def wait(app, cond, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond(): return True
        time.sleep(0.02)
    return bool(cond())

app = QApplication(sys.argv)
print(f"platform = {app.platformName()}  screens = {len(app.screens())}")

from main import MainWindow
w = MainWindow()
w.path_edit.setText(ROOT)
w._reset_for_new_dir(ROOT)
w.show()
pump(app)
shot(w, "01_main_window.png")

# ===== 扫描 =====
w.on_scan()
ok = wait(app, lambda: (not w._working and not w._plan_working and w.scan_result is not None), timeout=20)
print(f"scan done = {ok}")
print(f"scan_result.files = {len(w.scan_result.files) if w.scan_result else None}")
print(f"table.rowCount = {w.table.rowCount()}")
print(f"plan_valid = {w._plan_valid}  plan.total = {w.current_plan.total if w.current_plan else None}")
shot(w, "02_preview.png")

assert w.scan_result is not None and len(w.scan_result.files) == 4, "扫描结果应为 4 个文件"
assert w._plan_valid and w.current_plan is not None, "预览计划应有效"
assert w.table.rowCount() >= 1, "表格应有行"

# ===== 执行 =====
w.on_execute()
ok2 = wait(app, lambda: (not w._working and w.current_plan is not None and not w._exec_worker.isRunning() if hasattr(w, '_exec_worker') else True), timeout=30)
print(f"exec done = {ok2}")
print(f"storage = {w._storage}")
print(f"storage.root = {w._storage.root if w._storage else None}")

# 检查磁盘目标
import os
organized = []
for dp, dn, fn in os.walk(os.path.join(ROOT, "_已整理")):
    for f in fn:
        organized.append(os.path.join(dp, f))
print(f"organized files = {len(organized)}")
assert len(organized) == 4, f"归档目录应有 4 个文件，实际 {len(organized)}"
shot(w, "03_after_execute.png")

# ===== 关闭 =====
w.close()
pump(app)
print("main_window closed")

# ===== 验证数据库在磁盘 =====
db = os.path.join(ROOT, ".organizer", "history.db")
print(f"history.db exists = {os.path.exists(db)}")

print("GUI_SMOKE_OK")
sys.exit(0)
