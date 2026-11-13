"""
归档盒 V1.7 — 日常整理入口对话框：最近整理空间 / 待整理清单

最近空间只读本机使用记录：移除记录不会删除真实目录、配置或历史，
不存在的目录不会被自动创建。
待整理清单只登记范围，不移动文件、不写执行历史。
"""

from __future__ import annotations

import os
from typing import Callable, List

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import recent_spaces
from intake_model import IntakeList, is_within, normalize_path


class RecentSpacesDialog(QDialog):
    def __init__(self, open_callback: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.setWindowTitle("最近整理空间")
        self.resize(900, 520)
        self.open_callback = open_callback
        self._records = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        tip = QLabel(
            "只显示本机最近使用过的整理空间；记录保存在系统用户配置目录。\n"
            "移除记录只删除记录，不会删除真实目录、配置或历史；"
            "不存在的目录不会被自动创建。")
        tip.setWordWrap(True)
        tip.setStyleSheet("padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(tip)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["名称", "完整路径", "最近打开", "固定", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_open = QPushButton("打开空间")
        self.btn_open.clicked.connect(self.on_open)
        row.addWidget(self.btn_open)
        self.btn_pin = QPushButton("固定 / 取消固定")
        self.btn_pin.clicked.connect(self.on_toggle_pin)
        row.addWidget(self.btn_pin)
        self.btn_remove = QPushButton("从记录中移除")
        self.btn_remove.clicked.connect(self.on_remove)
        row.addWidget(self.btn_remove)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("关闭")
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.reject)
        lay.addWidget(bb)

    def refresh(self):
        self._records = recent_spaces.list_spaces()
        self.table.setRowCount(len(self._records))
        for r, rec in enumerate(self._records):
            self.table.setItem(r, 0, QTableWidgetItem(rec.name))
            path_item = QTableWidgetItem(rec.path)
            path_item.setToolTip(rec.path)
            self.table.setItem(r, 1, path_item)
            self.table.setItem(r, 2, QTableWidgetItem(rec.last_opened or "—"))
            self.table.setItem(r, 3, QTableWidgetItem("是" if rec.pinned else "否"))
            status = rec.status
            if rec.status_reason:
                status += f"：{rec.status_reason}"
            self.table.setItem(r, 4, QTableWidgetItem(status))
            self.table.item(r, 0).setData(Qt.UserRole, rec.path)
        self.table.resizeColumnsToContents()
        self._update_buttons()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _update_buttons(self, *_):
        rec = self._selected()
        self.btn_open.setEnabled(rec is not None and rec.usable)
        self.btn_pin.setEnabled(rec is not None and rec.exists)
        self.btn_remove.setEnabled(rec is not None)

    def on_open(self):
        rec = self._selected()
        if rec is None:
            return
        if not rec.usable:
            QMessageBox.warning(
                self, "无法打开空间",
                f"目录当前不可用：\n{rec.path}\n\n{rec.status_reason or rec.status}\n"
                "不会自动创建该目录。")
            return
        if self.open_callback(rec.path):
            self.accept()

    def on_toggle_pin(self):
        rec = self._selected()
        if rec is None:
            return
        recent_spaces.set_pinned(rec.path, not rec.pinned)
        self.refresh()

    def on_remove(self):
        rec = self._selected()
        if rec is None:
            return
        if QMessageBox.question(
                self, "移除记录",
                f"从最近空间记录中移除「{rec.name}」？\n"
                "只删除记录，不会删除真实目录、配置或历史。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        recent_spaces.remove(rec.path)
        self.refresh()


class _DropListWidget(QListWidget):
    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths)
        else:
            super().dropEvent(event)


class IntakeDialog(QDialog):
    """待整理清单：只登记范围，不移动文件、不写历史。"""

    def __init__(self, *, get_root: Callable[[], str],
                 get_summary: Callable[[], str],
                 get_items: Callable[[], IntakeList],
                 add_paths: Callable[[List[str]], List[str]],
                 remove_path: Callable[[str], bool],
                 clear_items: Callable[[], None],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("待整理清单 — 只登记范围")
        self.resize(820, 560)
        self.get_root = get_root
        self.get_summary = get_summary
        self.get_items = get_items
        self.add_paths = add_paths
        self.remove_path = remove_path
        self.clear_items = clear_items
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        self.scope_label = QLabel("")
        self.scope_label.setWordWrap(True)
        self.scope_label.setStyleSheet(
            "padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(self.scope_label)

        self.list_widget = _DropListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.dropped.connect(self._on_dropped)
        lay.addWidget(self.list_widget, 1)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#37474F;")
        lay.addWidget(self.message)

        row = QHBoxLayout()
        self.btn_add_files = QPushButton("添加文件…")
        self.btn_add_files.clicked.connect(self.on_add_files)
        row.addWidget(self.btn_add_files)
        self.btn_add_dir = QPushButton("添加文件夹…")
        self.btn_add_dir.clicked.connect(self.on_add_dir)
        row.addWidget(self.btn_add_dir)
        self.btn_remove = QPushButton("移除所选")
        self.btn_remove.clicked.connect(self.on_remove)
        row.addWidget(self.btn_remove)
        self.btn_clear = QPushButton("清空清单")
        self.btn_clear.clicked.connect(self.on_clear)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("关闭")
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.reject)
        lay.addWidget(bb)
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)

    def refresh(self, select_paths=None):
        root = self.get_root()
        self.list_widget.clear()
        items = self.get_items()
        for item in items.items:
            label = ("📁 " if item.kind == "dir" else "📄 ") + item.path
            entry = QListWidgetItem(label)
            entry.setData(Qt.UserRole, item.path)
            entry.setToolTip(item.path)
            self.list_widget.addItem(entry)
        self._select_paths(select_paths)
        root_text = root or "（尚未选择整理空间）"
        self.scope_label.setText(
            f"<b>当前整理空间：</b>{root_text}<br>"
            f"<b>清单：</b>{len(items)} 项<br>"
            f"<b>范围说明：</b>{self.get_summary()}<br>"
            "<span style='color:#37474F;'>拖入/添加只登记范围；"
            "不移动文件、不写执行历史。扫描仍经过保护与排除机制。</span>")
        self._update_buttons()

    def _select_paths(self, select_paths):
        """高亮刚拖入或刚加入的行。文件落在已有目录里时，选中那个目录。"""
        wanted = []
        for path in select_paths or []:
            if not path:
                continue
            try:
                wanted.append(normalize_path(path))
            except (OSError, ValueError):
                continue
        if not wanted:
            return
        first = None
        for row in range(self.list_widget.count()):
            entry = self.list_widget.item(row)
            item_path = entry.data(Qt.UserRole)
            if any(is_within(item_path, path) for path in wanted):
                entry.setSelected(True)
                if first is None:
                    first = row
        if first is None:
            return
        index = self.list_widget.model().index(first, 0)
        self.list_widget.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.Current)
        self.list_widget.scrollToItem(self.list_widget.item(first))

    def _update_buttons(self):
        has_items = self.list_widget.count() > 0
        self.btn_clear.setEnabled(has_items)
        self.btn_remove.setEnabled(
            bool(self.list_widget.selectedItems()))

    def _on_dropped(self, paths):
        self._add(paths)

    def _add(self, paths):
        messages = self.add_paths(paths)
        self._show_messages(messages)
        self.refresh(select_paths=paths)
        parent = self.parent()
        if parent is not None and hasattr(parent, "_pending_intake_selection"):
            parent._pending_intake_selection = []

    def _show_messages(self, messages):
        shown = [m for m in (messages or []) if m][-8:]
        self.message.setText("\n".join(shown))
        problem = any(not m.startswith("已加入") for m in shown)
        if not shown:
            color = "#37474F"
        elif problem:
            color = "#B71C1C"
        else:
            color = "#1B5E20"
        self.message.setStyleSheet(f"color:{color};")

    def on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加待整理文件", os.path.expanduser("~"),
            "所有文件 (*)")
        if paths:
            self._add(paths)

    def on_add_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "添加待整理文件夹", os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly)
        if path:
            self._add([path])

    def on_remove(self):
        failed = 0
        for entry in list(self.list_widget.selectedItems()):
            if not self.remove_path(entry.data(Qt.UserRole)):
                failed += 1
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "待整理清单",
                "有项目没有移除。窗口正在关闭，或当前有后台任务。")

    def on_clear(self):
        if QMessageBox.question(
                self, "清空清单",
                "清空待整理清单？只清除登记范围，不会移动或删除任何文件。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.clear_items()
        self.refresh()
