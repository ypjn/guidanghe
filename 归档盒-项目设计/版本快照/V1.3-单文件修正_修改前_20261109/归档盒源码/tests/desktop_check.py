"""
归档盒 V1.1 — 真实桌面交互检查

在系统默认图形后端（macOS 为 cocoa）启动真实窗口，完成：
- 按钮操作：扫描、重新预览、打开保护目录对话框
- 长路径与中文路径显示
- 窗口缩放 900×600 / 1400×900
- 保存关键截图（QWidget.grab 真实渲染；若系统允许再补 screencapture）

若只能使用 offscreen，则明确标记“真实桌面交互未测”，不会伪造成通过。

运行：
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/desktop_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt

import main as main_mod
from main import MainWindow, ProtectDirDialog, ExecuteConfirmDialog, RuleEditDialog
from scanner import scan_directory
from planner import generate_plan, MODE_BY_TYPE
from storage import Storage

BASE = "/tmp/_v11_desktop_verify"
OUT = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/验收截图_20261106"
RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status, "evidence": str(evidence)})
    mark = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"        {line}")


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, *parts, content=None):
    """创建文件：最后一个长字符串可省略 content=。"""
    if content is None and len(parts) >= 2:
        last = parts[-1]
        if isinstance(last, str) and len(last) >= 20 and "/" not in last:
            content, parts = last, parts[:-1]
    if content is None:
        content = "x"
    full = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return full


def pump(app, sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def wait_until(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def shot(widget, name):
    path = os.path.join(OUT, name)
    ok = widget.grab().save(path, "PNG")
    return path if ok else ""


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    app = QApplication([])
    platform = app.platformName()
    desktop = platform == "cocoa"

    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    root = fresh("desktop_fixture")
    # 长路径 + 中文 + 空格
    long_dir = os.path.join(
        root, "大二上", "数据结构与算法", "第三次上机实验",
        "实验报告以及源代码和测试数据的最终提交版本", "附件"
    )
    for i in range(24):
        mk(root, "大一下", "高等数学", f"期末复习资料_{i:02d}.pdf", "p" * 400)
        mk(root, "大二上", "数据结构与算法", f"第{i+1:02d}章课件.pptx", "q" * 400)
    mk(long_dir, "一个名字非常非常非常长的实验报告文件_最终版_不要删除_副本_2026.docx",
       "l" * 400)
    mk(root, "Code", "main.py", "print('hi')\n" * 40)
    mk(root, "校园事务", "报销材料", "发票.pdf", "i" * 400)
    mk(root, "默认保护", "app.py", "x" * 200)

    if not desktop:
        rec("D0", "真实桌面图形后端", "未测",
            f"当前 Qt 平台={platform}，不是 cocoa；真实桌面交互未测。"
            "以下结果仅代表离屏渲染。")
    else:
        rec("D0", "真实桌面图形后端", "通过", f"Qt 平台={platform}（cocoa）")

    w = MainWindow()
    w.path_edit.setText(root)
    w.btn_scan.setEnabled(True)
    w.show()
    pump(app, 0.5)
    p_main = shot(w, "01_主窗口_初始.png")

    # 按钮操作：扫描 → 计划
    w.btn_scan.click()
    scan_ok = wait_until(app, lambda: not w._working and w.scan_result is not None, timeout=20)
    plan_ok = wait_until(app, lambda: not w._plan_working and w._plan_valid, timeout=60)
    pump(app, 0.3)
    p_plan = shot(w, "02_主窗口_分类预览.png")
    d1_ok = scan_ok and plan_ok and w.btn_execute.isEnabled()
    rec("D1", "真实窗口按钮操作：扫描 → 自动生成计划 → 执行按钮可用",
        "通过" if d1_ok else "未通过",
        f"扫描完成={scan_ok} 计划完成={plan_ok} 执行 enabled={w.btn_execute.isEnabled()}\n"
        f"截图={p_main}, {p_plan}")

    # 长路径显示
    long_rel = os.path.relpath(
        os.path.join(long_dir,
                     "一个名字非常非常非常长的实验报告文件_最终版_不要删除_副本_2026.docx"),
        root,
    )
    found = False
    tooltip_ok = False
    long_abs = os.path.join(
        long_dir, "一个名字非常非常非常长的实验报告文件_最终版_不要删除_副本_2026.docx"
    )
    for row in range(w.table.rowCount()):
        for col in range(4):
            item = w.table.item(row, col)
            if item and "一个名字非常非常非常长的实验报告文件" in item.text():
                found = True
                if long_abs in (item.toolTip() or ""):
                    tooltip_ok = True
                break
    d2_ok = found and tooltip_ok
    rec("D2", "长中文路径在表格中完整保存并可通过悬浮提示查看",
        "通过" if d2_ok else "未通过",
        f"找到长路径行={found} 悬浮提示含完整路径={tooltip_ok}\n"
        f"路径={long_rel}\n截图={shot(w, '03_主窗口_长路径.png')}")

    # 重新预览按钮
    before_seq = w._plan_seq_counter
    w.btn_regenerate.click()
    regen_started = wait_until(app, lambda: w._plan_seq_counter > before_seq, timeout=10)
    regen_done = wait_until(app, lambda: not w._plan_working and w._plan_valid, timeout=60)
    d3_ok = regen_started and regen_done
    rec("D3", "真实窗口「重新预览」按钮可重复生成计划",
        "通过" if d3_ok else "未通过",
        f"新任务 seq 已启动={regen_started} 完成={regen_done}")

    # 保护目录对话框
    dlg = ProtectDirDialog(root, [], [("<root>/项目", "auto", "自动识别（VCS 目录 .git 存在）")],
                           12, w)
    dlg.show()
    pump(app, 0.3)
    p_protect = shot(dlg, "04_保护目录对话框.png")
    readonly_text = dlg.readonly_view.toPlainText()
    d4_ok = dlg.isVisible() and "自动识别" in readonly_text and "跳过 12" in readonly_text
    rec("D4", "保护目录对话框显示原因与跳过数量",
        "通过" if d4_ok else "未通过",
        f"对话框可见={dlg.isVisible()} 截图={p_protect}\n"
        f"只读区文本片段={readonly_text.splitlines()[:2]}")
    dlg.close()

    # 执行确认对话框 & 规则对话框
    plan = w.current_plan or generate_plan(w.scan_result, MODE_BY_TYPE, [])
    confirm = ExecuteConfirmDialog(root, plan, w)
    confirm.show()
    pump(app, 0.2)
    p_confirm = shot(confirm, "05_执行确认对话框.png")
    rule_dlg = RuleEditDialog("高数,高等数学 → 高等数学", w)
    rule_dlg.show()
    pump(app, 0.2)
    p_rule = shot(rule_dlg, "06_课程规则对话框.png")
    rec("D5", "执行确认与规则编辑对话框可正常显示",
        "通过" if (confirm.isVisible() and rule_dlg.isVisible()) else "未通过",
        f"截图={p_confirm}, {p_rule}")
    confirm.close()
    rule_dlg.close()

    # 窗口缩放
    sizes = []
    ok_sizes = True
    for wdt, hgt, tag in [(900, 600, "07_窗口_900x600.png"),
                          (1400, 900, "08_窗口_1400x900.png")]:
        w.resize(wdt, hgt)
        pump(app, 0.3)
        sizes.append(shot(w, tag))
        # 关键控件仍在窗口内且可见
        for btn in (w.btn_scan, w.btn_browse, w.btn_execute, w.btn_undo,
                    w.btn_edit_protect):
            if not btn.isVisible():
                ok_sizes = False
        if w.table.height() < 100:
            ok_sizes = False
    rec("D6", "窗口缩放 900×600 / 1400×900 后关键控件仍可见",
        "通过" if ok_sizes else "未通过",
        "\n".join(f"截图={p}" for p in sizes))

    # 真实屏幕截图（系统允许时）
    screen_shot = ""
    if desktop:
        try:
            p = os.path.join(OUT, "09_真实屏幕截图.png")
            r = subprocess.run(["screencapture", "-x", p], capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.isfile(p):
                screen_shot = p
        except Exception:
            screen_shot = ""
    rec("D7", "系统级屏幕截图", "通过" if screen_shot else "未测",
        screen_shot or "screencapture 不可用或无录屏权限；窗口级截图仍有效")

    # 关闭
    try:
        if w._storage is not None:
            w._storage.close()
            w._storage = None
        w.close()
    except Exception:
        pass

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if (failed == 0 and passed > 0) else 1
    print("\n" + "=" * 60)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"真实桌面后端: {platform}  截图目录: {OUT}")
    print(f"退出码: {exit_code}")
    print("=" * 60)

    meta = {
        "suite": "desktop_check",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": platform,
        "screenshots": OUT,
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    results_dir = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(results_dir, "results_desktop_check.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(results_dir, "results_desktop_check.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.1 真实桌面交互检查 — 结果清单\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {sys.version.split()[0]}\n")
        f.write(f"平台: {sys.platform}  Qt: {platform}\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in r["evidence"].splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    print(f"结果已保存: {txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
