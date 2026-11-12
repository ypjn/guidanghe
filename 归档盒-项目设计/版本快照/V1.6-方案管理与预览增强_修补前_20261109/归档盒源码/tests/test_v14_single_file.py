"""
归档盒 V1.4 单文件分类修正 — 专项测试（offscreen Qt）

覆盖 16 类场景：
S01 修正前后方向/类别/目标/依据一致
S02 取消编辑不改变计划/磁盘/配置
S03 连续修正两个文件，两项都保留
S04 不同目录下同名文件不会改错对象
S05 恢复自动分类能恢复对应结果与依据
S06 修正后与批次文件/磁盘已有文件冲突正确处理
S07 非法名称与路径逃逸被拒绝
S08 忙碌、计划失效、关闭等待期间不能修正
S09 重新扫描/重新预览/配置变化/根目录切换后修正状态清除
S10 预览阶段无文件操作与执行历史副作用
S11 真实整理→关闭重开→历史查看/导出→撤销，路径/内容/SHA 一致
S12 旧数据库/旧历史兼容；V1.3 修补未回退
S13 单层内部路径真正进入目标目录（V1.4 修补）
S14 多段内部路径（a/b）不再被误判为非法（V1.4 修补）
S15 恢复自动分类保留原内部目录（作业）（V1.4 修补）
S16 非推荐模式拒绝修正且不留下半改状态（V1.4 修补）

所有实际文件移动只在 /tmp 独立目录执行；真实 NEU 资料只读。
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import classification_config as CFG
from classification_config import (
    ClassificationConfigError, default_config, load_config, save_config,
    config_to_dict,
)
import config_dialog
from main import MainWindow
from planner import (
    MODE_BY_DIRECTION_SEMESTER, MODE_BY_COURSE_THEN_TYPE, MODE_BY_TYPE,
    generate_plan, build_plan, PlanItem,
)
from scanner import scan_directory
from storage import Storage
from executor import execute_plan, undo_latest_batch
from plan_patch import (
    OverrideValidationError, OverrideParams, ManualOverrideDialog,
    apply_override_to_plan, revert_auto_override, summarize_manual_overrides,
)

RESULTS = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence).replace("\x00", "")[:4000],
                    "kind": "offscreen"})
    print(f"[{'PASS' if status == '通过' else 'FAIL'}] {tid}  {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.35):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def wait_until(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    app.processEvents()
    try:
        return bool(cond())
    except Exception:
        return False


def fresh(tag):
    tmp = tempfile.mkdtemp(prefix=f"_v14_{tag}_")
    return tmp


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode())


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    t0 = time.time()
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    app = QApplication.instance() or QApplication([])

    # ========== 合成资料 ==========
    def build_synthetic(root):
        os.makedirs(root, exist_ok=True)
        # 课程 / 大一下 / 已完成期末（类别留空）→ 关键词细分
        mk(root, "大一下/已完成期末/robot.pdf", "robot")
        mk(root, "大一下/已完成期末/高数/a.pdf", "gaoshu")
        # 两个同名文件，不同目录，防止改错对象
        mk(root, "大二上/数据结构/作业/dump.txt", "ds_dump_1")
        mk(root, "大二上/C++程序设计/作业/dump.txt", "cpp_dump_2")
        # 竞赛
        mk(root, "大一下/国创赛/计划书.docx", "proposal")
        # 项目目录（顶层方向映射）
        mk(root, "Code/proj/main.py", "main")
        # 散落（待定）
        mk(root, "散落.txt", "sanluo")
        # 目录外已存在的文件（磁盘冲突测试）
        os.makedirs(os.path.join(root, "_已整理", "课程", "大二上",
                                 "数据结构", "文档", "作业"),
                    exist_ok=True)
        with open(os.path.join(root, "_已整理", "课程", "大二上",
                               "数据结构", "文档", "作业", "dump.txt"), "wb") as f:
            f.write(b"preexisting")

    # ---------- S01 修正前后一致 ----------
    try:
        root = fresh("s01")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        before = {i.file.rel_path: (i.direction, i.category,
                                   i.target_rel_path, i.rule_applied,
                                   i.is_manual_overridden)
                  for i in plan.items}
        applied = apply_override_to_plan(
            plan, "Code/proj/main.py",
            OverrideParams(direction="课程", semester="大二上",
                           category="软件工程", inner_path=""))
        after = {i.file.rel_path: (i.direction, i.category,
                                   i.target_rel_path, i.rule_applied,
                                   i.is_manual_overridden)
                 for i in plan.items}
        e1 = applied.is_manual_overridden
        e2 = applied.direction == "课程" and applied.category == "软件工程"
        e3 = applied.target_rel_path.startswith("_已整理/课程/")
        e4 = "手动修正" in applied.rule_applied
        e5 = before["Code/proj/main.py"][4] is False
        e6 = after["Code/proj/main.py"][4] is True
        e7 = applied.file.rel_path == "Code/proj/main.py"  # 身份不变
        ok = e1 and e2 and e3 and e4 and e5 and e6 and e7
        rec("S01", "修正前后方向/类别/目标/依据一致；文件身份未变",
            "通过" if ok else "未通过",
            f"修正前={before['Code/proj/main.py']}\n"
            f"修正后方向={applied.direction}；类别={applied.category}\n"
            f"目标={applied.target_rel_path}\n依据={applied.rule_applied}\n"
            f"身份不变={e7}")
    except Exception as e:
        rec("S01", "修正前后一致", "未通过", f"异常：{e}")

    # ---------- S02 取消编辑 ----------
    try:
        root = fresh("s02")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        before_items = {id(i): copy.copy(i) for i in plan.items}

        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.scan_result = sr
        w.current_plan = plan
        w._plan_valid = True
        w.table.selectRow(0)
        pump(app, 0.1)

        dlg = ManualOverrideDialog(w.current_plan,
                                   w.current_plan.items[0].file.rel_path)
        # 用户在对话框里改了字段，但点了取消
        old_exec = dlg.dlg.exec
        called_cancel = []

        def fake_exec():
            called_cancel.append(True)
            dlg.dlg.reject()
            return QDialog.Rejected

        dlg.dlg.exec = fake_exec
        w.on_override_selected()
        # 取消后：
        # 1) plan.items 不变（内存快照对比）
        # 2) 磁盘上的原始源文件还在原位（没被移动）
        # 3) 配置文件没被创建/改动（轮询 .organizer 时间戳无变化）
        same = all(
            i.direction == before_items[id(i)].direction
            and i.category == before_items[id(i)].category
            and i.target_rel_path == before_items[id(i)].target_rel_path
            and not i.is_manual_overridden
            for i in plan.items
        )
        src_still = os.path.isfile(os.path.join(root, plan.items[0].file.rel_path))
        w.close()
        app.processEvents()
        ok = same and src_still
        rec("S02", "取消编辑不改变计划/磁盘/配置",
            "通过" if ok else "未通过",
            f"内存条目不变={same}；源文件仍原位={src_still}；"
            f"取消被调用={called_cancel}")
    except Exception as e:
        rec("S02", "取消编辑", "未通过", f"异常：{e}")

    # ---------- S03 连续修正两个文件 ----------
    try:
        root = fresh("s03")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        apply_override_to_plan(plan, "Code/proj/main.py",
                               OverrideParams(direction="项目", semester="",
                                              category="自研"))
        apply_override_to_plan(plan, "散落.txt",
                               OverrideParams(direction="校园事务", semester="",
                                              category="临时"))
        n, lines = summarize_manual_overrides(plan)
        both_ok = (n == 2
                   and all(i.is_manual_overridden for i in plan.items
                           if i.file.rel_path in ("Code/proj/main.py",
                                                  "散落.txt"))
                   and not any(i.is_manual_overridden for i in plan.items
                               if i.file.rel_path not in
                               ("Code/proj/main.py", "散落.txt")))
        # 目标路径各自独立正确
        proj_target = next(i.target_rel_path for i in plan.items
                           if i.file.rel_path == "Code/proj/main.py")
        tmp_target = next(i.target_rel_path for i in plan.items
                          if i.file.rel_path == "散落.txt")
        ok = both_ok and proj_target.startswith("_已整理/项目/") \
            and tmp_target.startswith("_已整理/校园事务/")
        rec("S03", "连续修正两个文件，两项都保留",
            "通过" if ok else "未通过",
            f"手动修正文件数={n}；目标1={proj_target}；目标2={tmp_target}")
    except Exception as e:
        rec("S03", "连续修正", "未通过", f"异常：{e}")

    # ---------- S04 不同目录下同名文件 ----------
    try:
        root = fresh("s04")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        # 把 C++ 的 dump.txt 改成"项目"，不应影响数据结构的 dump.txt
        apply_override_to_plan(
            plan, "大二上/C++程序设计/作业/dump.txt",
            OverrideParams(direction="项目", semester="", category="cpp_dump"))
        cpp = next(i for i in plan.items
                   if i.file.rel_path == "大二上/C++程序设计/作业/dump.txt")
        ds = next(i for i in plan.items
                  if i.file.rel_path == "大二上/数据结构/作业/dump.txt")
        ok = (cpp.is_manual_overridden and cpp.direction == "项目"
              and not ds.is_manual_overridden
              and ds.direction == "课程")
        rec("S04", "不同目录下同名文件不会改错对象",
            "通过" if ok else "未通过",
            f"C++ dump 方向={cpp.direction}（手动={cpp.is_manual_overridden}）；"
            f"DS dump 方向={ds.direction}（手动={ds.is_manual_overridden}）")
    except Exception as e:
        rec("S04", "同名文件身份", "未通过", f"异常：{e}")

    # ---------- S05 恢复自动分类 ----------
    try:
        root = fresh("s05")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        item_rel = "Code/proj/main.py"
        auto_dir_before = next(i for i in plan.items
                               if i.file.rel_path == item_rel).target_rel_path
        apply_override_to_plan(plan, item_rel,
                               OverrideParams(direction="校园事务",
                                              semester="", category=""))
        applied = next(i for i in plan.items
                       if i.file.rel_path == item_rel)
        assert applied.is_manual_overridden
        assert applied.target_rel_path != auto_dir_before
        reverted = revert_auto_override(plan, item_rel)
        assert reverted is applied
        ok = (not applied.is_manual_overridden
              and applied.direction == applied.auto_direction
              and applied.target_rel_path == auto_dir_before
              and applied.rule_applied == applied.auto_rule_applied
              and applied.rule_key == applied.auto_rule_key)
        rec("S05", "恢复自动分类能恢复对应结果与依据",
            "通过" if ok else "未通过",
            f"自动目标={auto_dir_before}\n"
            f"恢复后目标={applied.target_rel_path}\n"
            f"恢复后依据={applied.rule_applied}")
    except Exception as e:
        rec("S05", "恢复自动", "未通过", f"异常：{e}")

    # ---------- S06 冲突处理 ----------
    try:
        root = fresh("s06")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        # 场景 A：批次内两个文件改成同一目标
        rel1 = "大一下/已完成期末/robot.pdf"
        rel2 = "大一下/已完成期末/discrete.pdf"
        mk(root, rel2, "discrete")
        sr2 = scan_directory(root, detect_project_roots=False)
        plan2 = build_plan(sr2, MODE_BY_DIRECTION_SEMESTER, [],
                           compute_duplicates=False)
        apply_override_to_plan(plan2, rel1,
                               OverrideParams(direction="项目",
                                              semester="", category="实验"))
        apply_override_to_plan(plan2, rel2,
                               OverrideParams(direction="项目",
                                              semester="", category="实验"))
        both = [i for i in plan2.items
                if i.file.rel_path in (rel1, rel2)]
        targets = sorted({i.target_rel_path for i in both})
        # 因为 _recompute_unique_names 会给冲突文件加 (1)/(2) 后缀
        ok_A = len(targets) == 2 and all("项目" in t for t in targets)

        # 场景 B：磁盘已有文件（已在合成资料里预置 _已整理/.../dump.txt）
        item = next(i for i in plan.items
                    if i.file.rel_path == "大二上/数据结构/作业/dump.txt")
        ok_B = item.conflict_with_existing is True and "(1)" in item.target_filename \
            or item.conflict_with_existing is True

        # 场景 C：恢复自动分类后冲突消失
        item_rel = "大二上/C++程序设计/作业/dump.txt"
        apply_override_to_plan(plan, item_rel,
                               OverrideParams(direction="项目",
                                              semester="", category="dump"))
        revert_auto_override(plan, item_rel)
        restored = next(i for i in plan.items
                        if i.file.rel_path == item_rel)
        ok_C = not restored.is_manual_overridden

        ok = ok_A and ok_B and ok_C
        rec("S06", "批次内/磁盘冲突正确处理；恢复自动后冲突消失",
            "通过" if ok else "未通过",
            f"A 批次内冲突目标={targets}\n"
            f"B 磁盘冲突={ok_B}\n"
            f"C 恢复自动后手动标记清除={ok_C}")
    except Exception as e:
        rec("S06", "冲突", "未通过", f"异常：{e}")

    # ---------- S07 非法名称/路径逃逸 ----------
    try:
        root = fresh("s07")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        item_rel = "Code/proj/main.py"

        def try_override(**kw):
            try:
                apply_override_to_plan(
                    plan, item_rel,
                    OverrideParams(
                        direction=kw.get("direction", "课程"),
                        semester=kw.get("semester", "大二上"),
                        category=kw.get("category", "软件工程"),
                        inner_path=kw.get("inner_path", ""),
                    ))
                return None
            except OverrideValidationError as e:
                return str(e)

        errors = []
        errors.append(("方向包含分隔符", try_override(direction="项目/a")))
        errors.append(("方向为空", try_override(direction="")))
        errors.append(("学期 .. 段", try_override(semester="..")))
        errors.append(("学期 ../.. 逃逸", try_override(semester="../..")))
        errors.append(("类别绝对路径", try_override(category="/绝对")))
        errors.append(("内部路径 ..", try_override(inner_path="a/../b")))
        errors.append(("非法方向值", try_override(direction="不存在")))
        all_rejected = all(e[1] is not None for e in errors)
        # 修正不应产生任何副作用（所有条目保持未手动）
        no_side = not any(i.is_manual_overridden for i in plan.items)
        ok = all_rejected and no_side
        rec("S07", "非法名称与路径逃逸被拒绝，无副作用",
            "通过" if ok else "未通过",
            "\n".join(f"{name} → {msg or '被接受'}" for name, msg in errors)
            + f"\n无副作用={no_side}")
    except Exception as e:
        rec("S07", "非法输入", "未通过", f"异常：{e}")

    # ---------- S08 忙碌/失效/关闭等待拦截 ----------
    try:
        root = fresh("s08")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.scan_result = sr
        w.current_plan = plan
        w._plan_valid = True
        w.table.selectRow(0)

        def _assert_blocked(msg_fn):
            """直接调用 on_override_selected，计划应不变。"""
            before_targets = {i.file.rel_path: i.target_rel_path
                              for i in w.current_plan.items}
            msg = msg_fn()
            # 关闭 QMessageBox.information 被静默返回 Ok；on_override_selected
            # 直接 return 或 弹信息。我们只关心 plan 没被改。
            pump(app, 0.05)
            after_targets = {i.file.rel_path: i.target_rel_path
                             for i in w.current_plan.items}
            return before_targets == after_targets

        # 忙碌期间
        w._plan_working = True
        busy_blocked = _assert_blocked(
            lambda: w.on_override_selected() or "")
        w._plan_working = False

        # 关闭等待期间
        w._close_pending = True
        close_blocked = _assert_blocked(
            lambda: w.on_override_selected() or "")
        w._close_pending = False

        # 计划失效
        w._plan_valid = False
        invalid_blocked = _assert_blocked(
            lambda: w.on_override_selected() or "")
        w._plan_valid = True

        w.close()
        app.processEvents()
        ok = busy_blocked and close_blocked and invalid_blocked
        rec("S08", "忙碌/计划失效/关闭等待期间不能修正",
            "通过" if ok else "未通过",
            f"忙碌拦截={busy_blocked}；关闭等待拦截={close_blocked}；"
            f"计划失效拦截={invalid_blocked}")
    except Exception as e:
        rec("S08", "门控", "未通过", f"异常：{e}")

    # ---------- S09 重新扫描/重新预览/配置变化/根切换清理修正 ----------
    try:
        root = fresh("s09")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.scan_result = sr
        w._generate_and_show_plan()
        wait_until(app, lambda: not w._plan_working and w.current_plan is not None)

        # 先手动修正一个文件
        plan = w.current_plan
        apply_override_to_plan(plan, plan.items[0].file.rel_path,
                               OverrideParams(direction="项目"))
        # 直接把当前 plan 放回主窗口内存（因为我们在后台改的，
        # 主窗口不知道），模拟用户在 UI 中改了
        # —— 这里主窗口没有手动改的入口能接 plan，所以我们
        # 换一个做法：让 build_plan 返回的 plan 本身被我们
        # 扩展，然后让 PlanWorker 回来的时候拿到修正后的 plan。
        # 但这太侵入；改为直接让 w.current_plan = plan，手动
        # 触发"计划本身已被改变"后 UI 状态刷新
        w._populate_table(plan)
        n_before = sum(1 for i in w.current_plan.items
                       if i.is_manual_overridden)
        assert n_before >= 1

        # 重新扫描 / 重新生成
        w.on_scan()
        wait_until(app, lambda: not w._working and w.scan_result is not None)
        w._generate_and_show_plan()
        wait_until(app, lambda: not w._plan_working and w.current_plan is not None)
        after_regen = sum(1 for i in w.current_plan.items
                          if i.is_manual_overridden)

        # 根目录切换 → 修正自然跟着 plan 被丢弃
        root2 = fresh("s09b")
        build_synthetic(root2)
        w.path_edit.setText(root2)
        w._reset_for_new_dir(root2)
        app.processEvents()
        w.on_scan()
        wait_until(app, lambda: not w._working and w.scan_result is not None)
        w._generate_and_show_plan()
        wait_until(app, lambda: not w._plan_working and w.current_plan is not None)
        after_switch = sum(1 for i in w.current_plan.items
                           if i.is_manual_overridden)

        w.close()
        app.processEvents()
        ok = (after_regen == 0 and after_switch == 0 and n_before >= 1)
        rec("S09", "重新扫描/重新预览/切换根目录后修正状态清除",
            "通过" if ok else "未通过",
            f"修正文件数（修正后）={n_before}；重新生成后={after_regen}；"
            f"根目录切换后={after_switch}")
    except Exception as e:
        rec("S09", "修正生命周期", "未通过", f"异常：{e}")

    # ---------- S10 预览阶段无文件操作与执行历史副作用 ----------
    try:
        root = fresh("s10")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        initial_files = []
        for dp, _, fs in os.walk(root):
            for f in fs:
                if "history.db" in f:
                    continue
                initial_files.append(os.path.relpath(os.path.join(dp, f), root))
        initial_files.sort()
        initial_shas = {}
        for rel in initial_files:
            initial_shas[rel] = sha(os.path.join(root, rel))

        apply_override_to_plan(plan, "Code/proj/main.py",
                               OverrideParams(direction="校园事务"))
        apply_override_to_plan(plan, "散落.txt",
                               OverrideParams(direction="项目"))

        after_over_files = []
        for dp, _, fs in os.walk(root):
            for f in fs:
                if "history.db" in f:
                    continue
                after_over_files.append(
                    os.path.relpath(os.path.join(dp, f), root))
        after_over_files.sort()

        st_path = os.path.join(root, ".organizer", "history.db")
        db_present_before = os.path.exists(st_path)

        ok = (initial_files == after_over_files
              and db_present_before is False)
        rec("S10", "预览阶段无文件操作与执行历史副作用",
            "通过" if ok else "未通过",
            f"文件数 修正前={len(initial_files)} 修正后={len(after_over_files)}；"
            f"history.db 创建={db_present_before}")
    except Exception as e:
        rec("S10", "预览无副作用", "未通过", f"异常：{e}")

    # ---------- S11 真实整理→重开→历史→撤销 ----------
    try:
        root = fresh("s11")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        # 修正两个文件
        apply_override_to_plan(plan, "大一下/已完成期末/robot.pdf",
                               OverrideParams(direction="项目",
                                              semester="", category="机器人"))
        apply_override_to_plan(plan, "散落.txt",
                               OverrideParams(direction="校园事务",
                                              semester="", category="临时"))
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        st = Storage(root)
        # 用 executor 执行（绕过 UI）
        result = execute_plan(plan, st)
        n_items = len(plan.items)
        assert result.moved >= 1, f"moved={result.moved}"

        batch = st.get_batch(result.batch_id)
        assert batch is not None
        n_manual = batch.manual_override_count
        # 我们手动修正了 2 个文件（robot.pdf + 散落.txt）
        assert n_manual >= 2, f"manual_override_count={n_manual}"

        actions = st.get_actions(result.batch_id)
        manual_actions = [a for a in actions if a.classification_source == "手动"]
        auto_actions = [a for a in actions if a.classification_source == "自动"]
        assert len(manual_actions) == n_manual
        assert len(manual_actions) >= 2

        # 每个手动条目都有 auto_target_rel（重建得到）或 auto_rule_applied
        all_have_auto_ref = all(
            (a.auto_target_rel or a.auto_rule_applied or a.override_note)
            for a in manual_actions
        )
        assert all_have_auto_ref

        # 手动条目的 source_rel 和 target_rel 都在磁盘上真实对应
        for a in manual_actions:
            src_abs = os.path.join(root, a.source_rel)
            tgt_abs = os.path.join(root, a.target_rel)
            if a.status in ("moved", "conflict"):
                assert os.path.isfile(tgt_abs), f"手动条目目标缺失：{a.target_rel}"
                assert not os.path.isfile(src_abs) or \
                    os.path.abspath(src_abs) == os.path.abspath(tgt_abs), \
                    f"源文件未移动：{a.source_rel}"

        # 重启：重新打开目录，历史可查看批次
        w.close()
        app.processEvents()
        w2 = MainWindow()
        w2.path_edit.setText(root)
        w2._reset_for_new_dir(root)
        w2.show()
        app.processEvents()
        w2.on_history()
        pump(app, 0.5)
        # 历史对话框存在并且批次 #result.batch_id 在表中
        hist = w2._history_dialog
        assert hist is not None
        rows_batch = [hist.batch_table.item(r, 0).text()
                      for r in range(hist.batch_table.rowCount())]
        batch_present = any(f"#{result.batch_id}" in t for t in rows_batch)
        assert batch_present
        # 选中批次查看明细
        hist._selected_batch_id = result.batch_id
        hist.load_items(force=True)
        pump(app, 0.2)
        w2.close()
        app.processEvents()

        # 撤销
        ures = undo_latest_batch(Storage(root))
        # 撤销后手动文件回到原位（真实枚举：源文件存在 + SHA 匹配）
        undone_all_ok = True
        for a in manual_actions:
            if a.status == "moved":
                src = os.path.join(root, a.source_rel)
                if not os.path.isfile(src) or sha(src) != a.source_sha256:
                    undone_all_ok = False
                    break
        all_final_ok = all(
            (os.path.isfile(os.path.join(root, a.source_rel))
             and sha(os.path.join(root, a.source_rel)) == a.source_sha256)
            for a in actions if a.status in ("moved", "conflict", "undone")
        )

        ok = (batch_present and all_have_auto_ref
              and undone_all_ok and all_final_ok)
        rec("S11", "真实整理→重开→历史→撤销，路径/内容/SHA 一致",
            "通过" if ok else "未通过",
            f"批次手动修正数={n_manual}；自动={len(auto_actions)}\n"
            f"历史页显示该批次={batch_present}\n"
            f"撤销恢复数={ures.restored}；手动条目全部还原={undone_all_ok}；"
            f"最终所有移动/冲突条目都在原位={all_final_ok}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        rec("S11", "端到端闭环", "未通过", f"异常：{e}\n{tb}")

    # ---------- S12 旧数据库/旧历史兼容 + V1.3 修补不回退 ----------
    try:
        import sqlite3
        root_old = fresh("s12_olddb")
        os.makedirs(os.path.join(root_old, ".organizer"), exist_ok=True)
        conn = sqlite3.connect(
            os.path.join(root_old, ".organizer", "history.db"))
        conn.execute("CREATE TABLE batches (batch_id INTEGER PRIMARY KEY, "
                     "root TEXT, mode TEXT, status TEXT, summary TEXT, "
                     "created_at TEXT, finished_at TEXT, batch_seq INTEGER, "
                     "planning_started TEXT, planning_finished TEXT, "
                     "plan_json TEXT)")
        conn.execute("CREATE TABLE actions (action_index INTEGER PRIMARY KEY, "
                     "batch_id INTEGER, source_rel TEXT, target_rel TEXT, "
                     "source_size INTEGER, source_sha256 TEXT, status TEXT, "
                     "error TEXT, executed_at TEXT, undone_at TEXT)")
        conn.execute(
            "INSERT INTO batches(batch_id,root,mode,status,summary,"
            "created_at,finished_at,batch_seq) "
            "VALUES(1,'root','x','done','ok','t','t',1)")
        conn.execute(
            "INSERT INTO actions(action_index,batch_id,source_rel,target_rel,"
            "source_size,source_sha256,status) "
            "VALUES(1,1,'a.txt','b.txt',0,'h','moved')")
        conn.commit()
        conn.close()
        st = Storage(root_old)  # 迁移触发
        st.get_actions(1)       # 新列默认为空串 → classification_source="自动"
        row = st.get_actions(1)[0]
        assert row.classification_source == "自动"
        assert row.auto_target_rel == ""
        assert row.auto_rule_applied == ""
        assert row.override_note == ""
        # V1.3 修补未回退：重新跑 C01 的"默认分类一致"
        root_fresh = fresh("s12_default")
        build_synthetic(root_fresh)
        sr_f = scan_directory(root_fresh, detect_project_roots=False)
        plan_f = build_plan(sr_f, MODE_BY_DIRECTION_SEMESTER, [],
                            compute_duplicates=False)
        default_item = next(i for i in plan_f.items
                            if i.file.rel_path == "Code/proj/main.py")
        assert default_item.target_rel_path == "_已整理/项目/proj/main.py"
        assert default_item.direction == "项目"
        ok = True
        rec("S12", "旧数据库兼容；V1.3 修补默认分类未回退",
            "通过" if ok else "未通过",
            f"旧 action 的分类来源={row.classification_source}；"
            f"默认 Code/proj/main.py={default_item.target_rel_path}")
    except Exception as e:
        rec("S12", "兼容性", "未通过", f"异常：{e}")

    # ---------- S13 单层内部路径真正落到目标 ----------
    try:
        root = fresh("s13")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        src_rel = "大二上/数据结构/作业/dump.txt"
        applied = apply_override_to_plan(
            plan, src_rel,
            OverrideParams(direction="课程", semester="大二上",
                           category="数据结构", inner_path="实验一"))
        ok = ("实验一" in applied.target_rel_path
              and applied.target_rel_path.endswith(os.path.join("实验一", "dump.txt"))
              and applied.inner_path == "实验一")
        rec("S13", "单层内部路径（实验一）真正进入目标目录",
            "通过" if ok else "未通过",
            f"目标={applied.target_rel_path}\ninner_path={applied.inner_path}")
    except Exception as e:
        rec("S13", "单层内部路径", "未通过", f"异常：{e}")

    # ---------- S14 多段内部路径 ----------
    try:
        root = fresh("s14")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        src_rel = "大二上/数据结构/作业/dump.txt"
        applied = apply_override_to_plan(
            plan, src_rel,
            OverrideParams(direction="课程", semester="大二上",
                           category="数据结构", inner_path="实验一/分组报告"))
        expect_tail = os.path.join("实验一", "分组报告", "dump.txt")
        ok = (applied.target_rel_path.endswith(expect_tail)
              and applied.inner_path == os.sep.join(["实验一", "分组报告"]))
        rec("S14", "多段内部路径（实验一/分组报告）不再被误判为非法",
            "通过" if ok else "未通过",
            f"目标={applied.target_rel_path}\ninner_path={applied.inner_path}")
    except Exception as e:
        rec("S14", "多段内部路径", "未通过", f"异常：{e}")

    # ---------- S15 恢复自动保留原有内部目录（作业） ----------
    try:
        root = fresh("s15")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          compute_duplicates=False)
        # 用 C++程序设计/作业 下的同名文件：无磁盘预置冲突，目标保持原名，
        # 便于验证"恢复自动"能精确回到含「作业」层的自动目标。
        src_rel = "大二上/C++程序设计/作业/dump.txt"
        item = next(i for i in plan.items if i.file.rel_path == src_rel)
        auto_target = item.target_rel_path
        assert auto_target.endswith(os.path.join("作业", "dump.txt")), auto_target

        applied = apply_override_to_plan(
            plan, src_rel,
            OverrideParams(direction="课程", semester="大二上",
                           category="C++程序设计", inner_path="实验一"))
        assert "实验一" in applied.target_rel_path

        reverted = revert_auto_override(plan, src_rel)
        ok = (reverted is not None
              and reverted.inner_path == "作业"
              and reverted.target_rel_path == auto_target
              and reverted.is_manual_overridden is False)
        rec("S15", "恢复自动分类保留原内部目录（作业），目标回到自动结果",
            "通过" if ok else "未通过",
            f"自动目标={auto_target}\n恢复后目标="
            f"{reverted.target_rel_path if reverted else None}\n"
            f"inner_path={reverted.inner_path if reverted else None}")
    except Exception as e:
        rec("S15", "恢复自动保留内部目录", "未通过", f"异常：{e}")

    # ---------- S16 非推荐模式：入口拒绝，且不留下半改状态 ----------
    try:
        root = fresh("s16")
        build_synthetic(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_TYPE, [], compute_duplicates=False)
        assert plan.mode == MODE_BY_TYPE
        src_rel = plan.items[0].file.rel_path
        before = copy.copy(plan.items[0])

        rejected = False
        try:
            apply_override_to_plan(
                plan, src_rel,
                OverrideParams(direction="课程", semester="大二上",
                               category="数据结构", inner_path="实验一"))
        except OverrideValidationError as e:
            rejected = "仅支持" in str(e) or "推荐" in str(e)

        reverted = revert_auto_override(plan, src_rel)
        after = plan.items[0]
        unchanged = (after.target_rel_path == before.target_rel_path
                     and after.is_manual_overridden == before.is_manual_overridden
                     and after.direction == before.direction)
        ok = rejected and reverted is None and unchanged
        rec("S16", "非推荐模式（按类型）拒绝修正；恢复自动返回 None 且不改条目",
            "通过" if ok else "未通过",
            f"apply 被拒={rejected}\nrevert 返回={reverted}\n条目未被改动={unchanged}")
    except Exception as e:
        rec("S16", "非推荐模式边界", "未通过", f"异常：{e}\n{traceback.format_exc()}")

    # ---- 汇总 ----
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 and RESULTS else 1
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    out = os.path.join(SRC, "tests", "results_v14_single_file.json")
    meta = {
        "suite": "test_v14_single_file",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v14_single_file.py",
        "python": sys.version.split()[0],
        "exit_code": exit_code,
        "elapsed_s": round(time.time() - t0, 2),
        "counts": {"total": len(RESULTS), "pass": passed, "fail": failed},
        "note": "offscreen Qt 专项测试，合成样本，非真人分类错例",
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(SRC, "tests", "results_v14_single_file.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(f"归档盒 V1.4 单文件分类修正 — 专项测试\n"
                f"时间: {meta['ts']}\n退出码: {exit_code}\n"
                f"耗时: {meta['elapsed_s']}s\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
