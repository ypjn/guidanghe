"""阶段四布局复核：820/1024/1280 × 未选中/长文件名选中/清空选择。

输出九张 PNG 和一份宽度/分栏/按钮日志，用于验收。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "cocoa")
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.append(SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QLabel  # noqa: E402
import main as main_mod  # noqa: E402

APP = QApplication.instance() or QApplication([])
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)


def process(seconds=0.15):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout=30):
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


def make_sample():
    root = tempfile.mkdtemp(prefix="stage4_layout_")
    long_name = ("高等数学课程设计报告与竞赛项目周报汇总" * 3) + ".txt"
    long_dir = os.path.join(root, "课程", "2026秋-大二上", "高等数学",
                            "非常长的课程资料文件夹名称用于检查换行")
    os.makedirs(long_dir, exist_ok=True)
    with open(os.path.join(long_dir, long_name), "w", encoding="utf-8") as handle:
        handle.write("高等数学 极限 导数 课程设计报告\n")
    for index in range(3):
        path = os.path.join(root, "下载", f"普通文件{index}.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("普通下载文件\n")
    return root, long_name


def widget_row(window, filename):
    for row, item in enumerate(window.current_plan.items):
        if item.file.filename == filename:
            return row
    return 0


def filter_widgets(window):
    return [
        ("搜索框", window.plan_search),
        ("全部类型", window.type_filter),
        ("全部目录", window.dir_filter),
        ("全部来源", window.source_filter),
        ("待定", window.pending_check),
        ("将改名", window.conflict_check),
        ("清除", window.btn_clear_filters),
    ]


def inspect_state(window, width, state, shots, log):
    window.resize(width, 820)
    process(0.25)
    actual = window.width()
    center = window.center_host if hasattr(window, "center_host") else None
    record = {
        "requested_width": width,
        "actual_width": actual,
        "height": window.height(),
        "state": state,
        "splitter_sizes": window.main_splitter.sizes(),
        "center_width": center.width() if center else None,
        "table_width": window.table.width(),
        "inspector_visible": not window.inspector_scroll.isHidden(),
        "inspector_width": window.inspector_scroll.width(),
        "filters": [],
        "labels": [],
    }
    for name, widget in filter_widgets(window):
        geo = widget.geometry()
        record["filters"].append({
            "name": name,
            "visible": widget.isVisible(),
            "enabled": widget.isEnabled(),
            "x": geo.x(), "y": geo.y(),
            "w": geo.width(), "h": geo.height(),
            "min_w": widget.minimumSizeHint().width(),
            "text": widget.text() if hasattr(widget, "text") else "",
        })
    for label in window.inspector_scroll.widget().findChildren(QLabel):
        if not label.text():
            continue
        record["labels"].append({
            "text": label.text()[:40],
            "visible": label.isVisible(),
            "w": label.width(), "h": label.height(),
            "needed_h": label.heightForWidth(max(1, label.width())),
        })
    button = window.btn_override_selected
    record["edit_button"] = {
        "text": button.text(),
        "visible": button.isVisible(),
        "w": button.width(),
        "h": button.height(),
        "min_w": button.minimumSizeHint().width(),
        "fits": button.width() >= button.minimumSizeHint().width(),
    }
    filters_ok = all(
        row["visible"] and row["w"] >= row["min_w"]
        for row in record["filters"])
    labels_ok = all(
        (not row["visible"]) or row["h"] >= row["needed_h"]
        for row in record["labels"])
    if state in ("none", "cleared"):
        inspector_ok = not record["inspector_visible"]
    else:
        inspector_ok = (record["inspector_visible"]
                        and record["edit_button"]["visible"]
                        and record["edit_button"]["fits"])
    record["state_ok"] = filters_ok and labels_ok and inspector_ok
    image = os.path.join(shots, f"{width}-{state}.png")
    window.grab().save(image)
    record["image"] = image
    log.append(record)
    print(json.dumps(record, ensure_ascii=False))


def main():
    root, long_name = make_sample()
    window = main_mod.MainWindow()
    window.path_edit.setText(root)
    window._reset_for_new_dir(root)
    window.resize(1280, 820)
    window.show()
    window.on_scan()
    assert wait_until(lambda: (not window._working)
                      and window.scan_result is not None)
    window._generate_and_show_plan()
    assert wait_until(lambda: (not window._plan_working)
                      and window.current_plan is not None
                      and window._plan_valid)
    shots = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(
        prefix="stage4_layout_shots_")
    os.makedirs(shots, exist_ok=True)
    log = []
    for width in (820, 1024, 1280):
        window.table.clearSelection()
        window._update_inspector()
        process()
        inspect_state(window, width, "none", shots, log)

        window.table.selectRow(widget_row(window, long_name))
        process()
        window._update_inspector()
        inspect_state(window, width, "long", shots, log)

        window.table.clearSelection()
        window._update_inspector()
        process()
        inspect_state(window, width, "cleared", shots, log)

    window._closing = True
    window.hide()
    window.deleteLater()
    process()
    print(f"SHOTS={shots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
