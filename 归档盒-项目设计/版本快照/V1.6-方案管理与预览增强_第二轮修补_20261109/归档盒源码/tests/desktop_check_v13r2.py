"""
归档盒 V1.3 复核修补 — 真实桌面检查（cocoa，程序化点击 + 截图）

本轮重点验证：
R0 当前进程能获取真实屏幕
R1 编辑器中「类别留空目录 + 关键词规则方向=项目」，试运行显示实际方向与目标
R2 保存后重新生成计划，主窗口表格中的目标位置与分类依据使用新方向
R3 非法 JSON 类型配置在真实窗口中被明确报告，推荐模式不生成计划

注意：
- 以下结果是程序化点击与截图，不等同于用户本人手工观察；
- 取消编辑的“成功信号已排队、线程未退出”时序由 offscreen 门控测试
  C17/C18 覆盖，本脚本不声称在 cocoa 下复现该时序；
- 所有文件操作只在 /tmp 下的独立合成目录执行，真实 NEU 资料不参与；
- 合成样本明确标注，不作为真人分类错例。

运行（不要设置 offscreen）：
  .venv/bin/python tests/desktop_check_v13r2.py
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

from PySide6.QtWidgets import QApplication, QMessageBox

import config_dialog
from main import MainWindow
from classification_config import config_path, config_to_dict, default_config
from config_dialog import ClassificationConfigDialog, _check_item, _text_item

BASE = "/tmp/_v13r2_desktop_check"
SHOT_DIR = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
            "验收截图_20261109_V13复核")
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


def write_config(root, payload):
    os.makedirs(os.path.dirname(config_path(root)), exist_ok=True)
    with open(config_path(root), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.makedirs(SHOT_DIR, exist_ok=True)
    t0 = time.time()
    app = QApplication([])
    screen = app.primaryScreen()
    platform = app.platformName()
    rec("R0", "当前进程可获取真实屏幕",
        "通过" if (screen is not None and platform == "cocoa") else "未通过",
        f"platform={platform}；screen={bool(screen)}；"
        f"尺寸={screen.geometry().width() if screen else 0}x"
        f"{screen.geometry().height() if screen else 0}")

    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # ---- R1/R2：关键词规则方向真正生效 ----
    root = os.path.join(BASE, "合成资料_关键词方向")
    os.makedirs(root)
    rel = "大一下/已完成期末/robot.pdf"
    mk(root, rel, "合成机器人报告")
    mk(root, "大一下/已完成期末/高数/f.pdf", "合成高数作业")

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
    shot(w, "01_修改前_默认计划.png")

    dlg = ClassificationConfigDialog(
        root=root, config=copy.deepcopy(w.classification_config),
        using_defaults=True, scan_result=w.scan_result,
        base_config=copy.deepcopy(w.classification_config),
        save_callback=lambda cfg: w._save_classification_config(root, cfg),
        worker_registry=w, parent=w)
    dlg.show()
    pump(app)
    r = dlg.table_keyword.rowCount()
    dlg.table_keyword.insertRow(r)
    dlg.table_keyword.setItem(r, 0, _check_item(True))
    dlg.table_keyword.setItem(r, 1, _text_item("robot"))
    dlg.table_keyword.setItem(r, 2, _text_item("机器人实验"))
    dlg.table_keyword.setItem(r, 3, _text_item("项目"))
    dlg.table_keyword.setItem(r, 4, _text_item("文件名"))
    dlg.table_keyword.setItem(r, 5, _text_item("类别留空的目录"))
    pump(app)
    shot(dlg, "02_新增关键词规则_方向项目.png")

    captured = {}
    real_ipd = config_dialog.ImpactPreviewDialog

    class _ShotIPD(real_ipd):
        def __init__(self, result, parent=None):
            captured["result"] = result
            super().__init__(result, parent)

        def exec(self):
            self.show()
            pump(app, 0.4)
            shot(self, "03_试运行_关键词方向影响.png")
            self.close()
            return 0

    config_dialog.ImpactPreviewDialog = _ShotIPD
    dlg._on_preview()
    preview_ok = wait_until(app, lambda: "result" in captured
                            and not dlg._previewing, timeout=30)
    config_dialog.ImpactPreviewDialog = real_ipd
    changed = None
    if captured.get("result"):
        changed = next((c for c in captured["result"].changed
                        if c.rel_path == rel), None)
    impact_ok = (preview_ok and changed is not None
                 and changed.new_target == "_已整理/项目/机器人实验/robot.pdf"
                 and "项目/机器人实验" in changed.new_reason)
    shot(dlg, "04_试运行完成后的编辑器.png")

    dlg._on_save()
    pump(app)
    saved = os.path.isfile(config_path(root))
    invalidated = (not w._plan_valid and w.current_plan is None
                   and not w.btn_execute.isEnabled() and w.invalid_label.isVisible())
    shot(w, "05_保存后旧计划失效.png")

    w._generate_and_show_plan()
    wait_until(app, lambda: not w._plan_working and w.current_plan is not None
               and w._plan_valid)
    pump(app)
    row = next((i for i in range(w.table.rowCount())
                if w.table.item(i, 1) is not None
                and w.table.item(i, 1).text() == rel), None)
    table_target = w.table.item(row, 2).text() if row is not None else ""
    table_reason = w.table.item(row, 3).text() if row is not None else ""
    plan_item = next((i for i in w.current_plan.items if i.file.rel_path == rel),
                     None)
    ui_ok = (row is not None and plan_item is not None
             and plan_item.direction == "项目"
             and table_target == "_已整理/项目/机器人实验/robot.pdf"
             and "项目/机器人实验" in table_reason)
    shot(w, "06_按新方向重新生成计划.png")
    w.close()
    pump(app)

    rec("R1", "界面新增「类别留空目录 + 关键词方向=项目」，试运行显示实际方向与目标",
        "通过" if impact_ok else "未通过",
        f"试运行完成={preview_ok}；变化项={changed.rel_path if changed else '无'}\n"
        f"新目标={changed.new_target if changed else '无'}\n"
        f"新依据={changed.new_reason if changed else '无'}")
    rec("R2", "保存后重新生成，主窗口表格的目标位置与分类依据使用新方向",
        "通过" if (saved and invalidated and ui_ok) else "未通过",
        f"配置已保存={saved}；旧计划失效/执行禁用={invalidated}\n"
        f"表格目标={table_target}\n表格依据={table_reason}\n"
        f"计划方向={plan_item.direction if plan_item else '无'}")

    # ---- R3：非法 JSON 类型在真实窗口中被报告并阻止计划 ----
    root_bad = os.path.join(BASE, "合成资料_非法配置类型")
    os.makedirs(root_bad)
    mk(root_bad, "大二上/数据结构/作业/a.pdf", "合成作业")
    payload = {"version": 1, "root": root_bad}
    payload.update(config_to_dict(default_config()))
    payload["semester_map"][0]["enabled"] = "false"
    write_config(root_bad, payload)

    w2 = MainWindow()
    w2.path_edit.setText(root_bad)
    w2._reset_for_new_dir(root_bad)
    w2.show()
    pump(app)
    shot(w2, "07_非法配置_明确报错.png")
    error_reported = (w2._config_error != "" and w2._config_source == "error"
                      and "不可用" in w2.config_label.text())
    w2.on_scan()
    wait_until(app, lambda: not w2._working and w2.scan_result is not None)
    w2._generate_and_show_plan()
    pump(app)
    blocked = (w2.current_plan is None and w2._plan_worker is None
               and w2.invalid_label.isVisible() and not w2.btn_execute.isEnabled())
    shot(w2, "08_非法配置_推荐模式不生成计划.png")
    w2.close()
    pump(app)

    rec("R3", "原始 JSON 类型错误在真实窗口中被明确报告，推荐模式不生成可执行计划",
        "通过" if (error_reported and blocked) else "未通过",
        f"记录错误/标签提示={error_reported}；无计划且执行禁用={blocked}")
    rec("R4", "取消编辑的关闭等待时序（桌面范围说明）", "未测",
        "本轮该时序由 offscreen 门控测试 C17/C18 精确复现；"
        "cocoa 桌面检查仅覆盖正常编辑/试运行/保存/非法配置，"
        "未声称在真实桌面复现该时序。")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 and RESULTS else 1
    meta = {
        "suite": "desktop_check_v13r2",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v13r2.py (cocoa)",
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
    out = os.path.join(here, "results_desktop_check_v13r2.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_desktop_check_v13r2.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.3 复核修补 — 真实桌面检查（cocoa，程序化点击）\n")
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
    print(f"结果已保存: {out}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
