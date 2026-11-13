"""Mac 复验：预览表行内勾选与手动修正入口。

Windows stage7 在 PySide6 6.12 上发现两处界面缺陷。本测试用当前源码复核：
选中一行后「修改所选文件」可用；行内复选框可以取消后再勾上。
"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

_tmp = tempfile.TemporaryDirectory(prefix="archivebox_mac_preview_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(_tmp.name, "recent.json")
os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(_tmp.name, "app")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(_tmp.name, "schemes")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from main import MainWindow  # noqa: E402
from planner import MODE_BY_DIRECTION_SEMESTER, OrganizePlan, PlanItem  # noqa: E402
from scanner import FileEntry  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    raise SystemExit(1)


def main() -> None:
    if not (Qt.CheckState(2) == Qt.CheckState.Checked):
        fail("Qt.CheckState(2) 不能表示勾选")
    if 2 == Qt.Checked:
        fail("当前 PySide6 仍把整数 2 当成 Qt.Checked，修复前提不成立")

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    root = os.path.join(_tmp.name, "root")
    os.makedirs(root)
    entry = FileEntry(
        abs_path=os.path.join(root, "笔记.pdf"),
        rel_path="笔记.pdf",
        filename="笔记.pdf",
        file_type="文档",
        size=4,
        size_display="4 B",
    )
    plan = OrganizePlan(
        root=root,
        mode=MODE_BY_DIRECTION_SEMESTER,
        rules=[],
        items=[PlanItem(
            file=entry,
            course="",
            file_type="文档",
            rule_applied="测试",
            target_dir_rel="_已整理/文档",
            target_filename="笔记.pdf",
            target_rel_path="_已整理/文档/笔记.pdf",
            selected=True,
            direction="课程",
            semester="大二上",
            category="数据结构",
        )],
    )
    window.current_plan = plan
    window._plan_valid = True
    window._populate_table(plan)
    app.processEvents()

    if window.btn_override_selected.isEnabled():
        fail("未选中行时手动修正按钮不应可用")
    window.table.selectRow(0)
    app.processEvents()
    if not window.btn_override_selected.isEnabled():
        fail("选中一行后手动修正按钮仍不可用")

    box = window.table.cellWidget(0, 0).findChild(QCheckBox)
    if box is None:
        fail("预览表没有行内复选框")
    box.setChecked(False)
    app.processEvents()
    if plan.items[0].selected:
        fail("取消勾选后计划仍为选中")
    box.setChecked(True)
    app.processEvents()
    if not plan.items[0].selected:
        fail("重新勾选后计划仍为未选中")

    window.close()
    app.processEvents()
    print("PASS 2/2 选中启用手动修正；行内复选框可取消后重新勾选")


if __name__ == "__main__":
    main()
