"""
归档盒 V1.5 — 长期修正规则 真实桌面检查（cocoa，程序化点击 + 截图）

覆盖：
V0 当前进程能获取真实屏幕（cocoa）
V1 配置编辑器出现「长期修正规则」页，可添加/上移/删除
V2 单文件修正对话框出现「保存为长期规则…」按钮（有上下文时可用）
V3 规则草稿对话框：默认匹配条件、字段、试运行结果
V4 保存规则后写入该根目录配置，重新生成会命中
V5 重新打开配置编辑器能看到刚保存的规则（往返一致）
V6 历史明细把长期规则命中的文件标为「长期规则」并显示规则依据

说明：以下为程序化点击与截图，不等同于用户本人手工观察；
合成样本，非真人分类错例。

运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v15_long_term.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox

from classification_config import (
    default_config, load_config, save_config,
    MATCH_DIR_SCOPE, INNER_KEEP,
)
from config_dialog import ClassificationConfigDialog
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory
from storage import Storage
from executor import execute_plan
from main import MainWindow, HistoryDialog
from plan_patch import (
    ManualOverrideDialog, RuleDraftDialog, OverrideParams,
    apply_override_to_plan,
)

BASE = "/tmp/_v15_desktop_check"
SHOT_DIR = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
            "验收截图_20261109_V15长期规则")
RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence), "kind": "程序化点击"})
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
        f.write(content.encode("utf-8"))


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.makedirs(SHOT_DIR, exist_ok=True)
    t0 = time.time()

    app = QApplication([])
    screen = app.primaryScreen()
    platform = app.platformName()
    rec("V0", "当前进程可获取真实屏幕（cocoa）",
        "通过" if (screen is not None and platform == "cocoa") else "未通过",
        f"platform={platform}；screen={bool(screen)}；"
        f"尺寸={screen.geometry().width() if screen else 0}x"
        f"{screen.geometry().height() if screen else 0}")

    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    root = os.path.join(BASE, "合成资料_V15")
    mk(root, "大二上/数据结构/作业/a.pdf", "V15-A")
    mk(root, "大二上/数据结构/实验/b.pdf", "V15-B")
    mk(root, "大二上/C++程序设计/作业/c.pdf", "V15-C")

    cfg = default_config()

    # ========== V1 配置编辑器：长期规则页 ==========
    dlg = ClassificationConfigDialog(
        root=root, config=cfg, using_defaults=True,
        base_config=cfg, save_callback=None)
    tabs = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    has_tab = "长期修正规则" in tabs
    dlg.tabs.setCurrentIndex(tabs.index("长期修正规则") if has_tab else 0)
    pump(app)
    dlg._add_long_term_row()
    pump(app)
    rows_after_add = dlg.table_longterm.rowCount()
    first_id = dlg.table_longterm.item(0, 10).text()
    shot(dlg, "01_配置编辑器_长期规则页.png")
    ok1 = has_tab and rows_after_add == 1 and bool(first_id)
    rec("V1", "配置编辑器含「长期修正规则」页，可添加规则并生成稳定标识",
        "通过" if ok1 else "未通过",
        f"标签页={tabs}\n行数={rows_after_add}\n规则标识={first_id}")
    dlg.reject()
    pump(app)

    # ========== V2 单文件修正对话框：保存为长期规则入口 ==========
    sr = scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                      compute_duplicates=False)
    rel_a = "大二上/数据结构/作业/a.pdf"
    apply_override_to_plan(plan, rel_a, OverrideParams(
        direction="项目", semester="", category="数据大创"))

    ctx = {"scan_result": sr, "config": cfg,
           "save_callback": lambda c: save_config(root, c)}
    ov = ManualOverrideDialog(plan, rel_a, mode=MODE_BY_DIRECTION_SEMESTER,
                              rule_context=ctx)
    ov.dlg.show()
    pump(app)
    shot(ov.dlg, "02_修正对话框_保存为长期规则入口.png")
    btn_enabled = ov.btn_save_rule.isEnabled()
    btn_text = ov.btn_save_rule.text()
    rec("V2", "单文件修正对话框出现「保存为长期规则…」按钮且可用",
        "通过" if (btn_enabled and "长期规则" in btn_text) else "未通过",
        f"按钮文字={btn_text}；可用={btn_enabled}")
    ov.dlg.reject()
    pump(app)

    # ========== V3 规则草稿对话框：默认条件与试运行 ==========
    draft = RuleDraftDialog(
        mode=MODE_BY_DIRECTION_SEMESTER, plan=plan, source_rel=rel_a,
        params=OverrideParams(direction="项目", semester="", category="数据大创"),
        scan_result=sr, config=cfg,
        save_callback=lambda c: save_config(root, c))
    draft.dlg.show()
    pump(app)
    default_match = draft.match_combo.currentData()
    default_source = draft.source_edit.text()
    # 切换为目录范围并试运行
    draft.match_combo.setCurrentIndex(1)
    draft.source_edit.setText("大二上/数据结构")
    draft.subdir_check.setChecked(True)
    draft._on_preview()
    pump(app)
    impact_text = draft.impact_view.toPlainText()
    shot(draft.dlg, "03_规则草稿_试运行结果.png")
    ok3 = (default_match == "exact_path" and default_source == rel_a
           and "命中文件数：2" in impact_text
           and "目标位置变化数：2" in impact_text)
    rec("V3", "规则草稿默认精确路径；切换到目录范围后试运行命中 2 个文件",
        "通过" if ok3 else "未通过",
        f"默认匹配={default_match}\n默认来源={default_source}\n"
        + "\n".join(impact_text.splitlines()[:6]))

    # ========== V4 保存规则并重新生成 ==========
    saved_before = draft.saved_rule
    draft._on_save()
    pump(app)
    cfg_disk, src = load_config(root)
    plan2 = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg_disk,
                       compute_duplicates=False)
    hit = [i for i in plan2.items if i.rule_key.startswith("long_term:")]
    ok4 = (saved_before is None and draft.saved_rule is not None
           and src == "custom" and len(cfg_disk.long_term_rules) == 1
           and len(hit) == 2
           and all("作业" in i.inner_path or "实验" in i.inner_path
                   for i in hit))
    shot(draft.dlg, "04_保存后对话框.png")
    rec("V4", "保存规则写入该根目录配置；重新生成命中 2 个文件且保留内部层级",
        "通过" if ok4 else "未通过",
        f"配置来源={src}；规则数={len(cfg_disk.long_term_rules)}\n"
        f"命中={[i.file.rel_path for i in hit]}\n"
        f"内部路径={[i.inner_path for i in hit]}\n"
        f"目标={[i.target_rel_path for i in hit]}")
    draft.dlg.accept()
    pump(app)

    # ========== V5 重新打开配置编辑器能看到规则 ==========
    dlg2 = ClassificationConfigDialog(
        root=root, config=cfg_disk, using_defaults=False,
        base_config=cfg_disk, save_callback=None)
    pump(app)
    tabs2 = [dlg2.tabs.tabText(i) for i in range(dlg2.tabs.count())]
    dlg2.tabs.setCurrentIndex(tabs2.index("长期修正规则"))
    pump(app)
    shown_rows = dlg2.table_longterm.rowCount()
    shown_name = (dlg2.table_longterm.item(0, 1).text()
                  if shown_rows else "")
    shown_match = (dlg2.table_longterm.item(0, 2).text()
                   if shown_rows else "")
    shot(dlg2, "05_重开配置编辑器_规则往返.png")
    ok5 = (shown_rows == 1 and shown_name == draft.saved_rule.name
           and shown_match == "目录范围")
    rec("V5", "重新打开配置编辑器可看到刚保存的规则（名称/匹配方式往返一致）",
        "通过" if ok5 else "未通过",
        f"行数={shown_rows}；名称={shown_name}；匹配方式={shown_match}")
    dlg2.reject()
    pump(app)

    # ========== V6 历史界面显示「长期规则」来源与依据 ==========
    sr2 = scan_directory(root, detect_project_roots=False)
    plan3 = build_plan(sr2, MODE_BY_DIRECTION_SEMESTER, [], config=cfg_disk,
                       compute_duplicates=True)
    res = execute_plan(plan3, Storage(root))

    real_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
    finally:
        QMessageBox.question = real_question
    w.show()
    pump(app)
    hist = HistoryDialog(w)
    hist._selected_batch_id = res.batch_id
    hist.load_items(force=True)
    hist.show()
    pump(app)

    cols = [hist.item_table.horizontalHeaderItem(i).text()
            for i in range(hist.item_table.columnCount())]
    ci = cols.index("分类来源")
    vals = [hist.item_table.item(r, ci).text()
            for r in range(hist.item_table.rowCount())]
    lt_rows = [r for r in range(hist.item_table.rowCount())
               if (hist.item_table.item(r, ci).text() == "长期规则")]
    tip = (hist.item_table.item(lt_rows[0], ci).toolTip() or "") if lt_rows else ""
    shot(hist, "06_历史界面_长期规则来源.png")
    # 2 个命中长期规则（作业/实验），1 个仍按原有映射自动分类（C++ 作业）
    ok6 = (len(lt_rows) == 2 and vals.count("自动") == 1
           and "命中依据" in tip and "long_term:" in tip)
    rec("V6", "历史明细把长期规则命中的文件标为「长期规则」并显示规则依据",
        "通过" if ok6 else "未通过",
        f"分类来源列={vals}\n长期规则行={lt_rows}\n悬浮提示={tip}")
    hist.reject()
    w.close()
    pump(app)

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 and RESULTS else 1
    meta = {
        "suite": "desktop_check_v15_long_term",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v15_long_term.py (cocoa)",
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
    out_json = os.path.join(here, "results_desktop_check_v15_long_term.json")
    out_txt = os.path.join(here, "results_desktop_check_v15_long_term.txt")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.5 长期修正规则 — 真实桌面检查（cocoa，程序化点击）\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"屏幕: {meta['screen']}；Qt platform: {platform}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n退出码: {exit_code}\n")
        f.write(f"截图目录: {SHOT_DIR}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / "
                f"未测 {untested}\n")
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / "
          f"未测 {untested}；退出码 {exit_code}")
    print(f"结果已保存: {out_json}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
