"""
归档盒阶段 5 · 学习建议窗口（只做展示与确认接线）

候选的归纳、状态与保存全部在 learning.py / preferences.py 里完成；
这里不移动文件，也不直接写偏好表。
"""

from __future__ import annotations

from typing import Callable, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

import learning
import preferences


class LearningCandidatesDialog(QDialog):
    def __init__(self, root: str, loader: Callable[[], List[learning.Candidate]],
                 confirm_cb, reject_cb, disable_cb, parent=None):
        super().__init__(parent)
        self.root = root
        self._loader = loader
        self._confirm_cb = confirm_cb
        self._reject_cb = reject_cb
        self._disable_cb = disable_cb
        self._candidates: List[learning.Candidate] = []
        self.setWindowTitle("学习建议")
        self.resize(920, 620)
        self._build_ui()
        self.reload()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        hint = QLabel(
            "这里只展示根据你最近的实际改单归纳出的候选。"
            "确认前不会写进偏好表；确认后下一次预览才会采用，"
            "文件仍要勾选并走原执行确认才会移动。")
        clue_note = ""
        try:
            clues = learning.list_feedback(root, learning.KIND_CLUE)
        except Exception:
            clues = []
        if clues:
            clue_note = (
                f"\n另有 {len(clues)} 条整批撤销线索（没有逐文件分类原因），"
                "只作待核对，不参与候选计数。")
        hint.setText(hint.text() + clue_note)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#475569;font-size:12px;")
        lay.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["状态", "触发条件", "建议去向", "样本数"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(1, 280)
        self.table.setColumnWidth(2, 220)
        splitter.addWidget(self.table)

        self.detail = QTextBrowser()
        self.detail.setMinimumWidth(320)
        splitter.addWidget(self.detail)
        splitter.setSizes([520, 380])
        lay.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#475569;font-size:12px;")
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_confirm = QPushButton("确认（仅当前整理空间）")
        self.btn_confirm.setToolTip("默认只对当前整理空间生效")
        self.btn_confirm.clicked.connect(self._on_confirm_space)
        row.addWidget(self.btn_confirm)
        self.btn_global = QPushButton("确认并设为全局…")
        self.btn_global.setToolTip("只有点这里才会写入全局偏好库，影响所有整理空间")
        self.btn_global.clicked.connect(self._on_confirm_global)
        row.addWidget(self.btn_global)
        self.btn_reject = QPushButton("拒绝")
        self.btn_reject.setToolTip("拒绝后重启或重复导入相同证据，也不会变回待确认")
        self.btn_reject.clicked.connect(self._on_reject)
        row.addWidget(self.btn_reject)
        self.btn_disable = QPushButton("停用已确认偏好")
        self.btn_disable.setToolTip("停用后偏好行仍保留，下一次预览不再采用")
        self.btn_disable.clicked.connect(self._on_disable)
        row.addWidget(self.btn_disable)
        row.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        lay.addLayout(row)

    # ---- 数据刷新 ----

    def reload(self, select_id: str = ""):
        try:
            self._candidates = list(self._loader())
        except Exception as e:
            QMessageBox.warning(self, "学习建议", f"读取学习候选失败：\n{e}")
            self._candidates = []
        if not select_id:
            selected = self._selected()
            select_id = selected.candidate_id if selected is not None else ""
        self.table.setRowCount(0)
        row_to_select = -1
        for index, cand in enumerate(self._candidates):
            self.table.insertRow(index)
            for col, text in enumerate((
                    cand.status_label,
                    cand.condition_text(),
                    cand.target_label,
                    str(len(cand.samples)))):
                item = QTableWidgetItem(text)
                item.setToolTip(cand.describe())
                self.table.setItem(index, col, item)
            if cand.candidate_id == select_id:
                row_to_select = index
        if row_to_select < 0 and self._candidates:
            row_to_select = 0
        if row_to_select >= 0:
            self.table.selectRow(row_to_select)
        else:
            self.detail.setPlainText("还没有可展示的学习建议。")
        self._refresh_buttons()

    def _selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        row = next(iter(rows))
        if 0 <= row < len(self._candidates):
            return self._candidates[row]
        return None

    def _show_detail(self):
        cand = self._selected()
        if cand is None:
            self.detail.setPlainText("")
        else:
            self.detail.setPlainText(cand.describe())
        self._refresh_buttons()

    def _refresh_buttons(self):
        cand = self._selected()
        confirmable = bool(cand is not None and cand.confirmable)
        self.btn_confirm.setEnabled(confirmable)
        self.btn_global.setEnabled(confirmable)
        self.btn_reject.setEnabled(
            cand is not None
            and cand.status in (learning.STATUS_PENDING,
                                learning.STATUS_CONFLICT,
                                learning.STATUS_BLOCKED))
        self.btn_disable.setEnabled(
            cand is not None and bool(cand.preference_id)
            and cand.status in (learning.STATUS_CONFIRMED,
                                learning.STATUS_DISABLED))

    # ---- 操作 ----

    def _run(self, action, success: str):
        cand = self._selected()
        if cand is None:
            return
        try:
            action(cand)
        except Exception as e:
            QMessageBox.warning(self, "学习建议", str(e))
            self.reload(cand.candidate_id)
            return
        self.status.setText(success)
        self.reload(cand.candidate_id)

    def _on_confirm_space(self):
        cand = self._selected()
        if cand is None:
            return
        self._run(
            lambda c: self._confirm_cb(c.candidate_id, preferences.SCOPE_SPACE),
            "已确认到当前整理空间；下一次预览生效，文件不会自动移动。")

    def _on_confirm_global(self):
        cand = self._selected()
        if cand is None:
            return
        reply = QMessageBox.question(
            self, "设为全局偏好",
            "这会把该候选写入全局偏好库，影响所有整理空间。\n\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._run(
            lambda c: self._confirm_cb(c.candidate_id, preferences.SCOPE_GLOBAL),
            "已设为全局偏好；各整理空间的下一次预览会采用。")

    def _on_reject(self):
        self._run(lambda c: self._reject_cb(c.candidate_id),
                  "已拒绝该候选；相同证据不会再次变成待确认。")

    def _on_disable(self):
        self._run(lambda c: self._disable_cb(c.candidate_id),
                  "已停用该偏好；偏好行仍保留，下一次预览不再采用。")
