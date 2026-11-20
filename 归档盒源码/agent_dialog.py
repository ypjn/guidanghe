"""阶段 6 · AI 管家对话框。

用户输入一句话 → 后台查找 → 展示命中理由与整理方案 → 澄清必要问题
→ 把方案交给主窗口现有预览；真正执行仍点主界面的「开始整理」。
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import agent


class AgentWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, root: str, instruction: str,
                 protect_dirs=None, parent=None):
        super().__init__(parent)
        self.root = root
        self.instruction = instruction
        self.protect_dirs = list(protect_dirs or [])
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = agent.run_agent(
                self.root,
                self.instruction,
                protect_dirs=self.protect_dirs,
                progress_cb=lambda cur, total, name: self.progress.emit(
                    cur, total, name),
                cancel_event=self.cancel_event,
            )
            if not self.cancel_event.is_set():
                self.done.emit(result)
        except Exception as exc:  # 界面线程统一提示，不吞异常
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))


class AgentDialog(QDialog):
    """非模态：主窗口仍可查看，但查找结果不会直接移动文件。"""

    def __init__(self, main_window, root: str, protect_dirs=None, parent=None):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.root = root
        self.protect_dirs = list(protect_dirs or [])
        self.result: agent.AgentResult | None = None
        self._worker: AgentWorker | None = None
        self._searching = False
        self._closing = False
        self._semester_choice: str | None = None
        self._course_choice: str | None = None

        self.setWindowTitle("AI 管家 · 说一句话整理")
        self.resize(940, 660)
        self.setModal(False)
        self._build_ui()

    # ---------- 界面 ----------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        intro = QLabel(
            "用一句话说明要找什么、怎么归。AI 管家只生成方案；"
            "点主界面「开始整理」并确认后才会移动文件。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#475569;")
        outer.addWidget(intro)

        row = QHBoxLayout()
        self.instruction_edit = QLineEdit()
        self.instruction_edit.setPlaceholderText(
            "例如：帮我找出这学期的数据结构实验材料，"
            "看看报告和代码怎么归到现有目录")
        self.instruction_edit.returnPressed.connect(self.start_search)
        row.addWidget(self.instruction_edit, stretch=1)
        self.btn_find = QPushButton("查找并给方案")
        self.btn_find.clicked.connect(self.start_search)
        row.addWidget(self.btn_find)
        outer.addLayout(row)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#64748B;font-size:12px;")
        outer.addWidget(self.hint)

        self.question_box = QGroupBox("需要确认一句")
        qlay = QVBoxLayout(self.question_box)
        self.question_label = QLabel("")
        self.question_label.setWordWrap(True)
        qlay.addWidget(self.question_label)
        qrow = QHBoxLayout()
        self.question_combo = QComboBox()
        self.question_combo.setMinimumWidth(280)
        qrow.addWidget(self.question_combo, stretch=1)
        self.btn_answer = QPushButton("按这个继续")
        self.btn_answer.clicked.connect(self.answer_question)
        qrow.addWidget(self.btn_answer)
        qlay.addLayout(qrow)
        self.question_box.hide()
        outer.addWidget(self.question_box)

        splitter = QSplitter(Qt.Orientation.Vertical)

        hits_box = QGroupBox("找到的文件与命中原因")
        hlay = QVBoxLayout(hits_box)
        self.hits_table = QTableWidget(0, 4)
        self.hits_table.setHorizontalHeaderLabels(
            ["文件", "位置", "命中原因", "状态"])
        self._setup_table(self.hits_table)
        self.hits_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.hits_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        hlay.addWidget(self.hits_table)
        splitter.addWidget(hits_box)

        plan_box = QGroupBox("整理方案（确认后才会移动）")
        play = QVBoxLayout(plan_box)
        self.plan_table = QTableWidget(0, 4)
        self.plan_table.setHorizontalHeaderLabels(
            ["原位置", "目标位置", "依据", "冲突"])
        self._setup_table(self.plan_table)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        play.addWidget(self.plan_table)
        splitter.addWidget(plan_box)
        splitter.setSizes([260, 280])
        outer.addWidget(splitter, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color:#0F172A;font-weight:600;")
        outer.addWidget(self.summary_label)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.btn_cancel)
        self.btn_apply = QPushButton("放到主界面预览")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self.apply_plan)
        bottom.addWidget(self.btn_apply)
        outer.addLayout(bottom)

    @staticmethod
    def _setup_table(table: QTableWidget):
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setWordWrap(True)

    # ---------- 查找 ----------

    def set_instruction(self, text: str):
        self.instruction_edit.setText(text)

    def start_search(self):
        if self._searching:
            return
        instruction = self.instruction_edit.text().strip()
        if not instruction:
            self.hint.setText("请先输入一句要查找或整理的话。")
            return
        if not os.path.isdir(self.root):
            QMessageBox.warning(self, "AI 管家", "整理空间不存在，请重新选择文件夹。")
            return
        self._semester_choice = None
        self._course_choice = None
        self._searching = True
        self._set_busy(True, "正在扫描并查找相关文件……")
        worker = AgentWorker(self.root, instruction, self.protect_dirs, self)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _set_busy(self, busy: bool, message: str = ""):
        self.btn_find.setEnabled(not busy)
        self.instruction_edit.setEnabled(not busy)
        self.btn_apply.setEnabled(False if busy else self.btn_apply.isEnabled())
        self.btn_answer.setEnabled(not busy)
        self.question_combo.setEnabled(not busy)
        if message:
            self.hint.setText(message)

    def _on_progress(self, cur: int, total: int, name: str):
        if self._closing:
            return
        self.hint.setText(f"正在扫描 [{cur}/{total}] {name}")

    def _on_done(self, result):
        if self._closing:
            return
        self._searching = False
        self._set_busy(False)
        self._render(result)

    def _on_failed(self, message: str):
        if self._closing:
            return
        self._searching = False
        self._set_busy(False)
        self.hint.setText(f"查找失败：{message}")
        QMessageBox.warning(self, "AI 管家", f"查找失败：\n{message}")

    def _on_worker_finished(self):
        self._worker = None
        if self._closing:
            self.deleteLater()

    # ---------- 结果渲染 ----------

    def _render(self, result: agent.AgentResult):
        self.result = result
        self.hits_table.setRowCount(0)
        self.plan_table.setRowCount(0)
        self.btn_apply.setEnabled(False)

        if result.question is not None:
            self.question_box.show()
            self.question_label.setText(result.question.text)
            self.question_combo.clear()
            for option in result.question.options:
                text = option.get("label", "")
                detail = option.get("detail", "")
                if detail:
                    text += f"（{detail}）"
                self.question_combo.addItem(text, option.get("value", ""))
            has_options = bool(result.question.options)
            self.question_combo.setVisible(has_options)
            self.btn_answer.setVisible(has_options)
            self.summary_label.setText(result.question.text)
            self.hint.setText(
                "先回答上面的问题；回答后会重新生成方案，确认前不会移动文件。")
            return

        self.question_box.hide()
        self._fill_hits(result)
        self._fill_plan(result)
        self.summary_label.setText(result.summary)
        self.hint.setText(
            "方案可以修改：回到主界面取消勾选或调整，再点「开始整理」确认。")
        self.btn_apply.setEnabled(
            result.plan is not None and bool(result.plan.items))

    def _fill_hits(self, result: agent.AgentResult):
        self.hits_table.setRowCount(len(result.hits))
        for row, hit in enumerate(result.hits):
            if hit.already:
                status = "已在目标目录"
            elif hit.uncertain:
                status = "归属不确定，保持原位"
            elif hit.conflict:
                status = "目标同名，将改名"
            else:
                status = "待移动"
            values = [
                hit.file.filename,
                hit.file.rel_path,
                hit.evidence,
                status,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if hit.conflict and col == 3:
                    item.setForeground(Qt.red)
                self.hits_table.setItem(row, col, item)

    def _fill_plan(self, result: agent.AgentResult):
        plan = result.plan
        if plan is None:
            self.plan_table.setRowCount(0)
            return
        self.plan_table.setRowCount(len(plan.items))
        for row, item in enumerate(plan.items):
            values = [
                item.file.rel_path,
                item.target_rel_path,
                item.rule_applied,
                "是（已改名为 " + item.target_filename + "）"
                if item.conflict_with_existing else "否",
            ]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if item.conflict_with_existing and col == 3:
                    cell.setForeground(Qt.red)
                self.plan_table.setItem(row, col, cell)

    # ---------- 澄清与落地 ----------

    def answer_question(self):
        if self.result is None or self.result.question is None:
            return
        value = self.question_combo.currentData()
        kind = self.result.question.kind
        if kind in ("semester", "semester_missing"):
            self._semester_choice = value
        elif kind == "course":
            self._course_choice = value
        self._rebuild()

    def _rebuild(self):
        if self.result is None:
            return
        try:
            new_result = agent.run_agent(
                self.root,
                self.result.instruction,
                scan=self.result.scan,
                texts=self.result.texts,
                semester_choice=self._semester_choice,
                course_choice=self._course_choice,
                protect_dirs=self.protect_dirs,
            )
        except Exception as exc:
            QMessageBox.warning(self, "AI 管家", f"重新生成方案失败：\n{exc}")
            return
        self._render(new_result)

    def apply_plan(self):
        if self.result is None or self.result.plan is None:
            return
        if self.result.question is not None:
            self.hint.setText("请先回答上面的确认问题。")
            return
        note = f"AI 管家：{self.result.summary}"
        if self.main_window.adopt_agent_plan(self.result.plan, note):
            self.accept()

    def reject(self):
        self._closing = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
        super().reject()

    def closeEvent(self, event):
        self._closing = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
        super().closeEvent(event)
