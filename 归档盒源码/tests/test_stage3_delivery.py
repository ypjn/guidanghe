"""阶段 3 · 课程演示与文件安全证据。

用真实主窗口走「扫描 → 预览 → 勾选 → 确认 → 执行 → 历史 → 撤销」，
并记录相对路径、文件数量、SHA-256 和批次状态。样本只建在临时目录。
应用数据、方案库和最近空间记录都指向独立临时目录。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_stage3_delivery.py
"""

from __future__ import annotations

import hashlib
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
    unique_ok = (
        result2.moved == 2 and any("(1)" in name for name in names)
        and "same-name-a" in contents[0] or "same-name-a" in "".join(contents)
        and "same-name-b" in "".join(contents)
        and len({os.path.dirname(i.target_rel_path) for i in plan2.items}) == 1
        and sha_a and sha_b)
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


def main():
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
