#!/usr/bin/env python3
"""用 macOS cocoa 主窗口实跑阶段三演示，并保存 02–05 号窗口画面。

分两个进程跑，模拟「关闭程序再打开」：

  python 课程交付/拍摄演示过程.py --phase 1 --root /tmp/演示样本
  python 课程交付/拍摄演示过程.py --phase 2 --root /tmp/演示样本

第 1 阶段准备空演示目录、做首次预览，并分别验证「取消」「直接关闭」
不写偏好后确认保存一条空间偏好。第 2 阶段重新打开同一目录，放入
新文件核对偏好命中，只点「仅改本次」，取消一次确认、再确认执行，
打开操作历史窗口，最后撤销并重新扫描。

确认框、修正对话框用与 offscreen 套件相同的程序入口驱动；抓取的是
真实 cocoa 窗口像素，不是 offscreen 截图。不设置 QT_QPA_PLATFORM。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "归档盒源码"
SHOTS = HERE / "证据" / "截图"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SRC))

os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage3_run_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage3_run_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage3_run_recent_")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QDialog, QMessageBox,
)

import plan_patch as pp  # noqa: E402
import preferences as P  # noqa: E402
import 重置演示样本  # noqa: E402
from classification_config import MATCH_DIR_SCOPE  # noqa: E402
from main import MainWindow  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
_ANSWER = {"question": QMessageBox.No}
QMessageBox.question = staticmethod(lambda *a, **k: _ANSWER["question"])
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)


def _sync_preview_worker():
    raise RuntimeError("stage3 demo: synchronous rule preview")


pp.rule_preview_worker_class = _sync_preview_worker

SUBJECT = "下载/归档盒/开发说明.md"
NOTE = "下载/笔记.txt"
PROJECT = "项目/AIGC/项目说明.md"
DRAFT = "下载/归档盒/接口草稿.md"
TEMP = "下载/临时稿.md"


def log(text=""):
    print(text, flush=True)


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def process(seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        APP.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


def open_window(root):
    win = MainWindow()
    win.resize(1420, 860)
    win.path_edit.setText(root)
    win._reset_for_new_dir(root)
    win.show()
    win.raise_()
    process(0.4)
    wait_until(lambda: not getattr(win, "_working", True))
    return win


def scan_and_preview(win):
    win.on_scan()
    assert wait_until(lambda: (not win._working) and win.scan_result is not None)
    win._generate_and_show_plan()
    assert wait_until(
        lambda: (not win._plan_working) and win.current_plan is not None
        and win._plan_valid)
    process(0.2)
    if win.current_plan is not None:
        win.table.setColumnWidth(2, 400)
        win.table.setColumnWidth(3, 330)
        process()


def item_of(plan, rel):
    return next(i for i in plan.items if i.file.rel_path == rel)


def select_row(win, rel):
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            win.table.selectRow(idx)
            process()
            return
    raise AssertionError(rel)


def uncheck(win, rel):
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            box = win.table.cellWidget(idx, 0)
            box.findChild(QCheckBox).setChecked(False)
            process()
            return
    raise AssertionError(rel)


def shot(win, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / name
    win.grab().save(str(path))
    log(f"截图：{path}")


def pref_rows(root):
    return len(P.list_space_preferences(root))


def fill_rule_dialog(dlg):
    dlg.name_edit.setText("开发文档按项目归档")
    for i in range(dlg.match_combo.count()):
        if dlg.match_combo.itemData(i) == MATCH_DIR_SCOPE:
            dlg.match_combo.setCurrentIndex(i)
            break
    dlg.source_edit.setText("下载/归档盒")
    dlg.subdir_check.setChecked(True)
    dlg.dir_combo.setCurrentText("项目")
    dlg.cat_edit.setText("归档盒")
    for i in range(dlg.inner_combo.count()):
        if "清空" in dlg.inner_combo.itemText(i):
            dlg.inner_combo.setCurrentIndex(i)
            break
    dlg.scope_combo.setCurrentIndex(0)
    dlg._on_preview()


def install_override(mode, on_rule=None):
    """打开修正对话框并处理「保存为长期规则」。

    mode: cancel / close / save。返回时恢复原对话框方法。
    """
    real_mo = pp.ManualOverrideDialog.exec
    real_rd = pp.RuleDraftDialog.exec

    def mo_exec(self):
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText("归档盒")
        self._on_save_as_rule()
        return self.dlg.result()

    def rd_exec(self):
        fill_rule_dialog(self)
        if on_rule is not None:
            on_rule(self)
        if mode == "cancel":
            self.dlg.reject()
        elif mode == "close":
            self.dlg.close()
        else:
            with yes_answer():
                self._on_save()
        result = self.dlg.result() == 1
        self._on_closed()
        return result

    pp.ManualOverrideDialog.exec = mo_exec
    pp.RuleDraftDialog.exec = rd_exec
    return real_mo, real_rd


def restore_override(real_mo, real_rd):
    pp.ManualOverrideDialog.exec = real_mo
    pp.RuleDraftDialog.exec = real_rd


class yes_answer:
    def __enter__(self):
        _ANSWER["question"] = QMessageBox.Yes

    def __exit__(self, *exc):
        _ANSWER["question"] = QMessageBox.No


def install_confirm(accepted):
    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted if accepted else QDialog.Rejected

    return Dialog


def install_one_off():
    real_mo = pp.ManualOverrideDialog.exec

    def mo_exec(self):
        self.dir_combo.setCurrentText("课程")
        self.sem_edit.setText("2026秋-大二上")
        self.cat_edit.setText("高等数学")
        self.dlg.accept()
        return self.dlg.result()

    pp.ManualOverrideDialog.exec = mo_exec
    return real_mo


def create_new_file(root, rel, text):
    """只在目标不存在时创建普通文件：O_CREAT|O_EXCL|O_NOFOLLOW，不跟随链接。

    父目录也不跟随符号链接。目标已存在、本身是符号链接、父路径不是真实
    目录时抛 OSError，调用方负责报告，不覆盖已有内容。
    """
    current = Path(root)
    parts = [part for part in rel.split("/") if part]
    for part in parts[:-1]:
        nxt = current / part
        if nxt.is_symlink():
            raise OSError(f"父目录是符号链接：{rel}")
        if nxt.exists() and not nxt.is_dir():
            raise OSError(f"父路径不是目录：{rel}")
        if not nxt.exists():
            nxt.mkdir()
        current = nxt
    dest = current / parts[-1]
    if dest.is_symlink() or dest.exists():
        raise OSError(f"已存在或是符号链接：{rel}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(dest, flags, 0o644)
    try:
        os.write(handle, text.encode("utf-8"))
    finally:
        os.close(handle)
    return True


def phase1(root):
    log(f"平台：{APP.platformName()}")
    log(f"整理空间：{root}")
    重置演示样本.reset(Path(root))
    log(f"首次重置：{重置演示样本.reset.last_note}")
    win = open_window(root)
    scan_and_preview(win)
    plan = win.current_plan
    log(f"首次预览行数：{len(plan.items)}")
    for item in plan.items:
        log(f"  {item.file.rel_path} | 目标={item.target_rel_path} | "
            f"勾选={item.selected} | 依据={item.rule_applied}")
    before_rows = pref_rows(root)
    log(f"保存前偏好行数：{before_rows}")

    def kill_preview_note(attempt):
        log(f"--- {attempt} ---")

    for mode, label in (("cancel", "取消"), ("close", "直接关闭")):
        real_mo, real_rd = install_override(mode)
        try:
            select_row(win, SUBJECT)
            win.on_override_selected()
            process(0.1)
        finally:
            restore_override(real_mo, real_rd)
        kill_preview_note(f"规则对话框{label}后")
        log(f"偏好行数：{pref_rows(root)}")

    captured = {}

    def capture(dlg):
        captured["desc"] = dlg.rule_desc.text()
        captured["impact"] = dlg.impact_view.toPlainText()
        captured["hint"] = dlg.preview_hint.text()

    real_mo, real_rd = install_override("save", capture)
    try:
        select_row(win, SUBJECT)
        win.on_override_selected()
        process(0.1)
    finally:
        restore_override(real_mo, real_rd)
    saved = P.list_space_preferences(root)
    log("确认前对话框内容：")
    for line in captured.get("desc", "").replace("<br>", "\n").replace("<b>", "").replace("</b>", "").splitlines():
        log(f"  {line}")
    log(f"  试运行提示：{captured.get('hint', '')}")
    for line in captured.get("impact", "").splitlines()[:12]:
        log(f"  {line}")
    log(f"保存后偏好行数：{len(saved)} "
        + (f"| {saved[0].preference_id} {saved[0].name}" if saved else ""))
    win.close()
    process(0.3)
    log("关闭第 1 个窗口（进程结束）")


def phase2(root):
    log(f"平台：{APP.platformName()}")
    log(f"整理空间：{root}")
    for rel in (DRAFT, TEMP):
        target = os.path.join(root, rel)
        if os.path.islink(target) or os.path.exists(target):
            log(f"拒绝写入：{rel} 已存在或本身是符号链接，第二阶段不覆盖")
            return 2
    try:
        create_new_file(root, DRAFT, "第二轮同目录新文件\n")
    except OSError as exc:
        log(f"拒绝写入：{DRAFT}（{exc}），第二阶段不覆盖")
        return 2
    log(f"放入 {DRAFT}")
    win = open_window(root)
    scan_and_preview(win)
    log(f"第二轮扫描行数：{len(win.current_plan.items)}")
    draft = item_of(win.current_plan, DRAFT)
    log(f"接口草稿：目标={draft.target_rel_path}")
    log(f"  依据={draft.rule_applied}")
    log(f"  勾选={draft.selected}")
    log(f"  偏好行数={pref_rows(root)}")
    shot(win, "02-第二轮偏好命中.png")

    try:
        create_new_file(root, TEMP, "只改这一次\n")
    except OSError as exc:
        log(f"拒绝写入：{TEMP}（{exc}），第二阶段不覆盖")
        win.close()
        return 2
    log(f"放入 {TEMP}，重新扫描")
    scan_and_preview(win)
    before_temp = item_of(win.current_plan, TEMP).target_rel_path
    before_rows = pref_rows(root)
    log(f"仅改本次前：目标={before_temp} 偏好行数={before_rows}")
    real_mo = install_one_off()
    try:
        select_row(win, TEMP)
        win.on_override_selected()
        process(0.1)
    finally:
        pp.ManualOverrideDialog.exec = real_mo
    after_temp = item_of(win.current_plan, TEMP).target_rel_path
    log(f"仅改本次后：目标={after_temp} 偏好行数={pref_rows(root)}")
    win.close()
    process(0.3)

    win = open_window(root)
    scan_and_preview(win)
    log(f"关闭重开后：偏好行数={pref_rows(root)} 行数={len(win.current_plan.items)}")
    temp_again = item_of(win.current_plan, TEMP)
    log(f"  临时稿目标={temp_again.target_rel_path} 依据={temp_again.rule_applied}")

    uncheck(win, DRAFT)
    keep_before = {
        DRAFT: sha(os.path.join(root, DRAFT)),
        NOTE: sha(os.path.join(root, NOTE)),
    }
    main_mod = sys.modules["main"]
    old_confirm = main_mod.ExecuteConfirmDialog
    main_mod.ExecuteConfirmDialog = install_confirm(False)
    try:
        win.on_execute()
        process(0.2)
    finally:
        main_mod.ExecuteConfirmDialog = old_confirm
    keep_after = {
        DRAFT: sha(os.path.join(root, DRAFT)),
        NOTE: sha(os.path.join(root, NOTE)),
    }
    log(f"取消确认后 SHA 不变：{keep_before == keep_after}")

    selected = {i.file.rel_path: i.target_rel_path
                for i in win.current_plan.items if i.selected}
    log(f"执行前勾选 {len(selected)} 项：{selected}")
    main_mod.ExecuteConfirmDialog = install_confirm(True)
    try:
        win.on_execute()
        assert wait_until(lambda: not win._working, 60)
    finally:
        main_mod.ExecuteConfirmDialog = old_confirm
    process(0.4)
    for rel, target in selected.items():
        log(f"  已移动 {rel} -> {target}")
    log(f"状态栏：{win.statusBar().currentMessage()}")
    shot(win, "03-执行结果.png")

    win.on_history()
    assert wait_until(lambda: win._history_dialog is not None
                      and win._history_dialog.isVisible(), 20)
    process(0.6)
    log("操作历史批次：")
    for line in history_lines(root):
        log(f"  {line}")
    shot(win._history_dialog, "04-操作历史.png")
    win._history_dialog.close()
    process(0.2)

    with yes_answer():
        win.on_undo()
    assert wait_until(lambda: not win._working, 60)
    process(0.3)
    log("撤销后历史：")
    for line in history_lines(root):
        log(f"  {line}")
    scan_and_preview(win)
    note_back = os.path.join(root, NOTE)
    note_target = item_of(win.current_plan, NOTE).target_rel_path
    log(f"撤销后笔记原路径存在={os.path.isfile(note_back)} "
        f"目标={note_target}")
    log(f"  笔记 SHA={sha(note_back) if os.path.isfile(note_back) else None}")
    log(f"状态栏：{win.statusBar().currentMessage()}")
    shot(win, "05-撤销后主窗口.png")
    win.close()
    process(0.3)
    return 0


def history_lines(root):
    from storage import Storage
    st = Storage(root)
    try:
        lines = []
        for batch in st.list_batches():
            actions = batch.actions or st.get_actions(batch.batch_id)
            lines.append(f"batch={batch.batch_id} status={batch.status} n={len(actions)}")
            for action in actions:
                lines.append(
                    f"  {action.status} {action.source_rel} -> {action.target_rel} "
                    f"sha={action.source_sha256}")
        return lines
    finally:
        st.close()


def main() -> int:
    global SHOTS
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, choices=(1, 2), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shots", type=Path, default=SHOTS,
                        help="截图目录，默认 课程交付/证据/截图")
    args = parser.parse_args()
    SHOTS = args.shots
    root = str(args.root)
    if args.phase == 1:
        phase1(root)
        return 0
    return phase2(root)


if __name__ == "__main__":
    raise SystemExit(main())
