"""
归档盒阶段 6 · 个人文件管理 Agent 回归（offscreen，真实窗口）。

覆盖：
  1. 正常流程：一句话查到文件 → 看命中理由与方案 → 确认整理 → 历史撤销；
  2. 指令说不清：先问学期，再问课程，回答后才给方案；
  3. 取消方案：文件位置与内容不变，不产生历史批次；
  4. 确认前文件被改：执行时识别内容变化并跳过，撤销只恢复实际移动的文件；
  5. 目标已有同名文件：保留原文件，计划改名，撤销后两边内容都可核对。

运行：
  QT_QPA_PLATFORM=offscreen <python> tests/test_stage6_agent.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage6_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage6_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage6_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import agent  # noqa: E402
import main as main_mod  # noqa: E402
from storage import Storage  # noqa: E402

APP = QApplication.instance() or QApplication([])
BASE = tempfile.mkdtemp(prefix="stage6_agent_")
CHECKS = []
MESSAGES = []
_ANSWER = {"question": QMessageBox.Yes}


def rec(tid, title, ok, evidence=""):
    CHECKS.append((tid, title, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    if evidence:
        print(evidence)


def process(seconds: float = 0.02):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 60.0) -> bool:
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


def _question(*args, **kwargs):
    return _ANSWER["question"]


def _remember(kind):
    def handler(*args, **kwargs):
        text = str(args[-1]) if args else ""
        MESSAGES.append((kind, text))
        return QMessageBox.Ok
    return handler


QMessageBox.question = staticmethod(_question)
QMessageBox.information = staticmethod(_remember("information"))
QMessageBox.warning = staticmethod(_remember("warning"))
QMessageBox.critical = staticmethod(_remember("critical"))


def confirm(win, accepted=True):
    """替换执行确认框，走主窗口真实 on_execute 入口。"""
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
            wait_until(lambda: not win._working and not win._plan_working, 60)
    finally:
        main_mod.ExecuteConfirmDialog = old
    process(0.05)


def space(tag):
    path = os.path.join(BASE, tag)
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root):
    """整理空间内除 .organizer 外的相对路径 → SHA-256。"""
    out = {}
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".organizer"]
        for name in files:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            out[rel] = sha256(path)
    return out


def open_window(root):
    win = main_mod.MainWindow()
    win.path_edit.setText(root)
    win._reset_for_new_dir(root)
    win.show()
    process()
    wait_until(lambda: not win._working and not win._plan_working, 15)
    return win


def close_window(win):
    try:
        win._closing = True
        win.close()
    except Exception:
        pass
    process(0.05)


def ask(win, instruction):
    """从主窗口打开 AI 管家并完成一次查找。"""
    win.on_agent()
    dlg = win._agent_dialog
    assert dlg is not None, "AI 管家窗口没有打开"
    dlg.set_instruction(instruction)
    dlg.start_search()
    assert wait_until(lambda: dlg._searching is False, 90), "AI 管家查找超时"
    assert dlg.result is not None, "AI 管家没有返回结果"
    return dlg


def choose(dlg, value):
    for index in range(dlg.question_combo.count()):
        if dlg.question_combo.itemData(index) == value:
            dlg.question_combo.setCurrentIndex(index)
            break
    dlg.answer_question()
    process(0.05)


def batches(root):
    storage = Storage(root)
    try:
        return storage.list_batches()
    finally:
        storage.close()


def actions(root, batch_id):
    storage = Storage(root)
    try:
        return storage.get_actions(batch_id)
    finally:
        storage.close()


def make_normal_space(tag):
    root = space(tag)
    for rel in ("大二上/数据结构/实验/报告", "大二上/数据结构/实验/代码",
                "大二上/待整理"):
        os.makedirs(os.path.join(root, rel), exist_ok=True)
    write(root, "大二上/数据结构/实验/报告/数据结构实验二报告.md", "旧版报告\n")
    write(root, "大二上/待整理/数据结构实验一报告.md",
          "数据结构 实验一 链表 实验报告\n")
    write(root, "大二上/待整理/数据结构实验二报告.md",
          "数据结构 实验二 栈 新版报告\n")
    write(root, "大二上/待整理/线性表实验代码.py",
          "# 数据结构 链表 代码实现\ndef build():\n    return []\n")
    write(root, "大二上/待整理/高等数学作业.md", "微积分 作业\n")
    return root


INSTRUCTION = ("帮我找出这学期的数据结构实验材料，"
               "先给我看看报告和代码怎么归到现有目录")


def main():
    # ---------------- 1. 正常流程：查找 → 方案 → 确认整理 → 撤销 ----------------
    normal = make_normal_space("normal")
    before = snapshot(normal)
    existing_report = os.path.join(
        normal, "大二上/数据结构/实验/报告/数据结构实验二报告.md")
    existing_sha = sha256(existing_report)

    win = open_window(normal)
    dlg = ask(win, INSTRUCTION)
    result = dlg.result
    plan = result.plan

    rec("S601",
        "一句话找到报告和代码，并解释命中原因；无关课程不进入结果",
        result.question is None and result.semester == "大二上"
        and result.course == "数据结构"
        and len(result.hits) == 4
        and plan is not None and len(plan.items) == 3
        and all(hit.evidence for hit in result.hits)
        and not any("高等数学" in hit.file.rel_path for hit in result.hits),
        f"学期={result.semester} 课程={result.course} "
        f"命中={[(h.file.rel_path, h.evidence) for h in result.hits]}")

    targets = {item.file.rel_path: item.target_rel_path for item in plan.items}
    rec("S602",
        "报告与代码分别归入已有的实验报告/代码目录，方案显示源和目标",
        targets.get("大二上/待整理/数据结构实验一报告.md")
        == os.path.join("大二上/数据结构/实验/报告/数据结构实验一报告.md")
        and targets.get("大二上/待整理/线性表实验代码.py")
        == os.path.join("大二上/数据结构/实验/代码/线性表实验代码.py")
        and dlg.plan_table.rowCount() == 3
        and dlg.hits_table.rowCount() == 4,
        f"目标={targets}\n方案行={dlg.plan_table.rowCount()} "
        f"命中行={dlg.hits_table.rowCount()}")

    conflict_item = next(
        item for item in plan.items
        if item.file.rel_path == "大二上/待整理/数据结构实验二报告.md")
    rec("S603",
        "目标已有同名文件时保留原文件，计划自动改名并标记冲突",
        conflict_item.conflict_with_existing
        and conflict_item.target_filename == "数据结构实验二报告 (1).md",
        f"目标={conflict_item.target_rel_path} 冲突={conflict_item.conflict_with_existing}")

    dlg.apply_plan()
    process(0.1)
    rec("S604",
        "方案进入主界面现有预览，旧确认作废，执行按钮可用",
        win.current_plan is plan and win._plan_valid
        and win.table.rowCount() == 3
        and win._confirmed_revision is None
        and win.btn_execute.isEnabled(),
        f"行数={win.table.rowCount()} 确认版本={win._confirmed_revision} "
        f"执行可用={win.btn_execute.isEnabled()}")

    # 方案改过一次：必须重新确认。
    win._after_plan_mutation("测试：方案改动后重新确认")
    rec("S605",
        "方案改动后旧确认失效，必须重新确认最终批次",
        win._confirmed_revision is None,
        f"确认版本={win._confirmed_revision}")

    confirm(win, True)
    process(0.1)
    moved_one = os.path.join(
        normal, "大二上/数据结构/实验/报告/数据结构实验一报告.md")
    moved_code = os.path.join(
        normal, "大二上/数据结构/实验/代码/线性表实验代码.py")
    renamed = os.path.join(
        normal, "大二上/数据结构/实验/报告/数据结构实验二报告 (1).md")
    rec("S606",
        "确认后走现有执行入口：文件移动到现有目录，同名文件另存，原文件不动",
        os.path.isfile(moved_one) and os.path.isfile(moved_code)
        and os.path.isfile(renamed)
        and not os.path.exists(os.path.join(normal, "大二上/待整理/数据结构实验一报告.md"))
        and not os.path.exists(os.path.join(normal, "大二上/待整理/线性表实验代码.py"))
        and sha256(existing_report) == existing_sha
        and sha256(renamed) == before["大二上/待整理/数据结构实验二报告.md"],
        f"报告={os.path.isfile(moved_one)} 代码={os.path.isfile(moved_code)} "
        f"改名={os.path.isfile(renamed)} 原文件SHA不变={sha256(existing_report) == existing_sha}")

    batch_rows = batches(normal)
    batch_actions = actions(normal, batch_rows[-1].batch_id) if batch_rows else []
    rec("S607",
        "执行写入可追溯历史：一个批次、三条动作、目标为实际落点",
        len(batch_rows) == 1 and len(batch_actions) == 3
        and all(a.status == "moved" for a in batch_actions)
        and any(a.target_rel.endswith("数据结构实验二报告 (1).md")
                for a in batch_actions),
        f"批次={[(b.batch_id, b.status) for b in batch_rows]} "
        f"动作={[(a.source_rel, a.target_rel, a.status) for a in batch_actions]}")

    win.on_undo()
    wait_until(lambda: not win._working and not win._plan_working, 60)
    process(0.05)
    after_undo = snapshot(normal)
    rec("S608",
        "从历史撤销后文件回到原位，内容与整理前逐字节一致",
        after_undo == before
        and not os.path.exists(moved_one) and not os.path.exists(moved_code)
        and not os.path.exists(renamed)
        and sha256(existing_report) == existing_sha,
        f"撤销前={before}\n撤销后={after_undo}")

    undo_batches = batches(normal)
    rec("S609",
        "撤销后批次状态为已撤销，历史仍保留",
        len(undo_batches) == 1 and undo_batches[0].status == "undone",
        f"批次={[(b.batch_id, b.status) for b in undo_batches]}")
    close_window(win)

    # ---------------- 2. 说不清的指令：先问学期，再问课程 ----------------
    amb = space("ambiguous")
    for rel in ("大二上/数据结构/实验/报告", "大二上/操作系统/实验/报告",
                "大一下/数据结构/实验/报告", "大二上/待整理"):
        os.makedirs(os.path.join(amb, rel), exist_ok=True)
    write(amb, "大二上/待整理/数据结构实验报告.md", "数据结构 链表 实验报告\n")
    write(amb, "大二上/待整理/操作系统实验报告.md", "操作系统 进程 实验报告\n")
    write(amb, "大一下/待整理/数据结构实验旧报告.md", "数据结构 旧 实验报告\n")

    awin = open_window(amb)
    amb_before = snapshot(amb)
    adlg = ask(awin, "帮我把这学期的实验材料整理一下")
    rec("S610",
        "「这学期」对应多个学期目录时先提问，不直接生成方案",
        adlg.result.question is not None
        and adlg.result.question.kind == "semester"
        and adlg.result.plan is None,
        f"问题={adlg.result.question.kind if adlg.result.question else None} "
        f"选项={[o['value'] for o in (adlg.result.question.options if adlg.result.question else [])]}")

    choose(adlg, "大二上")
    rec("S611",
        "回答学期后仍有多门课程时继续问课程，不猜课程",
        adlg.result.question is not None
        and adlg.result.question.kind == "course"
        and adlg.result.plan is None
        and adlg.result.semester == "大二上",
        f"问题={adlg.result.question.kind if adlg.result.question else None} "
        f"选项={[o['value'] for o in (adlg.result.question.options if adlg.result.question else [])]}")

    choose(adlg, "数据结构")
    plan2 = adlg.result.plan
    rec("S612",
        "回答课程后才生成方案，且只包含该学期该课程的文件",
        adlg.result.question is None and plan2 is not None
        and len(plan2.items) == 1
        and plan2.items[0].file.rel_path == "大二上/待整理/数据结构实验报告.md"
        and plan2.items[0].target_dir_rel.replace("\\", "/")
        == "大二上/数据结构/实验/报告",
        f"方案={[(i.file.rel_path, i.target_rel_path) for i in (plan2.items if plan2 else [])]}")

    adlg.reject()
    process(0.05)
    rec("S613",
        "澄清过程中取消不移动文件、不写历史批次",
        awin.current_plan is None and not awin._plan_valid
        and snapshot(amb) == amb_before
        and len(batches(amb)) == 0,
        f"当前计划={awin.current_plan} 历史批次={len(batches(amb))} "
        f"文件一致={snapshot(amb) == amb_before}")
    close_window(awin)

    # ---------------- 3. 取消方案：位置与内容不变 ----------------
    cancel_root = make_normal_space("cancel")
    cancel_before = snapshot(cancel_root)
    cwin = open_window(cancel_root)
    cdlg = ask(cwin, INSTRUCTION)
    had_plan = cdlg.result.plan is not None and len(cdlg.result.plan.items) == 3
    cdlg.reject()
    process(0.05)
    rec("S614",
        "取消方案后主界面没有新计划，文件位置与内容完全不变",
        had_plan and cwin.current_plan is None and not cwin._plan_valid
        and snapshot(cancel_root) == cancel_before
        and len(batches(cancel_root)) == 0,
        f"有方案={had_plan} 当前计划={cwin.current_plan} "
        f"文件一致={snapshot(cancel_root) == cancel_before}")
    close_window(cwin)

    # ---------------- 4. 确认前文件被改：执行时跳过并说明 ----------------
    changed_root = space("changed")
    for rel in ("大二上/数据结构/实验/报告", "大二上/数据结构/实验/代码",
                "大二上/待整理"):
        os.makedirs(os.path.join(changed_root, rel), exist_ok=True)
    report_rel = "大二上/待整理/数据结构实验报告A.md"
    code_rel = "大二上/待整理/数据结构实验代码A.py"
    write(changed_root, report_rel, "数据结构 原始报告\n")
    write(changed_root, code_rel, "# 数据结构 原始代码\n")
    original_report_sha = sha256(os.path.join(changed_root, report_rel))

    hwin = open_window(changed_root)
    hdlg = ask(hwin, "帮我找出这学期的数据结构实验报告和代码")
    hdlg.apply_plan()
    process(0.05)
    # 确认前用户改了报告内容
    with open(os.path.join(changed_root, report_rel), "w", encoding="utf-8") as handle:
        handle.write("数据结构 报告在确认前被改过\n")
    changed_sha = sha256(os.path.join(changed_root, report_rel))
    MESSAGES.clear()

    confirm(hwin, True)
    process(0.1)
    report_target = os.path.join(
        changed_root, "大二上/数据结构/实验/报告/数据结构实验报告A.md")
    code_target = os.path.join(
        changed_root, "大二上/数据结构/实验/代码/数据结构实验代码A.py")
    ch_batches = batches(changed_root)
    ch_actions = actions(changed_root, ch_batches[-1].batch_id) if ch_batches else []
    report_action = next((a for a in ch_actions if a.source_rel == report_rel), None)
    code_action = next((a for a in ch_actions if a.source_rel == code_rel), None)
    rec("S615",
        "确认前改过的文件执行时被识别为内容变化并跳过，未移动",
        os.path.isfile(os.path.join(changed_root, report_rel))
        and sha256(os.path.join(changed_root, report_rel)) == changed_sha
        and not os.path.exists(report_target)
        and report_action is not None and report_action.status == "skipped"
        and "内容已变化" in (report_action.error or ""),
        f"报告动作={None if report_action is None else (report_action.status, report_action.error)} "
        f"报告仍在原处={os.path.isfile(os.path.join(changed_root, report_rel))}")

    rec("S616",
        "同一批次里未变化的代码正常移动，历史分别记录 moved / skipped",
        os.path.isfile(code_target)
        and not os.path.exists(os.path.join(changed_root, code_rel))
        and code_action is not None and code_action.status == "moved",
        f"代码动作={None if code_action is None else (code_action.status, code_action.target_rel)}")

    hwin.on_undo()
    wait_until(lambda: not hwin._working and not hwin._plan_working, 60)
    process(0.05)
    rec("S617",
        "撤销只恢复实际移动的文件；被改过的文件保持用户改后的内容",
        os.path.isfile(os.path.join(changed_root, code_rel))
        and not os.path.exists(code_target)
        and sha256(os.path.join(changed_root, report_rel)) == changed_sha
        and not os.path.exists(report_target),
        f"代码回原位={os.path.isfile(os.path.join(changed_root, code_rel))} "
        f"报告SHA保持改动后={sha256(os.path.join(changed_root, report_rel)) == changed_sha}")
    close_window(hwin)

    # ---------------- 5. 窗口冒烟：入口按钮、表格文本、取消/应用 ----------------
    from agent_dialog import AgentDialog
    swin = open_window(make_normal_space("smoke"))
    swin.on_agent()
    sdlg = swin._agent_dialog
    rec("S618",
        "主界面 AI 管家入口可打开非模态窗口，并显示输入框与两个结果表",
        isinstance(sdlg, AgentDialog) and sdlg.instruction_edit.isEnabled()
        and sdlg.hits_table.columnCount() == 4
        and sdlg.plan_table.columnCount() == 4,
        f"窗口={type(sdlg).__name__} 命中列={sdlg.hits_table.columnCount()} "
        f"方案列={sdlg.plan_table.columnCount()}")
    sdlg.set_instruction(INSTRUCTION)
    sdlg.start_search()
    wait_until(lambda: sdlg._searching is False, 90)
    first_hit = sdlg.hits_table.item(0, 2).text() if sdlg.hits_table.rowCount() else ""
    rec("S619",
        "窗口里能读到每个文件的命中理由，并在未确认前禁用应用按钮之外的写操作",
        sdlg.hits_table.rowCount() >= 3 and "数据结构" in first_hit
        and "现有目录" in first_hit,
        f"命中行={sdlg.hits_table.rowCount()} 首行理由={first_hit!r}")
    sdlg.reject()
    process(0.05)
    close_window(swin)

    # ---------------- 汇总 ----------------
    failed = [c for c in CHECKS if not c[2]]
    print()
    print(f"未实测：真实 DeepSeek 调用、Windows 原生窗口、跨卷移动、"
          f"NAS/移动端、Mac 系统辅助功能点击。")
    print(f"合计 {len(CHECKS)} 项：通过 {len(CHECKS) - len(failed)} / "
          f"未通过 {len(failed)} / 未测 5")
    print("退出码: 0" if not failed else "退出码: 1")
    return 0 if not failed else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        print(f"合计 {len(CHECKS)} 项：通过 {sum(1 for c in CHECKS if c[2])} / "
              f"未通过 {max(1, len(CHECKS) - sum(1 for c in CHECKS if c[2]))} / 未测 5")
        print("退出码: 1")
        code = 1
    sys.exit(code)
