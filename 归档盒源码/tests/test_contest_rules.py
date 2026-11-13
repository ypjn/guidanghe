"""竞赛星级表：名单内的比赛归入竞赛，已登记的课程目录保持原分类。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from classification_config import (  # noqa: E402
    classify_context,
    compile_config,
    default_config,
    validate_config,
)
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER,
    classify_entries,
)
from scanner import scan_directory  # noqa: E402


def _plan(paths):
    root = tempfile.mkdtemp(prefix="archivebox_contest_")
    for rel in paths:
        full = os.path.join(root, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write("x")
    scan = scan_directory(root, detect_project_roots=False)
    items, _ = classify_entries(scan, MODE_BY_DIRECTION_SEMESTER, [])
    return {item.file.rel_path: item for item in items}


def main() -> None:
    errors = validate_config(default_config())
    if errors:
        raise SystemExit("默认配置不合法：\n" + "\n".join(errors))

    compiled = compile_config(default_config())
    contest_rules = [
        rule for rule in compiled.keyword_unmatched
        if rule.direction == "竞赛"
    ]
    if len(contest_rules) < 120:
        raise SystemExit(f"竞赛规则过少：{len(contest_rules)}")

    by = _plan([
        "第二十届全国大学生软件创新大赛-项目计划书模板.docx",
        "数学建模/论文.pdf",
        "大二上/全国大学生软件创新大赛/作品/说明.docx",
        "大二上/数据结构/作业/a.pdf",
        "大一下/国创赛/计划书.docx",
        "散落.txt",
        "大二上/附件-通知/n.pdf",
    ])

    software = by["第二十届全国大学生软件创新大赛-项目计划书模板.docx"]
    modeling = by["数学建模/论文.pdf"]
    inside = by["大二上/全国大学生软件创新大赛/作品/说明.docx"]
    course = by["大二上/数据结构/作业/a.pdf"]
    guochuang = by["大一下/国创赛/计划书.docx"]
    loose = by["散落.txt"]
    notice = by["大二上/附件-通知/n.pdf"]

    checks = [
        (software.direction == "竞赛" and software.category == "全国大学生软件创新大赛"
         and software.target_rel_path.startswith("_已整理/竞赛/全国大学生软件创新大赛/"),
         software.target_rel_path),
        (modeling.direction == "竞赛" and modeling.category == "数学建模"
         and modeling.target_rel_path == "_已整理/竞赛/数学建模/论文.pdf",
         modeling.target_rel_path),
        (inside.direction == "竞赛" and inside.category == "全国大学生软件创新大赛"
         and inside.target_rel_path.endswith("作品/说明.docx"),
         inside.target_rel_path),
        (course.direction == "课程" and course.category == "数据结构",
         f"{course.direction}/{course.category}"),
        (guochuang.direction == "竞赛" and guochuang.category == "国创赛",
         f"{guochuang.direction}/{guochuang.category}"),
        (loose.direction == "待定", loose.target_rel_path),
        (notice.direction == "校园事务" and notice.category == "通知",
         f"{notice.direction}/{notice.category}"),
    ]
    by.update(_plan([
        "alu.circ",
        "大二上/计组/计组实验/总电路.circ",
        "计组实验/个人实验/译码器.circ",
        "大二上/数据结构/作业/误放.circ",
    ]))
    loose_circ = by["alu.circ"]
    in_folder = by["大二上/计组/计组实验/总电路.circ"]
    below = by["计组实验/个人实验/译码器.circ"]
    misplaced = by["大二上/数据结构/作业/误放.circ"]
    checks.extend([
        (loose_circ.direction == "课程"
         and loose_circ.category == "计算机组成原理"
         and loose_circ.semester == "大二上"
         and loose_circ.target_rel_path
         == "_已整理/课程/大二上/计算机组成原理/工程图/alu.circ",
         loose_circ.target_rel_path),
        (in_folder.category == "计算机组成原理"
         and in_folder.rule_key.startswith("semester_dir:")
         and "计组实验" in in_folder.target_rel_path,
         f"{in_folder.rule_key} {in_folder.target_rel_path}"),
        (below.category == "计算机组成原理"
         and below.rule_key == "ext:circ"
         and below.target_rel_path
         == "_已整理/课程/大二上/计算机组成原理/工程图/计组实验/个人实验/译码器.circ",
         below.target_rel_path),
        (misplaced.category == "计算机组成原理"
         and misplaced.target_rel_path
         == "_已整理/课程/大二上/计算机组成原理/工程图/作业/误放.circ",
         misplaced.target_rel_path),
    ])
    failed = [detail for ok, detail in checks if not ok]
    if failed:
        raise SystemExit("分类不符合预期：\n" + "\n".join(failed))

    # 直接走分类函数，确认未登记学期目录会命中竞赛名单
    hit = classify_context(
        "大一下/蓝桥杯/报名.pdf", compile_config(default_config()))
    if hit.direction != "竞赛" or hit.category != "蓝桥杯全国软件和信息技术专业人才大赛":
        raise SystemExit(f"蓝桥杯未归入竞赛：{hit}")
    print(f"竞赛规则 {len(contest_rules)} 条，抽样通过")


if __name__ == "__main__":
    main()
