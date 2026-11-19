"""阶段四质量与性能样本：可重复生成，不读取本人桌面或文稿原件。

小样本：课程/竞赛/项目/下载四类，每份文件写明预期去向依据。
大样本：按 --large-files 生成，用于观察扫描/预览瓶颈。

运行（同一脚本可在阶段三基线和候选版本上各跑一次）：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/stage4_quality.py \
      --root /tmp/stage4-quality --label candidate --large-files 5000
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sys
import time
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.append(SRC)

from content_extract import extract_contents  # noqa: E402
from planner import (  # noqa: E402
    CourseRule, MODE_BY_COURSE_THEN_TYPE, generate_plan,
)
from scanner import scan_directory  # noqa: E402
import planner as _planner_module  # noqa: E402
import scanner as _scanner_module  # noqa: E402

RULES = [
    CourseRule(keywords=["高等数学", "高数"], course_name="高等数学"),
    CourseRule(keywords=["数据结构"], course_name="数据结构"),
    CourseRule(keywords=["数学建模"], course_name="数学建模"),
    CourseRule(keywords=["项目周报"], course_name="项目"),
]

SMALL = [
    # (相对路径, 正文, 预期分类依据, 预期目标关键词)
    ("课程/高等数学/极限笔记.txt", "高等数学 极限 导数\n", "正文关键词：高等数学", "高等数学"),
    ("课程/高等数学/习题.md", "高等数学 第三章习题\n", "正文关键词：高等数学", "高等数学"),
    ("课程/数据结构/链表作业.txt", "数据结构 链表 作业\n", "正文关键词：数据结构", "数据结构"),
    ("课程/数据结构/栈.md", "数据结构 栈与队列\n", "正文关键词：数据结构", "数据结构"),
    ("竞赛/数学建模/论文.txt", "数学建模 论文 初稿\n", "正文关键词：数学建模", "数学建模"),
    ("竞赛/数学建模/数据.txt", "数学建模 附件数据\n", "正文关键词：数学建模", "数学建模"),
    ("项目/周报/第1周.txt", "项目周报 第 1 周\n", "正文关键词：项目周报", "项目"),
    ("项目/周报/第2周.txt", "项目周报 第 2 周\n", "正文关键词：项目周报", "项目"),
    ("下载/高等数学复习.txt", "高等数学 复习提纲\n", "正文关键词：高等数学", "高等数学"),
    ("下载/数据结构笔记.txt", "数据结构 笔记\n", "正文关键词：数据结构", "数据结构"),
    ("下载/数学建模通知.txt", "数学建模 比赛通知\n", "正文关键词：数学建模", "数学建模"),
    ("下载/项目周报模板.txt", "项目周报 模板\n", "正文关键词：项目周报", "项目"),
    ("下载/无关键词.txt", "没有可识别关键词的普通文件\n", "无关键词，预期进入未分类/待确认", ""),
    ("下载/扫描资料.pdf", "%PDF-1.4\n", "PDF 未支持，预期待确认", ""),
]


def write(root, rel, data: bytes):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def make_small(root):
    for rel, text, _why, _expected in SMALL:
        write(root, rel, text.encode("utf-8"))
    # 补足到 80 个文件，仍围绕四类，预期依据与上面一致。
    index = 0
    while sum(len(files) for _dir, _sub, files in os.walk(root)) < 80:
        group = index % 4
        index += 1
        if group == 0:
            write(root, f"课程/高等数学/补充{index:03d}.txt",
                  f"高等数学 补充 {index}\n".encode("utf-8"))
        elif group == 1:
            write(root, f"课程/数据结构/补充{index:03d}.md",
                  f"数据结构 补充 {index}\n".encode("utf-8"))
        elif group == 2:
            write(root, f"竞赛/数学建模/补充{index:03d}.txt",
                  f"数学建模 补充 {index}\n".encode("utf-8"))
        else:
            write(root, f"项目/周报/补充{index:03d}.txt",
                  f"项目周报 补充 {index}\n".encode("utf-8"))


def make_large(root, count):
    for index in range(count):
        group = index % 5
        if group == 0:
            rel = f"课程/高等数学/文件{index:05d}.txt"
            text = f"高等数学 第 {index} 份练习\n"
        elif group == 1:
            rel = f"课程/数据结构/文件{index:05d}.md"
            text = f"数据结构 第 {index} 份笔记\n"
        elif group == 2:
            rel = f"竞赛/数学建模/文件{index:05d}.txt"
            text = f"数学建模 第 {index} 份材料\n"
        elif group == 3:
            rel = f"项目/周报/文件{index:05d}.txt"
            text = f"项目周报 第 {index} 周\n"
        else:
            rel = f"下载/普通{index:05d}.txt"
            text = f"普通资料 {index}\n"
        write(root, rel, text.encode("utf-8"))


def measure(root, label, expected_map=None):
    t0 = time.perf_counter()
    scan = scan_directory(root)
    scan_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    extraction = extract_contents(scan.files, root=root)
    content_s = time.perf_counter() - t1

    t2 = time.perf_counter()
    plan = generate_plan(
        scan, MODE_BY_COURSE_THEN_TYPE, RULES,
        content_texts=extraction.texts,
        content_errors=extraction.errors,
    )
    plan_s = time.perf_counter() - t2

    ext = Counter(os.path.splitext(f.filename)[1].lower() for f in scan.files)
    types = dict(sorted(ext.items()))
    total_bytes = sum(f.size for f in scan.files)
    selected = sum(1 for item in plan.items if item.selected)
    ambiguous = sum(1 for item in plan.items
                    if getattr(item, "ambiguity", None))
    pending = sum(
        1 for item in plan.items
        if getattr(item, "ambiguity", None)
        or str(getattr(item, "rule_key", "")).endswith("unmatched"))

    quality = {}
    if expected_map is not None:
        correct = wrong = 0
        details = []
        for item in plan.items:
            expected = expected_map.get(item.file.rel_path, "")
            actual = item.target_rel_path
            if not expected:
                continue
            ok = expected in actual
            correct += 1 if ok else 0
            wrong += 0 if ok else 1
            details.append({
                "rel": item.file.rel_path,
                "expected": expected,
                "actual": actual,
                "ok": ok,
                "rule": item.rule_key,
            })
        quality = {
            "expected_total": correct + wrong,
            "correct": correct,
            "wrong": wrong,
            "accuracy": round(correct / max(1, correct + wrong), 4),
            "manual_corrections": 0,
            "details": details,
        }

    slowest = max(
        [("scan", scan_s), ("content", content_s), ("plan", plan_s)],
        key=lambda pair: pair[1],
    )[0]
    return {
        "label": label,
        "source": SRC,
        "scanner_module": _scanner_module.__file__,
        "planner_module": _planner_module.__file__,
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "root": root,
        "files": len(scan.files),
        "total_bytes": total_bytes,
        "types": types,
        "scan_s": round(scan_s, 4),
        "content_s": round(content_s, 4),
        "plan_s": round(plan_s, 4),
        "total_s": round(scan_s + content_s + plan_s, 4),
        "slowest_stage": slowest,
        "selected": selected,
        "unselected": len(plan.items) - selected,
        "pending": pending,
        "pending_ratio": round(pending / max(1, len(plan.items)), 4),
        "ambiguous": ambiguous,
        "max_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "max_rss_unit": "bytes" if sys.platform == "darwin" else "KB",
        "content_errors": len(extraction.errors),
        "quality": quality,
    }


def main():
    parser = argparse.ArgumentParser(description="阶段四质量与性能样本")
    parser.add_argument("--root", default="/tmp/stage4-quality")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--large-files", type=int, default=5000)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if os.path.exists(args.root):
        shutil.rmtree(args.root)
    os.makedirs(args.root)
    small_root = os.path.join(args.root, "小样本")
    large_root = os.path.join(args.root, "大样本")
    os.makedirs(small_root)
    os.makedirs(large_root)

    make_small(small_root)
    expected_map = {rel: expected for rel, _text, _why, expected in SMALL}
    for rel in list(expected_map):
        if not os.path.exists(os.path.join(small_root, rel)):
            expected_map.pop(rel, None)
    # 补充文件按目录名给出预期
    for dirpath, _dirs, files in os.walk(small_root):
        rel_dir = os.path.relpath(dirpath, small_root)
        for name in files:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if rel in expected_map:
                continue
            if "高等数学" in rel:
                expected_map[rel] = "高等数学"
            elif "数据结构" in rel:
                expected_map[rel] = "数据结构"
            elif "数学建模" in rel:
                expected_map[rel] = "数学建模"
            elif "项目周报" in rel:
                expected_map[rel] = "项目"

    make_large(large_root, args.large_files)
    records = [
        measure(small_root, args.label + "-small", expected_map),
        measure(large_root, args.label + "-large"),
    ]
    out = args.out or os.path.join(args.root, f"stage4-quality-{args.label}.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"records": records}, handle, ensure_ascii=False, indent=2)
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    print(f"结果已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
