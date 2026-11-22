"""没登记的目录对照旁边已经归好的文件，内容重合才归到一起。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from planner import MODE_BY_DIRECTION_SEMESTER, classify_entries  # noqa: E402
from scanner import scan_directory  # noqa: E402


def _scan(paths):
    root = tempfile.mkdtemp(prefix="archivebox_neighbor_")
    for rel in paths:
        full = os.path.join(root, *rel.replace("\\", "/").split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(b"x")
    return scan_directory(root, detect_project_roots=False)


def _by(paths, ocr_texts=None):
    items, _ = classify_entries(
        _scan(paths), MODE_BY_DIRECTION_SEMESTER, [], ocr_texts=ocr_texts)
    # Windows 扫描结果用反斜杠；测试键统一成 /
    return {item.file.rel_path.replace("\\", "/"): item for item in items}


def main() -> None:
    got = _by([
        "大二上/数据结构/实验一.pdf",
        "images/实验一.png",
        "images/假期.png",
        "随机散落文件.txt",
        "大二上/数据结构/作业.pdf",
    ])
    hit = got["images/实验一.png"]
    assert hit.direction == "课程" and hit.category == "数据结构", hit.rule_applied
    assert hit.semester == "大二上", hit.semester
    assert hit.inner_path == "images", hit.inner_path
    assert hit.rule_key.startswith("neighbor:"), hit.rule_key
    assert hit.target_rel_path == os.path.join(
        "_已整理", "课程", "大二上", "数据结构", "图片", "images", "实验一.png"
    ), hit.target_rel_path
    assert "重合" in hit.rule_applied

    missed = got["images/假期.png"]
    assert missed.rule_key == "fallback", missed.rule_applied
    assert missed.rule_applied.startswith("未登记顶层目录「images」"), missed.rule_applied

    loose = got["随机散落文件.txt"]
    assert loose.direction == "待定", loose.rule_applied
    assert loose.rule_key == "fallback"

    anchored = got["大二上/数据结构/作业.pdf"]
    assert anchored.category == "数据结构"
    assert not anchored.rule_key.startswith("neighbor:")

    shared = _by([
        "大二上/数据结构/实验一.pdf",
        "images/实验一.png",
    ])
    moved = shared["images/实验一.png"]
    assert moved.category == "数据结构" and moved.semester == "大二上", moved.rule_applied

    tied = _by([
        "大二上/数据结构/笔记.pdf",
        "大二上/高等数学/笔记.pdf",
        "images/笔记.png",
    ])
    assert tied["images/笔记.png"].rule_key == "fallback", tied["images/笔记.png"].rule_applied

    ocr = _by(
        ["大二上/数据结构/实验一.pdf", "images/shot.png"],
        ocr_texts={"images/shot.png": "请看实验一的结果"},
    )
    seen = ocr["images/shot.png"]
    assert seen.category == "数据结构" and seen.semester == "大二上", seen.rule_applied
    assert seen.rule_key.startswith("neighbor:"), seen.rule_key

    wave = _by([
        "大二上/数据结构/实验一.pdf",
        "images/实验一.png",
        "images/实验一补充.png",
    ])
    assert wave["images/实验一补充.png"].category == "数据结构", (
        wave["images/实验一补充.png"].rule_applied)

    print("neighbor group checks passed")


if __name__ == "__main__":
    main()
