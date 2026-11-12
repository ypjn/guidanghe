"""
归档盒 V1.3 — 推荐模式规则配置化测试（offscreen Qt）

覆盖用户要求的 14 类场景：
C01 默认配置保持原路径结果
C02 合成场景：新增学期/课程映射后推荐模式结果实际改变
C03 方向/类别/关键词规则命中依据与目标正确
C04 启用/停用/顺序调整真实生效
C05 取消编辑不改变生效配置
C06 保存成功后旧计划失效，重新生成使用新配置
C07 重启恢复配置，不同根目录互相隔离
C08 保存失败/损坏配置/不兼容版本/非法字段
C09 路径逃逸与非法目标名称被拒绝
C10 试运行零副作用，并与正式计划共用分类逻辑
C11 后台任务使用配置快照，迟到结果不能覆盖新状态
C12 保护目录不受新规则影响
C13 自定义配置整理→重开→撤销，路径与内容恢复一致
C14 旧历史与旧配置格式兼容
C15 类别留空时关键词规则的方向真正生效（正式计划 + 试运行）
C16 原始 JSON 类型错误在转换前被拒绝（load_config + 主窗口加载路径）
C17 关闭等待期间不发布试运行结果；迟到/过期/错误根目录结果被忽略
C18 取消编辑不阻塞界面（异步清理），线程仍受管理并在结束后回收

合成样本（C02/C13/C15）明确标注为非真人分类错例。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v13_config.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
import planner as planner_mod
from main import MainWindow
from planner import (
    generate_plan, build_plan, MODE_BY_DIRECTION_SEMESTER,
    MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES,
)
from scanner import scan_directory
from storage import Storage
from executor import execute_plan, undo_latest_batch
from utils import sha256_file
from classification_config import (
    ClassificationConfigError, ClassificationConfig, GenericDirRule,
    KeywordRule, SemesterDirRule, SemesterRule, TopDirectionRule,
    config_path, config_to_dict, default_config, is_default_config,
    load_config, save_config, validate_config, SCOPE_FILENAME, SCOPE_PATH,
    TRIGGER_SUBDIR, TRIGGER_UNMATCHED,
)
from config_dialog import ClassificationConfigDialog

BASE = "/tmp/_v13_config_verify"
RESULTS: list = []
GATE = threading.Event()
CAPTURED: dict = {}


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    icon = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}[status]
    print(f"[{icon}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, rel, content="x" * 64):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return p


def inventory(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ("_已整理", ".organizer"))
        for fn in sorted(filenames):
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            with open(ap, "rb") as f:
                out[rel] = sha256_file(ap)
    return out


def pump(app, seconds=0.15):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
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


def cfg_with(root, mutate):
    cfg = default_config()
    mutate(cfg)
    save_config(root, cfg)
    return cfg


def targets(plan):
    return {i.file.rel_path: i.target_rel_path for i in plan.items}


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    t_suite = time.time()
    app = QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog
    REAL_GEN = planner_mod.generate_plan

    # ================= C01 默认配置保持原路径结果 =================
    try:
        root = fresh("c01")
        for rel in [
            "大二上/数据结构/作业/a.pdf",
            "大一下/已完成期末/高数/f.pdf",
            "Code/proj/x.py",
            "散落.txt",
            "大二上/附件-通知/n.pdf",
        ]:
            mk(root, rel)
        sr = scan_directory(root, detect_project_roots=False)
        p_default = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
        p_config = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                                 config=default_config())
        t = targets(p_default)
        expected = {
            "大二上/数据结构/作业/a.pdf":
                "_已整理/课程/大二上/数据结构/文档/作业/a.pdf",
            "大一下/已完成期末/高数/f.pdf":
                "_已整理/课程/大一下/高等数学/文档/高数/f.pdf",
            "Code/proj/x.py": "_已整理/项目/proj/x.py",
            "散落.txt": "_已整理/待定/散落.txt",
            "大二上/附件-通知/n.pdf": "_已整理/校园事务/通知/文档/n.pdf",
        }
        ok = (t == expected
              and targets(p_config) == expected
              and p_config.config_snapshot.get("version") == 1
              and not os.path.exists(config_path(root)))
        rec("C01", "默认配置保持 V1.1/V1.2 的推荐模式路径结果", 
            "通过" if ok else "未通过",
            f"路径一致={t == expected}；显式默认配置一致="
            f"{targets(p_config) == expected}；未创建配置文件="
            f"{not os.path.exists(config_path(root))}\n"
            + "\n".join(f"{k} -> {v}" for k, v in sorted(t.items())))
    except Exception as e:
        rec("C01", "默认配置保持原路径结果", "未通过", f"异常：{e}")

    # ================= C02 合成场景：新增学期/课程映射 =================
    try:
        root = fresh("c02_synthetic")
        rel = "自定义学期/机器人基础/实验一/report.pdf"
        mk(root, rel)
        sr = scan_directory(root, detect_project_roots=False)
        p0 = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
        old_target = targets(p0)[rel]

        def mutate(cfg):
            cfg.semester_rules.append(
                SemesterRule("自定义学期", "2026秋-自定义学期"))
            cfg.semester_dir_rules.append(
                SemesterDirRule("2026秋-自定义学期", "机器人基础",
                                "课程", "机器人基础"))
        save_config(root, cfg_with(root, mutate))
        cfg, src = load_config(root)
        p1 = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
        item = p1.items[0]
        ok = (src == "custom"
              and old_target == "_已整理/待定/自定义学期/report.pdf"
              and item.target_rel_path ==
              "_已整理/课程/2026秋-自定义学期/机器人基础/文档/实验一/report.pdf"
              and item.rule_key == "semester_dir:2026秋-自定义学期/机器人基础"
              and item.semester == "2026秋-自定义学期"
              and item.inner_path == "实验一")
        rec("C02", "合成场景（非真人反馈）：仅通过配置新增学期与课程映射，"
                   "推荐模式结果改变并保留内部目录层级",
            "通过" if ok else "未通过",
            f"原目标={old_target}\n新目标={item.target_rel_path}\n"
            f"依据={item.rule_applied}\n学期={item.semester} 内部路径={item.inner_path}")
    except Exception as e:
        rec("C02", "合成场景：新增学期/课程映射", "未通过", f"异常：{e}")

    # ================= C03 方向/类别/关键词规则命中依据 =================
    try:
        root = fresh("c03")
        mk(root, "Code/proj/a.py")
        mk(root, "大二上/数据结构/x.pdf")
        mk(root, "零散/report.pdf")
        sr = scan_directory(root, detect_project_roots=False)

        def mutate(cfg):
            for r in cfg.top_direction_rules:
                if r.source == "Code":
                    r.direction = "竞赛"
            for r in cfg.semester_dir_rules:
                if r.semester == "大二上" and r.source == "数据结构":
                    r.category = "数据结构（实验班）"
            cfg.keyword_rules.append(KeywordRule(
                ["report"], "机器人实验", "项目", SCOPE_FILENAME,
                TRIGGER_UNMATCHED))
        cfg = cfg_with(root, mutate)
        plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
        by = {i.file.rel_path: i for i in plan.items}
        a = by["Code/proj/a.py"]
        b = by["大二上/数据结构/x.pdf"]
        c = by["零散/report.pdf"]
        ok = (a.direction == "竞赛" and a.target_rel_path == "_已整理/竞赛/proj/a.py"
              and a.rule_key == "top_direction:Code"
              and b.category == "数据结构（实验班）"
              and b.target_rel_path ==
              "_已整理/课程/大二上/数据结构（实验班）/文档/x.pdf"
              and "数据结构（实验班）" in b.rule_applied
              and c.direction == "项目" and c.category == "机器人实验"
              and c.rule_key.startswith("keyword_unmatched:")
              and "关键词兜底" in c.rule_applied)
        rec("C03", "修改方向/类别/关键词规则后，命中依据与目标正确",
            "通过" if ok else "未通过",
            f"顶层方向：{a.direction} {a.target_rel_path} ({a.rule_applied})\n"
            f"类别：{b.category} {b.target_rel_path}\n"
            f"关键词：{c.direction}/{c.category} ({c.rule_key})")
    except Exception as e:
        rec("C03", "方向/类别/关键词规则", "未通过", f"异常：{e}")

    # ================= C04 启用/停用/顺序 =================
    try:
        root = fresh("c04")
        mk(root, "大二上/数据结构/作业/a.pdf")
        mk(root, "大一下/已完成期末/高数离散复习.pdf")
        sr = scan_directory(root, detect_project_roots=False)

        cfg = cfg_with(root, lambda c: [setattr(r, "category", "数据结构A")
                                        for r in c.semester_dir_rules
                                        if r.source == "数据结构"])
        p_on = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
        on_cat = {i.file.rel_path: i.category for i in p_on.items}[
            "大二上/数据结构/作业/a.pdf"]

        cfg2 = copy.deepcopy(cfg)
        for r in cfg2.semester_dir_rules:
            if r.source == "数据结构":
                r.enabled = False
        p_off = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg2)
        off_item = {i.file.rel_path: i for i in p_off.items}[
            "大二上/数据结构/作业/a.pdf"]

        # 关键词顺序：默认「离散」在「高数」之前；把高数移到最前
        p_kw1 = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                              config=cfg2)
        kw1 = {i.file.rel_path: i.category for i in p_kw1.items}[
            "大一下/已完成期末/高数离散复习.pdf"]
        cfg3 = copy.deepcopy(cfg2)
        hi = next(r for r in cfg3.keyword_rules if "高数" in r.keywords)
        cfg3.keyword_rules.remove(hi)
        cfg3.keyword_rules.insert(0, hi)
        p_kw2 = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                              config=cfg3)
        kw2 = {i.file.rel_path: i.category for i in p_kw2.items}[
            "大一下/已完成期末/高数离散复习.pdf"]

        # 停用关键词 → 保留目录名（未细分）
        cfg4 = copy.deepcopy(cfg3)
        for r in cfg4.keyword_rules:
            r.enabled = False
        p_kw3 = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                              config=cfg4)
        kw3 = {i.file.rel_path: i.category for i in p_kw3.items}[
            "大一下/已完成期末/高数离散复习.pdf"]

        ok = (on_cat == "数据结构A"
              and off_item.category == "数据结构"
              and "目录锚定" in off_item.rule_applied
              and kw1 == "离散数学" and kw2 == "高等数学"
              and kw3 == "已完成期末（未细分）")
        rec("C04", "启用、停用、顺序调整真实生效",
            "通过" if ok else "未通过",
            f"类别映射启用={on_cat}；停用后={off_item.category}"
            f"（{off_item.rule_applied}）\n"
            f"关键词顺序：默认={kw1}；高数置前={kw2}；全部停用={kw3}")
    except Exception as e:
        rec("C04", "启用/停用/顺序", "未通过", f"异常：{e}")

    # ================= C05 取消编辑不改变生效配置 =================
    try:
        root = fresh("c05")
        mk(root, "大二上/数据结构/a.pdf")
        saved = cfg_with(root, lambda c: c.semester_rules.append(
            SemesterRule("自定义学期", "自定义学期")))
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        before = copy.deepcopy(w.classification_config)
        file_before = open(config_path(root), encoding="utf-8").read()

        dlg = ClassificationConfigDialog(
            root=root, config=copy.deepcopy(w.classification_config),
            using_defaults=False, scan_result=None,
            base_config=copy.deepcopy(w.classification_config),
            save_callback=lambda cfg: w._save_classification_config(root, cfg),
            worker_registry=w,
            parent=w)
        dlg.table_semester.item(0, 1).setText("被取消的修改")
        dlg.table_semester.insertRow(dlg.table_semester.rowCount())
        dlg.reject()

        effective_unchanged = (
            len(w.classification_config.semester_rules)
            == len(before.semester_rules)
            and w.classification_config.semester_rules[-1].source
            == before.semester_rules[-1].source)
        file_unchanged = open(config_path(root), encoding="utf-8").read() == file_before
        ok = effective_unchanged and file_unchanged and dlg.result_config is None
        rec("C05", "取消编辑不改变已生效规则和配置文件",
            "通过" if ok else "未通过",
            f"内存生效配置未变={effective_unchanged}；文件未变={file_unchanged}；"
            f"result_config={dlg.result_config}")
        w.close()
        app.processEvents()
    except Exception as e:
        rec("C05", "取消编辑不改变生效配置", "未通过", f"异常：{e}")

    # ================= C06 保存成功 → 旧计划失效 → 重新生成 =================
    try:
        root = fresh("c06")
        mk(root, "自定义学期/机器人基础/实验一/report.pdf")
        mk(root, "散落.txt")
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.on_scan()
        ok_scan = wait_until(app, lambda: not w._working and w.scan_result is not None)
        w._generate_and_show_plan()
        ok_plan = wait_until(app, lambda: not w._plan_working
                             and w.current_plan is not None and w._plan_valid)
        old_targets = targets(w.current_plan)

        dlg = ClassificationConfigDialog(
            root=root, config=copy.deepcopy(w.classification_config),
            using_defaults=True, scan_result=w.scan_result,
            base_config=copy.deepcopy(w.classification_config),
            save_callback=lambda cfg: w._save_classification_config(root, cfg),
            worker_registry=w,
            parent=w)
        # 通过界面表格新增：学期映射 + 学期内目录映射
        row = dlg.table_semester.rowCount()
        dlg.table_semester.insertRow(row)
        from config_dialog import _check_item, _text_item
        dlg.table_semester.setItem(row, 0, _check_item(True))
        dlg.table_semester.setItem(row, 1, _text_item("自定义学期"))
        dlg.table_semester.setItem(row, 2, _text_item("2026秋-自定义"))
        row2 = dlg.table_semdir.rowCount()
        dlg.table_semdir.insertRow(row2)
        dlg.table_semdir.setItem(row2, 0, _check_item(True))
        dlg.table_semdir.setItem(row2, 1, _text_item("2026秋-自定义"))
        dlg.table_semdir.setItem(row2, 2, _text_item("机器人基础"))
        dlg.table_semdir.setItem(row2, 3, _text_item("课程"))
        dlg.table_semdir.setItem(row2, 4, _text_item("机器人基础"))
        dlg._on_save()

        saved_ok = os.path.isfile(config_path(root))
        plan_invalid = (not w._plan_valid and w.current_plan is None
                        and w.table.rowCount() == 0
                        and not w.btn_execute.isEnabled()
                        and w.invalid_label.isVisible())
        w._generate_and_show_plan()
        regenerated = wait_until(app, lambda: not w._plan_working
                                 and w.current_plan is not None and w._plan_valid)
        new_targets = targets(w.current_plan)
        changed = (new_targets.get("自定义学期/机器人基础/实验一/report.pdf")
                   == "_已整理/课程/2026秋-自定义/机器人基础/文档/实验一/report.pdf")
        ok = (ok_scan and ok_plan and plan_invalid and saved_ok
              and regenerated and changed
              and old_targets != new_targets)
        rec("C06", "界面保存成功后旧计划立即失效，重新生成使用新配置",
            "通过" if ok else "未通过",
            f"扫描/首计划={ok_scan}/{ok_plan}；保存文件={saved_ok}；"
            f"旧计划失效={plan_invalid}；重新生成={regenerated}\n"
            f"旧目标={old_targets.get('自定义学期/机器人基础/实验一/report.pdf')}\n"
            f"新目标={new_targets.get('自定义学期/机器人基础/实验一/report.pdf')}")
        w.close()
        app.processEvents()
    except Exception as e:
        rec("C06", "保存后旧计划失效并重新生成", "未通过", f"异常：{e}")

    # ================= C07 重启恢复 + 根目录隔离 =================
    try:
        r1 = fresh("c07_root1")
        r2 = fresh("c07_root2")
        mk(r1, "a.pdf")
        mk(r2, "a.pdf")
        cfg_with(r1, lambda c: c.semester_rules.append(
            SemesterRule("隔离标记一", "学期一")))
        cfg_with(r2, lambda c: c.semester_rules.append(
            SemesterRule("隔离标记二", "学期二")))
        c1, s1 = load_config(r1)
        c2, s2 = load_config(r2)
        w = MainWindow()
        w.path_edit.setText(r1)
        w._reset_for_new_dir(r1)
        first = [r.source for r in w.classification_config.semester_rules]
        w.path_edit.setText(r2)
        w._reset_for_new_dir(r2)
        second = [r.source for r in w.classification_config.semester_rules]
        w.close()
        app.processEvents()
        w2 = MainWindow()          # 模拟重启：新窗口重新加载
        w2.path_edit.setText(r1)
        w2._reset_for_new_dir(r1)
        restarted = [r.source for r in w2.classification_config.semester_rules]
        w2.close()
        app.processEvents()
        ok = ("隔离标记一" in first and "隔离标记二" not in first
              and "隔离标记二" in second and "隔离标记一" not in second
              and "隔离标记一" in restarted and s1 == s2 == "custom")
        rec("C07", "重启恢复配置，不同根目录互相隔离",
            "通过" if ok else "未通过",
            f"root1 首载含标记一={'隔离标记一' in first}；"
            f"root2 含标记二={'隔离标记二' in second}；"
            f"重启 root1 含标记一={'隔离标记一' in restarted}")
    except Exception as e:
        rec("C07", "重启恢复与根目录隔离", "未通过", f"异常：{e}")

    # ================= C08 保存失败/损坏/版本/非法字段 =================
    try:
        root = fresh("c08")
        good = default_config()
        good.semester_rules.append(SemesterRule("标记", "标记"))
        save_config(root, good)
        before = open(config_path(root), encoding="utf-8").read()

        # 保存失败：mkstemp 抛错，旧文件必须保持
        import classification_config as cc
        real_mkstemp = cc.tempfile.mkstemp
        cc.tempfile.mkstemp = lambda *a, **k: (_ for _ in ()).throw(
            OSError("模拟磁盘已满"))
        failed_msg = ""
        try:
            bad = copy.deepcopy(good)
            bad.semester_rules.append(SemesterRule("标记二", "标记二"))
            save_config(root, bad)
        except ClassificationConfigError as e:
            failed_msg = str(e)
        finally:
            cc.tempfile.mkstemp = real_mkstemp
        after_fail = open(config_path(root), encoding="utf-8").read()

        # 损坏
        with open(config_path(root), "w", encoding="utf-8") as f:
            f.write("{这不是 JSON")
        corrupt_msg = ""
        try:
            load_config(root)
        except ClassificationConfigError as e:
            corrupt_msg = str(e)
        corrupt_kept = open(config_path(root), encoding="utf-8").read() == "{这不是 JSON"

        # 版本不兼容
        payload = {"version": 99, "root": root, "semester_map": []}
        with open(config_path(root), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        version_msg = ""
        try:
            load_config(root)
        except ClassificationConfigError as e:
            version_msg = str(e)
        version_kept = json.load(open(config_path(root), encoding="utf-8"))["version"] == 99

        # 根目录不匹配
        payload = {"version": 1, "root": "/some/other/root", "semester_map": []}
        with open(config_path(root), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        root_msg = ""
        try:
            load_config(root)
        except ClassificationConfigError as e:
            root_msg = str(e)

        # 非法字段
        bad_cfg = default_config()
        bad_cfg.semester_rules.append(SemesterRule("dup", "x"))
        bad_cfg.semester_rules.append(SemesterRule("dup", "y"))
        bad_cfg.keyword_rules.append(KeywordRule([], "目标"))
        errors = validate_config(bad_cfg)
        save_rejected = False
        try:
            save_config(root, bad_cfg)
        except ClassificationConfigError:
            save_rejected = True

        ok = ("磁盘已满" in failed_msg and after_fail == before
              and "无法读取" in corrupt_msg and corrupt_kept
              and "版本不兼容" in version_msg and version_kept
              and "不属于当前根目录" in root_msg
              and any("重复" in e for e in errors)
              and any("至少需要一个关键词" in e for e in errors)
              and save_rejected)
        rec("C08", "保存失败、损坏、不兼容版本、非法字段均有明确处理且保留原文件",
            "通过" if ok else "未通过",
            f"保存失败=「{failed_msg.splitlines()[0] if failed_msg else '无'}」"
            f" 旧文件保留={after_fail == before}\n"
            f"损坏=「{corrupt_msg.splitlines()[0] if corrupt_msg else '无'}」"
            f" 原文件保留={corrupt_kept}\n"
            f"版本=「{version_msg.splitlines()[0] if version_msg else '无'}」\n"
            f"根不匹配=「{root_msg.splitlines()[0] if root_msg else '无'}」\n"
            f"非法字段错误数={len(errors)}；保存被拒={save_rejected}")
    except Exception as e:
        rec("C08", "保存失败/损坏/版本/非法字段", "未通过", f"异常：{e}")

    # ================= C09 路径逃逸与非法目标名 =================
    try:
        root = fresh("c09")
        cases = []
        for value in ("../逃逸", "/绝对路径", "a/b", "..", "~/家目录", "C:\\x"):
            cfg = default_config()
            cfg.semester_rules.append(SemesterRule(value, "X"))
            cases.append((value, validate_config(cfg)))
        bad_kw = default_config()
        bad_kw.keyword_rules.append(KeywordRule(["kw"], "../目标"))
        cases.append(("关键词目标 ../目标", validate_config(bad_kw)))
        ok = all(errs for _, errs in cases)
        rejected = 0
        cfg = default_config()
        cfg.semester_rules.append(SemesterRule("../逃逸", "X"))
        try:
            save_config(root, cfg)
        except ClassificationConfigError:
            rejected += 1
        ok = ok and rejected == 1 and not os.path.exists(config_path(root))
        rec("C09", "路径逃逸与非法目标名称被拒绝（不写盘）",
            "通过" if ok else "未通过",
            "\n".join(f"{v}: {errs[0] if errs else '未拒绝'}"
                      for v, errs in cases))
    except Exception as e:
        rec("C09", "路径逃逸与非法目标名称", "未通过", f"异常：{e}")

    # ================= C10 试运行零副作用 + 共用逻辑 =================
    try:
        root = fresh("c10")
        for rel in ("自定义学期/机器人基础/实验一/report.pdf",
                    "大二上/数据结构/a.pdf", "散落.txt"):
            mk(root, rel)
        sr = scan_directory(root, detect_project_roots=False)

        def mutate(cfg):
            cfg.semester_rules.append(SemesterRule("自定义学期", "2026秋"))
            cfg.semester_dir_rules.append(
                SemesterDirRule("2026秋", "机器人基础", "课程", "机器人基础"))
        draft = cfg_with(root, mutate)
        before = inventory(root)
        config_exists_before = os.path.exists(config_path(root))
        organized_before = os.path.exists(os.path.join(root, "_已整理"))

        import rule_preview
        sha_calls = {"n": 0}
        real_sha = planner_mod.sha256_file
        planner_mod.sha256_file = lambda p: (sha_calls.__setitem__(
            "n", sha_calls["n"] + 1) or real_sha(p))
        try:
            result = rule_preview.preview_impact(
                sr, draft, default_config(), MODE_BY_DIRECTION_SEMESTER, [])
        finally:
            planner_mod.sha256_file = real_sha

        after = inventory(root)
        no_side_effect = (before == after
                          and os.path.exists(config_path(root)) == config_exists_before
                          and os.path.exists(os.path.join(root, "_已整理"))
                          == organized_before)
        # 与正式计划共用同一分类/路径构造
        mini = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=draft,
                          compute_duplicates=False)
        formal = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=draft)
        same = (targets(mini) == targets(formal)
                and {i.file.rel_path: i.rule_key for i in mini.items}
                == {i.file.rel_path: i.rule_key for i in formal.items})
        changed_rel = {c.rel_path for c in result.changed}
        ok = (no_side_effect and sha_calls["n"] == 0 and same
              and "自定义学期/机器人基础/实验一/report.pdf" in changed_rel
              and "未计算 SHA-256" in result.duplicate_note)

        # UI 试运行入口（后台线程，seq + root 校验）
        import config_dialog
        captured = {}
        real_ipd = config_dialog.ImpactPreviewDialog

        class _StubIPD:
            def __init__(self, res, parent=None):
                captured["result"] = res
            def exec(self):
                return 0
        config_dialog.ImpactPreviewDialog = _StubIPD
        dlg = ClassificationConfigDialog(
            root=root, config=copy.deepcopy(draft), using_defaults=False,
            scan_result=sr, base_config=default_config(),
            save_callback=None, worker_registry=None)
        dlg._on_preview()
        ui_ok = wait_until(app, lambda: "result" in captured
                           and not dlg._previewing, timeout=20)
        config_dialog.ImpactPreviewDialog = real_ipd
        dlg.reject()
        ok = ok and ui_ok and captured["result"].sample_total == 3
        rec("C10", "试运行零文件操作副作用，不计算 SHA-256，并与正式计划共用分类逻辑",
            "通过" if ok else "未通过",
            f"文件清单不变={before == after}；SHA 调用={sha_calls['n']}；"
            f"与正式计划一致={same}；配置变化项={len(result.changed)}；"
            f"UI 后台试运行={ui_ok}\n{result.duplicate_note}")
    except Exception as e:
        rec("C10", "试运行零副作用与共用逻辑", "未通过", f"异常：{e}")

    # ================= C11 后台任务使用快照，迟到结果不覆盖 =================
    try:
        root = fresh("c11")
        for i in range(30):
            mk(root, f"自定义学期/机器人基础/f{i:02d}.pdf", "y" * 256)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.on_scan()
        wait_until(app, lambda: not w._working and w.scan_result is not None)

        GATE.clear()
        CAPTURED.clear()

        def gated_gen(scan_result, mode, rules, cancel_event=None,
                      progress_cb=None, config=None):
            CAPTURED["config"] = config
            if progress_cb:
                progress_cb(1, len(scan_result.files), "gated")
            GATE.wait(30)
            return REAL_GEN(scan_result, mode, rules,
                            cancel_event=cancel_event,
                            progress_cb=progress_cb, config=config)
        main_mod.generate_plan = gated_gen
        w._generate_and_show_plan()
        started = wait_until(app, lambda: w._plan_worker is not None
                             and w._plan_worker.isRunning(), timeout=5)
        cfg_b = copy.deepcopy(default_config())
        cfg_b.semester_rules.append(SemesterRule("新配置标记", "新配置标记"))
        w._save_classification_config(root, cfg_b)
        state_after_save = (w.current_plan is None and not w._plan_valid)
        GATE.set()
        stopped = wait_until(app, lambda: w._plan_worker is not None
                             and not w._plan_worker.isRunning(), timeout=20)
        pump(app, 0.3)
        late_ignored = w.current_plan is None and not w._plan_valid
        snapshot_ok = (CAPTURED.get("config") is not None
                       and CAPTURED["config"] is not w.classification_config
                       and "新配置标记" not in
                       [r.source for r in CAPTURED["config"].semester_rules])
        ok = (started and state_after_save and stopped and late_ignored
              and snapshot_ok and w._config_source == "custom")
        main_mod.generate_plan = REAL_GEN
        rec("C11", "后台任务使用独立配置快照，迟到结果不能覆盖新配置状态",
            "通过" if ok else "未通过",
            f"线程启动={started}；保存后旧计划已失效={state_after_save}；"
            f"迟到结果被忽略={late_ignored}；快照独立={snapshot_ok}")
        w.close()
        app.processEvents()
    except Exception as e:
        main_mod.generate_plan = REAL_GEN
        rec("C11", "配置快照与迟到结果", "未通过", f"异常：{e}")

    # ================= C12 保护目录不受新规则影响 =================
    try:
        root = fresh("c12")
        mk(root, "个人/高数笔记.pdf")
        mk(root, "公开/高数作业.pdf")
        protect = os.path.join(root, "个人")
        cfg = cfg_with(root, lambda c: c.keyword_rules.append(
            KeywordRule(["作业"], "高等数学", "课程", SCOPE_PATH,
                        TRIGGER_UNMATCHED)))
        sr = scan_directory(root, protect_dirs=[(protect, "manual", "用户手动保护")])
        plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
        rels = [i.file.rel_path for i in plan.items]
        ok = ("个人/高数笔记.pdf" not in rels
              and "公开/高数作业.pdf" in rels
              and sr.protected_count == 1
              and any(s.rel_path == "" or "个人" in s.abs_path for s in sr.skipped))
        rec("C12", "保护目录不受新规则影响（配置不能解除保护）",
            "通过" if ok else "未通过",
            f"计划文件={rels}；保护跳过={sr.protected_count}")
    except Exception as e:
        rec("C12", "保护目录不受新规则影响", "未通过", f"异常：{e}")

    # ================= C13 自定义配置整理→重开→撤销 =================
    try:
        root = fresh("c13")
        mk(root, "自定义学期/机器人基础/实验一/report.pdf", "内容-报告")
        mk(root, "自定义学期/机器人基础/实验一/数据.csv", "内容-数据")
        mk(root, "散落.txt", "内容-散落")
        before = inventory(root)

        def mutate(cfg):
            cfg.semester_rules.append(SemesterRule("自定义学期", "2026秋"))
            cfg.semester_dir_rules.append(
                SemesterDirRule("2026秋", "机器人基础", "课程", "机器人基础"))
        cfg_with(root, mutate)
        cfg, _ = load_config(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg)
        st = Storage(root)
        res = execute_plan(plan, st)
        moved_paths = [i.target_rel_path for i in plan.items
                       if os.path.exists(os.path.join(root, i.target_rel_path))]
        batch = st.get_batch(res.batch_id)
        snapshot_ok = bool(batch.config_snapshot) and any(
            r.get("source") == "自定义学期"
            for r in batch.config_snapshot.get("semester_map", []))
        st.close()

        # 模拟重启：重新打开 Storage 与配置
        cfg2, src2 = load_config(root)
        st2 = Storage(root)
        undo = undo_latest_batch(st2)
        st2.close()
        after = inventory(root)
        restored_files = all(os.path.exists(os.path.join(root, rel))
                             for rel in before)
        ok = (src2 == "custom" and len(moved_paths) == 3 and snapshot_ok
              and undo.is_complete and restored_files and before == after)
        rec("C13", "自定义配置生成的计划：整理→重开→撤销，路径与内容恢复一致",
            "通过" if ok else "未通过",
            f"移动={res.moved}；目标示例={moved_paths[:2]}；"
            f"批次配置快照={snapshot_ok}；撤销={undo.summary()}；"
            f"清单恢复一致={before == after}")
    except Exception as e:
        rec("C13", "自定义配置整理→重开→撤销", "未通过", f"异常：{e}")

    # ================= C14 旧历史与旧配置格式兼容 =================
    try:
        root = fresh("c14")
        os.makedirs(os.path.join(root, ".organizer"))
        # 造一个 V1.2 旧结构数据库（无 config_json 列）
        import sqlite3
        db = os.path.join(root, ".organizer", "history.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '', rules_json TEXT DEFAULT '',
                summary TEXT DEFAULT '');
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
                action_index INTEGER NOT NULL, source_rel TEXT NOT NULL,
                target_rel TEXT NOT NULL, source_size INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT DEFAULT '', executed_at TEXT DEFAULT '',
                undone_at TEXT DEFAULT '', UNIQUE(batch_id, action_index),
                FOREIGN KEY(batch_id) REFERENCES batches(id));
            CREATE TABLE created_dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL, FOREIGN KEY(batch_id) REFERENCES batches(id));
        """)
        target_rel = "_已整理/文档/旧文件.pdf"
        mk(root, target_rel, "旧内容")
        sha = sha256_file(os.path.join(root, target_rel))
        cur = conn.execute(
            "INSERT INTO batches(root,mode,status,created_at,finished_at,"
            "rules_json,summary) VALUES(?,?,?,?,?,?,?)",
            (root, MODE_BY_DIRECTION_SEMESTER, "done", "2026-01-01T10:00:00",
             "2026-01-01T10:01:00", "", "旧批次"))
        bid = cur.lastrowid
        conn.execute(
            "INSERT INTO actions(batch_id,action_index,source_rel,target_rel,"
            "source_size,source_sha256,status,executed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (bid, 0, "旧文件.pdf", target_rel, os.path.getsize(
                os.path.join(root, target_rel)), sha, "moved",
             "2026-01-01T10:00:30"))
        conn.commit()
        conn.close()

        st = Storage(root)          # 打开时迁移：只增列
        cols = [r[1] for r in st.conn.execute("PRAGMA table_info(batches)")]
        old = st.get_batch(bid)
        readable = (old is not None and old.status == "done"
                    and old.config_json == "" and old.config_summary == "")
        # 迁移后仍能写入/读取新批次（带配置快照）
        new_plan = generate_plan(
            scan_directory(root, detect_project_roots=False),
            MODE_BY_DIRECTION_SEMESTER, [], config=load_config(root)[0])
        new_id = st.create_batch(new_plan, "新批次")
        new_batch = st.get_batch(new_id)
        mixed_ok = (new_batch is not None and bool(new_batch.config_snapshot))
        # 旧批次仍可撤销（读/写兼容）
        undo = undo_latest_batch(st)
        st.close()
        # 迁移后最后批次是新建的空计划批次，撤销它会先处理它；
        # 直接再撤销一次以覆盖旧批次
        st2 = Storage(root)
        undo2 = undo_latest_batch(st2)
        st2.close()
        old_file_back = os.path.exists(os.path.join(root, "旧文件.pdf"))
        # 旧配置格式：没有 classification_config.json → 默认配置，不创建文件
        cfg, src = load_config(root)
        no_config_created = not os.path.exists(config_path(root))
        # 未来兼容：同版本多余字段应被忽略而不是报错
        payload = {
            "version": 1, "root": root,
            "semester_map": [{"source": "兼容学期", "semester": "兼容学期",
                              "enabled": True, "future_field": 123}],
            "top_direction": [], "semester_dirs": [], "generic_dirs": [],
            "keyword_rules": [], "unknown_section": {"x": 1},
        }
        with open(config_path(root), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        cfg3, src3 = load_config(root)
        future_ok = src3 == "custom" and any(
            r.source == "兼容学期" for r in cfg3.semester_rules)
        ok = ("config_json" in cols and readable and mixed_ok
              and undo.is_complete and undo2.is_complete and old_file_back
              and src == "default" and no_config_created and future_ok)
        rec("C14", "旧历史（无 config_json）与旧配置格式按兼容策略处理",
            "通过" if ok else "未通过",
            f"迁移列={'config_json' in cols}；旧批次可读={readable}；"
            f"新旧批次共存={mixed_ok}；旧批次可撤销={undo2.is_complete}；"
            f"旧文件恢复={old_file_back}；无配置时默认且不建文件="
            f"{src == 'default' and no_config_created}；同版本多余字段容忍={future_ok}")
    except Exception as e:
        rec("C14", "旧历史与旧配置格式兼容", "未通过", f"异常：{e}")

    # ================= C15 类别留空时关键词规则方向生效 =================
    try:
        root = fresh("c15_keyword_direction")
        robot_rel = "大一下/已完成期末/robot.pdf"
        default_rel = "大一下/已完成期末/高数/f.pdf"
        mk(root, robot_rel, "机器人报告")
        mk(root, default_rel, "高数作业")
        sr = scan_directory(root, detect_project_roots=False)
        cfg = default_config()
        cfg.keyword_rules.insert(0, KeywordRule(
            ["robot"], "机器人实验", "项目", SCOPE_FILENAME, TRIGGER_SUBDIR))
        save_config(root, cfg)
        loaded, source = load_config(root)
        preview_plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                                  config=loaded, compute_duplicates=False)
        formal_plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                                    config=loaded)
        by_plan = {
            "preview": {i.file.rel_path: i for i in preview_plan.items},
            "formal": {i.file.rel_path: i for i in formal_plan.items},
        }
        robot_items = {k: v[robot_rel] for k, v in by_plan.items()}
        default_items = {k: v[default_rel] for k, v in by_plan.items()}
        each_ok = all(
            i.direction == "项目" and i.category == "机器人实验"
            and i.target_rel_path == "_已整理/项目/机器人实验/robot.pdf"
            and i.rule_key.startswith("keyword_subdir:")
            and "项目/机器人实验" in i.rule_applied
            for i in robot_items.values())
        default_ok = all(
            i.direction == "课程" and i.category == "高等数学"
            and i.target_rel_path ==
            "_已整理/课程/大一下/高等数学/文档/高数/f.pdf"
            for i in default_items.values())

        import rule_preview
        impact = rule_preview.preview_impact(
            sr, loaded, default_config(), MODE_BY_DIRECTION_SEMESTER, [])
        changed = next((c for c in impact.changed if c.rel_path == robot_rel),
                       None)
        preview_ok = (changed is not None
                      and changed.new_target == "_已整理/项目/机器人实验/robot.pdf"
                      and "项目/机器人实验" in changed.new_reason)
        ok = (source == "custom" and each_ok and default_ok and preview_ok)
        rec("C15", "类别留空目录命中关键词规则时，配置方向与目标类别都真正生效"
                   "（合成场景，非真人反馈）",
            "通过" if ok else "未通过",
            f"正式计划与试运行一致={each_ok}；"
            f"目录映射方向=课程、关键词规则方向=项目\n"
            f"实际方向={robot_items['formal'].direction}；"
            f"目标={robot_items['formal'].target_rel_path}\n"
            f"依据={robot_items['formal'].rule_applied}\n"
            f"默认关键词路径保持兼容={default_ok}\n"
            f"试运行新目标={changed.new_target if changed else '未变化'}")
    except Exception as e:
        rec("C15", "关键词规则方向生效", "未通过", f"异常：{e}")

    # ================= C16 原始 JSON 类型错误在转换前被拒绝 =================
    try:
        root = fresh("c16_types")
        sr = scan_directory(root, detect_project_roots=False)

        def write_payload(payload):
            os.makedirs(os.path.dirname(config_path(root)), exist_ok=True)
            with open(config_path(root), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            return open(config_path(root), encoding="utf-8").read()

        base = {"version": 1, "root": root}
        base.update(config_to_dict(default_config()))
        cases = []

        p = copy.deepcopy(base)
        p["semester_map"][0]["enabled"] = "false"
        cases.append(("enabled 字符串", p,
                      ("semester_map 第 1 项", "enabled", "布尔值")))

        p = copy.deepcopy(base)
        p["semester_map"][0]["semester"] = 2026
        cases.append(("学期标签数字", p,
                      ("semester_map 第 1 项", "semester", "文本")))

        p = copy.deepcopy(base)
        p["version"] = True
        cases.append(("version 布尔值", p, ("version", "整数", "布尔值")))

        p = copy.deepcopy(base)
        p["keyword_rules"].append(
            {"keywords": [123], "target": "目标", "direction": "课程"})
        cases.append(("关键词元素数字", p,
                      ("keyword_rules 第 13 项",
                       "keywords 第 1 个元素", "文本")))

        p = copy.deepcopy(base)
        p["semester_map"] = {"bad": 1}
        cases.append(("规则数组写成对象", p, ("semester_map", "必须是数组")))

        p = copy.deepcopy(base)
        p["semester_map"][0].pop("source")
        cases.append(("缺少必填字段", p,
                      ("semester_map 第 1 项", "缺少必填字段 source")))

        results, files_kept = [], 0
        for label, payload, needles in cases:
            before_raw = write_payload(payload)
            msg = ""
            try:
                load_config(root)
            except ClassificationConfigError as e:
                msg = str(e)
            after_raw = open(config_path(root), encoding="utf-8").read()
            kept = before_raw == after_raw
            files_kept += int(kept)
            results.append((label, kept,
                            all(n in msg for n in needles),
                            msg.splitlines()[0] if msg else "未拒绝"))

        # 主窗口加载路径：错误类型必须记录错误并阻止推荐模式生成计划
        p = copy.deepcopy(base)
        p["semester_map"][0]["enabled"] = "false"
        write_payload(p)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()
        w.scan_result = sr
        w._generate_and_show_plan()
        pump(app, 0.2)
        blocked = (w._config_error != "" and w._config_source == "error"
                   and w._plan_worker is None and w.current_plan is None
                   and not w._plan_valid and not w.btn_execute.isEnabled()
                   and "不可用" in w.config_label.text())
        w.close()
        app.processEvents()

        # 合法的 false、空规则数组仍应正常工作
        empty_payload = {
            "version": 1, "root": root,
            "semester_map": [], "top_direction": [], "semester_dirs": [],
            "generic_dirs": [], "keyword_rules": []}
        write_payload(empty_payload)
        c_empty, s_empty = load_config(root)
        false_payload = {
            "version": 1, "root": root,
            "semester_map": [{"source": "学期A", "semester": "学期A",
                              "enabled": False}],
            "top_direction": [], "semester_dirs": [],
            "generic_dirs": [], "keyword_rules": []}
        write_payload(false_payload)
        c_false, s_false = load_config(root)
        legal_ok = (s_empty == "custom" and len(c_empty.semester_rules) == 0
                    and s_false == "custom"
                    and c_false.semester_rules[0].enabled is False)

        ok = (all(r[1] and r[2] for r in results)
              and files_kept == len(cases) and blocked and legal_ok)
        rec("C16", "原始 JSON 类型错误在类型转换前被拒绝；合法 false/空数组正常",
            "通过" if ok else "未通过",
            "\n".join(f"{label}：保留原文件={kept}；错误={judge}；{first}"
                      for label, kept, judge, first in results)
            + f"\n主窗口记录错误并阻止生成计划={blocked}"
            + f"；合法 false/空数组正常={legal_ok}")
    except Exception as e:
        rec("C16", "原始 JSON 类型校验", "未通过", f"异常：{e}")

    # ================= C17 关闭等待期间不发布试运行结果 =================
    try:
        import config_dialog
        import rule_preview
        root = fresh("c17_close_wait")
        for i in range(3):
            mk(root, f"大二上/数据结构/f{i}.pdf", "z" * 64)
        sr = scan_directory(root, detect_project_roots=False)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()

        real_worker_cls = config_dialog.PreviewWorker
        real_ipd = config_dialog.ImpactPreviewDialog
        real_critical = QMessageBox.critical
        opened = {"n": 0, "result": None}
        critical = {"n": 0}

        class _RecorderIPD:
            def __init__(self, result, parent=None):
                opened["n"] += 1
                opened["result"] = result

            def exec(self):
                return 0

        class GatedSuccessWorker(real_worker_cls):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.emitted = threading.Event()
                self.release = threading.Event()

            def run(self):
                result = rule_preview.preview_impact(
                    self.scan_result, self.draft, self.base,
                    mode=self.mode, rules=self.rules)
                self.finished_ok.emit(self.seq, self.root, result)
                self.emitted.set()          # 成功信号已排队
                self.release.wait(30)       # 线程尚未退出

        config_dialog.PreviewWorker = GatedSuccessWorker
        config_dialog.ImpactPreviewDialog = _RecorderIPD
        QMessageBox.critical = staticmethod(
            lambda *a, **k: (critical.__setitem__("n", critical["n"] + 1)
                             or QMessageBox.Ok))
        worker = None
        try:
            dlg = ClassificationConfigDialog(
                root=root, config=copy.deepcopy(default_config()),
                using_defaults=True, scan_result=sr,
                base_config=default_config(), save_callback=None,
                worker_registry=w, parent=w)
            dlg._on_preview()
            worker = dlg._worker
            deadline = time.time() + 10
            while (time.time() < deadline and worker is not None
                   and not worker.emitted.is_set()):
                time.sleep(0.005)
            started = worker is not None and worker.emitted.is_set()

            # 成功信号已排队、线程仍未退出时关闭主窗口
            w.close()
            pump(app, 0.35)
            close_wait = (w._close_pending and not w._closing
                          and w._storage is not None)
            not_published = (opened["n"] == 0 and critical["n"] == 0
                             and dlg._previewing
                             and not dlg.tabs.isEnabled()
                             and "试运行完成" not in dlg.preview_label.text())
            worker_alive = worker.isRunning() and worker in w._rule_preview_workers

            worker.release.set()
            finished = wait_until(
                app, lambda: not worker.isRunning(), timeout=15)
            closed = wait_until(
                app, lambda: not w.isVisible() and w._storage is None, timeout=15)
            cleaned = (worker not in w._rule_preview_workers
                       and dlg._worker is None)

            # 迟到失败 / 旧 seq / 错误 root：都不发布；正常结果仍可接受
            opened["n"], critical["n"] = 0, 0
            w2 = MainWindow()
            w2.path_edit.setText(root)
            w2._reset_for_new_dir(root)
            w2.show()
            app.processEvents()
            dlg2 = ClassificationConfigDialog(
                root=root, config=copy.deepcopy(default_config()),
                using_defaults=True, scan_result=sr,
                base_config=default_config(), save_callback=None,
                worker_registry=w2, parent=w2)
            dlg2._closed = True
            label_before = dlg2.preview_label.text()
            dlg2._on_preview_failed(dlg2._preview_seq, dlg2.root, "迟到失败")
            dlg2._on_preview_done(dlg2._preview_seq, dlg2.root, object())
            ignored_closed = (opened["n"] == 0 and critical["n"] == 0
                              and dlg2.preview_label.text() == label_before)
            dlg2._closed = False
            dlg2._on_preview_done(dlg2._preview_seq - 1, dlg2.root, object())
            dlg2._on_preview_done(dlg2._preview_seq, "/tmp/other_root",
                                  object())
            ignored_stale = opened["n"] == 0 and critical["n"] == 0

            config_dialog.PreviewWorker = real_worker_cls
            dlg2._on_preview()
            accepted = wait_until(
                app, lambda: opened["n"] == 1 and not dlg2._previewing,
                timeout=20)
            normal_ok = (accepted and opened["result"] is not None
                         and opened["result"].sample_total == 3
                         and "试运行完成" in dlg2.preview_label.text())
            # 正常使用时的失败信息仍应显示（对照关闭等待期被忽略）
            critical["n"] = 0
            dlg3 = ClassificationConfigDialog(
                root=root, config=copy.deepcopy(default_config()),
                using_defaults=True, scan_result=sr,
                base_config=default_config(), save_callback=None,
                worker_registry=w2, parent=w2)
            dlg3._on_preview_failed(dlg3._preview_seq, dlg3.root,
                                    "模拟试运行失败")
            failure_ok = (critical["n"] == 1
                          and "失败" in dlg3.preview_label.text())
            dlg3.reject()
            dlg2.reject()
            w2.close()
            app.processEvents()

            ok = (started and close_wait and not_published and worker_alive
                  and finished and closed and cleaned and ignored_closed
                  and ignored_stale and normal_ok and failure_ok)
            rec("C17", "关闭等待期间不发布试运行结果；迟到/过期/错误根目录结果被忽略",
                "通过" if ok else "未通过",
                f"成功信号已排队且线程仍在={started}；关闭等待={close_wait}；"
                f"结果未发布/控件未恢复={not_published}；线程仍受管理={worker_alive}\n"
                f"放行后线程结束={finished}；窗口自动关闭且数据库关闭={closed}；"
                f"登记与引用已回收={cleaned}\n"
                f"已关闭/旧seq/错误root被忽略={ignored_closed and ignored_stale}；"
                f"正常结果仍接受={normal_ok}；正常失败仍提示={failure_ok}")
        finally:
            if worker is not None:
                worker.release.set()
            config_dialog.PreviewWorker = real_worker_cls
            config_dialog.ImpactPreviewDialog = real_ipd
            QMessageBox.critical = real_critical
    except Exception as e:
        rec("C17", "关闭等待期试运行结果发布", "未通过", f"异常：{e}")

    # ================= C18 取消编辑不阻塞界面（异步清理） =================
    try:
        import config_dialog
        root = fresh("c18_cancel")
        for i in range(3):
            mk(root, f"大二上/数据结构/f{i}.pdf", "q" * 64)
        sr = scan_directory(root, detect_project_roots=False)
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()

        real_worker_cls = config_dialog.PreviewWorker
        real_ipd = config_dialog.ImpactPreviewDialog
        real_critical = QMessageBox.critical
        opened = {"n": 0}

        class _RecorderIPD:
            def __init__(self, result, parent=None):
                opened["n"] += 1

            def exec(self):
                return 0

        class GatedRunWorker(real_worker_cls):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.entered = threading.Event()
                self.release = threading.Event()

            def run(self):
                self.entered.set()          # 已真实进入阻塞点
                self.release.wait(30)
                super().run()

        config_dialog.PreviewWorker = GatedRunWorker
        config_dialog.ImpactPreviewDialog = _RecorderIPD
        QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
        worker = None
        try:
            dlg = ClassificationConfigDialog(
                root=root, config=copy.deepcopy(default_config()),
                using_defaults=True, scan_result=sr,
                base_config=default_config(), save_callback=None,
                worker_registry=w, parent=w)
            dlg._on_preview()
            worker = dlg._worker
            deadline = time.time() + 10
            while (time.time() < deadline and worker is not None
                   and not worker.entered.is_set()):
                time.sleep(0.005)
            entered = worker is not None and worker.entered.is_set()

            heartbeat = []
            QTimer.singleShot(60, lambda: heartbeat.append(True))
            t0 = time.time()
            dlg.reject()
            elapsed = time.time() - t0
            pump(app, 0.2)
            no_block = elapsed < 1.0
            still_managed = (worker.isRunning()
                             and worker in w._rule_preview_workers
                             and w._storage is not None
                             and dlg._closed and dlg._previewing
                             and not dlg.tabs.isEnabled())

            # 取消后的迟到进度/结果/失败不能更新已关闭的编辑器或弹窗
            label_before = dlg.preview_label.text()
            opened["n"] = 0
            dlg._on_preview_progress(999, dlg.root, 1, 2, "x")
            dlg._on_preview_done(999, dlg.root, object())
            dlg._on_preview_failed(999, dlg.root, "迟到失败")
            late_ignored = (opened["n"] == 0
                            and dlg.preview_label.text() == label_before)

            # 线程仍阻塞时关闭主窗口：先等待，不关闭数据库
            w.close()
            pump(app, 0.15)
            close_wait = (w._close_pending and not w._closing
                          and w._storage is not None)

            worker.release.set()
            closed = wait_until(
                app, lambda: not w.isVisible() and w._storage is None, timeout=15)
            reclaimed = (not worker.isRunning()
                         and worker not in w._rule_preview_workers
                         and dlg._worker is None)
            ok = (entered and no_block and bool(heartbeat) and still_managed
                  and late_ignored and close_wait and closed and reclaimed)
            rec("C18", "取消编辑不在界面线程同步等待；迟到结果被忽略，线程结束后回收",
                "通过" if ok else "未通过",
                f"线程真实进入阻塞点={entered}；reject 耗时={elapsed:.3f}s"
                f"（<1s={no_block}）；Qt 心跳={bool(heartbeat)}\n"
                f"取消后线程仍运行且受主窗口登记={still_managed}；"
                f"迟到进度/结果/失败被忽略={late_ignored}\n"
                f"随后关闭主窗口：等待中={close_wait}；"
                f"线程结束后自动关闭且数据库关闭={closed}；引用回收={reclaimed}")
        finally:
            if worker is not None:
                worker.release.set()
            config_dialog.PreviewWorker = real_worker_cls
            config_dialog.ImpactPreviewDialog = real_ipd
            QMessageBox.critical = real_critical
    except Exception as e:
        rec("C18", "取消编辑异步清理", "未通过", f"异常：{e}")

    # ---- 汇总 ----
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if (failed == 0 and RESULTS) else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "v13_config",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v13_config.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "base": BASE,
        "elapsed_s": round(time.time() - t_suite, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
        "note": "C02/C13 使用明确标注的合成样本，不是真人分类错例。",
    }
    out = os.path.join(here, "results_v13_config.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_v13_config.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.3 第一轮 — 推荐模式规则配置化测试结果\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / "
                f"未测 {untested}\n退出码: {exit_code}\n\n")
        f.write("说明：C02/C13 使用明确标注的合成样本，不是真人分类错例。\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
