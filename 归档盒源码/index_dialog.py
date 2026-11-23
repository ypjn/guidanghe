"""文件空间登记。登记和刷新只维护本地索引，不移动文件。"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox,
    QPushButton, QVBoxLayout,
)

from index_service import (
    reassociate_space, register_space, remove_space, set_space_enabled,
    update_space,
)
from index_store import IndexStore
from volume_identity import probe_volume


class IndexSpacesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件空间")
        self.resize(720, 480)
        self.store = IndexStore()
        self._specs = []

        tip = QLabel(
            "登记外置或本机文件夹后，刷新只更新本地索引。"
            "移除登记不会删除来源文件。卷身份不一致时请「重新关联」或改回正确路径。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#475569;")

        self.list = QListWidget()
        self.detail = QLabel("选择一项查看卷身份与路径。")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#334155;")
        self.list.currentRowChanged.connect(self._show_detail)

        add = QPushButton("登记文件夹")
        refresh = QPushButton("刷新索引")
        pause = QPushButton("暂停或启用")
        reassoc = QPushButton("重新关联卷")
        remove = QPushButton("移除登记")
        add.clicked.connect(self._add)
        refresh.clicked.connect(self._refresh)
        pause.clicked.connect(self._toggle)
        reassoc.clicked.connect(self._reassociate)
        remove.clicked.connect(self._remove)

        buttons = QHBoxLayout()
        for widget in (add, refresh, pause, reassoc, remove):
            buttons.addWidget(widget)

        layout = QVBoxLayout(self)
        layout.addWidget(tip)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        self._specs = self.store.list_spaces()
        for spec in self._specs:
            state = "启用" if spec.enabled else "暂停"
            link = "在线" if spec.online else "离线"
            indexed = spec.last_indexed_at or "未索引"
            fs = spec.filesystem or "?"
            self.list.addItem(
                f"{spec.name}  [{state}/{link}/{fs}]  {spec.root}  "
                f"索引于 {indexed}"
            )
        if self._specs:
            self.list.setCurrentRow(0)
        else:
            self.detail.setText("还没有登记空间。")

    def _current(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self._specs):
            return None
        return self._specs[row]

    def _show_detail(self, _row: int = -1) -> None:
        spec = self._current()
        if spec is None:
            self.detail.setText("还没有登记空间。")
            return
        live = None
        if os.path.isdir(spec.root):
            try:
                live = probe_volume(spec.root)
            except Exception:
                live = None
        lines = [
            f"名称：{spec.name}",
            f"路径：{spec.root}",
            f"登记卷身份：{spec.volume_id or '未知'}",
            f"文件系统：{spec.filesystem or '未知'}",
            f"状态：{'启用' if spec.enabled else '暂停'} / "
            f"{'在线' if spec.online else '离线'}",
            f"最近索引：{spec.last_indexed_at or '未索引'}",
        ]
        if live is None:
            lines.append("当前探测：读不到卷（可能已拔出）")
        else:
            match = "一致" if live.volume_id == spec.volume_id else "不一致，需重新关联或换回原盘"
            lines.append(
                f"当前探测：{live.volume_id} / {live.filesystem or '?'} / "
                f"卷标「{live.label or '无'}」→ {match}"
            )
        self.detail.setText("\n".join(lines))

    def _ocr_scans_enabled(self) -> bool:
        parent = self.parent()
        check = getattr(parent, "ocr_check", None)
        return bool(check is not None and check.isChecked())

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
        report = update_space(
            spec.space_id, store=self.store,
            ocr_scans=self._ocr_scans_enabled())
        title = "文件空间"
        if report.coverage == "identity_mismatch":
            QMessageBox.warning(
                self, title,
                "\n".join(report.errors)
                + "\n\n若确认是新盘占用了同一路径，可点「重新关联卷」。")
        elif report.coverage == "offline":
            QMessageBox.information(
                self, title,
                "\n".join(report.errors)
                + "\n离线时仍可搜索上次索引；打开/定位已禁用。")
        else:
            QMessageBox.information(
                self, title,
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

    def _reassociate(self) -> None:
        spec = self._current()
        if spec is None:
            return
        reply = QMessageBox.question(
            self, "重新关联卷",
            "只有在确认当前路径下的磁盘就是要使用的那块盘时才继续。\n"
            "会更新登记的卷身份，不会删除来源文件；下次刷新按新身份增量更新。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            reassociate_space(spec.space_id, store=self.store)
        except Exception as exc:
            QMessageBox.warning(self, "文件空间", str(exc))
            return
        self._reload()
        QMessageBox.information(self, "文件空间", "已重新关联。请再点「刷新索引」。")

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
