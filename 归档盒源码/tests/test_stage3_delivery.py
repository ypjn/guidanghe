"""阶段 3 · 课程演示与文件安全证据。

用真实主窗口走「扫描 → 预览 → 勾选 → 确认 → 执行 → 历史 → 撤销」，
并记录相对路径、文件数量、SHA-256 和批次状态。样本只建在临时目录。
应用数据、方案库和最近空间记录都指向独立临时目录。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_stage3_delivery.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage3_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage3_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage3_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QDialog, QMessageBox,
)

import main as main_mod  # noqa: E402
import plan_patch as pp  # noqa: E402
import preferences as P  # noqa: E402
from classification_config import MATCH_DIR_SCOPE  # noqa: E402
from executor import (  # noqa: E402
    execute_plan, recover_batch, undo_latest_batch,
)
from main import MainWindow  # noqa: E402
from planner import MODE_BY_DIRECTION_SEMESTER, generate_plan  # noqa: E402
from content_extract import extract_contents  # noqa: E402
from scanner import scan_directory  # noqa: E402
from storage import Storage  # noqa: E402

APP = QApplication.instance() or QApplication([])
BASE = tempfile.mkdtemp(prefix="stage3_delivery_")
CHECKS = []
_ANSWER = {"question": QMessageBox.No}


def _fake_question(*args, **kwargs):
    return _ANSWER["question"]


QMessageBox.question = staticmethod(_fake_question)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)

def _sync_preview_worker():
    raise RuntimeError("stage3: synchronous rule preview")


pp.rule_preview_worker_class = _sync_preview_worker
_REAL_MO = pp.ManualOverrideDialog.exec
_REAL_RD = pp.RuleDraftDialog.exec


class yes_answer:
    def __enter__(self):
        _ANSWER["question"] = QMessageBox.Yes

    def __exit__(self, *exc):
        _ANSWER["question"] = QMessageBox.No


def rec(tid, title, ok, evidence=""):
    CHECKS.append((tid, title, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    if evidence:
        print(evidence)


def process(seconds=0.02):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout=30.0):
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


def root_dir(tag):
    path = os.path.join(BASE, tag)
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def snapshot(root, rels):
    out = {}
    for rel in rels:
        path = os.path.join(root, rel)
        out[rel] = sha256(path) if os.path.isfile(path) else None
    return out


def open_window(root):
    win = MainWindow()
    win.path_edit.setText(root)
    win._reset_for_new_dir(root)
    win.show()
    process()
    wait_until(lambda: not getattr(win, "_working", True))
    return win


def scan_and_preview(win):
    win.on_scan()
    assert wait_until(lambda: (not win._working) and win.scan_result is not None)
    win._generate_and_show_plan()
    assert wait_until(
        lambda: (not win._plan_working) and win.current_plan is not None
        and win._plan_valid)
    process()


def close_window(win):
    try:
        win.close()
    except Exception:
        pass
    process(0.05)


def item_of(plan, rel):
    return next(i for i in plan.items if i.file.rel_path == rel)


def uncheck(win, rel):
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            box = win.table.cellWidget(idx, 0)
            check = box.findChild(QCheckBox)
            check.setChecked(False)
            process()
            return
    raise AssertionError(rel)


def history_lines(root):
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


def confirm(win, accepted):
    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted if accepted else QDialog.Rejected

    old = main_mod.ExecuteConfirmDialog
    main_mod.ExecuteConfirmDialog = Dialog
    try:
        win.on_execute()
        if accepted:
            assert wait_until(lambda: not win._working, 30)
    finally:
        main_mod.ExecuteConfirmDialog = old
    process(0.05)


def install_space_pref_save():
    def mo_exec(self):
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText("归档盒")
        self._on_save_as_rule()
        return self.dlg.result()

    def rd_exec(self):
        self.name_edit.setText("开发文档按项目归档")
        for i in range(self.match_combo.count()):
            if self.match_combo.itemData(i) == MATCH_DIR_SCOPE:
                self.match_combo.setCurrentIndex(i)
                break
        self.source_edit.setText("下载/归档盒")
        self.subdir_check.setChecked(True)
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText("归档盒")
        for i in range(self.inner_combo.count()):
            if "清空" in self.inner_combo.itemText(i):
                self.inner_combo.setCurrentIndex(i)
                break
        self.scope_combo.setCurrentIndex(0)
        self._on_preview()
        with yes_answer():
            self._on_save()
        result = self.dlg.result() == 1
        self._on_closed()
        return result

    pp.ManualOverrideDialog.exec = mo_exec
    pp.RuleDraftDialog.exec = rd_exec


def install_named_dir_pref(name, source, category):
    def mo_exec(self):
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText(category)
        self._on_save_as_rule()
        return self.dlg.result()

    def rd_exec(self):
        self.name_edit.setText(name)
        for i in range(self.match_combo.count()):
            if self.match_combo.itemData(i) == MATCH_DIR_SCOPE:
                self.match_combo.setCurrentIndex(i)
                break
        self.source_edit.setText(source)
        self.subdir_check.setChecked(True)
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText(category)
        for i in range(self.inner_combo.count()):
            if "清空" in self.inner_combo.itemText(i):
                self.inner_combo.setCurrentIndex(i)
                break
        self.scope_combo.setCurrentIndex(0)
        self._on_preview()
        with yes_answer():
            self._on_save()
        result = self.dlg.result() == 1
        self._on_closed()
        return result

    pp.ManualOverrideDialog.exec = mo_exec
    pp.RuleDraftDialog.exec = rd_exec


def install_one_off():
    def mo_exec(self):
        self.dir_combo.setCurrentText("课程")
        self.sem_edit.setText("2026秋-大二上")
        self.cat_edit.setText("高等数学")
        self.dlg.accept()
        return self.dlg.result()

    pp.ManualOverrideDialog.exec = mo_exec


def restore_dialogs():
    pp.ManualOverrideDialog.exec = _REAL_MO
    pp.RuleDraftDialog.exec = _REAL_RD


def select_row(win, rel):
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            win.table.selectRow(idx)
            process()
            return
    raise AssertionError(rel)


def test_two_rounds_and_execute():
    root = root_dir("demo")
    write(root, "下载/归档盒/开发说明.md", "接口说明与排期")
    write(root, "下载/笔记.txt", "高等数学 极限 导数")
    write(root, "项目/AIGC/项目说明.md", "项目会议记录")
    outside = os.path.join(os.path.dirname(root), "范围外.md")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("不应被扫描")
    outside_sha = sha256(outside)

    win = open_window(root)
    scan_and_preview(win)
    rels = [i.file.rel_path for i in win.current_plan.items]
    scope_ok = (
        "下载/归档盒/开发说明.md" in rels
        and "下载/笔记.txt" in rels
        and "项目/AIGC/项目说明.md" in rels
        and len(rels) == 3
        and sha256(outside) == outside_sha)
    dev = item_of(win.current_plan, "下载/归档盒/开发说明.md")
    note = item_of(win.current_plan, "下载/笔记.txt")
    first_ok = (
        "待定" in dev.target_rel_path
        and "高等数学" in note.target_rel_path
        and note.selected is True)
    rec("S01", "首次扫描只含空间内 3 个文件，范围外文件不动",
        scope_ok and first_ok,
        f"计划={rels}\n开发说明={dev.target_rel_path} selected={dev.selected}\n"
        f"笔记={note.target_rel_path}\n范围外sha不变={sha256(outside) == outside_sha}")

    install_space_pref_save()
    try:
        select_row(win, "下载/归档盒/开发说明.md")
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    saved = P.list_space_preferences(root)
    pref_ok = len(saved) == 1 and saved[0].category == "归档盒"
    close_window(win)

    write(root, "下载/归档盒/接口草稿.md", "下一轮同目录新文件")
    write(root, "下载/临时稿.md", "只改这一次")
    win2 = open_window(root)
    scan_and_preview(win2)
    draft = item_of(win2.current_plan, "下载/归档盒/接口草稿.md")
    round2_ok = (
        bool(saved)
        and draft.target_dir_rel.endswith(os.path.join("项目", "归档盒"))
        and str(draft.rule_key).startswith("long_term:")
        and saved[0].name in (draft.rule_applied or "")
        and draft.selected is True)
    rows_before = len(P.list_space_preferences(root))
    install_one_off()
    try:
        select_row(win2, "下载/临时稿.md")
        win2.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    one_off = item_of(win2.current_plan, "下载/临时稿.md")
    one_off_ok = (
        "高等数学" in one_off.target_rel_path
        and len(P.list_space_preferences(root)) == rows_before)
    pref_text = (
        f"{saved[0].preference_id} {saved[0].name}" if saved else "无")
    rec("S02", "第二轮命中空间偏好；仅改本次不写偏好库",
        pref_ok and round2_ok and one_off_ok,
        f"偏好={pref_text}\n"
        f"接口草稿={getattr(draft, 'target_rel_path', None)}\n"
        f"依据={getattr(draft, 'rule_applied', None)}\n"
        f"仅改本次={one_off.target_rel_path} 偏好行={len(P.list_space_preferences(root))}")

    src_keep = os.path.join(root, "下载/归档盒/接口草稿.md")
    src_move = os.path.join(root, "下载/笔记.txt")
    before = {
        "下载/归档盒/接口草稿.md": sha256(src_keep),
        "下载/笔记.txt": sha256(src_move),
    }
    uncheck(win2, "下载/归档盒/接口草稿.md")
    note_target = item_of(win2.current_plan, "下载/笔记.txt").target_rel_path
    confirm(win2, False)
    cancel_ok = snapshot(root, before) == before
    confirm(win2, True)
    target = os.path.join(root, note_target)
    move_ok = (
        os.path.isfile(target) and sha256(target) == before["下载/笔记.txt"]
        and not os.path.exists(src_move)
        and os.path.isfile(src_keep)
        and sha256(src_keep) == before["下载/归档盒/接口草稿.md"])
    hist = history_lines(root)
    hist_ok = any("moved" in line and "下载/笔记.txt" in line for line in hist)
    rec("S03", "取消确认不动文件；确认后只移动勾选项，历史与 SHA 一致",
        cancel_ok and move_ok and hist_ok,
        "取消后sha=" + str(cancel_ok) + "\n移动目标=" + note_target
        + "\n" + "\n".join(hist))

    with yes_answer():
        win2.on_undo()
    assert wait_until(lambda: not win2._working, 30)
    undo_ok = (
        os.path.isfile(src_move) and sha256(src_move) == before["下载/笔记.txt"]
        and not os.path.exists(target))
    rec("S04", "撤销后笔记回到原路径且 SHA 不变", undo_ok,
        f"存在={os.path.isfile(src_move)} sha一致="
        f"{os.path.isfile(src_move) and sha256(src_move) == before['下载/笔记.txt']}\n"
        + "\n".join(history_lines(root)))
    close_window(win2)


def test_stale_and_conflict():
    root = root_dir("safety")
    write(root, "下载/甲.md", "甲的原文")
    write(root, "下载/乙.md", "乙的原文")
    write(root, "下载/丙.md", "丙的原文")
    win = open_window(root)
    scan_and_preview(win)
    plan = win.current_plan
    for item in plan.items:
        item.selected = True
    edited = item_of(plan, "下载/甲.md")
    gone = item_of(plan, "下载/乙.md")
    occupied = item_of(plan, "下载/丙.md")
    write(root, "下载/甲.md", "甲被改过")
    os.remove(os.path.join(root, "下载/乙.md"))
    occupant = os.path.join(root, occupied.target_rel_path)
    os.makedirs(os.path.dirname(occupant), exist_ok=True)
    with open(occupant, "w", encoding="utf-8") as f:
        f.write("别人的不同内容")
    occupant_sha = sha256(occupant)
    edited_sha = sha256(os.path.join(root, "下载/甲.md"))

    st = Storage(root)
    result = execute_plan(plan, st)
    st.close()
    errors = " | ".join(result.errors)
    ok = (
        result.moved == 0
        and "源文件内容已变化" in errors
        and "源文件已不存在" in errors
        and "目标路径已被占用" in errors
        and sha256(os.path.join(root, "下载/甲.md")) == edited_sha
        and not os.path.exists(os.path.join(root, "下载/乙.md"))
        and sha256(occupant) == occupant_sha
        and edited.target_rel_path
        and gone.target_rel_path)
    rec("S05", "预览后源被改、被删或目标被占，执行跳过且不覆盖",
        ok, f"moved={result.moved} skipped={result.skipped}\n{errors}\n"
            f"甲sha不变={sha256(os.path.join(root, '下载/甲.md')) == edited_sha} "
            f"占位sha不变={sha256(occupant) == occupant_sha}")
    close_window(win)

    root2 = root_dir("unique")
    write(root2, "下载/笔记.txt", "高等数学 极限 导数\nsame-name-a")
    write(root2, "资料/笔记.txt", "高等数学 极限 导数\nsame-name-b")
    sha_a = sha256(os.path.join(root2, "下载/笔记.txt"))
    sha_b = sha256(os.path.join(root2, "资料/笔记.txt"))
    scan = scan_directory(root2, detect_project_roots=False)
    extracted = extract_contents(scan.files)
    plan2 = generate_plan(
        scan, MODE_BY_DIRECTION_SEMESTER, [],
        content_texts=extracted.texts, content_errors=extracted.errors)
    for item in plan2.items:
        item.selected = True
    names = sorted(i.target_filename for i in plan2.items)
    st2 = Storage(root2)
    result2 = execute_plan(plan2, st2)
    st2.close()
    files = []
    contents = []
    for item in plan2.items:
        path = os.path.join(root2, item.target_rel_path)
        if os.path.isfile(path):
            files.append(item.target_filename)
            contents.append(open(path, encoding="utf-8").read())
    files = sorted(files)
    contents = sorted(contents)
    joined = "".join(contents)
    unique_ok = (
        result2.moved == 2 and any("(1)" in name for name in names)
        and "same-name-a" in joined and "same-name-b" in joined
        and len({os.path.dirname(i.target_rel_path) for i in plan2.items}) == 1
        and sha_a and sha_b)
    rec("S06", "同名不同内容在预览阶段分配唯一名，执行后两份都保留",
        unique_ok,
        f"计划={[ (i.file.rel_path, i.target_rel_path) for i in plan2.items ]}\n"
        f"文件名={names}\n实际={files}\n内容={contents}")

    root3 = root_dir("same-bytes")
    same = "高等数学 极限 导数\nidentical"
    write(root3, "下载/作业.txt", same)
    write(root3, "资料/作业.txt", same)
    before = snapshot(root3, ["下载/作业.txt", "资料/作业.txt"])
    scan3 = scan_directory(root3, detect_project_roots=False)
    extracted3 = extract_contents(scan3.files)
    plan3 = generate_plan(
        scan3, MODE_BY_DIRECTION_SEMESTER, [],
        content_texts=extracted3.texts, content_errors=extracted3.errors)
    for item in plan3.items:
        item.selected = True
    st3 = Storage(root3)
    result3 = execute_plan(plan3, st3)
    st3.close()
    after = {
        item.target_rel_path: sha256(os.path.join(root3, item.target_rel_path))
        for item in plan3.items}
    same_ok = (
        result3.moved == 2
        and len(set(item.target_filename for item in plan3.items)) == 2
        and set(after.values()) == set(before.values())
        and all(not os.path.exists(os.path.join(root3, rel)) for rel in before))
    rec("S10", "同名同内容也分配两个目标，两份 SHA 都保留",
        same_ok,
        f"移动前={before}\n移动后={after}\n"
        f"目标={[item.target_rel_path for item in plan3.items]}")

    root4 = root_dir("same-occupant")
    write(root4, "下载/丁.md", "丁的原文")
    scan4 = scan_directory(root4, detect_project_roots=False)
    plan4 = generate_plan(scan4, MODE_BY_DIRECTION_SEMESTER, [])
    plan4.items[0].selected = True
    occupant4 = os.path.join(root4, plan4.items[0].target_rel_path)
    os.makedirs(os.path.dirname(occupant4), exist_ok=True)
    with open(occupant4, "w", encoding="utf-8") as f:
        f.write("丁的原文")
    source_sha = sha256(os.path.join(root4, "下载/丁.md"))
    occupant_same = sha256(occupant4)
    st4 = Storage(root4)
    result4 = execute_plan(plan4, st4)
    st4.close()
    same_skip = (
        result4.moved == 0
        and any("目标路径已被占用" in text for text in result4.errors)
        and sha256(os.path.join(root4, "下载/丁.md")) == source_sha
        and sha256(occupant4) == occupant_same)
    rec("S11", "目标已有相同内容时仍跳过，源和占位文件都不改",
        same_skip,
        f"moved={result4.moved} errors={result4.errors}\n"
        f"源sha不变={sha256(os.path.join(root4, '下载/丁.md')) == source_sha} "
        f"占位sha不变={sha256(occupant4) == occupant_same}")


def test_cancel_interrupt_undo_conflict():
    root = root_dir("cancel")
    write(root, "下载/一.md", "第一份")
    write(root, "下载/二.md", "第二份")
    sha1 = sha256(os.path.join(root, "下载/一.md"))
    sha2 = sha256(os.path.join(root, "下载/二.md"))
    scan = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(scan, MODE_BY_DIRECTION_SEMESTER, [])
    for item in plan.items:
        item.selected = True
    flag = threading.Event()

    def progress(done, total, rel):
        flag.set()

    st = Storage(root)
    result = execute_plan(plan, st, progress_cb=progress, cancel_event=flag)
    lines = history_lines(root)
    st.close()
    moved_items = [i for i in plan.items
                   if not os.path.exists(os.path.join(root, i.file.rel_path))]
    stayed = [i for i in plan.items
              if os.path.exists(os.path.join(root, i.file.rel_path))]
    cancel_ok = (
        result.was_cancelled and result.moved == 1 and result.cancelled == 1
        and len(moved_items) == 1 and len(stayed) == 1
        and sha256(os.path.join(root, moved_items[0].target_rel_path))
        == {"下载/一.md": sha1, "下载/二.md": sha2}[moved_items[0].file.rel_path]
        and sha256(os.path.join(root, stayed[0].file.rel_path))
        == {"下载/一.md": sha1, "下载/二.md": sha2}[stayed[0].file.rel_path])
    rec("S07", "第一项移动后取消，第二项留在原处",
        cancel_ok, f"moved={result.moved} cancelled={result.cancelled}\n"
                   + "\n".join(lines))

    root_i = root_dir("interrupt")
    write(root_i, "下载/已完成.md", "已经移动")
    write(root_i, "下载/未完成.md", "还在原地")
    scan_i = scan_directory(root_i, detect_project_roots=False)
    plan_i = generate_plan(scan_i, MODE_BY_DIRECTION_SEMESTER, [])
    child = os.path.join(BASE, "_interrupt_child.py")
    with open(child, "w", encoding="utf-8") as f:
        f.write(
            "import os, sys\n"
            f"sys.path.insert(0, {SRC!r})\n"
            "from scanner import scan_directory\n"
            "from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER\n"
            "from storage import Storage\n"
            "from utils import sha256_file\n"
            "root = sys.argv[1]\n"
            "plan = generate_plan(scan_directory(root, detect_project_roots=False), "
            "MODE_BY_DIRECTION_SEMESTER, [])\n"
            "st = Storage(root)\n"
            "bid = st.create_batch(plan, '中断')\n"
            "item = plan.items[0]\n"
            "src = os.path.join(root, item.file.rel_path)\n"
            "st.create_action(bid, item, 0, sha256_file(src) or '')\n"
            "item2 = plan.items[1]\n"
            "st.create_action(bid, item2, 1, sha256_file(os.path.join(root, item2.file.rel_path)) or '')\n"
            "st.update_batch_status(bid, 'executing')\n"
            "tgt = os.path.join(root, item.target_rel_path)\n"
            "os.makedirs(os.path.dirname(tgt), exist_ok=True)\n"
            "os.rename(src, tgt)\n"
            "os._exit(9)\n")
    proc = subprocess.run([sys.executable, child, root_i], capture_output=True, text=True)
    st_i = Storage(root_i)
    recovered = recover_batch(st_i)
    again = recover_batch(st_i)
    batches = history_lines(root_i)
    st_i.close()
    done_item = plan_i.items[0]
    pending_item = plan_i.items[1]
    recover_ok = (
        proc.returncode == 9
        and os.path.isfile(os.path.join(root_i, done_item.target_rel_path))
        and os.path.isfile(os.path.join(root_i, pending_item.file.rel_path))
        and recovered.recovered_moved >= 1
        and again.recovered_moved == 0)
    rec("S08", "中断后核对已移动项，重复恢复不再移动",
        recover_ok,
        f"exit={proc.returncode} first={recovered.summary()}\n"
        f"second={again.summary()}\n" + "\n".join(batches))

    root_u = root_dir("undo-occupied")
    write(root_u, "下载/占用.md", "原内容")
    original = sha256(os.path.join(root_u, "下载/占用.md"))
    scan_u = scan_directory(root_u, detect_project_roots=False)
    plan_u = generate_plan(scan_u, MODE_BY_DIRECTION_SEMESTER, [])
    for item in plan_u.items:
        item.selected = True
    st_u = Storage(root_u)
    moved = execute_plan(plan_u, st_u)
    target = os.path.join(root_u, plan_u.items[0].target_rel_path)
    holder = os.path.join(root_u, "下载/占用.md")
    with open(holder, "w", encoding="utf-8") as f:
        f.write("占位文件")
    holder_sha = sha256(holder)
    undo = undo_latest_batch(st_u)
    st_u.close()
    conflict_ok = (
        moved.moved == 1
        and undo.conflict >= 1
        and os.path.isfile(holder) and sha256(holder) == holder_sha
        and os.path.isfile(target) and sha256(target) == original
        and any("原路径已被占用" in text for text in undo.conflicts + undo.errors_list))
    rec("S09", "原路径被占时撤销报冲突，占位文件和已移动文件都保留",
        bool(conflict_ok),
        f"conflict={undo.conflict} errors={undo.errors_list}\n"
        f"占位sha不变={sha256(holder) == holder_sha} 目标仍在={os.path.isfile(target)}")


def load_reset_module():
    path = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    spec = importlib.util.spec_from_file_location("stage3_reset_sample", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reset_keeps_unrelated_files():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")

    parent = tempfile.mkdtemp(prefix="stage3_reset_root_")
    space = os.path.join(parent, "普通资料")
    kept = os.path.join(space, "作业", "保留.md")
    os.makedirs(os.path.dirname(kept))
    with open(kept, "w", encoding="utf-8") as f:
        f.write("不要删\n")
    outside = os.path.join(parent, "范围外-不要放进样本.md")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("用户自己的对照\n")
    kept_sha = sha256(kept)
    outside_sha = sha256(outside)
    proc = subprocess.run(
        [sys.executable, script, "--root", space],
        capture_output=True, text=True)
    note = os.path.join(space, "下载", "笔记.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write("演示中被改过")
    module.reset(__import__("pathlib").Path(space))
    default_parent = tempfile.mkdtemp(prefix="stage3_reset_default_")
    default_script = os.path.join(default_parent, "重置演示样本.py")
    with open(script, encoding="utf-8") as src, open(default_script, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    default_space = os.path.join(default_parent, "演示样本")
    extra = os.path.join(default_space, "普通资料", "保留.md")
    os.makedirs(os.path.dirname(extra))
    with open(extra, "w", encoding="utf-8") as f:
        f.write("默认命令也要留着\n")
    extra_sha = sha256(extra)
    proc_default = subprocess.run(
        [sys.executable, default_script],
        capture_output=True, text=True)
    user_parent = tempfile.mkdtemp(prefix="stage3_reset_user_")
    user_space = os.path.join(user_parent, "普通资料")
    user_note = os.path.join(user_space, "下载", "笔记.txt")
    os.makedirs(os.path.dirname(user_note))
    with open(user_note, "w", encoding="utf-8") as f:
        f.write("用户原来的笔记\n")
    user_sha = sha256(user_note)
    proc_user = subprocess.run(
        [sys.executable, script, "--root", user_space],
        capture_output=True, text=True)
    link_space = os.path.join(user_parent, "链接样本")
    outside_file = os.path.join(user_parent, "范围外普通文件.txt")
    with open(outside_file, "w", encoding="utf-8") as f:
        f.write("目录外的正文\n")
    outside_file_sha = sha256(outside_file)
    os.makedirs(os.path.join(link_space, "下载"))
    os.symlink(outside_file, os.path.join(link_space, "下载", "笔记.txt"))
    proc_link = subprocess.run(
        [sys.executable, script, "--root", link_space],
        capture_output=True, text=True)
    ok = (
        proc.returncode == 0
        and proc_default.returncode == 0
        and proc_user.returncode == 0
        and proc_link.returncode == 0
        and sha256(kept) == kept_sha
        and sha256(outside) == outside_sha
        and os.path.isfile(os.path.join(space, "下载", "归档盒", "开发说明.md"))
        and open(note, encoding="utf-8").read() == "高等数学 极限 导数\n"
        and sha256(extra) == extra_sha
        and sha256(user_note) == user_sha
        and sha256(outside_file) == outside_file_sha
        and os.path.islink(os.path.join(link_space, "下载", "笔记.txt"))
        and "未覆盖" in proc.stdout
        and "已有文件未覆盖：下载/笔记.txt" in proc_user.stdout
        and "符号链接未跟随：下载/笔记.txt" in proc_link.stdout
        and "保留的非演示文件" in proc.stdout
        and "保留的非演示文件" in proc_default.stdout)
    rec("S12", "重置不覆盖已有文件和目录外符号链接，只写回脚本自己创建的文件",
        ok,
        f"root退出码={proc.returncode}\n{proc.stdout}"
        f"默认路径退出码={proc_default.returncode}\n{proc_default.stdout}"
        f"已有文件退出码={proc_user.returncode}\n{proc_user.stdout}"
        f"符号链接退出码={proc_link.returncode}\n{proc_link.stdout}"
        f"保留sha不变={sha256(kept) == kept_sha} "
        f"范围外sha不变={sha256(outside) == outside_sha} "
        f"用户笔记sha不变={sha256(user_note) == user_sha} "
        f"链接目标sha不变={sha256(outside_file) == outside_file_sha} "
        f"演示样本内其他文件sha不变={sha256(extra) == extra_sha}")


def test_reset_returns_to_first_preview():
    module = load_reset_module()
    root = root_dir("repeat-demo")
    subject = "下载/归档盒/开发说明.md"
    module.reset(__import__("pathlib").Path(root))
    win = open_window(root)
    scan_and_preview(win)
    first = item_of(win.current_plan, subject)
    install_space_pref_save()
    try:
        select_row(win, subject)
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    saved = len(P.list_space_preferences(root)) == 1
    close_window(win)
    module.reset(__import__("pathlib").Path(root))
    win2 = open_window(root)
    scan_and_preview(win2)
    second = item_of(win2.current_plan, subject)
    again = (
        saved
        and len(P.list_space_preferences(root)) == 0
        and "待定" in second.target_rel_path
        and not str(second.rule_key).startswith("long_term:")
        and second.selected is False
        and second.target_rel_path == first.target_rel_path)
    rec("S13", "同一目录再次重置后，开发说明回到待定且未勾选",
        again,
        f"第一次={first.target_rel_path} {first.rule_key} selected={first.selected}\n"
        f"重置后={second.target_rel_path} {second.rule_key} selected={second.selected}\n"
        f"偏好行={len(P.list_space_preferences(root))}")
    close_window(win2)


def test_reset_keeps_preexisting_preferences():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    root = root_dir("existing-space")
    personal = os.path.join(root, "下载", "归档盒", "自己的资料.md")
    os.makedirs(os.path.dirname(personal))
    with open(personal, "w", encoding="utf-8") as handle:
        handle.write("这是整理空间里原有的资料\n")
    personal_sha = sha256(personal)
    win = open_window(root)
    scan_and_preview(win)
    install_space_pref_save()
    try:
        select_row(win, "下载/归档盒/自己的资料.md")
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    before = P.list_space_preferences(root)
    close_window(win)
    win = open_window(root)
    scan_and_preview(win)
    hit = item_of(win.current_plan, "下载/归档盒/自己的资料.md")
    close_window(win)
    proc = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    module.reset(__import__("pathlib").Path(root))
    win = open_window(root)
    scan_and_preview(win)
    after = item_of(win.current_plan, "下载/归档盒/自己的资料.md")
    kept_ids = [row.preference_id for row in P.list_space_preferences(root)]
    ok = (
        len(before) == 1
        and str(hit.rule_key).startswith("long_term:")
        and hit.selected is True
        and proc.returncode == 0
        and "已保留进入演示前的偏好" in proc.stdout
        and "已清除" not in proc.stdout
        and sha256(personal) == personal_sha
        and kept_ids == [before[0].preference_id]
        and after.rule_key == hit.rule_key
        and after.selected is True
        and "待定" not in after.target_rel_path)
    rec("S14", "从未做过演示的整理空间，重置不删除已有偏好",
        ok,
        f"重置前={hit.target_rel_path} {hit.rule_key} selected={hit.selected}\n"
        f"{proc.stdout}"
        f"重置后={after.target_rel_path} {after.rule_key} selected={after.selected}\n"
        f"偏好={kept_ids} sha不变={sha256(personal) == personal_sha}")
    close_window(win)


def test_later_preference_on_ordinary_space_stays():
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    root = root_dir("ordinary-later")
    personal = os.path.join(root, "下载", "归档盒", "自己的资料.md")
    os.makedirs(os.path.dirname(personal))
    with open(personal, "w", encoding="utf-8") as handle:
        handle.write("原有资料\n")
    win = open_window(root)
    scan_and_preview(win)
    install_space_pref_save()
    try:
        select_row(win, "下载/归档盒/自己的资料.md")
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    close_window(win)
    first = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    new_rel = "后来新加的资料/实验记录.md"
    new_file = os.path.join(root, new_rel)
    os.makedirs(os.path.dirname(new_file))
    with open(new_file, "w", encoding="utf-8") as handle:
        handle.write("后来确认的实验记录\n")
    new_sha = sha256(new_file)
    win = open_window(root)
    scan_and_preview(win)
    install_named_dir_pref("后来新增的项目资料", "后来新加的资料", "新增资料")
    try:
        select_row(win, new_rel)
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    close_window(win)
    win = open_window(root)
    scan_and_preview(win)
    before = item_of(win.current_plan, new_rel)
    ids_before = {row.preference_id for row in P.list_space_preferences(root)}
    close_window(win)
    second = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    win = open_window(root)
    scan_and_preview(win)
    after = item_of(win.current_plan, new_rel)
    ids_after = {row.preference_id for row in P.list_space_preferences(root)}
    ok = (
        first.returncode == 0
        and second.returncode == 0
        and "已清除" not in second.stdout
        and len(ids_before) == 2
        and ids_after == ids_before
        and str(before.rule_key).startswith("long_term:")
        and before.selected is True
        and after.rule_key == before.rule_key
        and after.selected is True
        and "新增资料" in after.target_rel_path
        and sha256(new_file) == new_sha)
    rec("S16", "普通空间在重置之后新确认的偏好，再次重置仍保留",
        ok,
        f"第一次重置：{first.stdout}"
        f"第二次重置：{second.stdout}"
        f"重置前={before.target_rel_path} {before.rule_key} selected={before.selected}\n"
        f"重置后={after.target_rel_path} {after.rule_key} selected={after.selected}\n"
        f"偏好前={sorted(ids_before)} 偏好后={sorted(ids_after)} "
        f"sha不变={sha256(new_file) == new_sha}")
    close_window(win)


def test_dangling_outside_symlink_is_not_followed():
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_dangling_")
    root = os.path.join(parent, "样本")
    os.makedirs(root)
    target = os.path.join(parent, "尚不存在的外部文件.txt")
    link = os.path.join(parent, "范围外-不要放进样本.md")
    os.symlink(target, link)
    proc = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    ok = (
        proc.returncode == 0
        and os.path.islink(link)
        and not os.path.exists(target)
        and "未跟随" in proc.stdout)
    rec("S17", "范围外对照是悬空符号链接时，不在目录外创建文件",
        ok,
        f"退出码={proc.returncode}\n{proc.stdout}"
        f"链接仍在={os.path.islink(link)} 外部文件被创建={os.path.exists(target)}")


def run_demo_round(root, module, move_rels=None):
    """重置 → 确认一条空间偏好 → 用主窗口执行演示文件。

    返回 (首次预览基线, 执行目标, 批次历史)。首次预览基线在执行前记录，
    是「回到首次预览」的对照。
    """
    module.reset(__import__("pathlib").Path(root))
    win = open_window(root)
    scan_and_preview(win)
    baseline = {item.file.rel_path: (item.target_rel_path, item.selected)
                for item in win.current_plan.items}
    install_space_pref_save()
    try:
        select_row(win, "下载/归档盒/开发说明.md")
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    win._generate_and_show_plan()
    assert wait_until(
        lambda: (not win._plan_working) and win.current_plan is not None
        and win._plan_valid)
    wanted = set(move_rels if move_rels is not None else module.OWNED)
    for item in win.current_plan.items:
        item.selected = item.file.rel_path in wanted
    targets = {item.file.rel_path: item.target_rel_path
               for item in win.current_plan.items}
    confirm(win, True)
    hist = history_lines(root)
    close_window(win)
    return baseline, targets, hist


def test_empty_demo_reset_returns_to_first_preview():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_empty_demo_")
    root = os.path.join(parent, "演示样本")
    os.makedirs(root)
    first = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    baseline, targets, hist = run_demo_round(root, module)
    moved_ok = (
        len(hist) == 4 and all("moved" in line for line in hist[1:])
        and all(os.path.isfile(os.path.join(root, targets[rel]))
                for rel in module.OWNED)
        and all(not os.path.exists(os.path.join(root, rel))
                for rel in module.OWNED))
    second = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    back_ok = (
        second.returncode == 0
        and second.stdout.count("已按 inode 移回演示文件") == len(module.OWNED)
        and all(os.path.isfile(os.path.join(root, rel)) for rel in module.OWNED)
        and all(not os.path.exists(os.path.join(root, targets[rel]))
                for rel in module.OWNED))
    win = open_window(root)
    scan_and_preview(win)
    rows = [item.file.rel_path for item in win.current_plan.items]
    dev = item_of(win.current_plan, "下载/归档盒/开发说明.md")
    note = item_of(win.current_plan, "下载/笔记.txt")
    project = item_of(win.current_plan, "项目/AIGC/项目说明.md")
    after = {
        "下载/笔记.txt": (note.target_rel_path, note.selected),
        "项目/AIGC/项目说明.md": (project.target_rel_path, project.selected),
    }
    preview_ok = (
        len(rows) == 3
        and "待定" in dev.target_rel_path
        and dev.selected is False
        and not str(dev.rule_key).startswith("long_term:")
        and all(after[rel] == baseline[rel] for rel in after))
    prefs_cleared = len(P.list_space_preferences(root)) == 0
    history_kept = os.path.isfile(os.path.join(root, ".organizer", "history.db"))
    ok = (
        first.returncode == 0
        and "本空间没有已保存的偏好" in first.stdout
        and moved_ok and back_ok and preview_ok
        and prefs_cleared and history_kept)
    rec("S18", "空演示目录执行后再重置，按 inode 移回并回到首次预览",
        ok,
        f"首次重置退出码={first.returncode}\n{first.stdout}"
        f"执行历史={hist}\n执行目标={targets}\n"
        f"再次重置退出码={second.returncode}\n{second.stdout}"
        f"再扫描={rows}\n开发说明={dev.target_rel_path} "
        f"selected={dev.selected} 依据={dev.rule_key}\n"
        f"笔记={note.target_rel_path} 项目={project.target_rel_path}\n"
        f"首次基线={baseline}\n偏好行={len(P.list_space_preferences(root))} "
        f"history.db={history_kept}")
    close_window(win)


def test_reset_keeps_user_materials_around_moved_demo():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_demo_with_user_")
    root = os.path.join(parent, "演示样本")
    os.makedirs(root)
    owned_user = write(root, "_已整理/用户自有.md", "用户自己放在已整理里的文件\n")
    normal = write(root, "普通资料/说明.md", "普通资料，不参与演示\n")
    outside_target = os.path.join(parent, "外部目标.md")
    with open(outside_target, "w", encoding="utf-8") as handle:
        handle.write("目录外的正文\n")
    link = os.path.join(root, "已有链接.md")
    os.symlink(outside_target, link)
    dangling = os.path.join(root, "悬空链接.md")
    os.symlink(os.path.join(parent, "尚不存在.md"), dangling)
    before = {path: sha256(path) for path in (owned_user, normal, outside_target)}
    baseline, targets, hist = run_demo_round(root, module)
    second = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    after = {path: sha256(path) for path in (owned_user, normal, outside_target)}
    demo_back = (
        all(os.path.isfile(os.path.join(root, rel)) for rel in module.OWNED)
        and all(not os.path.exists(os.path.join(root, targets[rel]))
                for rel in module.OWNED))
    win = open_window(root)
    scan_and_preview(win)
    rows = [item.file.rel_path for item in win.current_plan.items]
    ok = (
        second.returncode == 0
        and after == before
        and os.path.islink(link) and os.path.islink(dangling)
        and demo_back
        and sorted(rows) == sorted(list(module.OWNED) + ["普通资料/说明.md"])
        and all(os.path.isfile(os.path.join(root, rel)) for rel in module.OWNED))
    rec("S19", "用户文件与符号链接在重置前后 SHA 不变，演示文件不重复",
        ok,
        f"重置退出码={second.returncode}\n{second.stdout}"
        f"用户材料前={before}\n用户材料后={after}\n"
        f"链接仍在={os.path.islink(link)} 悬空链接仍在={os.path.islink(dangling)}\n"
        f"再扫描={rows}\n执行目标={targets}")
    close_window(win)


def test_undo_after_reset_keeps_restored_files():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_undo_after_reset_")
    root = os.path.join(parent, "演示样本")
    os.makedirs(root)
    owned_user = write(root, "_已整理/用户自有.md", "用户文件\n")
    normal = write(root, "普通资料/说明.md", "不参与演示\n")
    before_user = {path: sha256(path) for path in (owned_user, normal)}
    baseline, targets, hist = run_demo_round(root, module)
    reset_proc = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    demo_before = {rel: sha256(os.path.join(root, rel)) for rel in module.OWNED}
    win = open_window(root)
    scan_and_preview(win)
    with yes_answer():
        win.on_undo()
    assert wait_until(lambda: not win._working, 30)
    process(0.1)
    after_hist = history_lines(root)
    demo_after = {rel: sha256(os.path.join(root, rel)) for rel in module.OWNED}
    after_user = {path: sha256(path) for path in (owned_user, normal)}
    ok = (
        reset_proc.returncode == 0
        and demo_after == demo_before
        and all(demo_after.values())
        and after_user == before_user
        and any(line.startswith("batch=1") for line in after_hist)
        and os.path.isfile(os.path.join(root, ".organizer", "history.db")))
    rec("S20", "重置之后从主窗口撤销，已恢复的演示文件和用户文件都不变",
        ok,
        f"重置退出码={reset_proc.returncode}\n"
        f"撤销后历史={after_hist}\n演示前={demo_before}\n演示后={demo_after}\n"
        f"用户前={before_user}\n用户后={after_user}")
    close_window(win)


def test_ledger_tmp_symlink_stays_outside():
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_ledger_link_")
    root = os.path.join(parent, "样本")
    org = os.path.join(root, ".organizer")
    os.makedirs(org)
    external = os.path.join(parent, "目录外清单.txt")
    with open(external, "w", encoding="utf-8") as handle:
        handle.write("外部文件原来的内容\n")
    external_sha = sha256(external)
    os.symlink(external, os.path.join(org, "演示样本清单.json.tmp"))
    config_external = os.path.join(parent, "目录外配置.txt")
    with open(config_external, "w", encoding="utf-8") as handle:
        handle.write("外部配置原来的内容\n")
    config_sha = sha256(config_external)
    os.symlink(config_external, os.path.join(org, "classification_config.json.tmp"))
    proc = subprocess.run(
        [sys.executable, script, "--root", root],
        capture_output=True, text=True)
    ok = (
        proc.returncode == 0
        and sha256(external) == external_sha
        and open(external, encoding="utf-8").read() == "外部文件原来的内容\n"
        and sha256(config_external) == config_sha
        and os.path.islink(os.path.join(org, "演示样本清单.json.tmp")))
    rec("S15", "清单临时文件是符号链接时，不改写目录外文件",
        ok,
        f"退出码={proc.returncode}\n{proc.stdout}"
        f"外部sha不变={sha256(external) == external_sha} "
        f"配置sha不变={sha256(config_external) == config_sha}")


def _strip_ledger_sha(root):
    """把清单改成 657ceef 的旧格式：文件身份只有设备号和 inode。"""
    path = os.path.join(root, ".organizer", "演示样本清单.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    for info in (data.get("files") or {}).values():
        if isinstance(info, dict):
            info.pop("sha256", None)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def test_reset_never_rewrites_outside_hardlink():
    module = load_reset_module()
    script = os.path.join(os.path.dirname(SRC), "课程交付", "重置演示样本.py")
    parent = tempfile.mkdtemp(prefix="stage3_hardlink_")
    personal = "个人内容，不能被重置改写\n"
    note_text = module.OWNED["下载/笔记.txt"]

    root_a = os.path.join(parent, "旧清单")
    module.reset(__import__("pathlib").Path(root_a))
    _strip_ledger_sha(root_a)
    inside_a = os.path.join(root_a, "下载", "笔记.txt")
    moved_a = os.path.join(root_a, "_已整理", "课程", "未识别学期",
                           "高等数学", "文档", "笔记.txt")
    os.makedirs(os.path.dirname(moved_a), exist_ok=True)
    os.rename(inside_a, moved_a)
    outside_a = os.path.join(parent, "目录外个人笔记-a.txt")
    os.link(moved_a, outside_a)
    with open(outside_a, "w", encoding="utf-8") as handle:
        handle.write(personal)
    personal_sha = sha256(outside_a)
    proc_a = subprocess.run([sys.executable, script, "--root", root_a],
                            capture_output=True, text=True)
    a_ok = (
        proc_a.returncode == 0
        and sha256(outside_a) == personal_sha
        and open(outside_a, encoding="utf-8").read() == personal
        and sha256(moved_a) == personal_sha
        and os.path.isfile(inside_a)
        and open(inside_a, encoding="utf-8").read() == note_text
        and os.stat(inside_a).st_ino != os.stat(outside_a).st_ino
        and "已按 inode 移回" not in proc_a.stdout)

    root_b = os.path.join(parent, "新清单")
    module.reset(__import__("pathlib").Path(root_b))
    inside_b = os.path.join(root_b, "下载", "笔记.txt")
    outside_b = os.path.join(parent, "目录外个人笔记-b.txt")
    os.link(inside_b, outside_b)
    with open(outside_b, "w", encoding="utf-8") as handle:
        handle.write(personal)
    personal_b_sha = sha256(outside_b)
    proc_b = subprocess.run([sys.executable, script, "--root", root_b],
                            capture_output=True, text=True)
    b_ok = (
        proc_b.returncode == 0
        and sha256(outside_b) == personal_b_sha
        and open(outside_b, encoding="utf-8").read() == personal
        and sha256(inside_b) == personal_b_sha
        and os.stat(inside_b).st_nlink > 1
        and "硬链接" in proc_b.stdout)

    rec("S21", "目录外硬链接的 inode 不被截断或覆盖，原路径改成新 inode",
        a_ok and b_ok,
        f"旧清单退出码={proc_a.returncode}\n{proc_a.stdout}"
        f"目录外sha不变={sha256(outside_a) == personal_sha} "
        f"原路径是新 inode={os.stat(inside_a).st_ino != os.stat(outside_a).st_ino}\n"
        f"新清单退出码={proc_b.returncode}\n{proc_b.stdout}"
        f"目录外sha不变={sha256(outside_b) == personal_b_sha} "
        f"原路径nlink={os.stat(inside_b).st_nlink}")


def test_writeback_refuses_link_added_after_move():
    """复现 1：按 inode 链接成功后、写回之前，目录外多出一个硬链接。

    检查通过之后才截断的窗口：nlink 在 _move_back 里还是 1，真正的
    ftruncate/write 在 _write_regular 里。中间多出的链接不能被改写。
    """
    module = load_reset_module()
    parent = tempfile.mkdtemp(prefix="stage3_writeback_after_link_")
    root = os.path.join(parent, "演示样本")
    module.reset(__import__("pathlib").Path(root))
    inside = os.path.join(root, "下载", "笔记.txt")
    moved = os.path.join(root, "_已整理", "课程", "未识别学期",
                         "高等数学", "文档", "笔记.txt")
    os.makedirs(os.path.dirname(moved), exist_ok=True)
    os.rename(inside, moved)
    outside = os.path.join(parent, "目录外个人笔记-a.txt")
    personal = "个人内容，不能被重置改写\n"
    real_link = os.link
    state = {"n": 0}

    def fake_link(a, b, *args, **kwargs):
        real_link(a, b, *args, **kwargs)
        if os.path.abspath(str(a)) == os.path.abspath(moved) and state["n"] == 0:
            state["n"] += 1
            # 链接成功后、写回之前，另一个程序把新名字再硬链接到空间外
            real_link(str(b), outside)
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write(personal)

    os.link = fake_link
    try:
        notes = module.place_owned_files(__import__("pathlib").Path(root), None)
    finally:
        os.link = real_link
    personal_sha = hashlib.sha256(personal.encode("utf-8")).hexdigest()
    outside_ok = (
        sha256(outside) == personal_sha
        and read_text(outside) == personal
        and os.path.isfile(outside))
    inside_ok = (not os.path.exists(inside)
                 or read_text(inside) == personal)
    note_ok = (
        any(line.startswith("硬链接未覆盖") and "笔记.txt" in line
            for line in notes)
        and not any("已按 inode 移回" in line and "笔记.txt" in line
                    for line in notes))
    rec("S24", "检查通过后才多出的目录外硬链接，写回前再查 nlink，不截断",
        state["n"] == 1 and outside_ok and inside_ok and note_ok,
        f"注入={state['n']} 说明={notes}\n"
        f"目录外sha不变={sha256(outside) == personal_sha} "
        f"目录外内容={read_text(outside)!r}\n"
        f"原路径内容={read_text(inside)!r} 存在={os.path.exists(inside)}")


def test_writeback_refuses_link_added_before_open():
    """复现 2：文件还在原路径、nlink 为 1，open 之前被硬链接到目录外。

    nlink 的第一次检查在调用 _write_regular 之前。真正打开、
    截断、写入之间又多出一个链接时，也不能改动目录外的字节。
    """
    module = load_reset_module()
    parent = tempfile.mkdtemp(prefix="stage3_writeback_before_open_")
    root = os.path.join(parent, "演示样本")
    module.reset(__import__("pathlib").Path(root))
    inside = os.path.join(root, "下载", "笔记.txt")
    outside = os.path.join(parent, "目录外个人笔记-b.txt")
    personal = "个人内容，不能被重置改写\n"
    real_open = os.open
    real_link = os.link
    state = {"n": 0}

    def fake_open(path, flags, *args, **kwargs):
        target = os.path.abspath(str(path))
        if (target == os.path.abspath(inside) and not (flags & os.O_CREAT)
                and state["n"] == 0):
            state["n"] += 1
            # 真正打开（非新建）之前，另一个程序把同一 inode 链接到空间外
            real_link(inside, outside)
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write(personal)
        return real_open(path, flags, *args, **kwargs)

    os.open = fake_open
    try:
        notes = module.place_owned_files(__import__("pathlib").Path(root), None)
    finally:
        os.open = real_open
    personal_sha = hashlib.sha256(personal.encode("utf-8")).hexdigest()
    outside_ok = (
        sha256(outside) == personal_sha
        and read_text(outside) == personal
        and os.path.isfile(outside))
    inside_ok = read_text(inside) == personal
    note_ok = (
        any(line.startswith("硬链接未覆盖") and "笔记.txt" in line
            for line in notes)
        and not any("已写回脚本创建的文件" in line and "笔记.txt" in line
                    for line in notes))
    nlink = os.stat(inside).st_nlink if os.path.exists(inside) else None
    rec("S25", "打开之前才多出的目录外硬链接，打开后立刻查 nlink，不写入",
        state["n"] == 1 and outside_ok and inside_ok and note_ok,
        f"注入={state['n']} 说明={notes}\n"
        f"目录外sha不变={sha256(outside) == personal_sha} "
        f"目录外内容={read_text(outside)!r}\n"
        f"原路径内容={read_text(inside)!r} nlink={nlink}")


def test_reset_keeps_replacement_at_old_path():
    module = load_reset_module()
    root = root_dir("replace-source")
    module.reset(__import__("pathlib").Path(root))
    src = os.path.join(root, "下载", "笔记.txt")
    old = os.path.join(root, "_已整理", "课程", "未识别学期",
                       "高等数学", "文档", "笔记.txt")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    os.rename(src, old)
    replacement = "另一应用刚保存的用户文件\n"
    real_link = os.link

    def fake_link(a, b, *args, **kwargs):
        real_link(a, b, *args, **kwargs)
        if os.path.abspath(str(a)) == os.path.abspath(old):
            # 链接成功后、删除旧名之前，另一个程序把旧路径换成新文件
            os.unlink(str(a))
            with open(str(a), "w", encoding="utf-8") as handle:
                handle.write(replacement)

    os.link = fake_link
    try:
        notes = module.place_owned_files(__import__("pathlib").Path(root), None)
    finally:
        os.link = real_link
    ok = (
        read_text(old) == replacement
        and read_text(src) == module.OWNED["下载/笔记.txt"]
        and any("未删除" in line and "另一份文件" in line for line in notes)
        and os.path.isfile(os.path.join(root, "下载", "归档盒", "开发说明.md"))
        and os.path.isfile(os.path.join(root, "项目", "AIGC", "项目说明.md")))
    rec("S22", "旧路径被换成另一份文件时，不删除替换进去的文件",
        ok, f"说明={notes}\n旧路径内容={read_text(old)!r}\n"
            f"原路径内容={read_text(src)!r}")


def test_demo_phase2_refuses_existing_paths():
    script = os.path.join(os.path.dirname(SRC), "课程交付", "拍摄演示过程.py")
    parent = tempfile.mkdtemp(prefix="stage3_phase2_")
    draft_text = "用户自己的接口草稿\n"
    temp_text = "用户自己的临时稿\n"

    root_a = os.path.join(parent, "已有文件")
    os.makedirs(os.path.join(root_a, "下载", "归档盒"))
    draft_a = os.path.join(root_a, "下载", "归档盒", "接口草稿.md")
    temp_a = os.path.join(root_a, "下载", "临时稿.md")
    with open(draft_a, "w", encoding="utf-8") as handle:
        handle.write(draft_text)
    with open(temp_a, "w", encoding="utf-8") as handle:
        handle.write(temp_text)
    before_a = {draft_a: sha256(draft_a), temp_a: sha256(temp_a)}
    proc_a = subprocess.run(
        [sys.executable, script, "--phase", "2", "--root", root_a,
         "--shots", tempfile.mkdtemp(prefix="stage3_shots_a_")],
        capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    after_a = {path: sha256(path) for path in before_a}
    a_ok = (
        proc_a.returncode != 0
        and "Traceback" not in proc_a.stdout + proc_a.stderr
        and "下载/归档盒/接口草稿.md" in proc_a.stdout + proc_a.stderr
        and after_a == before_a)

    root_b = os.path.join(parent, "链接")
    os.makedirs(os.path.join(root_b, "下载", "归档盒"))
    outside = os.path.join(parent, "目录外个人文档.md")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("目录外个人文档\n")
    outside_sha = sha256(outside)
    os.symlink(outside, os.path.join(root_b, "下载", "归档盒", "接口草稿.md"))
    os.symlink(os.path.join(parent, "尚不存在.md"),
               os.path.join(root_b, "下载", "临时稿.md"))
    proc_b = subprocess.run(
        [sys.executable, script, "--phase", "2", "--root", root_b,
         "--shots", tempfile.mkdtemp(prefix="stage3_shots_b_")],
        capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    b_ok = (
        proc_b.returncode != 0
        and "Traceback" not in proc_b.stdout + proc_b.stderr
        and sha256(outside) == outside_sha
        and os.path.islink(os.path.join(root_b, "下载", "归档盒", "接口草稿.md"))
        and os.path.islink(os.path.join(root_b, "下载", "临时稿.md"))
        and not os.path.exists(os.path.join(parent, "尚不存在.md")))

    root_c = os.path.join(parent, "新建")
    os.makedirs(root_c)
    link_parent = os.path.join(parent, "父目录链接")
    os.makedirs(os.path.join(link_parent, "真实目录"))
    os.symlink(os.path.join(link_parent, "真实目录"),
               os.path.join(root_c, "外链"))
    probe = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('demo', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "m.create_new_file(sys.argv[2], '下载/归档盒/接口草稿.md', '新稿件\\n')\n"
        "print('CREATED')\n"
        "try:\n"
        "    m.create_new_file(sys.argv[2], '外链/躲出去.md', 'x\\n')\n"
        "except OSError as exc:\n"
        "    print('REFUSED', exc)\n")
    proc_c = subprocess.run(
        [sys.executable, "-c", probe, script, root_c],
        capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    created = "CREATED" in proc_c.stdout
    refused = "REFUSED" in proc_c.stdout and proc_c.returncode == 0
    c_ok = (
        created
        and os.path.isfile(os.path.join(root_c, "下载", "归档盒", "接口草稿.md"))
        and not os.path.islink(os.path.join(root_c, "下载", "归档盒", "接口草稿.md"))
        and refused
        and not os.path.exists(os.path.join(link_parent, "真实目录", "躲出去.md")))
    rec("S23", "第二阶段拒绝覆盖已有文件和符号链接，只新建普通文件",
        a_ok and b_ok and c_ok,
        f"已有文件退出码={proc_a.returncode}\n{proc_a.stdout}{proc_a.stderr}"
        f"sha不变={after_a == before_a}\n"
        f"链接退出码={proc_b.returncode}\n{proc_b.stdout}{proc_b.stderr}"
        f"目录外sha不变={sha256(outside) == outside_sha}\n"
        f"新建探测退出码={proc_c.returncode}\n{proc_c.stdout}{proc_c.stderr}"
        f"新建={created} 父目录链接被拒={refused}")


def main():
    test_reset_keeps_unrelated_files()
    test_reset_returns_to_first_preview()
    test_reset_keeps_preexisting_preferences()
    test_later_preference_on_ordinary_space_stays()
    test_dangling_outside_symlink_is_not_followed()
    test_ledger_tmp_symlink_stays_outside()
    test_empty_demo_reset_returns_to_first_preview()
    test_reset_keeps_user_materials_around_moved_demo()
    test_undo_after_reset_keeps_restored_files()
    test_reset_never_rewrites_outside_hardlink()
    test_writeback_refuses_link_added_after_move()
    test_writeback_refuses_link_added_before_open()
    test_reset_keeps_replacement_at_old_path()
    test_demo_phase2_refuses_existing_paths()
    test_two_rounds_and_execute()
    test_stale_and_conflict()
    test_cancel_interrupt_undo_conflict()
    failed = [row for row in CHECKS if not row[2]]
    print()
    print(f"合计 {len(CHECKS)} 项：通过 {len(CHECKS) - len(failed)} / 未通过 {len(failed)}")
    for tid, title, ok, evidence in CHECKS:
        if not ok:
            print(f"未通过 {tid} {title}\n{evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
