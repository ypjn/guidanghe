"""
归档盒 V1.7 — 重复文件窗口与后台核对线程

只读视图：展示内容重复、同名不同内容、待核对/核对失败；
不删除、不合并、不移动，也不改变计划勾选。
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from duplicate_report import (
    KIND_CONTENT, KIND_SAME_NAME_DIFF, KIND_SAME_NAME_PENDING,
    KIND_UNVERIFIED, DuplicateReport, DupEntry, assemble_report,
    export_csv_text, export_json_text, verify_entries,
    write_csv_file,
)
from main import _reveal_in_file_manager


class DuplicateCheckWorker(QThread):
    """后台核对指纹：只处理缺失或可能过期的条目。"""

    progress = Signal(int, str, int, int, str)
    finished_ok = Signal(int, str, object)
    failed = Signal(int, str, str)
    cancelled = Signal(int, str)
    finished_seq = Signal(int, str)

    def __init__(self, seq: int, root: str, entries: List[DupEntry],
                 scope_note: str, source: str, parent=None):
        super().__init__(None)
        self.seq = seq
        self.root = root
        self.entries = entries
        self.scope_note = scope_note
        self.source = source
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            def _progress(cur, total, path):
                self.progress.emit(self.seq, self.root, cur, total, path)

            ok = verify_entries(self.entries, progress_cb=_progress,
                                cancel_event=self.cancel_event)
            if not ok or self.cancel_event.is_set():
                self.cancelled.emit(self.seq, self.root)
                return
            report = assemble_report(
                self.root, self.entries, scope_note=self.scope_note,
                source=self.source)
            self.finished_ok.emit(self.seq, self.root, report)
        except Exception as e:
            self.failed.emit(self.seq, self.root, str(e))
        finally:
            self.finished_seq.emit(self.seq, self.root)


class DuplicateDialog(QDialog):
    def __init__(self, root: str, scope_note: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重复文件查看 — 只读核对")
        self.resize(1050, 700)
        self.setModal(False)
        self.root = root
        self.scope_note = scope_note
        self.report: Optional[DuplicateReport] = None
        self.cancel_callback = None
        self.recheck_callback = None
        self._running = False
        self._build_ui()
        self.set_running(True)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(
            "padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(self.info)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.progress_label = QLabel("")
        self.progress_label.setWordWrap(True)
        lay.addWidget(self.progress_label)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名 / 相对路径 / 完整路径……")
        self.search.textChanged.connect(self._apply_filter)
        row.addWidget(self.search, 3)
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("全部结果", "")
        self.kind_filter.addItem("内容重复", KIND_CONTENT)
        self.kind_filter.addItem("同名不同内容", KIND_SAME_NAME_DIFF)
        self.kind_filter.addItem("同名（待核对）", KIND_SAME_NAME_PENDING)
        self.kind_filter.addItem("待核对 / 核对失败", KIND_UNVERIFIED)
        self.kind_filter.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.kind_filter, 1)
        self.btn_expand = QPushButton("展开全部")
        self.btn_expand.clicked.connect(lambda: self.tree.expandAll())
        row.addWidget(self.btn_expand)
        self.btn_collapse = QPushButton("折叠全部")
        self.btn_collapse.clicked.connect(lambda: self.tree.collapseAll())
        row.addWidget(self.btn_collapse)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(
            ["分组 / 文件名", "源相对路径", "大小", "核对状态", "指纹", "计划目标"])
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(5, QHeaderView.Stretch)
        lay.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.btn_open = QPushButton("打开文件")
        self.btn_open.clicked.connect(self.on_open)
        actions.addWidget(self.btn_open)
        self.btn_reveal = QPushButton("在文件管理器中定位")
        self.btn_reveal.clicked.connect(self.on_reveal)
        actions.addWidget(self.btn_reveal)
        self.btn_copy = QPushButton("复制路径")
        self.btn_copy.clicked.connect(self.on_copy_path)
        actions.addWidget(self.btn_copy)
        self.btn_recheck = QPushButton("重新核对")
        self.btn_recheck.clicked.connect(self.on_recheck)
        actions.addWidget(self.btn_recheck)
        self.btn_csv = QPushButton("导出 CSV…")
        self.btn_csv.clicked.connect(lambda: self.on_export("csv"))
        actions.addWidget(self.btn_csv)
        self.btn_json = QPushButton("导出 JSON…")
        self.btn_json.clicked.connect(lambda: self.on_export("json"))
        actions.addWidget(self.btn_json)
        actions.addStretch(1)
        lay.addLayout(actions)

        self.bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.bb.button(QDialogButtonBox.Close).setText("关闭")
        self.bb.rejected.connect(self.reject)
        self.bb.accepted.connect(self.reject)
        lay.addWidget(self.bb)

        self._set_actions_enabled(False)

    # ---- 生命周期 ----
    def set_running(self, running: bool):
        self._running = running
        self.progress.setVisible(running)
        if running:
            self.progress.setValue(0)
            self.progress_label.setText("正在后台核对文件指纹（不移动任何文件）……")
            self.info.setText(
                f"<b>范围：</b>{self.scope_note}<br>"
                f"<b>根目录：</b>{self.root}<br>"
                "仅核对当前整理空间、当前扫描纳入的文件；"
                "不宣称已核对整块硬盘，也不包含被保护/排除区域。")
            self._set_actions_enabled(False)

    def show_progress(self, cur: int, total: int, path: str):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(cur)
        self.progress_label.setText(f"正在核对 [{cur}/{total}] {path}")

    def show_report(self, report: DuplicateReport):
        self.report = report
        self._running = False
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self.info.setText(
            f"<b>范围：</b>{report.scope_note}<br>"
            f"<b>根目录：</b>{report.root}<br>"
            f"<b>核对时间：</b>{report.generated_at}<br>"
            f"<b>统计：</b>{report.group_count_text}；{report.status_text}<br>"
            "<span style='color:#37474F;'>只读视图：不会删除、合并或移动文件；"
            "大小合计不代表可回收磁盘空间。</span>")
        self._populate()
        self._set_actions_enabled(True)
        self.btn_recheck.setEnabled(True)

    def show_failure(self, message: str):
        self._running = False
        self.progress.setVisible(False)
        self.progress_label.setText("核对失败")
        self.info.setText(message)
        self._set_actions_enabled(False)

    def show_cancelled(self):
        self._running = False
        self.progress.setVisible(False)
        self.progress_label.setText("核对已取消（没有生成结论）")
        self._set_actions_enabled(False)

    def reject(self):
        if self._running and self.cancel_callback is not None:
            try:
                self.cancel_callback()
            except Exception:
                pass
        super().reject()

    def _set_actions_enabled(self, enabled: bool):
        for w in (self.btn_open, self.btn_reveal, self.btn_copy,
                  self.btn_csv, self.btn_json):
            w.setEnabled(bool(enabled))
        self.btn_recheck.setEnabled(bool(enabled))

    # ---- 展示 ----
    def _populate(self):
        self.tree.clear()
        if self.report is None:
            return
        for group in self.report.groups:
            top = QTreeWidgetItem([
                group.label, "", f"{group.count} 份", group.kind_label,
                "", group.note])
            top.setData(0, Qt.UserRole, None)
            top.setData(3, Qt.UserRole, group.kind)
            top.setToolTip(0, group.note)
            self.tree.addTopLevelItem(top)
            for entry in group.entries:
                child = QTreeWidgetItem([
                    entry.filename, entry.rel_path, entry.size_display,
                    entry.status_label, entry.fingerprint_short,
                    entry.target_rel_path])
                child.setData(0, Qt.UserRole, entry)
                child.setToolTip(0, entry.abs_path)
                child.setToolTip(1, entry.abs_path)
                if entry.status != "verified":
                    child.setForeground(3, Qt.red)
                top.addChild(child)
            top.setExpanded(True)
        self._apply_filter()

    def _apply_filter(self, *_):
        text = self.search.text().strip().lower()
        kind = self.kind_filter.currentData() or ""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            group_kind = top.data(3, Qt.UserRole) or ""
            kind_ok = (not kind) or group_kind == kind
            any_visible = False
            for j in range(top.childCount()):
                child = top.child(j)
                hay = " ".join(child.text(c) for c in range(6)).lower()
                hit = (not text) or text in hay
                child.setHidden(not hit)
                if hit:
                    any_visible = True
            group_text = " ".join(top.text(c) for c in range(6)).lower()
            group_hit = (not text) or text in group_text
            top.setHidden(not kind_ok or not (any_visible or group_hit))

    # ---- 选择 ----
    def _selected_entry(self) -> Optional[DupEntry]:
        item = self.tree.currentItem()
        if item is None:
            return None
        entry = item.data(0, Qt.UserRole)
        if entry is not None:
            return entry
        if item.childCount() > 0:
            return item.child(0).data(0, Qt.UserRole)
        return None

    def _require_existing(self, entry: DupEntry) -> bool:
        if entry.exists:
            return True
        QMessageBox.warning(
            self, "文件不可用",
            f"文件当前不存在或无法访问：\n{entry.abs_path}\n\n"
            f"相对路径：{entry.rel_path}\n"
            "请重新扫描后再核对，窗口不会自动创建或移动文件。")
        return False

    def on_open(self):
        entry = self._selected_entry()
        if entry is None or not self._require_existing(entry):
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(entry.abs_path)):
            QMessageBox.warning(self, "无法打开", f"系统未能打开：{entry.abs_path}")

    def on_reveal(self):
        entry = self._selected_entry()
        if entry is None or not self._require_existing(entry):
            return
        err = _reveal_in_file_manager(entry.abs_path)
        if err:
            QMessageBox.warning(self, "无法定位", f"{err}\n{entry.abs_path}")

    def on_copy_path(self):
        entry = self._selected_entry()
        if entry is None:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(entry.abs_path)
        self.progress_label.setText(f"已复制路径：{entry.abs_path}")

    def on_recheck(self):
        if self.recheck_callback is not None:
            self.recheck_callback()

    def on_export(self, fmt: str):
        if self.report is None:
            return
        default = os.path.join(
            os.path.expanduser("~"),
            f"重复核对结果.{ 'json' if fmt == 'json' else 'csv' }")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出核对结果", default,
            "JSON 文件 (*.json)" if fmt == "json" else "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as f:
                    f.write(export_json_text(self.report))
            else:
                # S2-43：UTF-8 BOM + newline=''，Excel 不乱码、不出现 \r\r\n
                write_csv_file(path, export_csv_text(self.report))
        except OSError as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(
            self, "导出完成",
            f"已导出：{path}\n范围：{self.report.scope_note}\n"
            f"核对时间：{self.report.generated_at}")
