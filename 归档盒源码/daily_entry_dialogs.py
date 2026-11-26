"""
归档盒 V1.7 — 日常整理入口对话框：最近整理空间 / 待整理清单

最近空间只读本机使用记录：移除记录不会删除真实目录、配置或历史，
不存在的目录不会被自动创建。
待整理清单只登记范围，不移动文件、不写执行历史。
"""

from __future__ import annotations

import os
from typing import Callable, List

from PySide6.QtCore import QEvent, QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QDialogButtonBox, QFileDialog,
    QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import recent_spaces
from intake_model import IntakeList, is_within, normalize_path
from operation_feedback import install


def _format_opened(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "—"
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _elide_text(text: str, limit: int = 72) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


class RecentSpacesDialog(QDialog):
    """最近整理空间。

    不使用 QTableWidget/QListWidget 的选择模型：Qt 6.12 的 macOS 辅助功能
    在「选中单元格替换占位符」时会删除父表接口，VoiceOver 读取
    NSAccessibility 层级时崩溃（QTBUG-149612，历史明细同源）。这里改为
    普通的单选按钮行，辅助功能直接看到「选择空间：名称、路径……」，按下
    即可选中，不进入 item view 的选择路径；所有控件都在布局里，没有浮层。
    """

    def __init__(self, open_callback: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.setWindowTitle("最近整理空间")
        self.setMinimumWidth(860)
        self.resize(980, 520)
        self.open_callback = open_callback
        self._records = []
        self._row_group = None
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

        hint = QLabel("用鼠标、方向键或辅助功能选中一行，再点「打开空间」。")
        hint.setStyleSheet("color:#455A64;")
        lay.addWidget(hint)

        self.rows_host = QWidget()
        self.rows_grid = QGridLayout(self.rows_host)
        self.rows_grid.setColumnStretch(2, 1)
        self.rows_grid.setHorizontalSpacing(10)
        self.rows_grid.setVerticalSpacing(6)
        self.rows_grid.setContentsMargins(8, 8, 16, 8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.rows_host)
        lay.addWidget(self.scroll, 1)

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
        self.feedback = install(lay)

    def _clear_rows(self):
        for i in range(self.rows_grid.rowCount()):
            self.rows_grid.setRowStretch(i, 0)
        while self.rows_grid.count():
            item = self.rows_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if self._row_group is not None:
            for button in self._row_group.buttons():
                self._row_group.removeButton(button)
            self._row_group.deleteLater()
            self._row_group = None

    def _add_header(self):
        headers = ["选择", "名称", "完整路径", "最近打开", "固定", "状态"]
        for col, text in enumerate(headers):
            label = QLabel(text)
            label.setStyleSheet("color:#546E7A;font-weight:600;")
            label.setMinimumWidth(label.fontMetrics().horizontalAdvance(text) + 12)
            self.rows_grid.addWidget(label, 0, col)
        self.rows_grid.setColumnMinimumWidth(0, 64)
        self.rows_grid.setColumnMinimumWidth(1, 96)
        self.rows_grid.setColumnMinimumWidth(3, 132)

    def refresh(self):
        self._clear_rows()
        try:
            self._records = recent_spaces.list_spaces()
        except recent_spaces.RecentSpacesError as exc:
            self._records = []
            self.feedback.reflect(f"最近空间读取失败：{exc}")
            QMessageBox.warning(self, "最近整理空间", str(exc))
            self._update_buttons()
            return
        self._add_header()
        if not self._records:
            empty = QLabel("还没有最近打开的空间。")
            empty.setStyleSheet("color:#78909C;padding:8px;")
            self.rows_grid.addWidget(empty, 1, 0, 1, 6)
            self._update_buttons()
            self.feedback.reflect("还没有最近打开的空间。")
            return
        self._row_group = QButtonGroup(self)
        self._row_group.setExclusive(True)
        for r, rec in enumerate(self._records):
            status = rec.status
            if rec.status_reason:
                status += f"：{rec.status_reason}"
            pick = QRadioButton()
            pick.setFixedWidth(24)
            opened = _format_opened(rec.last_opened)
            pick.setAccessibleName(
                f"选择空间：{rec.name}，{rec.path}，"
                f"最近打开 {opened}，"
                f"固定{'是' if rec.pinned else '否'}，状态 {status}")
            pick.setToolTip("选择这一行，再点「打开空间」")
            pick.installEventFilter(self)
            self._row_group.addButton(pick, r)
            pick.toggled.connect(lambda _checked=False: self._update_buttons())
            self.rows_grid.addWidget(pick, r + 1, 0)

            name_label = QLabel(_elide_text(rec.name, 20))
            name_label.setToolTip(rec.name)
            self.rows_grid.addWidget(name_label, r + 1, 1)

            path_label = QLabel(_elide_text(rec.path, 60))
            path_label.setToolTip(rec.path)
            path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            path_label.setMinimumWidth(180)
            self.rows_grid.addWidget(path_label, r + 1, 2)

            opened_label = QLabel(opened)
            opened_label.setMinimumWidth(132)
            opened_label.setMinimumWidth(132)
            self.rows_grid.addWidget(opened_label, r + 1, 3)

            pin_label = QLabel("是" if rec.pinned else "否")
            self.rows_grid.addWidget(pin_label, r + 1, 4)

            status_label = QLabel(_elide_text(status, 36))
            status_label.setToolTip(status)
            self.rows_grid.addWidget(status_label, r + 1, 5)
        self.rows_grid.setRowStretch(len(self._records) + 1, 1)
        self._update_buttons()
        self.feedback.reflect(f"已列出 {len(self._records)} 个最近空间。")

    def row_count(self) -> int:
        return len(self._records)

    def eventFilter(self, obj, event):
        """方向键在单选行之间移动，键盘和辅助功能都能选中一行。"""
        if (isinstance(obj, QRadioButton)
                and event.type() == QEvent.KeyPress
                and self._row_group is not None):
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Right):
                self._move_selection(1)
                return True
            if key in (Qt.Key_Up, Qt.Key_Left):
                self._move_selection(-1)
                return True
        return super().eventFilter(obj, event)

    def _move_selection(self, delta: int):
        buttons = self._row_group.buttons() if self._row_group else []
        if not buttons:
            return
        current = self._row_group.checkedId()
        if current < 0:
            current = 0
        target = max(0, min(len(buttons) - 1, current + delta))
        buttons[target].setChecked(True)
        buttons[target].setFocus()

    def _selected(self):
        if self._row_group is None:
            return None
        row = self._row_group.checkedId()
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
            self.feedback.reflect(
                f"打不开「{rec.name}」：{rec.status_reason or rec.status}")
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
        now_pinned = not rec.pinned
        try:
            recent_spaces.set_pinned(rec.path, now_pinned)
        except recent_spaces.RecentSpacesError as exc:
            self.feedback.reflect(f"固定失败：{exc}")
            QMessageBox.warning(self, "最近整理空间", str(exc))
            return
        self.refresh()
        self.feedback.reflect("已固定这个空间。" if now_pinned else "已取消固定。")

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
        try:
            recent_spaces.remove(rec.path)
        except recent_spaces.RecentSpacesError as exc:
            self.feedback.reflect(f"移除失败：{exc}")
            QMessageBox.warning(self, "最近整理空间", str(exc))
            return
        self.refresh()
        self.feedback.reflect("已从最近记录中移除。文件夹本身还在。")


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
        self.feedback = install(lay, self.message, hide_source=False)

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
