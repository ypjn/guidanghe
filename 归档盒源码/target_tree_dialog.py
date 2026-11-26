"""
归档盒 V1.6 — 目标目录结构查看（只展示计划，不创建真实目录）

树形展示当前计划准备生成的目录层级、文件与数量，支持：
- 查看完整目标路径（悬浮提示）；
- 在「整份计划」与「当前筛选范围」之间切换；
- 由主窗口在筛选变化时节流刷新，避免每次输入都重建整棵树。
"""

from __future__ import annotations

import os
from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from operation_feedback import install


class TargetTreeDialog(QDialog):
    def __init__(self, plan, visible_flags, parent=None):
        super().__init__(parent)
        self.setWindowTitle("目标目录结构（仅预览，不创建真实目录）")
        self.resize(860, 640)
        self.plan = plan
        self.visible_flags = list(visible_flags or [])
        self._build_ui()
        self._rebuild()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(self.info)

        row = QHBoxLayout()
        self.scope_check = QCheckBox("仅当前筛选范围")
        self.scope_check.setChecked(True)
        self.scope_check.stateChanged.connect(self._rebuild)
        row.addWidget(self.scope_check)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._rebuild)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["目标结构", "数量 / 大小"])
        self.tree.setAlternatingRowColors(True)
        lay.addWidget(self.tree, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("关闭")
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.reject)
        lay.addWidget(bb)
        self.feedback = install(lay, self.info, hide_source=False)

    def set_visible_flags(self, flags: List[bool]):
        """由主窗口筛选变化调用（建议节流后调用）。"""
        self.visible_flags = list(flags or [])
        if self.scope_check.isChecked() and self.isVisible():
            self._rebuild()

    def _scope_indices(self) -> List[int]:
        total = len(self.plan.items)
        flags = self.visible_flags or [True] * total
        use_filter = self.scope_check.isChecked()
        return [i for i in range(total)
                if i < len(flags) and (flags[i] or not use_filter)]

    def _rebuild(self, *_):
        if self.plan is None:
            return
        indices = self._scope_indices()
        total = len(self.plan.items)
        scope_name = "当前筛选范围" if self.scope_check.isChecked() else "整份计划"
        self.info.setText(
            f"共 {total} 个文件；当前展示{scope_name}：{len(indices)} 个文件。"
            f"<br>该视图只展示计划，不会创建任何真实目录。")

        self.tree.clear()
        root_label = os.path.basename(self.plan.root.rstrip(os.sep)) or self.plan.root
        root_item = QTreeWidgetItem([f"{root_label} / 计划目标结构", ""])
        root_item.setExpanded(True)
        self.tree.addTopLevelItem(root_item)

        nodes: Dict[tuple, QTreeWidgetItem] = {}
        counts: Dict[tuple, int] = {}

        def _ensure(path_parts):
            parent = root_item
            key = ()
            for part in path_parts:
                key = key + (part,)
                if key not in nodes:
                    node = QTreeWidgetItem([part, ""])
                    parent.addChild(node)
                    nodes[key] = node
                    if len(path_parts) >= 2 and len(key) <= 2:
                        node.setExpanded(True)
                parent = nodes[key]
                counts[key] = counts.get(key, 0) + 1
            return parent, key

        for i in indices:
            item = self.plan.items[i]
            parts = [p for p in item.target_rel_path.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            folder, key = _ensure(parts[:-1])
            leaf = QTreeWidgetItem([parts[-1], item.file.size_display])
            tip = os.path.join(self.plan.root, *parts)
            leaf.setToolTip(0, f"完整目标路径：{tip}")
            if item.conflict_with_existing:
                leaf.setText(0, parts[-1] + "（目标已存在）")
                leaf.setForeground(0, Qt.red)
            folder.addChild(leaf)

        for key, node in nodes.items():
            node.setText(1, f"{counts.get(key, 0)} 个文件")
            node.setToolTip(0, "路径：" + "/".join(key))

        self.tree.resizeColumnToContents(0)
