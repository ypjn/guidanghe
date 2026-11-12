"""
归档盒 V1.3 — 真实桌面检查（cocoa，程序化点击 + 截图）

覆盖：编辑配置 → 试运行影响预览 → 保存（旧计划失效）→ 重新生成 →
整理 → 历史中的配置快照 → 关闭重开（重启恢复）。

注意：
- 本脚本是程序化点击与截图，不等同于用户本人手工观察；
- 所有文件操作只在 /tmp 下的独立合成目录执行，真实 NEU 资料不参与；
- 合成样本明确标注，不作为真人分类错例。

运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v13.py
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

import main as main_mod
import config_dialog
from main import MainWindow
from classification_config import config_path
from config_dialog import ClassificationConfigDialog, _check_item, _text_item

BASE = "/tmp/_v13_desktop_check"
SHOT_DIR = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
            "验收截图_20261108_V13")
RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence, "kind": "程序化点击"})
    print(f"[{'PASS' if status == '通过' else 'FAIL'}] {tid} {title}")
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
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    class _AutoAccept(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAccept

    root = os.path.join(BASE, "合成资料_含空格")
    os.makedirs(root)
    mk(root, "自定义学期/机器人基础/实验一/report.pdf", "合成报告内容")
    mk(root, "自定义学期/机器人基础/实验一/数据.csv", "1,2,3")
    mk(root, "大二上/数据结构/作业/作业1.pdf", "数据结构作业")
    mk(root, "散落说明.txt", "散落文件")

    w = MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w.show()
    pump(app)
    shot(w, "01_默认推荐模式主窗口.png")

    w.on_scan()
    wait_until(app, lambda: not w._working and w.scan_result is not None)
    w._generate_and_show_plan()
    wait_until(app, lambda: not w._plan_working and w.current_plan is not None
               and w._plan_valid)
    pump(app)
    old_targets = {i.file.rel_path: i.target_rel_path for i in w.current_plan.items}
    shot(w, "02_默认规则计划预览.png")

    # ---- 编辑器（草稿） ----
    dlg = ClassificationConfigDialog(
        root=root, config=copy.deepcopy(w.classification_config),
        using_defaults=True, scan_result=w.scan_result,
        base_config=copy.deepcopy(w.classification_config),
        save_callback=lambda cfg: w._save_classification_config(root, cfg),
        worker_registry=w, parent=w)
    dlg.show()
    pump(app)
    shot(dlg, "03_推荐模式配置编辑器.png")

    # ---- 取消编辑：不改变生效配置与文件 ----
    file_before = open(config_path(root), encoding="utf-8").read() \
        if os.path.exists(config_path(root)) else ""
    dlg.table_semester.item(0, 1).setText("取消用的临时修改")
    dlg.reject()
    pump(app)
    cancel_no_file = not os.path.exists(config_path(root))
    cancel_ok = cancel_no_file and file_before == ""
    shot(w, "04_取消编辑后主窗口.png")

    # ---- 重新打开并添加学期 + 课程映射 ----
    dlg = ClassificationConfigDialog(
        root=root, config=copy.deepcopy(w.classification_config),
        using_defaults=True, scan_result=w.scan_result,
        base_config=copy.deepcopy(w.classification_config),
        save_callback=lambda cfg: w._save_classification_config(root, cfg),
        worker_registry=w, parent=w)
    dlg.show()
    pump(app)
    r = dlg.table_semester.rowCount()
    dlg.table_semester.insertRow(r)
    dlg.table_semester.setItem(r, 0, _check_item(True))
    dlg.table_semester.setItem(r, 1, _text_item("自定义学期"))
    dlg.table_semester.setItem(r, 2, _text_item("2026秋-自定义学期"))
    r2 = dlg.table_semdir.rowCount()
    dlg.table_semdir.insertRow(r2)
    dlg.table_semdir.setItem(r2, 0, _check_item(True))
    dlg.table_semdir.setItem(r2, 1, _text_item("2026秋-自定义学期"))
    dlg.table_semdir.setItem(r2, 2, _text_item("机器人基础"))
    dlg.table_semdir.setItem(r2, 3, _text_item("课程"))
    dlg.table_semdir.setItem(r2, 4, _text_item("机器人基础"))
    pump(app)
    shot(dlg, "05_新增学期与课程映射.png")

    # ---- 试运行 / 影响预览（后台线程） ----
    captured = {}
    real_ipd = config_dialog.ImpactPreviewDialog

    class _ShotIPD(real_ipd):
        def __init__(self, result, parent=None):
            captured["result"] = result
            super().__init__(result, parent)

        def exec(self):
            self.show()
            pump(app, 0.4)
            shot(self, "06_规则影响预览.png")
            self.close()
            return 0

    config_dialog.ImpactPreviewDialog = _ShotIPD
    dlg._on_preview()
    preview_ok = wait_until(app, lambda: "result" in captured
                            and not dlg._previewing, timeout=30)
    config_dialog.ImpactPreviewDialog = real_ipd
    shot(dlg, "07_试运行完成后的编辑器.png")

    # ---- 保存：旧计划必须失效 ----
    dlg._on_save()
    pump(app)
    saved = os.path.isfile(config_path(root))
    invalidated = (not w._plan_valid and w.current_plan is None
                   and not w.btn_execute.isEnabled()
                   and w.invalid_label.isVisible())
    shot(w, "08_保存后旧计划失效.png")

    # ---- 重新生成 ----
    w._generate_and_show_plan()
    wait_until(app, lambda: not w._plan_working and w.current_plan is not None
               and w._plan_valid)
    pump(app)
    new_targets = {i.file.rel_path: i.target_rel_path for i in w.current_plan.items}
    expected = ("_已整理/课程/2026秋-自定义学期/机器人基础/文档/实验一/report.pdf")
    changed = (old_targets.get("自定义学期/机器人基础/实验一/report.pdf")
               != new_targets.get("自定义学期/机器人基础/实验一/report.pdf"))
    shot(w, "09_按新配置重新生成的计划.png")

    # ---- 整理（仅合成临时目录） ----
    w.on_execute()
    wait_until(app, lambda: not w._working and w._exec_worker is not None
               and not w._exec_worker.isRunning())
    pump(app)
    shot(w, "10_整理完成.png")
    batch = w._storage.get_latest_batch()
    snapshot_ok = (batch is not None and bool(batch.config_snapshot)
                   and any(r.get("source") == "自定义学期"
                           for r in batch.config_snapshot.get("semester_map", [])))

    # ---- 历史中的配置快照 ----
    w.on_history()
    pump(app, 0.5)
    hist = w._history_dialog
    history_ok = hist is not None and hist.isVisible()
    if hist is not None:
        shot(hist, "11_历史批次与配置快照.png")
        hist.close()
        w._history_dialog = None
    pump(app)

    # ---- 关闭重开（重启恢复） ----
    w.close()
    pump(app)
    w2 = MainWindow()
    w2.path_edit.setText(root)
    w2._reset_for_new_dir(root)
    w2.show()
    pump(app)
    restored = any(r.source == "自定义学期"
                   for r in w2.classification_config.semester_rules)
    shot(w2, "12_重启后恢复自定义配置.png")
    w2.close()
    pump(app)

    rec("D1", "程序中打开配置编辑器、取消编辑不改变生效配置与文件",
        "通过" if cancel_ok else "未通过",
        f"取消后未创建配置文件={cancel_no_file}；原文件内容未变={file_before == ''}")
    rec("D2", "界面新增学期与课程映射，试运行显示影响（后台线程）",
        "通过" if (preview_ok and captured.get("result")) else "未通过",
        f"试运行完成={preview_ok}；样本={captured.get('result').sample_total if captured.get('result') else '无'}；"
        f"变化={captured.get('result').changed_count if captured.get('result') else '无'}")
    rec("D3", "保存成功后旧计划失效、执行禁用，重新生成使用新配置",
        "通过" if (saved and invalidated and changed
                   and new_targets.get("自定义学期/机器人基础/实验一/report.pdf")
                   == expected) else "未通过",
        f"配置文件={saved}；旧计划失效={invalidated}；结果变化={changed}\n"
        f"新目标={new_targets.get('自定义学期/机器人基础/实验一/report.pdf')}")
    rec("D4", "整理批次保存实际使用的配置快照，历史可查看",
        "通过" if (snapshot_ok and history_ok) else "未通过",
        f"批次配置快照={snapshot_ok}；历史窗口={history_ok}；批次=#{batch.batch_id if batch else '无'}")
    rec("D5", "关闭重开后按根目录恢复自定义配置",
        "通过" if restored else "未通过",
        f"重启后含「自定义学期」={restored}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 and RESULTS else 1
    meta = {
        "suite": "desktop_check_v13",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v13.py (cocoa)",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", "cocoa(default)"),
        "base": BASE,
        "screenshot_dir": SHOT_DIR,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed, "fail": failed},
        "note": "程序化点击与截图；人工观察项需用户本人查看截图后确认。"
                "合成样本，非真人分类错例。",
    }
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "results_desktop_check_v13.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_desktop_check_v13.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.3 — 真实桌面检查（cocoa，程序化点击）\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n退出码: {exit_code}\n")
        f.write(f"截图目录: {SHOT_DIR}\n\n")
        f.write("说明：以下为程序化点击结果；人工观察项需用户本人看截图确认。\n"
                "合成样本，不是真人分类错例。\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    print(f"结果已保存: {out}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
