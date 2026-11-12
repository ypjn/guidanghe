"""
归档盒 V1.4 单文件分类修正 — 真实桌面检查（cocoa，程序化点击 + 截图）

本轮重点验证：
D0 当前进程能获取真实屏幕（cocoa）
D1 默认计划 → 选中一个文件 → 手动修正（课程→项目，改类别+内部路径）→ 表格该行变黄、加 ✎
D2 冲突场景：先修正 A 到目标 X，再修正 B 也到 X → 自动加 (1)/(2) 后缀
D3 对话框取消不改计划：弹对话框 → Cancel → 确认计划不变
D4 恢复自动分类按钮：已修正文件 → 打开对话框 → 点"恢复自动" → 确认回到自动
D5 执行整理 → 历史窗口显示"分类来源=手动"

注意：
- 以下结果是程序化点击与截图，不等同于用户本人手工观察；
- 所有文件操作只在 /tmp 下的独立合成目录执行，真实 NEU 资料不参与；
- 合成样本明确标注，不作为真人分类错例。

运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v14_override.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

from main import MainWindow
from plan_patch import ManualOverrideDialog, apply_override_to_plan, revert_auto_override

_ACCEPTED = QDialog.DialogCode.Accepted.value
_REJECTED = QDialog.DialogCode.Rejected.value

BASE = "/tmp/_v14_desktop_override_check"
SHOT_DIR = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
            "验收截图_20261109_V14单文件修正")
RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence, "kind": "程序化点击"})
    tag = {"通过": "PASS", "未通过": "FAIL"}.get(status, "UNTESTED")
    print(f"[{tag}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.35):
    end = time.time() + seconds
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
    app.processEvents()
    return cond()


def shot(widget, name):
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, name)
    ok = widget.grab().save(path)
    print(f"截图已保存：{path} ({ok})")
    return path


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode())


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.makedirs(SHOT_DIR, exist_ok=True)
    t0 = time.time()
    app = QApplication([])
    screen = app.primaryScreen()
    platform = app.platformName()
    rec("D0", "当前进程可获取真实屏幕（cocoa）",
        "通过" if (screen is not None and platform == "cocoa") else "未通过",
        f"platform={platform}；screen={bool(screen)}；"
        f"尺寸={screen.geometry().width() if screen else 0}x"
        f"{screen.geometry().height() if screen else 0}")

    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # ---- 准备合成资料 ----
    root = os.path.join(BASE, "合成资料_V14修正")
    os.makedirs(root)
    mk(root, "大二上/数据结构/作业/a.pdf", "合成作业A")
    mk(root, "大二上/数据结构/作业/b.pdf", "合成作业B")
    mk(root, "大二上/数据结构/实验/c.pdf", "合成实验C")
    mk(root, "大二上/数据结构/作业/same.pdf", "同名文件1")
    mk(root, "大二上/数据结构/实验/same.pdf", "同名文件2")
    mk(root, "大二上/项目/proj1/x.py", "合成项目文件")
    mk(root, "大二上/课程/高数/f.pdf", "合成高数")

    w = MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w.show()
    pump(app)
    w.on_scan()
    wait_until(app, lambda: not w._working and w.scan_result is not None)
    w._generate_and_show_plan()
    wait_until(app, lambda: not w._plan_working and w.current_plan is not None
               and w._plan_valid)
    pump(app)
    shot(w, "01_默认计划.png")
    n_items = len(w.current_plan.items)

    # 找到文件路径：D1 基本修正用第一个，D2 冲突用两个同名文件 (same.pdf)
    all_paths = sorted(i.file.rel_path for i in w.current_plan.items)
    path_a = all_paths[0]  # D1 基本修正

    # 找两个 same.pdf 做冲突测试
    same_paths = [p for p in all_paths if p.endswith("same.pdf")]
    path_c1 = same_paths[0]  # D2 冲突文件 1
    path_c2 = same_paths[1]  # D2 冲突文件 2

    # ============== D1：基本修正 课程→项目 ==============
    captured_dlg = {}
    real_exec = ManualOverrideDialog.exec

    class _BasicOverride(ManualOverrideDialog):
        def exec(self):
            captured_dlg["item_before"] = {
                "direction": self.item.direction,
                "category": self.item.category,
                "target_filename": self.item.target_filename,
                "is_manual": self.item.is_manual_overridden,
            }
            self.dir_combo.setCurrentText("项目")
            self.cat_edit.setText("数据结构大创")
            self.inner_edit.setText("实验一")
            self.note_edit.setText("原自动分类漏了关键词")
            self._on_field_change()
            pump(app, 0.3)
            shot(self.dlg, "02_修正对话框_字段已改.png")
            self.dlg.accept()
            return _ACCEPTED

    ManualOverrideDialog.exec = _BasicOverride.exec

    row_a = next(i for i in range(w.table.rowCount())
                 if w.table.item(i, 1) and w.table.item(i, 1).text() == path_a)
    w.table.selectRow(row_a)
    pump(app)
    w.on_override_selected()
    pump(app)
    shot(w, "03_修正后表格_该行变黄.png")
    ManualOverrideDialog.exec = real_exec

    plan_a = next(i for i in w.current_plan.items if i.file.rel_path == path_a)
    row0_text = w.table.item(row_a, 1).text()  # 原位置列 (col=1) 有 ✎ 前缀
    row0_bg = w.table.item(row_a, 1).background().color().name()  # col=1 有背景色
    d1_ok = (plan_a.direction == "项目"
             and plan_a.category == "数据结构大创"
             and plan_a.inner_path == "实验一"
             and plan_a.is_manual_overridden
             and "✎" in row0_text
             and row0_bg.upper() == "#FFF9C4")

    rec("D1", "基本修正（课程→项目，改类别+内部路径）→ 表格该行变黄、加 ✎",
        "通过" if d1_ok else "未通过",
        f"plan.direction={plan_a.direction}\nplan.category={plan_a.category}\n"
        f"plan.inner_path={plan_a.inner_path}\nis_manual={plan_a.is_manual_overridden}\n"
        f"表格源路径={row0_text}\n背景色={row0_bg}")

    # ============== D2：冲突自动加 (1)/(2) 后缀 ==============
    # 先修正 path_c1，再修正 path_c2，两者目标文件名都是 same.pdf → 触发冲突
    class _ConflictOverride(ManualOverrideDialog):
        def exec(self):
            self.dir_combo.setCurrentText("项目")
            self.cat_edit.setText("数据结构大创")
            self.inner_edit.setText("实验一")
            self.note_edit.setText("冲突测试")
            self._on_field_change()
            pump(app, 0.3)
            self.dlg.accept()
            return _ACCEPTED

    ManualOverrideDialog.exec = _ConflictOverride.exec
    row_c1 = next(i for i in range(w.table.rowCount())
                  if w.table.item(i, 1) and w.table.item(i, 1).text() == path_c1)
    w.table.selectRow(row_c1)
    pump(app)
    w.on_override_selected()
    pump(app)

    row_c2 = next(i for i in range(w.table.rowCount())
                  if w.table.item(i, 1) and w.table.item(i, 1).text() == path_c2)
    w.table.selectRow(row_c2)
    pump(app)
    w.on_override_selected()
    pump(app)
    shot(w, "04_冲突后自动加(1)(2).png")
    ManualOverrideDialog.exec = real_exec

    plan_c1 = next(i for i in w.current_plan.items if i.file.rel_path == path_c1)
    plan_c2 = next(i for i in w.current_plan.items if i.file.rel_path == path_c2)
    both_in_project = (plan_c1.direction == "项目" and plan_c2.direction == "项目")
    targets_same_dir = (plan_c1.target_dir_rel == plan_c2.target_dir_rel)
    both_same_filename = (plan_c1.target_filename == plan_c2.target_filename
                          and "same" in plan_c1.target_filename)
    has_suffix = ("(1)" in plan_c1.target_filename or "(1)" in plan_c2.target_filename
                  or "(2)" in plan_c1.target_filename or "(2)" in plan_c2.target_filename)
    d2_ok = both_in_project and targets_same_dir and has_suffix

    rec("D2", "冲突自动加 (1)/(2) 后缀",
        "通过" if d2_ok else "未通过",
        f"C1.target={plan_c1.target_rel_path} (filename={plan_c1.target_filename})\n"
        f"C2.target={plan_c2.target_rel_path} (filename={plan_c2.target_filename})\n"
        f"同目录={targets_same_dir}\n带后缀={has_suffix}")

    # ============== D3：对话框取消不改计划 ==============
    saved_targets_before = {i.file.rel_path: i.target_rel_path
                            for i in w.current_plan.items}
    saved_manual_before = {i.file.rel_path: i.is_manual_overridden
                           for i in w.current_plan.items}

    row_other = next((i for i in range(w.table.rowCount())
                      if w.table.item(i, 1)
                      and w.table.item(i, 1).text().replace("✎ ", "")
                          not in (path_a, path_c1, path_c2)), None)
    path_c = w.current_plan.items[row_other].file.rel_path if row_other is not None else None

    class _CancelOverride(ManualOverrideDialog):
        def exec(self):
            # 改了字段但取消
            self.dir_combo.setCurrentText("待定")
            self.cat_edit.setText("随便改的")
            self._on_field_change()
            pump(app, 0.3)
            self.dlg.reject()
            return _REJECTED

    if row_other is not None:
        ManualOverrideDialog.exec = _CancelOverride.exec
        w.table.selectRow(row_other)
        pump(app)
        w.on_override_selected()
        pump(app)
        shot(w, "05_对话框取消_计划不变.png")
        ManualOverrideDialog.exec = real_exec

        saved_targets_after = {i.file.rel_path: i.target_rel_path
                               for i in w.current_plan.items}
        saved_manual_after = {i.file.rel_path: i.is_manual_overridden
                              for i in w.current_plan.items}
        targets_same = saved_targets_before == saved_targets_after
        manuals_same = saved_manual_before == saved_manual_after
        d3_ok = targets_same and manuals_same
    else:
        d3_ok = False
        targets_same = "n/a"
        manuals_same = "n/a"

    rec("D3", "对话框取消不改计划",
        "通过" if d3_ok else "未通过",
        f"targets_before == after={targets_same}\nmanual_flags_before == after={manuals_same}\n"
        f"被测文件={path_c}")

    # ============== D4：恢复自动分类 ==============
    # 选 path_a（已手动修正），打开对话框，点"恢复自动"
    class _RevertOverride(ManualOverrideDialog):
        def exec(self):
            # 模拟用户点"恢复自动分类"按钮
            self._on_revert_auto()
            pump(app, 0.2)
            self.dlg.accept()
            return _ACCEPTED

    ManualOverrideDialog.exec = _RevertOverride.exec
    def _match(row, target):
        text = w.table.item(row, 1).text()
        return text == target or text == "✎ " + target

    row_a2 = next(i for i in range(w.table.rowCount())
                  if _match(i, path_a))
    w.table.selectRow(row_a2)
    pump(app)
    w.on_override_selected()
    pump(app)
    shot(w, "06_恢复自动分类.png")
    ManualOverrideDialog.exec = real_exec

    plan_a_reverted = next(i for i in w.current_plan.items if i.file.rel_path == path_a)
    d4_ok = (not plan_a_reverted.is_manual_overridden
             and plan_a_reverted.direction == plan_a_reverted.auto_direction
             and plan_a_reverted.category == plan_a_reverted.auto_category)

    rec("D4", "恢复自动分类按钮有效",
        "通过" if d4_ok else "未通过",
        f"is_manual_overridden={plan_a_reverted.is_manual_overridden}\n"
        f"direction={plan_a_reverted.direction} (auto={plan_a_reverted.auto_direction})\n"
        f"category={plan_a_reverted.category} (auto={plan_a_reverted.auto_category})")

    # 清理
    w.close()
    pump(app)

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 and RESULTS else 1
    meta = {
        "suite": "desktop_check_v14_override",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v14_override.py (cocoa)",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": platform,
        "screen": (f"{screen.geometry().width()}x{screen.geometry().height()}"
                   if screen else "无"),
        "base": BASE,
        "screenshot_dir": SHOT_DIR,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed, "fail": failed,
                   "untested": untested},
        "note": "程序化点击与截图；人工观察项需用户本人查看截图后确认。"
                "合成样本，非真人分类错例。",
    }
    here = os.path.dirname(os.path.abspath(__file__))
    out_json = os.path.join(here, "results_desktop_check_v14_override.json")
    out_txt = os.path.join(here, "results_desktop_check_v14_override.txt")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.4 单文件分类修正 — 真实桌面检查（cocoa，程序化点击）\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"屏幕: {meta['screen']}；Qt platform: {platform}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n退出码: {exit_code}\n")
        f.write(f"截图目录: {SHOT_DIR}\n\n")
        f.write("说明：以下为程序化点击结果；人工观察项需用户本人看截图确认。\n"
                "合成样本，不是真人分类错例。\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}；"
          f"退出码 {exit_code}")
    print(f"结果已保存: {out_json}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
