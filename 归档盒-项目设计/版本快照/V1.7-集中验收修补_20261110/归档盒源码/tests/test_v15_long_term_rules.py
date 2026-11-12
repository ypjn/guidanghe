"""
归档盒 V1.5 — 长期修正规则（保存为长期规则）专项验证

覆盖（对应用户要求的 10 类场景）：
T01 临时修正流程保持：仅在当前计划生效，不写配置
T02 由手动修正生成长期规则 → 保存 → 重新生成后自动命中
T03 新规则覆盖已经命中原有学期/课程映射的文件
T04 精确路径匹配只命中该文件
T05 目录范围：包含子目录 / 仅本级 / 路径边界（课程A 不匹配 课程AB）
T06 文件名关键词匹配，可限定来源目录
T07 重叠规则按顺序执行；启停、删除、排序实际改变计划；rule_id 不随行号变化
T08 试运行与正式计划的分类、目标及依据一致
T09 保存、重启、切换根目录后规则恢复且互不串用
T10 取消 / 非法配置 / 保存失败不破坏既有状态
T11 内部目录层级保留；目标冲突时所有副本保留（真实移动）
T12 真实整理 → 重开 → 查看历史（长期规则标识/依据）→ 撤销
T13 先单文件修正，再做规则试运行，最后关闭窗口，任务生命周期正常
T14 历史明细/批次提示/CSV/JSON 导出都包含长期规则标识与依据
T15 试运行在后台执行：界面不阻塞、运行中可关闭、迟到结果丢弃、线程回收
T16 试运行缓存与当前输入一致：内部路径/匹配/目标改动后必须先重新试运行

所有实际文件操作只在独立临时目录进行。
合成样本，非真人分类错例。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v15_long_term_rules.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

import classification_config as CFG
from classification_config import (
    ClassificationConfigError, default_config, load_config, save_config,
    config_path, config_to_dict, validate_config, ensure_valid,
    LongTermRule, new_rule_id,
    MATCH_EXACT_PATH, MATCH_DIR_SCOPE, MATCH_FILENAME_KEYWORD,
    INNER_KEEP, INNER_REPLACE, INNER_CLEAR,
    match_long_term_rule, compile_config,
)
from planner import (
    MODE_BY_DIRECTION_SEMESTER, build_plan, generate_plan,
)
from scanner import scan_directory
from storage import Storage
from executor import execute_plan, undo_latest_batch
from plan_patch import (
    OverrideValidationError, OverrideParams,
    apply_override_to_plan, revert_auto_override,
    build_rule_draft, preview_rule_impact,
)

RESULTS = []
BASE = "/tmp/_v15_long_term_rules"


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence).replace("\x00", "")[:4000],
                    "kind": "offscreen"})
    print(f"[{'PASS' if status == '通过' else 'FAIL'}] {tid}  {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def fresh(tag):
    path = os.path.join(BASE, tag)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_of(root, cfg=None, compute_duplicates=False):
    sr = scan_directory(root, detect_project_roots=False)
    return sr, build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          config=cfg, compute_duplicates=compute_duplicates)


def item_of(plan, rel):
    for i in plan.items:
        if i.file.rel_path == rel:
            return i
    raise AssertionError(f"计划中缺少 {rel}")


def dir_rule(name, source, direction, category="", sub=True,
             inner_mode=INNER_KEEP, inner_path="", rule_id=None):
    return LongTermRule(
        rule_id=rule_id or new_rule_id(), name=name,
        match_type=MATCH_DIR_SCOPE, source_path=source,
        include_subdirs=sub, direction=direction, semester="",
        category=category, inner_mode=inner_mode, inner_path=inner_path,
    )


def main():
    t0 = time.time()
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    app = QApplication.instance() or QApplication([])
    # 所有提示类弹窗一律返回 Ok/Yes，避免测试被真实模态框阻塞
    for _n in ("information", "warning", "critical"):
        setattr(QMessageBox, _n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # ================= T01 临时修正仍只影响当前计划 =================
    try:
        root = fresh("t01")
        mk(root, "大二上/数据结构/作业/a.pdf", "t01-a")
        sr, plan = plan_of(root)
        rel = "大二上/数据结构/作业/a.pdf"
        apply_override_to_plan(plan, rel, OverrideParams(
            direction="项目", semester="", category="临时改判"))
        it = item_of(plan, rel)
        in_plan = (it.is_manual_overridden and it.direction == "项目"
                   and "项目" in it.target_rel_path)
        cfg_after, src = load_config(root)
        no_rule = len(cfg_after.long_term_rules) == 0
        # 重新生成 → 临时修正消失
        _, plan2 = plan_of(root)
        it2 = item_of(plan2, rel)
        restored = (not it2.is_manual_overridden
                    and it2.direction == "课程")
        ok = in_plan and no_rule and restored and src == "default"
        rec("T01", "临时修正只在当前计划生效，不写配置、重扫后消失",
            "通过" if ok else "未通过",
            f"计划内生效={in_plan}；配置无长期规则={no_rule}；"
            f"重扫恢复自动={restored}；配置来源={src}")
    except Exception as e:
        rec("T01", "临时修正流程", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T02 生成规则 → 保存 → 重新生成命中 =================
    try:
        root = fresh("t02")
        mk(root, "大二上/数据结构/作业/a.pdf", "t02-a")
        sr, plan = plan_of(root)
        rel = "大二上/数据结构/作业/a.pdf"
        params = OverrideParams(direction="项目", semester="", category="数据大创",
                                inner_path="")
        rule = build_rule_draft(
            params=params, name="a.pdf 精确规则",
            match_type=MATCH_EXACT_PATH, source_path=rel,
            include_subdirs=True, keywords=[],
            inner_mode=INNER_KEEP, inner_path="",
            reason="由手动修正生成")
        cfg = default_config()
        cfg.long_term_rules.insert(0, rule)
        ensure_valid(cfg)
        saved = save_config(root, cfg)
        cfg_loaded, src = load_config(root)
        _, plan2 = plan_of(root, cfg=cfg_loaded)
        it = item_of(plan2, rel)
        ok = (saved and src == "custom"
              and len(cfg_loaded.long_term_rules) == 1
              and cfg_loaded.long_term_rules[0].rule_id == rule.rule_id
              and it.rule_key == f"long_term:{rule.rule_id}"
              and it.direction == "项目"
              and it.inner_path == "作业")
        rec("T02", "由手动修正生成长期规则 → 保存 → 重新生成后自动命中",
            "通过" if ok else "未通过",
            f"写入配置={saved}；来源={src}；规则数={len(cfg_loaded.long_term_rules)}\n"
            f"rule_key={it.rule_key}\n方向={it.direction} 内部={it.inner_path}\n"
            f"目标={it.target_rel_path}")
    except Exception as e:
        rec("T02", "规则生成并保存", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T03 覆盖已命中原有映射的文件 =================
    try:
        root = fresh("t03")
        mk(root, "大二上/数据结构/作业/a.pdf", "t03-a")
        sr, p0 = plan_of(root)
        rel = "大二上/数据结构/作业/a.pdf"
        old = item_of(p0, rel)
        old_key = old.rule_key
        old_target = old.target_rel_path

        cfg = default_config()
        cfg.long_term_rules.insert(0, dir_rule(
            "数据结构整体归项目", "大二上/数据结构", "项目", "数据大创"))
        _, p1 = plan_of(root, cfg=cfg)
        new = item_of(p1, rel)
        ok = (old_key.startswith("semester_dir:")
              and new.rule_key.startswith("long_term:")
              and new.direction == "项目"
              and new.target_rel_path.startswith(
                  os.path.join("_已整理", "项目", "数据大创"))
              and new.inner_path == "作业"
              and new.target_rel_path != old_target)
        rec("T03", "长期规则覆盖已命中「学期内目录」映射的文件",
            "通过" if ok else "未通过",
            f"原依据={old_key}\n原目标={old_target}\n"
            f"新依据={new.rule_key}\n新目标={new.target_rel_path}\n"
            f"保留内部路径={new.inner_path}")
    except Exception as e:
        rec("T03", "覆盖原有映射", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T04 精确路径只命中该文件 =================
    try:
        root = fresh("t04")
        mk(root, "大二上/数据结构/作业/a.pdf", "t04-a")
        mk(root, "大二上/数据结构/作业/b.pdf", "t04-b")
        cfg = default_config()
        cfg.long_term_rules.insert(0, LongTermRule(
            rule_id="lt-t04", name="只改 a.pdf",
            match_type=MATCH_EXACT_PATH,
            source_path="大二上/数据结构/作业/a.pdf",
            direction="项目", semester="", category="单文件"))
        _, plan = plan_of(root, cfg=cfg)
        a = item_of(plan, "大二上/数据结构/作业/a.pdf")
        b = item_of(plan, "大二上/数据结构/作业/b.pdf")
        ok = (a.rule_key == "long_term:lt-t04" and a.direction == "项目"
              and b.rule_key != "long_term:lt-t04"
              and b.direction == "课程")
        rec("T04", "精确路径匹配只改变目标文件，同目录其他文件不受影响",
            "通过" if ok else "未通过",
            f"a 依据={a.rule_key} 方向={a.direction}\n"
            f"b 依据={b.rule_key} 方向={b.direction}")
    except Exception as e:
        rec("T04", "精确路径匹配", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T05 目录范围与路径边界 =================
    try:
        root = fresh("t05")
        mk(root, "大二上/数据结构/direct.pdf", "t05-1")
        mk(root, "大二上/数据结构/作业/sub.pdf", "t05-2")
        mk(root, "大二上/数据结构实验/other.pdf", "t05-3")

        cfg_only = default_config()
        cfg_only.long_term_rules.insert(0, dir_rule(
            "数据结构仅本级", "大二上/数据结构", "项目", "仅本级",
            sub=False))
        _, plan_only = plan_of(root, cfg=cfg_only)

        cfg_sub = default_config()
        cfg_sub.long_term_rules.insert(0, dir_rule(
            "数据结构含子目录", "大二上/数据结构", "项目", "含子目录",
            sub=True))
        _, plan_sub = plan_of(root, cfg=cfg_sub)

        direct = item_of(plan_only, "大二上/数据结构/direct.pdf")
        sub_only = item_of(plan_only, "大二上/数据结构/作业/sub.pdf")
        sub_in = item_of(plan_sub, "大二上/数据结构/作业/sub.pdf")
        sibling = item_of(plan_sub, "大二上/数据结构实验/other.pdf")

        ok = (direct.rule_key.startswith("long_term:")
              and not sub_only.rule_key.startswith("long_term:")
              and sub_in.rule_key.startswith("long_term:")
              # 路径边界：「数据结构」不应命中「数据结构实验」
              and not sibling.rule_key.startswith("long_term:"))
        rec("T05", "目录范围：仅本级 / 含子目录 / 路径边界不误匹配",
            "通过" if ok else "未通过",
            f"本级目录直接子文件命中={direct.rule_key}\n"
            f"仅本级模式下子目录文件={sub_only.rule_key}\n"
            f"含子目录模式下子目录文件={sub_in.rule_key}\n"
            f"相似目录（数据结构实验）={sibling.rule_key}")
    except Exception as e:
        rec("T05", "目录范围与边界", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T06 文件名关键词（可限定目录） =================
    try:
        root = fresh("t06")
        mk(root, "大二上/数据结构/期末试卷.pdf", "t06-a")
        mk(root, "大二上/数据结构/作业.pdf", "t06-b")
        mk(root, "大二上/C++程序设计/期末试卷.pdf", "t06-c")

        cfg = default_config()
        cfg.long_term_rules.insert(0, LongTermRule(
            rule_id="lt-t06", name="数据结构期末归复习",
            match_type=MATCH_FILENAME_KEYWORD,
            source_path="大二上/数据结构",
            keywords=["期末"],
            direction="课程", semester="大二上", category="期末复习"))
        _, plan = plan_of(root, cfg=cfg)
        hit = item_of(plan, "大二上/数据结构/期末试卷.pdf")
        miss_name = item_of(plan, "大二上/数据结构/作业.pdf")
        out_scope = item_of(plan, "大二上/C++程序设计/期末试卷.pdf")
        ok = (hit.rule_key == "long_term:lt-t06"
              and not miss_name.rule_key.startswith("long_term:")
              and not out_scope.rule_key.startswith("long_term:"))
        rec("T06", "文件名关键词命中，且限定目录之外的同名关键词不命中",
            "通过" if ok else "未通过",
            f"目录内+关键词={hit.rule_key}\n"
            f"目录内但无关键词={miss_name.rule_key}\n"
            f"目录外但有关键词={out_scope.rule_key}")
    except Exception as e:
        rec("T06", "文件名关键词", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T07 优先级 / 启停 / 删除 / rule_id 稳定 =================
    try:
        root = fresh("t07")
        mk(root, "大二上/数据结构/作业/a.pdf", "t07-a")
        rel = "大二上/数据结构/作业/a.pdf"
        rule_a = dir_rule("规则A", "大二上/数据结构", "项目", "A类",
                          rule_id="lt-a")
        rule_b = dir_rule("规则B", "大二上/数据结构", "竞赛", "B类",
                          rule_id="lt-b")

        cfg = default_config()
        cfg.long_term_rules = [rule_a, rule_b]
        _, p1 = plan_of(root, cfg=cfg)
        first = item_of(p1, rel).rule_key

        cfg2 = default_config()
        cfg2.long_term_rules = [rule_b, rule_a]
        _, p2 = plan_of(root, cfg=cfg2)
        swapped = item_of(p2, rel).rule_key

        cfg3 = default_config()
        rule_a_off = dir_rule("规则A", "大二上/数据结构", "项目", "A类",
                              rule_id="lt-a")
        rule_a_off.enabled = False
        cfg3.long_term_rules = [rule_a_off, rule_b]
        _, p3 = plan_of(root, cfg=cfg3)
        disabled = item_of(p3, rel).rule_key

        cfg4 = default_config()
        cfg4.long_term_rules = [rule_b]
        _, p4 = plan_of(root, cfg=cfg4)
        deleted = item_of(p4, rel).rule_key

        ids_stable = (rule_a.rule_id == "lt-a"
                      and cfg2.long_term_rules[1].rule_id == "lt-a")

        ok = (first == "long_term:lt-a"
              and swapped == "long_term:lt-b"
              and disabled == "long_term:lt-b"
              and deleted == "long_term:lt-b"
              and ids_stable)
        rec("T07", "重叠规则按顺序取第一条；排序/启停/删除实际改变计划；标识稳定",
            "通过" if ok else "未通过",
            f"A在前={first}\nB在前={swapped}\n禁用A={disabled}\n删除A={deleted}\n"
            f"rule_id 不随行号变化={ids_stable}")
    except Exception as e:
        rec("T07", "优先级与启停删除", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T08 试运行与正式计划一致 =================
    try:
        root = fresh("t08")
        mk(root, "大二上/数据结构/作业/a.pdf", "t08-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t08-b")
        mk(root, "大二上/数据结构/讲义/c.pdf", "t08-c")
        sr, base_plan = plan_of(root)
        cfg0 = default_config()
        rule = dir_rule("数据结构归项目", "大二上/数据结构", "项目", "数据大创")
        impact = preview_rule_impact(sr, MODE_BY_DIRECTION_SEMESTER, [],
                                     cfg0, rule)

        cfg = default_config()
        cfg.long_term_rules.insert(0, rule)
        _, real_plan = plan_of(root, cfg=cfg)

        # 逐项对比：命中数、变化数、代表样本的目标与依据
        real_changed = 0
        mismatches = []
        sample_ok = True
        for item in real_plan.items:
            old = item_of(base_plan, item.file.rel_path)
            if old.target_rel_path != item.target_rel_path:
                real_changed += 1
        for rel, _old_t, new_t, why in impact.samples:
            actual = item_of(real_plan, rel)
            if actual.target_rel_path != new_t or actual.rule_applied != why:
                sample_ok = False
                mismatches.append(rel)
        ok = (impact.error == ""
              and impact.matched == 3
              and impact.changed == real_changed
              and sample_ok)
        rec("T08", "试运行与正式计划的命中数/目标变化/依据完全一致",
            "通过" if ok else "未通过",
            f"试运行命中={impact.matched} 变化={impact.changed} 冲突={impact.conflicts}\n"
            f"正式计划实际变化={real_changed}\n"
            f"样本依据一致={sample_ok} 不一致项={mismatches}")
    except Exception as e:
        rec("T08", "试运行与正式计划一致", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T09 重启恢复 + 根目录隔离 =================
    try:
        root1 = fresh("t09_a")
        root2 = fresh("t09_b")
        mk(root1, "大二上/数据结构/作业/a.pdf", "t09-a")
        mk(root2, "大二上/数据结构/作业/a.pdf", "t09-b")

        cfg = default_config()
        cfg.long_term_rules.insert(0, dir_rule(
            "root1 专属", "大二上/数据结构", "项目", "一号目录"))
        save_config(root1, cfg)

        # 模拟重启：重新 load
        cfg1, src1 = load_config(root1)
        cfg2, src2 = load_config(root2)
        _, p1 = plan_of(root1, cfg=cfg1)
        _, p2 = plan_of(root2, cfg=cfg2)
        i1 = item_of(p1, "大二上/数据结构/作业/a.pdf")
        i2 = item_of(p2, "大二上/数据结构/作业/a.pdf")
        ok = (src1 == "custom" and len(cfg1.long_term_rules) == 1
              and cfg1.long_term_rules[0].name == "root1 专属"
              and src2 == "default" and len(cfg2.long_term_rules) == 0
              and i1.rule_key.startswith("long_term:")
              and not i2.rule_key.startswith("long_term:"))
        rec("T09", "保存→重启→规则恢复；不同根目录互不串用",
            "通过" if ok else "未通过",
            f"root1 来源={src1} 规则数={len(cfg1.long_term_rules)} "
            f"名称={cfg1.long_term_rules[0].name if cfg1.long_term_rules else None}\n"
            f"root2 来源={src2} 规则数={len(cfg2.long_term_rules)}\n"
            f"root1 命中={i1.rule_key}；root2 命中={i2.rule_key}")
    except Exception as e:
        rec("T09", "重启与根目录隔离", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T10 取消 / 非法 / 保存失败不破坏状态 =================
    try:
        root = fresh("t10")
        mk(root, "大二上/数据结构/作业/a.pdf", "t10-a")
        cfg = default_config()
        cfg.long_term_rules.insert(0, dir_rule(
            "原有规则", "大二上/数据结构", "项目", "原目标"))
        save_config(root, cfg)
        before = open(config_path(root), encoding="utf-8").read()

        # 1) 非法规则：名称为空
        bad = default_config()
        bad.long_term_rules.insert(0, LongTermRule(
            rule_id="lt-bad", name="", match_type=MATCH_DIR_SCOPE,
            source_path="x", direction="项目", category=""))
        errs_empty_name = validate_config(bad)

        # 2) 非法路径：路径逃逸
        bad2 = default_config()
        bad2.long_term_rules.insert(0, LongTermRule(
            rule_id="lt-bad2", name="逃逸", match_type=MATCH_EXACT_PATH,
            source_path="../secret.txt", direction="项目", category=""))
        errs_escape = validate_config(bad2)

        # 3) 保存非法配置应抛错且原文件不变
        raised = False
        try:
            save_config(root, bad2)
        except ClassificationConfigError:
            raised = True
        after = open(config_path(root), encoding="utf-8").read()

        # 4) 取消编辑 = 不调用 save，配置字节不变（这里用 before==after 表达）
        ok = (len(errs_empty_name) > 0 and len(errs_escape) > 0
              and raised and before == after)
        rec("T10", "取消/非法配置/保存失败不破坏既有配置",
            "通过" if ok else "未通过",
            f"空名称错误数={len(errs_empty_name)}；"
            f"路径逃逸错误数={len(errs_escape)}\n"
            f"非法保存被拒绝={raised}；原文件字节不变={before == after}")
    except Exception as e:
        rec("T10", "非法配置与保存失败", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T11 内部层级保留 + 冲突副本保留 =================
    try:
        root = fresh("t11")
        mk(root, "甲组/dup.txt", "same-name-a")
        mk(root, "乙组/dup.txt", "same-name-b")
        cfg = default_config()
        cfg.long_term_rules = [
            dir_rule("甲组合并", "甲组", "项目", "合并", rule_id="lt-j"),
            dir_rule("乙组合并", "乙组", "项目", "合并", rule_id="lt-y"),
        ]
        _, plan = plan_of(root, cfg=cfg, compute_duplicates=True)
        targets = sorted(i.target_filename for i in plan.items)
        both_in_same_dir = len({
            os.path.dirname(i.target_rel_path) for i in plan.items}) == 1

        st = Storage(root)
        result = execute_plan(plan, st)
        dest_dir = os.path.join(root, "_已整理", "项目", "合并")
        files = sorted(os.listdir(dest_dir)) if os.path.isdir(dest_dir) else []
        contents = sorted(
            open(os.path.join(dest_dir, f), "rb").read().decode("utf-8")
            for f in files)
        ok = (result.moved >= 2
              and both_in_same_dir
              and any("(1)" in t for t in targets)
              and len(files) == 2
              and contents == ["same-name-a", "same-name-b"])
        rec("T11", "同名目标自动分配唯一名，执行后两份副本都保留",
            "通过" if ok else "未通过",
            f"计划目标文件名={targets}\n同一目标目录={both_in_same_dir}\n"
            f"实际文件={files}\n内容={contents}")
    except Exception as e:
        rec("T11", "冲突副本保留", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T12 整理 → 重开 → 历史 → 撤销 =================
    try:
        root = fresh("t12")
        mk(root, "大二上/数据结构/作业/a.pdf", "t12-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t12-b")
        cfg = default_config()
        rule = dir_rule("数据结构归项目", "大二上/数据结构", "项目", "历史验证",
                        rule_id="lt-t12")
        cfg.long_term_rules.insert(0, rule)
        save_config(root, cfg)

        cfg_loaded, _ = load_config(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          config=cfg_loaded, compute_duplicates=True)
        pre_sha = {
            i.file.rel_path: sha(os.path.join(root, i.file.rel_path))
            for i in plan.items
        }
        result = execute_plan(plan, Storage(root))
        n = len(plan.items)

        # 模拟重开程序：新 Storage 实例
        st2 = Storage(root)
        actions = st2.get_actions(result.batch_id)
        all_long_term = all(
            a.rule_key == "long_term:lt-t12" for a in actions)
        has_reason = all("数据结构归项目" in a.rule_applied for a in actions)

        batch = st2.get_batch(result.batch_id)
        snap_rules = (batch.config_snapshot.get("long_term_rules") or [])
        snapshot_has_rule = any(
            r.get("rule_id") == "lt-t12" for r in snap_rules)

        ures = undo_latest_batch(Storage(root))
        restored = []
        for a in st2.get_actions(result.batch_id):
            if a.status in ("moved", "undone", "conflict"):
                src_abs = os.path.join(root, a.source_rel)
                restored.append(
                    os.path.isfile(src_abs)
                    and sha(src_abs) == pre_sha.get(a.source_rel))
        ok = (result.moved == n and all_long_term and has_reason
              and snapshot_has_rule and all(restored) and len(restored) == n)
        rec("T12", "真实整理→重开→历史保留长期规则标识/依据→撤销恢复内容",
            "通过" if ok else "未通过",
            f"移动={result.moved}/{n}\n"
            f"全部记录 rule_key=long_term:lt-t12 → {all_long_term}\n"
            f"依据含规则名 → {has_reason}\n"
            f"批次配置快照含该规则 → {snapshot_has_rule}\n"
            f"撤销后源文件与 SHA 恢复 → {all(restored)} ({len(restored)}/{n})")
    except Exception as e:
        rec("T12", "历史与撤销", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T13 生命周期：修正 → 试运行 → 关闭 =================
    try:
        from main import MainWindow

        root = fresh("t13")
        mk(root, "大二上/数据结构/作业/a.pdf", "t13-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t13-b")

        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w.show()
        app.processEvents()

        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          config=default_config(), compute_duplicates=False)
        rel = "大二上/数据结构/作业/a.pdf"
        apply_override_to_plan(plan, rel, OverrideParams(
            direction="项目", semester="", category="生命周期验证"))
        it = item_of(plan, rel)

        rule = build_rule_draft(
            params=OverrideParams(direction="项目", semester="",
                                  category="生命周期验证"),
            name="生命周期规则", match_type=MATCH_DIR_SCOPE,
            source_path="大二上/数据结构", include_subdirs=True,
            keywords=[], inner_mode=INNER_KEEP, inner_path="")
        impact = preview_rule_impact(
            sr, MODE_BY_DIRECTION_SEMESTER, [], default_config(), rule)

        w.close()
        app.processEvents()

        ok = (it.is_manual_overridden
              and impact.error == ""
              and impact.matched == 2
              and not getattr(w, "_plan_working", False))
        rec("T13", "先单文件修正，再做规则试运行，最后关闭窗口，生命周期正常",
            "通过" if ok else "未通过",
            f"临时修正已应用={it.is_manual_overridden}\n"
            f"试运行命中={impact.matched} 错误={impact.error!r}\n"
            f"关闭后无后台计划任务={not getattr(w, '_plan_working', False)}")
    except Exception as e:
        rec("T13", "生命周期", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T14 历史界面与导出包含长期规则信息 =================
    try:
        import csv as _csv
        import main as main_mod
        from main import MainWindow, HistoryDialog

        root = fresh("t14")
        mk(root, "大二上/数据结构/作业/a.pdf", "t14-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t14-b")

        cfg = default_config()
        cfg.long_term_rules.insert(0, dir_rule(
            "数据结构归项目", "大二上/数据结构", "项目", "历史展示",
            rule_id="lt-t14"))
        save_config(root, cfg)
        cfg_loaded, _ = load_config(root)
        sr = scan_directory(root, detect_project_roots=False)
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                          config=cfg_loaded, compute_duplicates=True)
        result = execute_plan(plan, Storage(root))

        # 该目录已有批次：主窗口初始化会询问"是否撤销"。本用例只想看历史，
        # 这里临时回答"否"，避免触发撤销流程影响被验证的记录。
        real_question = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
        try:
            w = MainWindow()
            w.path_edit.setText(root)
            w._reset_for_new_dir(root)
        finally:
            QMessageBox.question = real_question
        w.show()
        app.processEvents()
        hist = HistoryDialog(w)
        hist._selected_batch_id = result.batch_id
        hist.load_items(force=True)
        app.processEvents()

        headers = [hist.item_table.horizontalHeaderItem(i).text()
                   for i in range(hist.item_table.columnCount())]
        col = headers.index("分类来源")
        vals = [hist.item_table.item(r, col).text()
                for r in range(hist.item_table.rowCount())]
        tip0 = hist.item_table.item(0, col).toolTip() or ""

        batch_attention = ""
        for r in range(hist.batch_table.rowCount()):
            cell = hist.batch_table.item(r, 0)
            if cell is not None and cell.text() == f"#{result.batch_id}":
                batch_attention = hist.batch_table.item(
                    r, hist.batch_table.columnCount() - 1).text()

        out_json = os.path.join(root, "_export.json")
        out_csv = os.path.join(root, "_export.csv")

        class _FakeFileDialog:
            """替换 main.QFileDialog，避免真实保存对话框阻塞测试。"""
            queue = []

            @staticmethod
            def getSaveFileName(*a, **k):
                return _FakeFileDialog.queue.pop(0)

        _FakeFileDialog.queue = [(out_json, "JSON 文件 (*.json)"),
                                 (out_csv, "CSV 文件 (*.csv)")]
        real_fd = main_mod.QFileDialog
        main_mod.QFileDialog = _FakeFileDialog
        try:
            hist.btn_export.setEnabled(True)
            hist._on_export()
            hist.btn_export.setEnabled(True)
            hist._on_export()
        finally:
            main_mod.QFileDialog = real_fd

        with open(out_json, encoding="utf-8") as f:
            jd = json.load(f)
        ja = jd["actions"][0]
        json_ok = (ja.get("rule_key") == "long_term:lt-t14"
                   and "数据结构归项目" in (ja.get("rule_applied") or "")
                   and ja.get("classification_source") == "长期规则")

        with open(out_csv, encoding="utf-8-sig") as f:
            rows = list(_csv.reader(f))
        head = rows[1]
        csv_ok = ("规则标识" in head and "命中依据" in head
                  and "long_term:lt-t14" in rows[2])

        ok = (set(vals) == {"长期规则"}
              and "long_term:lt-t14" in tip0
              and "长期规则" in batch_attention
              and json_ok and csv_ok)
        rec("T14", "历史明细显示「长期规则」并带规则依据；CSV/JSON 导出含规则标识与依据",
            "通过" if ok else "未通过",
            f"分类来源列={vals}\n悬浮提示={tip0!r}\n批次注意列={batch_attention!r}\n"
            f"JSON 字段={json_ok}（rule_key={ja.get('rule_key')}）\n"
            f"CSV 表头含规则列={csv_ok}")
        hist.reject()
        w.close()
        app.processEvents()
    except Exception as e:
        rec("T14", "历史界面与导出", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ================= T15 试运行在后台执行，不阻塞界面 =================
    try:
        import plan_patch as pp
        from plan_patch import RuleDraftDialog

        class _Registry:
            def __init__(self):
                self.workers = []
            def register_preview_worker(self, worker):
                self.workers.append(worker)
            def unregister_preview_worker(self, worker):
                if worker in self.workers:
                    self.workers.remove(worker)

        root = fresh("t15")
        mk(root, "大二上/数据结构/作业/a.pdf", "t15-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t15-b")
        sr = scan_directory(root, detect_project_roots=False)
        cfg = default_config()
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                          compute_duplicates=False)

        real_preview = pp.preview_rule_impact
        gate = {"delay": 0.6}

        def slow_preview(*a, **k):
            time.sleep(gate["delay"])
            return real_preview(*a, **k)

        pp.preview_rule_impact = slow_preview
        registry = _Registry()
        try:
            dlg = RuleDraftDialog(
                mode=MODE_BY_DIRECTION_SEMESTER, plan=plan,
                source_rel="大二上/数据结构/作业/a.pdf",
                params=OverrideParams(direction="项目", semester="",
                                      category="后台验证"),
                scan_result=sr, config=cfg, save_callback=None,
                worker_registry=registry)
            dlg.match_combo.setCurrentIndex(1)
            dlg.source_edit.setText("大二上/数据结构")

            t0 = time.time()
            dlg._on_preview()
            call_elapsed = time.time() - t0

            # 0.35 秒内主线程应能持续处理事件（心跳）
            ticks = 0
            while time.time() - t0 < 0.35:
                app.processEvents()
                ticks += 1
                time.sleep(0.005)

            running_mid = dlg._previewing and dlg._worker is not None

            # 运行中关闭：应立即返回，不阻塞
            t1 = time.time()
            dlg._on_closed()
            close_elapsed = time.time() - t1
            impact_text_after_close = dlg.impact_view.toPlainText()

            deadline = time.time() + 8
            while time.time() < deadline:
                app.processEvents()
                if dlg._worker is None:
                    break
                time.sleep(0.01)
            app.processEvents()

            released = (dlg._worker is None and not registry.workers)
            late_ignored = ("命中文件数" not in impact_text_after_close)
            ok = (call_elapsed < 0.25 and ticks > 30
                  and running_mid and close_elapsed < 0.2
                  and released and late_ignored and dlg._closed)
            rec("T15", "试运行在后台线程执行：界面不阻塞、运行中可关闭、迟到结果丢弃、线程回收",
                "通过" if ok else "未通过",
                f"调用返回耗时={call_elapsed:.3f}s（应 <0.25s）\n"
                f"0.35s 内事件心跳={ticks} 次（应 >30）\n"
                f"中途仍在运行={running_mid}\n"
                f"关闭返回耗时={close_elapsed:.3f}s（应 <0.2s）\n"
                f"关闭后迟到结果被忽略={late_ignored}\n"
                f"线程已回收={released}（registry 剩余={len(registry.workers)}）")
            dlg.dlg.accept()
        finally:
            pp.preview_rule_impact = real_preview
    except Exception as e:
        rec("T15", "后台试运行", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ========== T16 试运行缓存与当前输入的一致性 ==========
    try:
        from plan_patch import RuleDraftDialog

        root = fresh("t16")
        mk(root, "大二上/数据结构/作业/a.pdf", "t16-a")
        mk(root, "大二上/数据结构/实验/b.pdf", "t16-b")
        sr = scan_directory(root, detect_project_roots=False)
        cfg = default_config()
        plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                          compute_duplicates=False)
        src_rel = "大二上/数据结构/作业/a.pdf"

        def make_draft(saved):
            def cb(new_cfg):
                saved.append(new_cfg)

            return RuleDraftDialog(
                mode=MODE_BY_DIRECTION_SEMESTER, plan=plan,
                source_rel=src_rel,
                params=OverrideParams(direction="项目", semester="",
                                      category="缓存验证"),
                scan_result=sr, config=cfg, save_callback=cb)

        def do_preview(dlg, timeout=10):
            dlg._on_preview()
            deadline = time.time() + timeout
            while time.time() < deadline:
                app.processEvents()
                if not dlg._previewing and dlg._impact is not None:
                    return True
                time.sleep(0.005)
            return False

        # 1) 试运行后只改「内部路径值」→ 缓存失效、保存被拒绝
        saved_value = []
        d1 = make_draft(saved_value)
        d1.match_combo.setCurrentIndex(1)
        d1.source_edit.setText("大二上/数据结构")
        d1.inner_combo.setCurrentText("替换为指定路径")
        d1.inner_edit.setText("旧目录")
        p1 = do_preview(d1)
        d1.inner_edit.setText("新目录")
        invalidated_value = (d1._impact is None and d1._previewed_rule is None)
        d1._on_save()
        reject_value = not saved_value

        # 2) 试运行后切换内部路径方式（替换 → 清空）→ 保存被拒绝
        saved_mode = []
        d2 = make_draft(saved_mode)
        d2.match_combo.setCurrentIndex(1)
        d2.source_edit.setText("大二上/数据结构")
        d2.inner_combo.setCurrentText("替换为指定路径")
        d2.inner_edit.setText("旧目录")
        p2 = do_preview(d2)
        d2.inner_combo.setCurrentText("清空内部路径")
        d2._on_save()
        reject_mode = not saved_mode

        # 3) 试运行后改关键词 / 目标类别 → 保存被拒绝
        saved_fields = []
        d3 = make_draft(saved_fields)
        d3.match_combo.setCurrentIndex(2)        # 文件名关键词
        d3.source_edit.setText("大二上/数据结构")
        d3.kw_edit.setText("作业")
        p3 = do_preview(d3)
        d3.kw_edit.setText("实验")
        d3._on_save()
        reject_kw = not saved_fields
        d3.cat_edit.setText("另一个类别")
        d3._on_save()
        reject_cat = not saved_fields

        # 4) 改值后重新试运行 → 保存的是新值
        saved_new = []
        d4 = make_draft(saved_new)
        d4.match_combo.setCurrentIndex(1)
        d4.source_edit.setText("大二上/数据结构")
        d4.inner_combo.setCurrentText("替换为指定路径")
        d4.inner_edit.setText("旧目录")
        p4 = do_preview(d4)
        d4.inner_edit.setText("新目录")
        d4._on_save()
        reject_before_retry = not saved_new
        p5 = do_preview(d4)
        d4._on_save()
        new_rule = saved_new[0].long_term_rules[0] if saved_new else None
        saved_new_value = (new_rule is not None
                           and new_rule.inner_mode == INNER_REPLACE
                           and new_rule.inner_path == "新目录")

        # 5) 未改动草稿时：保存内容与试运行规则、标识一致
        saved_same = []
        d5 = make_draft(saved_same)
        d5.match_combo.setCurrentIndex(1)
        d5.source_edit.setText("大二上/数据结构")
        p6 = do_preview(d5)
        preview_rule = d5._previewed_rule
        preview_sig = d5._rule_signature(preview_rule)
        d5._on_save()
        same_rule = saved_same[0].long_term_rules[0] if saved_same else None
        same_ident = (same_rule is not None and preview_rule is not None
                      and same_rule.rule_id == preview_rule.rule_id
                      and d5._rule_signature(same_rule) == preview_sig)

        ok16 = (p1 and p2 and p3 and p4 and p5 and p6
                and invalidated_value and reject_value and reject_mode
                and reject_kw and reject_cat and reject_before_retry
                and saved_new_value and same_ident)
        rec("T16", "试运行缓存与当前输入一致：改内部路径/匹配/目标后必须先重新试运行",
            "通过" if ok16 else "未通过",
            f"各次试运行成功={[p1, p2, p3, p4, p5, p6]}\n"
            f"改内部路径值后缓存失效={invalidated_value}；保存被拒={reject_value}\n"
            f"切换内部路径方式后保存被拒={reject_mode}\n"
            f"改关键词后保存被拒={reject_kw}；改目标类别后保存被拒={reject_cat}\n"
            f"未重试运行保存被拒={reject_before_retry}\n"
            f"重新试运行后保存新值={saved_new_value}\n"
            f"未改动时保存与试运行标识/字段一致={same_ident}")
        for d in (d1, d2, d3, d4, d5):
            try:
                d._on_closed()
                d.dlg.close()
            except Exception:
                pass
    except Exception as e:
        rec("T16", "试运行缓存一致性", "未通过",
            f"异常：{e}\n{traceback.format_exc()}")

    # ---- 汇总 ----
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 and RESULTS else 1
    meta = {
        "suite": "test_v15_long_term_rules",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python "
                   "tests/test_v15_long_term_rules.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "base": BASE,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "untested": untested},
        "note": "全部为 offscreen 自动化断言；合成样本，非真人分类错例。",
    }
    here = os.path.dirname(os.path.abspath(__file__))
    out_json = os.path.join(here, "results_v15_long_term_rules.json")
    out_txt = os.path.join(here, "results_v15_long_term_rules.txt")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.5 长期修正规则 — 专项验证\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"Python: {meta['python']}；platform: {meta['platform']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n退出码: {exit_code}\n\n")
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
    # 退出前在应用仍存活时确定性销毁所有顶层控件：
    # 本套件创建多个 MainWindow / HistoryDialog / RuleDraftDialog 且仅 close，
    # 若留到解释器结束阶段由 Qt 统一清理，父子析构顺序不稳定，
    # 可能在 QPushButtonWrapper 析构中崩溃（与断言结果无关的 139）。
    try:
        import main as _main_mod
        _main_mod.shutdown_widgets()
    except Exception as e:                       # 清理失败不应改变断言结论
        print(f"控件清理提示：{e}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
