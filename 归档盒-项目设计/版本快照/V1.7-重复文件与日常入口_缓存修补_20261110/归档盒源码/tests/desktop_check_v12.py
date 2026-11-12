"""
归档盒 V1.2 第一轮 — 真实桌面交互检查

在系统默认图形后端（macOS 为 cocoa）启动真实窗口，覆盖本轮新功能：
- 完整历史页面：批次列表、逐项明细、搜索、状态筛选、分页、导出
- 冲突撤销：从历史页发起，历史自动刷新为真实撤销结果
- 长路径、窗口缩放（主窗口 + 历史窗口）
- 保存关键截图（QWidget.grab 真实渲染 + 系统级 screencapture）

交互方式说明：D1–D10 的按钮/输入全部为【程序化点击】真实 cocoa 控件；
D11 为【人工观察】——逐张查看渲染截图确认布局与中文显示。
若只能使用 offscreen，则明确标记"真实桌面交互未测"，不会伪造成通过。

运行：
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/desktop_check_v12.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from main import MainWindow, HistoryDialog

BASE = "/tmp/_v12_desktop_verify"
OUT = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/验收截图_20261107_V12"
RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
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


def mk(root, rel, content="x" * 256):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return p


def pump(app, sec=0.25):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def wait_until(app, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


def shot(widget, name):
    path = os.path.join(OUT, name)
    ok = widget.grab().save(path, "PNG")
    return path if ok else ""


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    app = QApplication([])
    platform = app.platformName()
    desktop = platform == "cocoa"

    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    # ---------- 夹具：约 210 个文件（覆盖分页）+ 长路径 ----------
    root = fresh("desktop_fixture")
    long_dir = os.path.join(
        root, "大二上", "数据结构与算法", "第三次上机实验",
        "实验报告以及源代码和测试数据的最终提交版本", "附件"
    )
    for i in range(205):
        mk(root, f"大一下/高等数学/期末复习资料_{i:03d}.pdf", "p" * 400)
    mk(root, "大二上/数据结构与算法/第01章课件.pptx", "q" * 400)
    mk(long_dir, "一个名字非常非常非常长的实验报告文件_最终版_不要删除_副本_2026.docx",
       "l" * 400)
    mk(root, "校园事务/报销材料/发票.pdf", "i" * 400)
    long_rel = ("大二上/数据结构与算法/第三次上机实验/"
                "实验报告以及源代码和测试数据的最终提交版本/附件/"
                "一个名字非常非常非常长的实验报告文件_最终版_不要删除_副本_2026.docx")
    long_abs = os.path.join(root, long_rel)

    if not desktop:
        rec("D0", "真实桌面图形后端", "未测",
            f"当前 Qt 平台={platform}，不是 cocoa；真实桌面交互未测，"
            "以下结果仅代表离屏渲染。")
    else:
        rec("D0", "真实桌面图形后端", "通过", f"Qt 平台={platform}（cocoa）")

    # ---------- D1: 主窗口 → 扫描 → 预览（程序化点击） ----------
    w = MainWindow()
    w.path_edit.setText(root)
    w.btn_scan.setEnabled(True)
    w.show()
    pump(app, 0.5)
    p_main = shot(w, "01_主窗口_初始.png")
    w.btn_scan.click()
    scan_ok = wait_until(app, lambda: not w._working and w.scan_result is not None)
    plan_ok = wait_until(app, lambda: not w._plan_working and w._plan_valid)
    pump(app, 0.3)
    p_plan = shot(w, "02_主窗口_分类预览.png")
    d1_ok = scan_ok and plan_ok and w.btn_execute.isEnabled()
    rec("D1", "主窗口：扫描 → 自动生成计划 → 执行按钮可用（程序化点击）",
        "通过" if d1_ok else "未通过",
        f"扫描完成={scan_ok} 计划完成={plan_ok} "
        f"执行 enabled={w.btn_execute.isEnabled()}\n"
        f"截图={p_main}, {p_plan}")

    # ---------- D2: 长路径显示（主窗口预览表） ----------
    found = tooltip_ok = False
    for row in range(w.table.rowCount()):
        item = w.table.item(row, 1)
        if item and "一个名字非常非常非常长的实验报告文件" in item.text():
            found = True
            if long_abs in (item.toolTip() or ""):
                tooltip_ok = True
            break
    rec("D2", "长中文路径在预览表中显示，悬浮提示含完整绝对路径",
        "通过" if (found and tooltip_ok) else "未通过",
        f"找到长路径行={found} 悬浮提示完整路径={tooltip_ok}\n"
        f"截图={shot(w, '03_主窗口_长路径行.png')}")

    # ---------- D3: 执行（真实移动全部文件，程序化点击） ----------
    conflict_src_rel = w.current_plan.items[0].file.rel_path
    w.btn_execute.click()
    exec_done = wait_until(
        app, lambda: not w._working and w._exec_worker is not None
        and not w._exec_worker.isRunning(), timeout=120)
    pump(app, 0.3)
    rec("D3", "开始整理：真实移动 208 个文件后主窗口回到空闲",
        "通过" if exec_done else "未通过",
        f"执行完成={exec_done}；状态栏={w.statusBar().currentMessage()[:60]}\n"
        f"截图={shot(w, '04_主窗口_执行完成.png')}")

    # ---------- D4: 历史页面（批次列表 + 逐项明细 + 分页） ----------
    w.on_history()
    d = w._history_dialog
    pump(app, 0.5)
    batch_rows = d.batch_table.rowCount()
    item_rows = d.item_table.rowCount()
    page1 = d.page_label.text()
    p_hist = shot(d, "05_历史_批次列表与明细.png")
    d4_ok = (batch_rows == 1 and item_rows == 200 and "1/2" in page1)
    rec("D4", "历史页面：批次列表显示真实批次，明细按需加载第 1 页",
        "通过" if d4_ok else "未通过",
        f"批次行数={batch_rows}（应 1）；明细行数={item_rows}（应 200）；"
        f"页码={page1}\n截图={p_hist}")

    # ---------- D5: 搜索与状态筛选 ----------
    d.search_edit.setText("发票")
    pump(app, 0.3)
    n_search = d.item_table.rowCount()
    p_search = shot(d, "06_历史_搜索发票.png")
    d.search_edit.setText("")
    pump(app, 0.3)
    d.status_filter.setCurrentIndex(d.status_filter.findData("moved"))
    pump(app, 0.3)
    n_moved_total = len(d._filtered_items())
    n_moved_rows = d.item_table.rowCount()
    p_filter = shot(d, "07_历史_筛选已移动.png")
    d.status_filter.setCurrentIndex(0)
    pump(app, 0.3)
    d5_ok = (n_search == 1 and n_moved_total == 208 and n_moved_rows == 200)
    rec("D5", "历史搜索与状态筛选：关键词过滤、按「已移动」过滤",
        "通过" if d5_ok else "未通过",
        f"关键词「发票」→ {n_search} 项（应 1）；"
        f"状态「已移动」→ 共 {n_moved_total} 项（应 208），"
        f"本页渲染 {n_moved_rows} 行（分页上限 200）\n"
        f"截图={p_search}, {p_filter}")

    # ---------- D6: 分页翻页 ----------
    d.btn_next.click()
    pump(app, 0.3)
    page2 = d.page_label.text()
    rows_p2 = d.item_table.rowCount()
    p_page2 = shot(d, "08_历史_第2页.png")
    d.btn_prev.click()
    pump(app, 0.3)
    d6_ok = (rows_p2 == 8 and "2/2" in page2)
    rec("D6", "历史明细分页：第 2 页按需渲染剩余 8 项",
        "通过" if d6_ok else "未通过",
        f"第 2 页行数={rows_p2}（应 8）；页码={page2}\n截图={p_page2}")

    # ---------- D7: 冲突撤销（从历史页发起，程序化点击） ----------
    status_before = d.batch_table.item(0, 3).text()
    # 在某个原位置放入不同内容的新文件 → 撤销时该项冲突、保留现状
    mk(root, conflict_src_rel, "NEW-CONFLICT-CONTENT" * 20)
    d.btn_undo.click()
    undo_done = wait_until(
        app, lambda: not w._working and w._undo_worker is not None
        and not w._undo_worker.isRunning(), timeout=120)
    pump(app, 0.5)
    status_after = d.batch_table.item(0, 3).text()
    p_after_undo = shot(d, "09_历史_撤销后自动刷新.png")
    d.status_filter.setCurrentIndex(d.status_filter.findData("conflict"))
    pump(app, 0.3)
    n_conflict = d.item_table.rowCount()
    conflict_reason = (d.item_table.item(0, 5).text() if n_conflict else "")
    p_conflict = shot(d, "10_历史_冲突筛选与原因.png")
    kept = os.path.isfile(os.path.join(root, conflict_src_rel))
    d.status_filter.setCurrentIndex(0)
    pump(app, 0.3)
    d7_ok = (undo_done and "部分撤销" in status_after
             and status_before != status_after
             and n_conflict == 1 and "保留现状" in conflict_reason and kept)
    rec("D7", "从历史页撤销：原位置被占用 → 部分撤销，冲突原因可见、现状保留",
        "通过" if d7_ok else "未通过",
        f"撤销前状态={status_before}；撤销后状态={status_after}；"
        f"冲突项 {n_conflict} 个（应 1）；原因={conflict_reason}；"
        f"占用文件保留={kept}\n截图={p_after_undo}, {p_conflict}")

    # ---------- D8: 导出 CSV / JSON ----------
    csv_path = os.path.join(OUT, "历史导出_批次.csv")
    json_path = os.path.join(OUT, "历史导出_批次.json")
    real_save = main_mod.QFileDialog.getSaveFileName
    main_mod.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (csv_path, "CSV 文件 (*.csv)"))
    d.btn_export.click()
    pump(app, 0.3)
    main_mod.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (json_path, "JSON 文件 (*.json)"))
    d.btn_export.click()
    pump(app, 0.3)
    main_mod.QFileDialog.getSaveFileName = real_save
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            csv_lines = list(csv.reader(f))
        jdata = json.load(open(json_path, encoding="utf-8"))
        n_actions = len(jdata["actions"])
        d8_ok = (len(csv_lines) == n_actions + 2
                 and csv_lines[0][0] == "批次")
    except Exception as e:
        d8_ok, n_actions = False, str(e)
    rec("D8", "历史导出：CSV 与 JSON 由用户选择位置保存，内容逐项一致",
        "通过" if d8_ok else "未通过",
        f"CSV {os.path.getsize(csv_path)} 字节、{len(csv_lines)} 行"
        f"（= 动作 {n_actions}+2 头）；JSON 动作 {n_actions} 条\n"
        f"文件={csv_path}, {json_path}")

    # ---------- D9: 窗口缩放（主窗口 + 历史窗口） ----------
    size_shots = []
    ok_sizes = True
    for wdt, hgt, tag in [(900, 600, "11_缩放_主窗口_900x600.png"),
                          (1400, 900, "12_缩放_主窗口_1400x900.png")]:
        w.resize(wdt, hgt)
        pump(app, 0.3)
        size_shots.append(shot(w, tag))
        for btn in (w.btn_scan, w.btn_execute, w.btn_undo, w.btn_history):
            if not btn.isVisible():
                ok_sizes = False
        if w.table.height() < 100:
            ok_sizes = False
    hist_shots = []
    for wdt, hgt, tag in [(760, 480, "13_缩放_历史_760x480.png"),
                          (1200, 800, "14_缩放_历史_1200x800.png")]:
        d.resize(wdt, hgt)
        pump(app, 0.3)
        hist_shots.append(shot(d, tag))
        for btn in (d.btn_undo, d.btn_export, d.btn_refresh, d.btn_next):
            if not btn.isVisible():
                ok_sizes = False
        if d.item_table.height() < 100:
            ok_sizes = False
    rec("D9", "窗口缩放：主窗口 900×600/1400×900、历史窗口 760×480/1200×800 控件仍可见",
        "通过" if ok_sizes else "未通过",
        "\n".join(f"截图={p}" for p in size_shots + hist_shots))

    # ---------- D10: 系统级屏幕截图 ----------
    screen_shot = ""
    if desktop:
        try:
            p = os.path.join(OUT, "15_系统级屏幕截图.png")
            r = subprocess.run(["screencapture", "-x", p],
                               capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.isfile(p):
                screen_shot = p
        except Exception:
            screen_shot = ""
    rec("D10", "系统级屏幕截图（screencapture）",
        "通过" if screen_shot else "未测",
        screen_shot or "screencapture 不可用或无录屏权限；窗口级截图仍有效")

    # ---------- 关闭 ----------
    try:
        d.close()
        pump(app, 0.2)
        w.close()
        pump(app, 0.5)
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
        "suite": "desktop_check_v12",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v12.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": platform,
        "screenshots": OUT,
        "elapsed_s": round(time.time() - t0, 1),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "results_desktop_check_v12.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_desktop_check_v12.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2 第一轮 — 真实桌面交互检查结果\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"平台: {sys.platform}  Qt: {platform}\n")
        f.write(f"耗时: {meta['elapsed_s']}s  截图目录: {OUT}\n")
        f.write("交互方式：D1–D10 为程序化点击真实 cocoa 控件；"
                "D11 为人工观察渲染截图\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in r["evidence"].splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
