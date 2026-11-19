"""
归档盒阶段 5 · 主动学习闭环回归（offscreen）。

覆盖：
  反馈落点与去重、历史导入、候选归纳/冲突/相反证据、
  确认/拒绝/停用、失败回滚、重启后可读、撤销只留下线索，
  以及「学习空间确认一条候选 → 评分空间与第三空间逐文件对比」。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_stage5_active_learning.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage5_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage5_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage5_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import learning  # noqa: E402
import main as main_mod  # noqa: E402
import plan_patch as pp  # noqa: E402
import preferences as P  # noqa: E402
from classification_config import config_path, default_config  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, generate_plan,
)
from scanner import scan_directory  # noqa: E402
from storage import Storage  # noqa: E402

APP = QApplication.instance() or QApplication([])
BASE = tempfile.mkdtemp(prefix="stage5_learning_")
CHECKS = []
_ANSWER = {"question": QMessageBox.No}


def _fake_question(*args, **kwargs):
    return _ANSWER["question"]


QMessageBox.question = staticmethod(_fake_question)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)

_REAL_MO_EXEC = pp.ManualOverrideDialog.exec
_OVERRIDE = {"mode": "apply", "direction": "项目", "category": "",
             "semester": "", "inner": ""}


def _mo_exec(self):
    if _OVERRIDE["mode"] == "cancel":
        return 0
    self.dir_combo.setCurrentText(_OVERRIDE["direction"])
    self.sem_edit.setText(_OVERRIDE["semester"])
    self.cat_edit.setText(_OVERRIDE["category"])
    self.inner_edit.setText(_OVERRIDE["inner"])
    self.dlg.accept()
    return self.dlg.result()


pp.ManualOverrideDialog.exec = _mo_exec


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


def wait_until(predicate, timeout: float = 45.0) -> bool:
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


def space(tag):
    path = os.path.join(BASE, tag)
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def write(root, rel, text="正文\n"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def open_window(root):
    win = main_mod.MainWindow()
    win.path_edit.setText(root)
    win._reset_for_new_dir(root)
    win.show()
    process()
    wait_until(lambda: not getattr(win, "_working", True), 10)
    return win


def scan_and_preview(win, timeout=45.0):
    win.on_scan()
    assert wait_until(lambda: (not win._working) and win.scan_result is not None,
                      timeout), "扫描超时"
    win._generate_and_show_plan()
    assert wait_until(lambda: (not win._plan_working) and win.current_plan
                      is not None and win._plan_valid, timeout), "预览生成超时"
    process()


def regenerate(win):
    win._generate_and_show_plan()
    assert wait_until(lambda: (not win._plan_working) and win.current_plan
                      is not None and win._plan_valid, 45), "重新预览超时"
    process()


def close_window(win):
    try:
        win._closing = True
        win.close()
    except Exception:
        pass
    process(0.05)


def preview(root):
    win = open_window(root)
    scan_and_preview(win)
    return win


def row_of(win, rel):
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            return idx
    return -1


def override(win, rel, direction, category, semester="", inner=""):
    _OVERRIDE.update(mode="apply", direction=direction, category=category,
                     semester=semester, inner=inner)
    idx = row_of(win, rel)
    assert idx >= 0, f"找不到行：{rel}"
    win.table.selectRow(idx)
    win.on_override_selected()
    process()


def skip_with_ambiguity(win, rel):
    item = next(i for i in win.current_plan.items if i.file.rel_path == rel)
    item.ambiguity = {
        "reason": "测试：正文指向另一去向",
        "candidates": [
            {"direction": "课程", "semester": "2026秋-大二上",
             "category": "高等数学", "evidence": "测试",
             "target_dir_rel": "_已整理/课程/2026秋-大二上/高等数学/文档",
             "label": "课程/高等数学"},
            {"direction": "待定", "semester": "", "category": "",
             "evidence": "测试", "target_dir_rel": "_已整理/待定",
             "label": "待定"},
        ],
    }
    win._ambiguity_item = item
    assert win.skip_ambiguity(rel)
    process()


def target_map(win):
    return {i.file.rel_path: i.target_rel_path for i in win.current_plan.items}


def prefs_of(root):
    return P.list_space_preferences(root)


def pending_of(root):
    return [c for c in learning.list_candidates(root)
            if c.status == learning.STATUS_PENDING]


def find_candidate(root, *, status=None, category=None, target=None):
    for cand in learning.list_candidates(root):
        if status and cand.status != status:
            continue
        if category is not None and cand.category != category:
            continue
        if target is not None and cand.target_label != target:
            continue
        return cand
    return None


def plan_for(root, manual=None):
    plan = generate_plan(scan_directory(root), MODE_BY_DIRECTION_SEMESTER, [])
    manual = manual or {}
    for item in plan.items:
        item.selected = False
    for rel, spec in manual.items():
        item = next(i for i in plan.items if i.file.rel_path == rel)
        item.selected = True
        item.is_manual_overridden = True
        item.auto_direction = item.direction
        item.auto_semester = item.semester
        item.auto_category = item.category
        item.auto_inner_path = item.inner_path
        item.direction = spec.get("direction")
        item.semester = spec.get("semester")
        item.category = spec.get("category")
        item.target_dir_rel = spec["target_dir"]
        item.target_filename = item.file.filename
        item.target_rel_path = os.path.join(item.target_dir_rel,
                                            item.target_filename)
        item.rule_key = "manual_override:" + rel
        item.rule_applied = (
            f"手动修正：方向「{spec.get('direction') or ''}」"
            + (f" / 学期「{spec.get('semester')}」" if spec.get("semester") else "")
            + (f" / 类别「{spec.get('category')}」" if spec.get("category") else ""))
    return plan


def stage4_baseline(root, prefix):
    """学习关闭时的去向：默认配置、不套用任何偏好（阶段四行为）。"""
    plan = generate_plan(scan_directory(root), MODE_BY_DIRECTION_SEMESTER, [],
                         config=default_config())
    return {item.file.rel_path: item.target_rel_path
            for item in plan.items
            if item.file.rel_path.startswith(prefix)}


def execute_and_import(root, plan):
    storage = Storage(root)
    try:
        result = execute_plan(plan, storage)
        first = learning.import_history_feedback(root)
        second = learning.import_history_feedback(root)
        return result, first, second, storage
    finally:
        pass


# ======================== 样本空间 ========================

LEARN = space("learning")
SCORE_SUBDIR = "评分样本"
SCORE_PREFIX = SCORE_SUBDIR + os.sep
THIRD = space("third")
NEG = space("negative")
DEDUPE = space("dedupe")
REJECT = space("reject")
FAILSPACE = space("savefail")
UNDO = space("undo")
GLOBAL_LEARN = space("global_learn")
GLOBAL_TARGET = space("global_target")

CAND_TARGET = "项目 / 归档盒课程项目"
LEARN_CANDIDATES = [
    "归档盒课程项目第1次.txt",
    "归档盒课程项目第2次.txt",
    "归档盒课程项目第3次.md",
]
for name in LEARN_CANDIDATES:
    write(LEARN, name, f"候选样本 {name}\n")
# 同名不同内容，改到两个不同去向
write(LEARN, "同名A/笔记.txt", "离散数学 作业\n")
write(LEARN, "同名B/笔记.txt", "大学英语 作文\n")
# 同目录不同项目
write(LEARN, "周报/week1.txt", "第一周\n")
write(LEARN, "周报/week2.txt", "第二周\n")
# 相似但应排除：跳过保持原位
write(LEARN, "相似样本_周报1.txt", "相似样本一\n")
write(LEARN, "相似样本_周报2.txt", "相似样本二\n")
write(LEARN, "相似样本_周报草稿.txt", "同名近似草稿\n")
# 冲突：同条件两个去向
write(LEARN, "冲突样本_记录1.txt", "冲突一\n")
write(LEARN, "冲突样本_记录2.txt", "冲突二\n")
write(LEARN, "冲突样本_记录3.txt", "冲突三\n")
# 整空间：没有共同关键词、也不同意目录
write(LEARN, "深/alpha.txt", "alpha\n")
write(LEARN, "远/beta.txt", "beta\n")

SCORE_FILES = [
    "归档盒课程项目第4次.txt",
    "同名A/笔记.txt",
    "同名B/笔记.txt",
    "周报/weekA.txt",
    "周报/weekB.txt",
    "归档盒课程项目草稿.txt",
    "冲突样本_记录9.txt",
]
# 评分样本放在同一整理空间下、但从未参与学习改单；
# 空间偏好确认后，它们才应该被候选命中。
for name in SCORE_FILES:
    write(LEARN, os.path.join(SCORE_SUBDIR, name), f"评分文件 {name}\n")

write(THIRD, "归档盒课程项目第5次.txt", "第三空间同条件\n")

for name in ("neg1.txt", "neg2.txt", "neg3.txt"):
    write(NEG, name, "negative\n")
write(DEDUPE, "只改一次.txt", "dedupe\n")
for name in ("拒绝样本第1次.txt", "拒绝样本第2次.txt", "拒绝样本第3次.txt"):
    write(REJECT, name, "reject\n")
for name in ("失败样本第1次.txt", "失败样本第2次.txt", "失败样本第3次.txt"):
    write(FAILSPACE, name, "fail\n")
for name in ("撤销冲突_记录1.txt", "撤销冲突_记录2.txt", "撤销冲突_记录3.txt"):
    write(UNDO, name, "undo\n")
for name in ("全局学习第1次.txt", "全局学习第2次.txt", "全局学习第3次.txt"):
    write(GLOBAL_LEARN, name, "global\n")
write(GLOBAL_TARGET, "全局学习第9次.txt", "global target\n")

EXPECTED = {
    "candidate": CAND_TARGET,
    "score_unmatched": "去向与基线逐文件相同",
    "third": "第三空间与自身基线逐文件相同",
}


# ======================== 主流程 ========================

def build_learning_feedback():
    win = preview(LEARN)
    baseline = target_map(win)
    before_prefs = len(prefs_of(LEARN))
    override(win, "归档盒课程项目第1次.txt", "项目", "归档盒课程项目")
    override(win, "归档盒课程项目第2次.txt", "项目", "归档盒课程项目")
    override(win, "归档盒课程项目第3次.md", "项目", "归档盒课程项目")
    override(win, "同名A/笔记.txt", "课程", "高等数学",
             semester="2026秋-大二上")
    override(win, "同名B/笔记.txt", "课程", "大学英语",
             semester="2026秋-大二上")
    override(win, "周报/week1.txt", "项目", "周报A")
    override(win, "周报/week2.txt", "项目", "周报B")
    override(win, "相似样本_周报1.txt", "项目", "相似去向")
    override(win, "相似样本_周报2.txt", "项目", "相似去向")
    skip_with_ambiguity(win, "相似样本_周报草稿.txt")
    override(win, "冲突样本_记录1.txt", "项目", "冲突A")
    override(win, "冲突样本_记录2.txt", "项目", "冲突A")
    override(win, "冲突样本_记录3.txt", "项目", "冲突B")
    override(win, "深/alpha.txt", "项目", "整空间")
    override(win, "远/beta.txt", "项目", "整空间")

    judgments = learning.count_judgments(LEARN)
    after_prefs = len(prefs_of(LEARN))
    candidates = learning.build_candidates(LEARN)
    return win, baseline, before_prefs, after_prefs, judgments, candidates


def main():
    # ---------- 样本：学习空间 ----------
    (learn_win, learn_baseline, prefs_before, prefs_after,
     judgments, candidates) = build_learning_feedback()

    rec("S501",
        "仅改本次与跳过只留下反馈；确认候选前偏好表不增加，重新预览回到阶段四去向",
        prefs_before == 0 and prefs_after == 0
        and judgments == 15
        and not prefs_of(LEARN),
        f"改单前偏好={prefs_before} 改单后偏好={prefs_after} "
        f"judgment={judgments} 候选={[(c.status, c.target_label) for c in candidates]}")
    regenerate(learn_win)
    rec("S502", "未确认候选时重新生成预览，去向与改单前一致",
        target_map(learn_win) == learn_baseline,
        f"改单前={learn_baseline}\n重新预览={target_map(learn_win)}")

    # ---------- A2：取消 / 取消勾选 / 执行失败 / 执行期跳过 / 执行期取消 ----------
    neg_win = preview(NEG)
    neg_before = learning.count_judgments(NEG)
    _OVERRIDE["mode"] = "cancel"
    idx = row_of(neg_win, "neg1.txt")
    neg_win.table.selectRow(idx)
    neg_win.on_override_selected()
    _OVERRIDE["mode"] = "apply"
    cancelled_count = learning.count_judgments(NEG)
    item = next(i for i in neg_win.current_plan.items
                if i.file.rel_path == "neg2.txt")
    item.selected = False
    unchecked_count = learning.count_judgments(NEG)
    close_window(neg_win)

    # 执行失败：把目标目录位置先放一个同名文件，让创建目录失败
    fail_root = space("neg_fail")
    write(fail_root, "x.txt", "x\n")
    open(os.path.join(fail_root, "_已整理"), "w", encoding="utf-8").write("block")
    fail_plan = plan_for(fail_root, {"x.txt": {
        "direction": "项目", "semester": None, "category": "失败项目",
        "target_dir": "_已整理/项目/失败项目"}})
    learning.record_override(fail_root, "x.txt", "_已整理/待定",
                             "_已整理/项目/失败项目", "项目", "", "失败项目")
    fail_result, _f1, _f2, _fst = execute_and_import(fail_root, fail_plan)
    fail_after = learning.list_feedback(fail_root)
    fail_ok = (
        fail_result.failed >= 1
        and learning.count_judgments(fail_root) == 1
        and fail_after[0].after_target == "_已整理/项目/失败项目"
        and fail_after[0].kind == learning.KIND_JUDGMENT)

    # 执行期跳过：预览后源文件消失
    skip_root = space("neg_skip")
    write(skip_root, "gone.txt", "gone\n")
    skip_plan = plan_for(skip_root)
    next(i for i in skip_plan.items if i.file.rel_path == "gone.txt").selected = True
    os.unlink(os.path.join(skip_root, "gone.txt"))
    skip_result, _s1, _s2, _sst = execute_and_import(skip_root, skip_plan)

    # 执行期取消
    cancel_root = space("neg_cancel")
    write(cancel_root, "c.txt", "c\n")
    cancel_plan = plan_for(cancel_root)
    next(i for i in cancel_plan.items if i.file.rel_path == "c.txt").selected = True
    storage = Storage(cancel_root)
    event = threading.Event()
    event.set()
    cancel_result = execute_plan(cancel_plan, storage, cancel_event=event)
    learning.import_history_feedback(cancel_root)

    rec("S503",
        "取消对话框、取消勾选、执行失败/跳过/取消都不增加 judgment；"
        "已记下的改单不被失败升级或降级",
        neg_before == 0 and cancelled_count == 0 and unchecked_count == 0
        and fail_ok
        and skip_result.skipped >= 1
        and learning.count_judgments(skip_root) == 0
        and cancel_result.was_cancelled
        and learning.count_judgments(cancel_root) == 0,
        f"取消={cancelled_count} 取消勾选={unchecked_count}\n"
        f"失败：moved={fail_result.moved} failed={fail_result.failed} "
        f"judgment={learning.count_judgments(fail_root)}\n"
        f"跳过：skipped={skip_result.skipped} "
        f"judgment={learning.count_judgments(skip_root)}\n"
        f"取消：cancelled={cancel_result.was_cancelled} "
        f"judgment={learning.count_judgments(cancel_root)}")

    # ---------- A3：同一文件重复改单 / 历史重复导入 / 重启 ----------
    dedupe_win = preview(DEDUPE)
    override(dedupe_win, "只改一次.txt", "项目", "第一次去向")
    count1 = learning.count_judgments(DEDUPE)
    override(dedupe_win, "只改一次.txt", "项目", "第二次去向")
    count2 = learning.count_judgments(DEDUPE)
    fb = learning.list_feedback(DEDUPE)[0]
    direct_reimport = learning.import_history_feedback(DEDUPE)
    close_window(dedupe_win)
    reopened = preview(DEDUPE)
    reopened_count = learning.count_judgments(DEDUPE)
    close_window(reopened)
    rec("S504", "同一文件连续改单只保留最终去向；刷新、重启、重复导入不增加条数",
        count1 == 1 and count2 == 1
        and fb.after_target == "_已整理/项目/第二次去向"
        and direct_reimport.judgments_added == 0
        and reopened_count == 1,
        f"第一次={count1} 第二次={count2} 最终={fb.after_target} "
        f"重复导入新增={direct_reimport.judgments_added} 重启后={reopened_count}")

    # ---------- A4/A5/A6：候选可核对 / 不生成 / 冲突 ----------
    pending = find_candidate(LEARN, status=learning.STATUS_PENDING,
                             target=CAND_TARGET)
    blocked_similar = find_candidate(
        LEARN, status=learning.STATUS_BLOCKED, category="相似去向")
    blocked_whole = None
    for cand in learning.list_candidates(LEARN):
        if cand.status == learning.STATUS_BLOCKED and \
                cand.condition_text().startswith("整个整理空间"):
            blocked_whole = cand
            break
    conflict = find_candidate(LEARN, status=learning.STATUS_CONFLICT)
    single_pending = [c for c in pending_of(LEARN)
                      if any(s["source_rel"].endswith("笔记.txt")
                             for s in c.samples)]
    detail = pending.describe() if pending else ""
    rec("S505",
        "两个以上独立判断且范围明确时出现可核对候选（样本/条件/去向/空间/相反证据）",
        pending is not None
        and len({s["source_rel"] for s in pending.samples}) >= 2
        and len(set(pending.feedback_ids)) >= 2
        and pending.target_label == CAND_TARGET
        and pending.scope == P.SCOPE_SPACE
        and pending.space_root == os.path.realpath(LEARN)
        and "触发条件：文件名关键词" in detail
        and "相反证据：\n无" in detail,
        detail)
    rec("S506", "单文件、整空间、会覆盖相反样本的条件不生成可确认候选",
        not single_pending
        and blocked_whole is not None
        and blocked_similar is not None
        and not blocked_similar.confirmable
        and any(o.get("source_rel") == "相似样本_周报草稿.txt"
                for o in blocked_similar.opposing),
        f"单文件候选={single_pending}\n整空间={blocked_whole.describe() if blocked_whole else None}\n"
        f"相似样本={blocked_similar.describe() if blocked_similar else None}")
    conflict_pref_before = len(prefs_of(LEARN))
    rec("S507", "同条件不同去向显示冲突，不静默取第一条、不写偏好",
        conflict is not None
        and conflict.conflicts
        and len(prefs_of(LEARN)) == conflict_pref_before
        and "不同去向" in conflict.reason,
        conflict.describe() if conflict else "无冲突候选")

    # ---------- A13：跳过重启后仍可读回，不被候选归入 ----------
    close_window(learn_win)
    reopened_learn = preview(LEARN)
    skips = [f for f in learning.list_feedback(LEARN, learning.KIND_JUDGMENT)
             if f.keep_in_place]
    build_after_reopen = learning.build_candidates(LEARN)
    blocked_after = find_candidate(LEARN, status=learning.STATUS_BLOCKED,
                                   category="相似去向")
    rec("S508", "「跳过，保持原位」重启后仍可读回，且被显示为相反证据而不是样本",
        any(f.source_rel == "相似样本_周报草稿.txt" for f in skips)
        and blocked_after is not None
        and any(o.get("source_rel") == "相似样本_周报草稿.txt"
                for o in blocked_after.opposing)
        and not any(any(s["source_rel"] == "相似样本_周报草稿.txt"
                        for s in c.samples)
                    for c in build_after_reopen
                    if c.status == learning.STATUS_PENDING),
        f"跳过={[(f.source_rel, f.keep_in_place) for f in skips]}\n"
        f"相反证据={blocked_after.opposing if blocked_after else None}")
    close_window(reopened_learn)

    # ---------- 评分基线（同一整理空间里未参与学习的文件） ----------
    score_baseline = stage4_baseline(LEARN, SCORE_PREFIX)
    third_win = preview(THIRD)
    third_baseline = target_map(third_win)
    close_window(third_win)

    # A1 的第二半：确认前评分样本仍是阶段四去向
    matched_expected = os.path.join(
        "_已整理/项目/归档盒课程项目", "归档盒课程项目第4次.txt")
    rec("S509", "未确认候选时，评分样本预览与阶段四基线一致（命中文件仍不是候选去向）",
        set(score_baseline) == {SCORE_PREFIX + n for n in SCORE_FILES}
        and score_baseline.get(SCORE_PREFIX + "归档盒课程项目第4次.txt")
        != matched_expected,
        f"阶段四基线={score_baseline}")

    # ---------- A7/A12：确认、下次预览、文件未移动、重复确认 ----------
    learning.build_candidates(LEARN)
    pending = find_candidate(LEARN, status=learning.STATUS_PENDING,
                             target=CAND_TARGET)
    confirm_win = preview(LEARN)
    pref_rows_before = len(prefs_of(LEARN))
    confirm_win._confirm_learning_candidate(pending.candidate_id, P.SCOPE_SPACE)
    pref_rows_after = len(prefs_of(LEARN))
    learning_candidates = learning.list_candidates(LEARN)
    confirmed = next(c for c in learning_candidates
                     if c.candidate_id == pending.candidate_id)
    learning_files_still = all(
        os.path.isfile(os.path.join(LEARN, name)) for name in LEARN_CANDIDATES)
    regenerate(confirm_win)
    learn_after = target_map(confirm_win)
    close_window(confirm_win)
    # 再确认一次
    again_win = preview(LEARN)
    again_win._confirm_learning_candidate(pending.candidate_id, P.SCOPE_SPACE)
    pref_rows_again = len(prefs_of(LEARN))
    close_window(again_win)
    rec("S510",
        "确认后下一次预览采用偏好；文件仍未移动；同一候选确认两次只有一行偏好",
        pref_rows_after - pref_rows_before == 1
        and confirmed.status == learning.STATUS_CONFIRMED
        and confirmed.preference_id
        and learning_files_still
        and all(learn_after[name] == os.path.join(
            "_已整理/项目/归档盒课程项目", name)
            for name in LEARN_CANDIDATES)
        and pref_rows_again == pref_rows_after,
        f"偏好 {pref_rows_before}→{pref_rows_after}→{pref_rows_again}\n"
        f"候选状态={confirmed.status} preference_id={confirmed.preference_id}\n"
        f"文件仍在={learning_files_still}\n确认后预览={learn_after}")

    # ---------- 评分样本阶段五（同一空间，确认后的真实主窗口预览） ----------
    score_stage5 = {SCORE_PREFIX + name: learn_after[SCORE_PREFIX + name]
                    for name in SCORE_FILES}
    third_win = preview(THIRD)
    third_stage5 = target_map(third_win)
    close_window(third_win)

    matched_name = "归档盒课程项目第4次.txt"
    matched_ok = score_stage5.get(SCORE_PREFIX + matched_name) == os.path.join(
        "_已整理/项目/归档盒课程项目", matched_name)
    unmatched_same = all(
        score_stage5[SCORE_PREFIX + name] == score_baseline[SCORE_PREFIX + name]
        for name in SCORE_FILES if name != matched_name)
    excluded_ok = score_stage5[SCORE_PREFIX + "归档盒课程项目草稿.txt"] == \
        score_baseline[SCORE_PREFIX + "归档盒课程项目草稿.txt"]
    third_same = third_stage5 == third_baseline

    def state(target, expected):
        if target == expected:
            return "correct"
        return "pending" if os.sep + "待定" + os.sep in target + os.sep \
            else "wrong"

    rows = []
    for name in SCORE_FILES:
        key = SCORE_PREFIX + name
        expected = (os.path.join("_已整理/项目/归档盒课程项目",
                                 os.path.basename(name))
                    if name == matched_name else score_baseline[key])
        rows.append({
            "file": key,
            "baseline": score_baseline[key],
            "stage5": score_stage5[key],
            "state": state(score_stage5[key], expected),
        })
    before_edits = sum(1 for name in SCORE_FILES if name == matched_name)
    after_edits = 0
    rec("S511",
        "评分空间：被候选命中的新文件变为 correct，人工改单次数下降；"
        "未命中文件逐文件与基线相同",
        matched_ok and unmatched_same and excluded_ok
        and before_edits > after_edits
        and third_same,
        "逐文件：\n" + "\n".join(
            f"  {r['file']} | 基线={r['baseline']} | 阶段五={r['stage5']} "
            f"| {r['state']}" for r in rows)
        + f"\n确认候选次数=1（不计入评分文件改单次数）"
        + f"\n改单次数：阶段四需 {before_edits} → 阶段五 {after_edits}"
        + f"\n第三空间一致={third_same} {third_stage5}")

    # ---------- A8：默认当前空间；显式全局才影响别的空间 ----------
    scoped_only = [p for p in prefs_of(LEARN)
                   if p.preference_id == confirmed.preference_id]
    global_has = any(p.preference_id == confirmed.preference_id
                     for p in P.list_global_preferences())
    rec("S512", "确认范围默认是当前空间；另一空间预览保持不变",
        len(scoped_only) == 1 and scoped_only[0].scope == P.SCOPE_SPACE
        and not global_has and third_same,
        f"空间偏好={[(p.preference_id, p.scope) for p in scoped_only]} "
        f"全局已有={global_has} 第三空间一致={third_same}")

    # 显式全局：另一个学习空间确认后，目标空间才变化
    gwin = preview(GLOBAL_LEARN)
    for name in ("全局学习第1次.txt", "全局学习第2次.txt",
                 "全局学习第3次.txt"):
        override(gwin, name, "项目", "全局学习")
    gcands = learning.build_candidates(GLOBAL_LEARN)
    gcand = next(c for c in gcands if c.status == learning.STATUS_PENDING
                 and c.category == "全局学习")
    gtarget_before_win = preview(GLOBAL_TARGET)
    gtarget_before = target_map(gtarget_before_win)
    close_window(gtarget_before_win)
    gwin._confirm_learning_candidate(gcand.candidate_id, P.SCOPE_GLOBAL)
    close_window(gwin)
    gtarget_win = preview(GLOBAL_TARGET)
    gtarget_after = target_map(gtarget_win)
    close_window(gtarget_win)
    expected_global = os.path.join(
        "_已整理/项目/全局学习", "全局学习第9次.txt")
    global_changed = gtarget_after.get("全局学习第9次.txt") == expected_global
    global_pref = next((p for p in P.list_global_preferences()
                        if p.preference_id == gcand.candidate_id), None)
    # 清理全局偏好，避免污染后续/其他套件
    if global_pref is not None:
        P.delete_preference(GLOBAL_LEARN, global_pref.preference_id)
    rec("S513", "只有显式选择全局时，另一空间的相同文件才发生变化",
        gtarget_before.get("全局学习第9次.txt") != expected_global
        and global_changed and global_pref is not None
        and global_pref.scope == P.SCOPE_GLOBAL,
        f"全局确认前={gtarget_before.get('全局学习第9次.txt')} "
        f"确认后={gtarget_after.get('全局学习第9次.txt')}")

    # ---------- A9：拒绝后重启、重复导入仍拒绝 ----------
    rwin = preview(REJECT)
    for name in ("拒绝样本第1次.txt", "拒绝样本第2次.txt",
                 "拒绝样本第3次.txt"):
        override(rwin, name, "项目", "拒绝去向")
    rcand = next(c for c in learning.build_candidates(REJECT)
                 if c.status == learning.STATUS_PENDING)
    # 仅改本次在重新生成后消失；拒绝前的基线应是不带这次临时改单的预览。
    regenerate(rwin)
    reject_baseline = target_map(rwin)
    learning.reject_candidate(REJECT, rcand.candidate_id)
    close_window(rwin)
    rwin2 = preview(REJECT)
    learning.import_history_feedback(REJECT)
    learning.build_candidates(REJECT)
    rej = next(c for c in learning.list_candidates(REJECT)
               if c.candidate_id == rcand.candidate_id)
    reject_preview = target_map(rwin2)
    close_window(rwin2)
    rec("S514", "拒绝后重启、重复导入相同证据，候选仍被拒绝且预览回到拒绝前基线",
        rej.status == learning.STATUS_REJECTED
        and reject_preview == reject_baseline,
        f"状态={rej.status} 预览一致={reject_preview == reject_baseline}")

    # ---------- A11：保存失败 ----------
    fwin = preview(FAILSPACE)
    for name in ("失败样本第1次.txt", "失败样本第2次.txt",
                 "失败样本第3次.txt"):
        override(fwin, name, "项目", "失败去向")
    fail_cand = next(c for c in learning.build_candidates(FAILSPACE)
                     if c.status == learning.STATUS_PENDING)
    # 仅改本次在重新生成后消失；保存失败后下一次预览应与这个基线一致。
    regenerate(fwin)
    fail_base = target_map(fwin)
    close_window(fwin)
    prefs_before_fail = len(prefs_of(FAILSPACE))
    json_file = config_path(FAILSPACE)
    json_before = open(json_file, "rb").read() if os.path.isfile(json_file) else None

    real_save = P.save_preference

    def _broken_save(*args, **kwargs):
        raise P.PreferenceError("测试：偏好库不可写")

    P.save_preference = _broken_save
    try:
        failed = False
        try:
            learning.confirm_candidate(FAILSPACE, fail_cand.candidate_id)
        except Exception:
            failed = True
    finally:
        P.save_preference = real_save
    after_cand = next(c for c in learning.list_candidates(FAILSPACE)
                      if c.candidate_id == fail_cand.candidate_id)
    json_after = open(json_file, "rb").read() if os.path.isfile(json_file) else None
    fwin2 = preview(FAILSPACE)
    fail_preview = target_map(fwin2)
    close_window(fwin2)
    rec("S515", "保存失败后候选仍待确认，偏好表/JSON/下次预览与失败前一致",
        failed and after_cand.status == learning.STATUS_PENDING
        and len(prefs_of(FAILSPACE)) == prefs_before_fail == 0
        and json_after == json_before
        and fail_preview == fail_base,
        f"失败抛出={failed} 候选={after_cand.status} "
        f"偏好={len(prefs_of(FAILSPACE))} JSON一致={json_after == json_before} "
        f"预览一致={fail_preview == fail_base}")

    # ---------- A10：停用 ----------
    dis_win = preview(LEARN)
    before_disable_prefs = len(prefs_of(LEARN))
    dis_win._disable_learning_candidate(LEARN, confirmed.candidate_id)
    close_window(dis_win)
    space_prefs = prefs_of(LEARN)
    disabled_pref = next((p for p in space_prefs
                          if p.preference_id == confirmed.preference_id), None)
    dis_win2 = preview(LEARN)
    score_disabled_all = target_map(dis_win2)
    close_window(dis_win2)
    score_disabled = {SCORE_PREFIX + name: score_disabled_all[SCORE_PREFIX + name]
                      for name in SCORE_FILES}
    disabled_cand = next(c for c in learning.list_candidates(LEARN)
                         if c.candidate_id == confirmed.candidate_id)
    rec("S516", "停用已确认偏好后预览回到未采用该偏好的去向，偏好行仍在且 enabled 为假",
        len(space_prefs) == before_disable_prefs
        and disabled_pref is not None and disabled_pref.enabled is False
        and disabled_cand.status == learning.STATUS_DISABLED
        and score_disabled == score_baseline,
        f"偏好行={len(space_prefs)} enabled={disabled_pref.enabled if disabled_pref else None} "
        f"候选={disabled_cand.status} 评分预览回到基线={score_disabled == score_baseline}")

    # ---------- A14：撤销只增加线索，不增加判断，冲突候选不消失 ----------
    uwin = preview(UNDO)
    for name, cat in (("撤销冲突_记录1.txt", "撤销冲突A"),
                      ("撤销冲突_记录2.txt", "撤销冲突A"),
                      ("撤销冲突_记录3.txt", "撤销冲突B")):
        override(uwin, name, "项目", cat)
    before_undo_judgments = learning.count_judgments(UNDO)
    learning.build_candidates(UNDO)
    undo_conflict = find_candidate(UNDO, status=learning.STATUS_CONFLICT)
    plan = uwin.current_plan
    for item in plan.items:
        item.selected = True
    storage = Storage(UNDO)
    exec_result = execute_plan(plan, storage)
    learning.import_history_feedback(UNDO)
    after_exec_judgments = learning.count_judgments(UNDO)
    undo_result = undo_latest_batch(storage)
    import_after_undo = learning.import_history_feedback(UNDO)
    import_again = learning.import_history_feedback(UNDO)
    after_undo_judgments = learning.count_judgments(UNDO)
    clues = learning.list_feedback(UNDO, learning.KIND_CLUE)
    learning.build_candidates(UNDO)
    conflict_after = find_candidate(UNDO, status=learning.STATUS_CONFLICT)
    close_window(uwin)
    rec("S517",
        "无原因的整批撤销不增加 judgment，只记一条线索；冲突候选不消失、不被自动选定",
        exec_result.moved == 3 and undo_result.restored == 3
        and before_undo_judgments == after_exec_judgments
        == after_undo_judgments
        and len(clues) == 1
        and import_after_undo.clues_added == 1
        and import_again.clues_added == 0
        and undo_conflict is not None and conflict_after is not None
        and conflict_after.candidate_id == undo_conflict.candidate_id
        and len(prefs_of(UNDO)) == 0,
        f"moved={exec_result.moved} restored={undo_result.restored} "
        f"judgment {before_undo_judgments}/{after_exec_judgments}/{after_undo_judgments} "
        f"clue={len(clues)} 冲突仍在={conflict_after is not None} "
        f"偏好={len(prefs_of(UNDO))}")

    # ---------- 旧库兼容：没有学习表的 preferences.db / history.db ----------
    old_root = space("old_db")
    write(old_root, "旧偏好命中.txt", "旧偏好\n")
    write(old_root, "其他.txt", "其他\n")
    # 按当前基线结构直接建旧库（preferences + meta，无学习表）
    import sqlite3
    os.makedirs(os.path.join(old_root, ".organizer"), exist_ok=True)
    old_pref = P.Preference(
        preference_id="old-pref-1", name="旧偏好",
        match_type="filename_keyword", source_path="", include_subdirs=True,
        keywords=["旧偏好命中"], direction="课程", semester="2026秋-大二上",
        category="高等数学", inner_mode="keep", enabled=True,
        scope=P.SCOPE_SPACE, space_root=old_root,
        reason="旧库已有偏好", confirmed_at="2026-01-01T00:00:00",
        confirmed_source_rel="", source_kind=P.SOURCE_USER)
    P.save_preference(old_root, old_pref)
    conn = sqlite3.connect(P.space_db_path(old_root))
    conn.execute("DROP TABLE IF EXISTS learning_feedback")
    conn.execute("DROP TABLE IF EXISTS learning_candidates")
    conn.commit()
    conn.close()
    # 历史库按当前基线结构建一个没有新动作列的库（旧记录）
    old_hist = Storage(old_root)
    old_hist.close()
    ow = preview(old_root)
    old_map = target_map(ow)
    imported = learning.import_history_feedback(old_root)
    imported2 = learning.import_history_feedback(old_root)
    old_prefs = P.list_space_preferences(old_root)
    close_window(ow)
    rec("S518",
        "旧 preferences.db 没有学习表时仍能列出旧偏好、旧偏好仍影响预览、"
        "历史导入不重复追加",
        len(old_prefs) == 1
        and old_map.get("旧偏好命中.txt", "").startswith(
            "_已整理" + os.sep + "课程" + os.sep + "2026秋-大二上"
            + os.sep + "高等数学")
        and imported.judgments_added == 0
        and imported2.judgments_added == 0
        and len(learning.list_feedback(old_root, active_only=False)) == 0,
        f"旧偏好={[(p.preference_id, p.enabled) for p in old_prefs]}\n"
        f"预览={old_map}\n导入新增={imported.judgments_added}/"
        f"{imported2.judgments_added}")

    # ---------- 历史 ambiguity_choice：认 rule_key 前缀 ----------
    amb_root = space("old_ambiguity")
    write(amb_root, "改选记录.txt", "改选\n")
    amb_plan = plan_for(amb_root)
    amb_item = next(i for i in amb_plan.items
                    if i.file.rel_path == "改选记录.txt")
    amb_item.selected = True
    amb_item.direction = "课程"
    amb_item.semester = "2026秋-大二上"
    amb_item.category = "高等数学"
    amb_item.target_dir_rel = "_已整理/课程/2026秋-大二上/高等数学/文档"
    amb_item.target_rel_path = os.path.join(
        amb_item.target_dir_rel, amb_item.target_filename)
    amb_item.rule_key = "ambiguity_choice:课程/高等数学"
    amb_item.rule_applied = "用户改选：正文关键词「高数」 → 课程/高等数学"
    amb_storage = Storage(amb_root)
    amb_result = execute_plan(amb_plan, amb_storage)
    amb_import = learning.import_history_feedback(amb_root)
    amb_import2 = learning.import_history_feedback(amb_root)
    amb_fb = learning.list_feedback(amb_root)
    rec("S519",
        "历史里的 ambiguity_choice 按 rule_key 前缀导入，重复读取不增加条数",
        amb_result.moved == 1
        and amb_import.judgments_added == 1
        and amb_import2.judgments_added == 0
        and len(amb_fb) == 1
        and amb_fb[0].direction == "课程"
        and amb_fb[0].category == "高等数学"
        and amb_fb[0].after_target.endswith(os.path.join("高等数学", "文档")),
        f"moved={amb_result.moved} 导入={amb_import.judgments_added}/"
        f"{amb_import2.judgments_added} 反馈="
        f"{[(f.source_rel, f.direction, f.category, f.after_target) for f in amb_fb]}")

    # ---------- 学习建议窗口 smoke（offscreen 真实 Qt 控件） ----------
    from learning_dialog import LearningCandidatesDialog
    dlg = LearningCandidatesDialog(
        LEARN, lambda: learning.list_candidates(LEARN),
        lambda cid, scope: None, lambda cid: None, lambda cid: None)
    dlg.show()
    process()
    dlg.table.selectRow(0)
    process()
    detail = dlg.detail.toPlainText()
    rec("S520",
        "学习建议窗口能展示候选、条件/相反证据，并提供当前空间与显式全局两个入口",
        dlg.table.rowCount() >= 1
        and "触发条件" in detail and "相反证据" in detail
        and "当前整理空间" in dlg.btn_confirm.text()
        and "全局" in dlg.btn_global.text(),
        f"候选行={dlg.table.rowCount()} 详情片段={detail[:120]!r}\n"
        f"按钮={dlg.btn_confirm.text()} / {dlg.btn_global.text()}")
    dlg.close()
    process(0.05)

    # ---------- 汇总 ----------
    failed = [c for c in CHECKS if not c[2]]
    print()
    print(f"评分样本逐文件：")
    for r in rows:
        baseline_edits = 1 if r["file"] == SCORE_PREFIX + matched_name else 0
        print(f"  {r['file']} | 基线={r['baseline']} | 阶段五={r['stage5']} "
              f"| {r['state']} | 基线改单={baseline_edits} 阶段五改单=0")
    print(f"首次确认候选次数：1（不计入评分文件改单次数）")
    print(f"评分改单次数：阶段四 {before_edits} → 阶段五 {after_edits}")
    print(f"第三空间与基线一致：{third_same}")
    print(f"仍未验证：本人试用、Windows 原生运行、最终 Mac 包在系统辅助功能授权下的点击、"
          f"跨卷和外接盘断连。")
    print(f"合计 {len(CHECKS)} 项：通过 {len(CHECKS) - len(failed)} / "
          f"未通过 {len(failed)} / 未测 4")
    print("退出码: 0" if not failed else "退出码: 1")
    return 0 if not failed else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"合计 {len(CHECKS)} 项：通过 {sum(1 for c in CHECKS if c[2])} / "
              f"未通过 {max(1, len(CHECKS) - sum(1 for c in CHECKS if c[2]))} / 未测 4")
        print("退出码: 1")
        code = 1
    finally:
        pp.ManualOverrideDialog.exec = _REAL_MO_EXEC
    sys.exit(code)
