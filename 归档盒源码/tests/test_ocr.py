"""图片文字参与关键词匹配，不改已经按目录归好的文件。"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from classification_config import (  # noqa: E402
    MATCH_FILENAME_KEYWORD, LongTermRule, default_config,
)
from ocr_engine import (  # noqa: E402
    collect_image_text, engine_label, engine_names, is_ocr_candidate,
    ocr_supported, recognize_image,
)
from planner import (  # noqa: E402
    DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER,
    classify_entries,
)
from scanner import scan_directory  # noqa: E402


def _scan(paths):
    root = tempfile.mkdtemp(prefix="archivebox_ocr_")
    for rel in paths:
        full = os.path.join(root, rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(b"x")
    return scan_directory(root, detect_project_roots=False)


def _by(scan, mode, rules=None, config=None, ocr_texts=None):
    items, _ = classify_entries(
        scan, mode, rules or [], config=config, ocr_texts=ocr_texts)
    return {item.file.rel_path: item for item in items}


def main() -> None:
    assert is_ocr_candidate("a.PNG")
    assert is_ocr_candidate("a.jpg", 100)
    assert not is_ocr_candidate("a.jpg", 0)
    assert not is_ocr_candidate("a.pdf")
    assert not is_ocr_candidate("a.svg")
    assert not is_ocr_candidate("a.jpg", 26 * 1024 * 1024)
    names = engine_names()
    assert names[0] == "vision", names
    assert "rapidocr" in names
    assert "windows" not in names
    assert "本地模型" in engine_label()
    assert "Windows" in engine_label()

    scan = _scan([
        "shot.png",
        "大二上/数据结构/作业/a.png",
        "高数作业.png",
    ])
    texts = {
        "shot.png": "今天的高等数学作业",
        "大二上/数据结构/作业/a.png": "高等数学",
        "高数作业.png": "线性代数第二章",
    }
    got = _by(scan, MODE_BY_DIRECTION_SEMESTER, ocr_texts=texts)
    loose = got["shot.png"]
    assert loose.direction == "课程" and loose.category == "高等数学", loose.rule_applied
    assert "图片文字" in loose.rule_applied, loose.rule_applied
    assert loose.ocr_text == "今天的高等数学作业"

    anchored = got["大二上/数据结构/作业/a.png"]
    assert anchored.category == "数据结构", anchored.rule_applied
    assert "图片文字" not in anchored.rule_applied

    named = got["高数作业.png"]
    assert named.category == "高等数学", named.rule_applied
    assert "图片文字" not in named.rule_applied

    # 不传图片文字时，散落图片仍是待定
    plain = _by(scan, MODE_BY_DIRECTION_SEMESTER)
    assert plain["shot.png"].direction == "待定"
    assert plain["shot.png"].ocr_text is None

    course = _by(
        scan, MODE_BY_COURSE_THEN_TYPE, rules=DEFAULT_COURSE_RULES,
        ocr_texts={"shot.png": "线性代数笔记"},
    )
    assert course["shot.png"].course == "线性代数", course["shot.png"].rule_applied
    assert "图片文字" in course["shot.png"].rule_applied
    assert course["高数作业.png"].course == "高等数学"
    assert "图片文字" not in course["高数作业.png"].rule_applied

    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-ocr-invoice",
        name="发票",
        match_type=MATCH_FILENAME_KEYWORD,
        keywords=["发票"],
        direction="校园事务",
        category="报销",
    ))
    invoice_scan = _scan(["大二上/数据结构/作业/bill.png", "发票.png"])
    invoiced = _by(
        invoice_scan, MODE_BY_DIRECTION_SEMESTER, config=cfg,
        ocr_texts={
            "大二上/数据结构/作业/bill.png": "电子发票",
            "发票.png": "无关",
        },
    )
    bill = invoiced["大二上/数据结构/作业/bill.png"]
    assert bill.direction == "校园事务" and bill.category == "报销", bill.rule_applied
    assert "图片文字" in bill.rule_applied
    named_bill = invoiced["发票.png"]
    assert named_bill.category == "报销"
    assert "图片文字" not in named_bill.rule_applied

    if ocr_supported():
        _check_vision()
        _check_checkbox()
    print("ocr checks passed")


def _check_vision():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    root = tempfile.mkdtemp(prefix="archivebox_ocr_img_")
    path = os.path.join(root, "note.png")
    image = QImage(900, 240, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(QColor("black"))
    painter.setFont(QFont("PingFang SC", 64))
    painter.drawText(
        image.rect(), int(Qt.AlignmentFlag.AlignCenter), "高等数学作业")
    painter.end()
    assert image.save(path, "PNG")

    scan = scan_directory(root, detect_project_roots=False)
    texts, errors = collect_image_text(scan.files)
    assert not errors, errors
    text = texts.get("note.png", "")
    assert "数学" in text or "高等" in text, repr(text)
    # 第二次命中缓存，结果相同
    again, _ = collect_image_text(scan.files)
    assert again["note.png"] == text
    # 同一张图也可以走本地模型。这是 Windows 和 Linux 会用的那一套。
    local = recognize_image(path, engine="rapidocr")
    assert "数学" in local or "高等" in local, repr(local)
    try:
        recognize_image(path, engine="windows")
    except Exception as exc:
        assert "Windows" in str(exc)
    else:
        raise AssertionError("Mac 上不应走通 Windows 系统识别")
    del app


def _check_checkbox():
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    assert window.ocr_check.text() == "识别图片文字"
    assert window.ocr_check.isEnabled()
    assert not window.ocr_check.isChecked()

    from scanner import FileEntry
    from planner import PlanItem
    entry = FileEntry(
        abs_path="/tmp/a.png", rel_path="a.png", filename="a.png",
        file_type="图片", size=10, size_display="10 B")
    item = PlanItem(
        file=entry, course="高等数学", file_type="图片",
        rule_applied="关键词兜底「高等数学」→ 课程/高等数学（图片文字）",
        target_dir_rel="_已整理/课程/未识别学期/高等数学/图片",
        target_filename="a.png",
        target_rel_path="_已整理/课程/未识别学期/高等数学/图片/a.png",
        ocr_text="高等数学作业",
    )
    window._show_inspector_ocr(item)
    assert not window.inspector_ocr.isHidden()
    assert window.inspector_ocr.text() == "高等数学作业"
    doc = FileEntry(
        abs_path="/tmp/a.pdf", rel_path="a.pdf", filename="a.pdf",
        file_type="文档", size=10, size_display="10 B")
    doc_item = PlanItem(
        file=doc, course="", file_type="文档", rule_applied="按类型模式",
        target_dir_rel="_已整理/文档", target_filename="a.pdf",
        target_rel_path="_已整理/文档/a.pdf")
    window._show_inspector_ocr(doc_item)
    assert window.inspector_ocr.isHidden()
    window.close()
    del app


if __name__ == "__main__":
    main()
