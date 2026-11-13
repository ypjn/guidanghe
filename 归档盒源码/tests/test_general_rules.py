"""常见课程名、校园事务和顶层目录的默认规则。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from classification_config import default_config, validate_config  # noqa: E402
from planner import MODE_BY_DIRECTION_SEMESTER, classify_entries  # noqa: E402
from scanner import scan_directory  # noqa: E402


def _plan(paths):
    root = tempfile.mkdtemp(prefix="archivebox_general_")
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

    by = _plan([
        "散落.txt",
        "大二上/数据结构/作业/a.pdf",
        "大一下/已完成期末/高数/f.pdf",
        "高数作业.pdf",
        "大三上/计网/实验/a.pdf",
        "入党申请书.pdf",
        "全国大学生英语竞赛.pdf",
        "项目/归档盒/src/main.py",
        "实习/某公司/总结.pdf",
        "javascript笔记.pdf",
        "大二上/周报/本周.docx",
    ])

    def target(rel):
        return by[rel].target_rel_path

    checks = [
        (by["散落.txt"].direction == "待定", target("散落.txt")),
        (by["大二上/数据结构/作业/a.pdf"].category == "数据结构"
         and by["大二上/数据结构/作业/a.pdf"].rule_key.startswith("semester_dir:"),
         target("大二上/数据结构/作业/a.pdf")),
        (by["大一下/已完成期末/高数/f.pdf"].category == "高等数学",
         target("大一下/已完成期末/高数/f.pdf")),
        (by["高数作业.pdf"].direction == "课程"
         and by["高数作业.pdf"].category == "高等数学"
         and target("高数作业.pdf")
         == "_已整理/课程/未识别学期/高等数学/文档/高数作业.pdf",
         target("高数作业.pdf")),
        (by["大三上/计网/实验/a.pdf"].category == "计算机网络"
         and by["大三上/计网/实验/a.pdf"].semester == "大三上"
         and target("大三上/计网/实验/a.pdf")
         == "_已整理/课程/大三上/计算机网络/文档/实验/a.pdf",
         target("大三上/计网/实验/a.pdf")),
        (by["入党申请书.pdf"].direction == "校园事务"
         and by["入党申请书.pdf"].category == "入党",
         target("入党申请书.pdf")),
        (by["全国大学生英语竞赛.pdf"].direction == "竞赛",
         target("全国大学生英语竞赛.pdf")),
        (by["项目/归档盒/src/main.py"].direction == "项目"
         and by["项目/归档盒/src/main.py"].category == "归档盒"
         and target("项目/归档盒/src/main.py")
         == "_已整理/项目/归档盒/src/main.py",
         target("项目/归档盒/src/main.py")),
        (by["实习/某公司/总结.pdf"].direction == "校园事务"
         and by["实习/某公司/总结.pdf"].category == "某公司",
         target("实习/某公司/总结.pdf")),
        (by["javascript笔记.pdf"].direction == "待定",
         target("javascript笔记.pdf")),
        (by["大二上/周报/本周.docx"].direction == "校园事务"
         and by["大二上/周报/本周.docx"].rule_key.startswith("semester_dir:"),
         target("大二上/周报/本周.docx")),
    ]
    failed = [detail for ok, detail in checks if not ok]
    if failed:
        raise SystemExit("分类不符合预期：\n" + "\n".join(failed))
    print("常见课程与校园事务规则通过")


if __name__ == "__main__":
    main()
