"""跨已登记空间的查找窗口。查找不移动文件。"""

from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QVBoxLayout,
)

from index_store import IndexStore
from search_intent import understand
from search_session import SearchSession, locate_hit, run_lookup

PAGE_SIZE = 50


class _LookupThread(QThread):
    finished_ok = Signal(int, object)

    def __init__(self, store, understood, space_ids, offset, token):
        super().__init__()
        self.store = store
        self.understood = understood
        self.space_ids = space_ids
        self.offset = offset
        self.token = token
        self.error = ""

    def run(self):
        store = IndexStore(self.store.path)
        try:
            page = run_lookup(
                store, self.understood, self.space_ids,
                limit=PAGE_SIZE, offset=self.offset)
        except Exception as exc:
            self.error = str(exc)
            page = None
        finally:
            store.close()
        self.finished_ok.emit(self.token, page)


class SearchDialog(QDialog):
    def __init__(self, parent=None, store: IndexStore | None = None):
        super().__init__(parent)
        self.setWindowTitle("搜索")
        self.resize(860, 560)
        self.store = store or IndexStore()
        self._owns_store = store is None
        self.session = SearchSession()
        self.understood = None
        self.hits = []
        self.offset = 0
        self.worker = None
        self._closing = False
        self._pending_plan = None
        self._pending_root = ""
        self.query = QLineEdit()
        self.query.setPlaceholderText("文件名、项目名，或正文里的词")
        self.summary = QLabel("只查找，不移动。尚未做语义检索。")
        self.summary.setWordWrap(True)
        self.spaces = QComboBox()
        self.status = QLabel("输入后搜索。结果按页显示，不会一次装入全部正文。")
        self.status.setWordWrap(True)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setUniformItemSizes(False)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results.setTextElideMode(Qt.TextElideMode.ElideNone)
        search = QPushButton("搜索")
        more = QPushButton("下一页")
        reveal = QPushButton("定位到文件")
        organize = QPushButton("整理所选")
        confirm = QPushButton("确认移动")
        search.clicked.connect(lambda: self.start_search(0))
        more.clicked.connect(self._next_page)
        reveal.clicked.connect(self._reveal)
        organize.clicked.connect(self.prepare_organize)
        confirm.clicked.connect(self.confirm_organize)
        self.query.returnPressed.connect(lambda: self.start_search(0))
        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(self.spaces)
        top.addWidget(search)
        top.addWidget(more)
        actions = QHBoxLayout()
        actions.addWidget(reveal)
        actions.addWidget(organize)
        actions.addWidget(confirm)
        actions.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.status)
        layout.addLayout(actions)
        self._load_spaces()

    def _load_spaces(self) -> None:
        self.spaces.clear()
        self.spaces.addItem("全部空间", "")
        try:
            specs = self.store.list_spaces()
        except Exception as exc:
            self.status.setText(f"索引暂时打不开：{exc}")
            return
        for spec in specs:
            state = "启用" if spec.enabled else "暂停"
            self.spaces.addItem(f"{spec.name}（{state}）", spec.space_id)

    def start_search(self, offset: int) -> None:
        self.offset = max(0, offset)
        token = self.session.begin()
        self.understood = understand(self.query.text())
        self.summary.setText(self.understood.summary())
        if self.understood.action == "organize":
            self.status.setText("这句话是整理。请先查看结果，再选择要移动的文件。")
        space_id = self.spaces.currentData()
        space_ids = [space_id] if space_id else []
        self.worker = _LookupThread(
            self.store, self.understood, space_ids, self.offset, token)
        self.worker.finished_ok.connect(self._deliver)
        self.worker.start()

    def search_blocking(self) -> None:
        """测试和需要立即看到结果时使用。仍遵守查询代次。"""
        self.offset = 0
        token = self.session.begin()
        self.understood = understand(self.query.text())
        self.summary.setText(self.understood.summary())
        space_id = self.spaces.currentData()
        space_ids = [space_id] if space_id else []
        page = run_lookup(
            self.store, self.understood, space_ids, limit=PAGE_SIZE, offset=0)
        self.session.deliver(token, page, self._show)

    def _deliver(self, token: int, page) -> None:
        if self._closing:
            return
        worker = self.worker
        if worker is not None and worker.error and token == self.session.generation:
            self.status.setText(f"搜索失败：{worker.error}")
            return
        self.session.deliver(token, page, self._show)

    def _show(self, page) -> None:
        if page is None:
            return
        self.hits = list(page.hits)
        self.results.clear()
        if not self.hits:
            self.results.addItem("没有匹配的文件")
        for hit in self.hits:
            state = {
                "complete": "已索引",
                "partial": "部分索引",
                "failed": "失败",
                "unsupported": "未读取正文",
            }.get(hit.content_status, hit.content_status or hit.availability or "")
            field = {"body": "正文", "name": "文件名"}.get(hit.field, "文件")
            snippet = (hit.evidence or "").replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            self.results.addItem(
                f"{hit.rel_path}  [{field} {hit.location} {state}]  {snippet}")
        note = page.note or ""
        self.status.setText(
            f"本页 {len(self.hits)} 条，从第 {self.offset + 1} 条开始。{note}")

    def _next_page(self) -> None:
        if not self.hits:
            return
        self.start_search(self.offset + PAGE_SIZE)

    def current_hit(self):
        row = self.results.currentRow()
        if row < 0 or row >= len(self.hits):
            return None
        return self.hits[row]

    def _reveal(self) -> None:
        hit = self.current_hit()
        if hit is None:
            self.status.setText("先选择一条结果。")
            return
        path, reason = locate_hit(self.store, hit)
        if not path:
            self.status.setText(reason)
            return
        self.status.setText(path)
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def prepare_organize(self):
        from planner import DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan
        from scanner import scan_directory
        from search_session import selection_plan

        self._pending_plan = None
        hit = self.current_hit()
        if self.understood is None or self.understood.action != "organize":
            self.status.setText("当前是查找。查找不会生成移动计划。")
            return [], ["当前是查找"]
        if hit is None:
            self.status.setText("先选择要整理的文件。受保护项目即使能搜到也不会被移动。")
            return [], ["没有选择"]
        spec = self.store.get_space(hit.space_id)
        if spec is None:
            self.status.setText("空间已移除，不能整理。")
            return [], ["空间已移除"]
        chosen = {item.rel_path for item in self.hits
                  if item.space_id == hit.space_id and item.availability == "online"}
        if self.results.currentRow() >= 0:
            chosen = {hit.rel_path}
        scan = scan_directory(spec.root)
        plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
        hits_by_path = {item.rel_path: item for item in self.hits}
        kept, notes = selection_plan(spec.root, plan, chosen, hit.space_id, hits_by_path)
        self._pending_plan = plan
        self._pending_root = spec.root
        lines = [f"{item.file.rel_path} → {item.target_rel_path}" for item in kept]
        self.status.setText("；".join(lines + notes) or "没有可移动的文件")
        return kept, notes

    def confirm_organize(self):
        from executor import execute_plan
        from storage import Storage

        if self._pending_plan is None or not self._pending_root:
            self.status.setText("还没有可确认的整理计划。")
            return None
        storage = Storage(self._pending_root)
        try:
            result = execute_plan(self._pending_plan, storage)
        finally:
            storage.close()
        self._pending_plan = None
        self.status.setText(f"已移动 {result.moved} 个文件。可在历史里撤销。")
        return result

    def shutdown(self) -> None:
        self._closing = True
        worker = self.worker
        self.worker = None
        if worker is not None:
            if worker.isRunning():
                worker.wait(5000)
            worker.deleteLater()
        if self._owns_store:
            self.store.close()
            self._owns_store = False

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
