"""软著、专利、论文分开，不同项目也不并到一处。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from planner import MODE_BY_DIRECTION_SEMESTER, classify_entries  # noqa: E402
from scanner import scan_directory  # noqa: E402


def _plan(paths):
    root = tempfile.mkdtemp(prefix="archivebox_output_")
    for rel in paths:
        full = os.path.join(root, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write("x")
    scan = scan_directory(root, detect_project_roots=False)
    items, _ = classify_entries(scan, MODE_BY_DIRECTION_SEMESTER, [])
    return {item.file.rel_path: item for item in items}


def main() -> None:
    by = _plan([
        "归档盒/软著/申请.pdf",
        "软著/另一个项目/申请.pdf",
        "盒A/专利/交底书.docx",
        "盒B/专利/交底书.docx",
        "软著/申报/申请表.pdf",
        "论文/图像分类/初稿.pdf",
        "毕业论文.pdf",
        "大二上/数据结构/论文.pdf",
        "项目/归档盒/软著/说明.pdf",
        "专利法笔记.pdf",
        "期刊论文.pdf",
    ])

    def target(rel):
        return by[rel].target_rel_path

    checks = [
        (target("归档盒/软著/申请.pdf")
         == "_已整理/项目/归档盒/软著/申请.pdf",
         target("归档盒/软著/申请.pdf")),
        (target("软著/另一个项目/申请.pdf")
         == "_已整理/项目/另一个项目/软著/申请.pdf",
         target("软著/另一个项目/申请.pdf")),
        (by["盒A/专利/交底书.docx"].category == "盒A"
         and by["盒B/专利/交底书.docx"].category == "盒B"
         and target("盒A/专利/交底书.docx") != target("盒B/专利/交底书.docx"),
         f"{target('盒A/专利/交底书.docx')} | {target('盒B/专利/交底书.docx')}"),
        (target("软著/申报/申请表.pdf")
         == "_已整理/项目/软著/申报/申请表.pdf",
         target("软著/申报/申请表.pdf")),
        (target("论文/图像分类/初稿.pdf")
         == "_已整理/项目/图像分类/论文/初稿.pdf",
         target("论文/图像分类/初稿.pdf")),
        (by["毕业论文.pdf"].direction == "课程"
         and by["毕业论文.pdf"].category == "毕业设计",
         target("毕业论文.pdf")),
        (by["大二上/数据结构/论文.pdf"].category == "数据结构",
         target("大二上/数据结构/论文.pdf")),
        (target("项目/归档盒/软著/说明.pdf")
         == "_已整理/项目/归档盒/软著/说明.pdf",
         target("项目/归档盒/软著/说明.pdf")),
        (by["专利法笔记.pdf"].direction != "项目",
         target("专利法笔记.pdf")),
        (by["期刊论文.pdf"].category == "论文"
         and target("期刊论文.pdf").startswith("_已整理/项目/论文/"),
         target("期刊论文.pdf")),
    ]
    failed = [detail for ok, detail in checks if not ok]
    if failed:
        raise SystemExit("成果分类不符合预期：\n" + "\n".join(failed))
    print("软著、专利、论文按项目分开")


if __name__ == "__main__":
    main()
