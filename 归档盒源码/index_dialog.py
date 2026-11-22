"""文件空间登记。登记和刷新只维护本地索引，不移动文件。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QListWidget, QMessageBox, QPushButton, QVBoxLayout,
    QFileDialog,
)

from index_service import register_space, remove_space, set_space_enabled, update_space
from index_store import IndexStore


class IndexSpacesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件空间")
        self.resize(640, 420)
        self.store = IndexStore()
        self.list = QListWidget()
        add = QPushButton("登记文件夹")
        refresh = QPushButton("刷新索引")
        pause = QPushButton("暂停或启用")
        remove = QPushButton("移除登记")
        add.clicked.connect(self._add)
        refresh.clicked.connect(self._refresh)
        pause.clicked.connect(self._toggle)
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        for widget in (add, refresh, pause, remove):
            buttons.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addLayout(buttons)
        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        self._specs = self.store.list_spaces()
        for spec in self._specs:
            state = "启用" if spec.enabled else "暂停"
            self.list.addItem(f"{spec.name}  [{state}]  {spec.root}")

    def _current(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self._specs):
            return None
        return self._specs[row]

    def _add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "登记文件空间")
        if not path:
            return
        try:
            register_space(path, store=self.store)
        except Exception as exc:
            QMessageBox.warning(self, "文件空间", str(exc))
            return
        self._reload()

    def _refresh(self) -> None:
        spec = self._current()
        if spec is None:
            return
        report = update_space(spec.space_id, store=self.store)
        QMessageBox.information(
            self, "文件空间",
            f"覆盖范围：{report.coverage}\n"
            f"新增 {report.added}，更新 {report.updated}，"
            f"删除 {report.deleted}，复用 {report.reused}，"
            f"重读 {report.reread}")
        self._reload()

    def _toggle(self) -> None:
        spec = self._current()
        if spec is None:
            return
        set_space_enabled(spec.space_id, not spec.enabled, store=self.store)
        self._reload()

    def _remove(self) -> None:
        spec = self._current()
        if spec is None:
            return
        remove_space(spec.space_id, store=self.store)
        self._reload()

    def closeEvent(self, event):
        try:
            self.store.close()
        finally:
            super().closeEvent(event)
