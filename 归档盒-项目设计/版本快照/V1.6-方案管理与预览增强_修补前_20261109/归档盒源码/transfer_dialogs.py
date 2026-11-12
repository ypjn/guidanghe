"""
归档盒 V1.6 — 方案管理 / 导入应用 / 影响预览界面

- 所有影响预览都在后台线程（复用 rule_preview.PreviewWorker）；
- 应用前必须经过「替换 / 合并」选择与差异、影响确认；
- 界面不直接写配置文件，统一调用传入的回调（由主窗口负责备份、
  保存、重新加载与旧计划失效）。
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox,
    QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QButtonGroup,
)

from classification_config import (
    ClassificationConfig, ClassificationConfigError, clone_config,
    config_summary,
)
from config_transfer import (
    ConfigDiff, MergeConflict, MergeResult, config_diff, merge_configs,
    backup_summary, list_backups, restore_latest_backup,
)
from planner import MODE_BY_DIRECTION_SEMESTER
from rule_preview import PreviewWorker
import scheme_store


GROUP_LABELS = {
    "semester_map": "学期映射",
    "top_direction": "顶层方向",
    "semester_dirs": "学期内目录",
    "generic_dirs": "通用目录前缀",
    "keyword_rules": "关键词规则",
    "long_term_rules": "长期修正规则",
}

CONFLICT_LABELS = {
    "same_id_changed": "同标识不同内容",
    "name_duplicate": "长期规则同名",
    "mapping_changed": "映射冲突 / 已跳过",
}


# ======================== 替换 / 合并选择 ========================

class ConfigChoiceDialog(QDialog):
    """导入方案后的替换 / 合并选择与冲突处理。"""

    def __init__(self, base_config: ClassificationConfig,
                 incoming_config: ClassificationConfig, *,
                 source_label: str = "", imported_info: List[str] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"导入方案 — {source_label or '方案'}")
        self.resize(860, 620)
        self.base_config = clone_config(base_config)
        self.incoming_config = clone_config(incoming_config)
        self.decisions: Dict[str, str] = {}
        self.result_config: Optional[ClassificationConfig] = None
        self.result_mode = "replace"
        self.merge_result: Optional[MergeResult] = None
        self.diff: Optional[ConfigDiff] = None
        self._building = False

        lay = QVBoxLayout(self)
        info = QLabel(
            ("<br>".join(imported_info) + "<br><br>" if imported_info else "")
            + f"<b>当前目录配置：</b>{config_summary(self.base_config)}<br>"
            f"<b>导入方案配置：</b>{config_summary(self.incoming_config)}<br>"
            "<span style='color:#37474F;'>只迁移「推荐模式」与长期修正规则；"
            "不包含手动保护配置、操作历史、文件清单或临时手动修正。</span>"
        )
        info.setWordWrap(True)
        info.setStyleSheet("padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(info)

        mode_row = QHBoxLayout()
        self.rb_replace = QRadioButton("替换当前推荐模式配置")
        self.rb_merge = QRadioButton("合并到当前配置（现有优先）")
        self.rb_replace.setChecked(True)
        self.bg = QButtonGroup(self)
        self.bg.addButton(self.rb_replace, 0)
        self.bg.addButton(self.rb_merge, 1)
        mode_row.addWidget(self.rb_replace)
        mode_row.addWidget(self.rb_merge)
        mode_row.addStretch(1)
        lay.addLayout(mode_row)

        self.mode_hint = QLabel("")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color:#37474F;")
        lay.addWidget(self.mode_hint)

        self.diff_label = QLabel("")
        self.diff_label.setWordWrap(True)
        self.diff_label.setStyleSheet(
            "padding:6px;background:#E3F2FD;border-radius:4px;")
        lay.addWidget(self.diff_label)

        self.conflict_table = QTableWidget(0, 4)
        self.conflict_table.setHorizontalHeaderLabels(
            ["类型", "规则", "说明", "处理"])
        self.conflict_table.verticalHeader().setVisible(False)
        self.conflict_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.conflict_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.conflict_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.conflict_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.conflict_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.conflict_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents)
        lay.addWidget(self.conflict_table, 1)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "color:#B71C1C;background:#FFEBEE;padding:6px;border-radius:4px;")
        self.error_label.hide()
        lay.addWidget(self.error_label)

        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("下一步：查看影响")
        self.bb.button(QDialogButtonBox.Cancel).setText("取消")
        self.bb.accepted.connect(self._on_ok)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

        self.rb_replace.toggled.connect(self._on_mode_changed)
        self.rb_merge.toggled.connect(self._on_mode_changed)
        self._recompute()

    def _on_mode_changed(self, *_):
        if self._building:
            return
        self._recompute()

    def _recompute(self):
        self._building = True
        is_merge = self.rb_merge.isChecked()
        self.mode_hint.setText(
            "合并：保留现有规则；相同标识且内容一致的长期规则跳过；"
            "同标识不同内容 / 同名长期规则列为冲突，默认保留现有；"
            "映射冲突保留现有，不静默改名或改优先级。"
            if is_merge else
            "替换：用导入方案完整替换当前目录的推荐模式配置；"
            "长期规则的稳定标识与顺序按方案文件保留。")
        if is_merge:
            self.merge_result = merge_configs(
                self.base_config, self.incoming_config, self.decisions)
            cfg = self.merge_result.config
            self.diff = config_diff(self.base_config, cfg)
            conflicts = self.merge_result.conflicts
            summary = (f"合并结果：新增 {self.merge_result.added}，"
                       f"采用导入 {self.merge_result.updated}，"
                       f"跳过 {self.merge_result.skipped}，"
                       f"无效跳过 {self.merge_result.invalid}")
        else:
            self.merge_result = None
            cfg = clone_config(self.incoming_config)
            self.diff = config_diff(self.base_config, cfg)
            conflicts = []
            summary = "替换结果：导入方案将成为当前目录的推荐模式配置。"
        self.result_config = cfg
        self.result_mode = "merge" if is_merge else "replace"
        self._fill_conflicts(conflicts)
        self.diff_label.setText(summary + "<br>" + "<br>".join(self.diff.lines()))
        errors = self.merge_result.errors if self.merge_result else []
        if errors:
            self.error_label.setText(
                "合并结果未通过配置校验：\n" + "\n".join(f"· {e}" for e in errors))
            self.error_label.show()
            self.bb.button(QDialogButtonBox.Ok).setEnabled(False)
        else:
            self.error_label.hide()
            self.bb.button(QDialogButtonBox.Ok).setEnabled(True)
        self._building = False

    def _fill_conflicts(self, conflicts: List[MergeConflict]):
        self.conflict_table.setRowCount(len(conflicts))
        for r, c in enumerate(conflicts):
            self.conflict_table.setItem(
                r, 0, QTableWidgetItem(CONFLICT_LABELS.get(c.kind, c.kind)))
            self.conflict_table.setItem(
                r, 1, QTableWidgetItem(f"{GROUP_LABELS.get(c.group, c.group)}：{c.label}"))
            self.conflict_table.setItem(r, 2, QTableWidgetItem(c.detail))
            if c.selectable:
                combo = QComboBox()
                combo.addItem("保留现有", "keep")
                combo.addItem("采用导入", "take")
                combo.setCurrentIndex(0 if c.decision == "keep" else 1)
                combo.currentIndexChanged.connect(
                    lambda _i, key=c.key, cb=combo: self._set_decision(key, cb.currentData()))
                self.conflict_table.setCellWidget(r, 3, combo)
            else:
                self.conflict_table.setItem(r, 3, QTableWidgetItem("保留现有"))

    def _set_decision(self, key: str, value: str):
        if self._building:
            return
        self.decisions[key] = value or "keep"
        self._recompute()

    def _on_ok(self):
        if self.result_config is None:
            return
        self.accept()

    def chosen_config(self) -> Optional[ClassificationConfig]:
        return clone_config(self.result_config) if self.result_config else None


# ======================== 应用前差异 + 文件影响 ========================

class ApplyPreviewDialog(QDialog):
    """显示配置差异；有扫描结果时后台计算文件影响，完成前不允许应用。"""

    def __init__(self, *, root: str, base_config: ClassificationConfig,
                 draft_config: ClassificationConfig, scan_result,
                 diff: ConfigDiff, worker_registry=None, mode: str = "",
                 rules=None, title: str = "应用方案前确认", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 620)
        self.root = os.path.abspath(root)
        self.base_config = base_config
        self.draft_config = draft_config
        self.scan_result = scan_result
        self.mode = mode or MODE_BY_DIRECTION_SEMESTER
        self.rules = rules or []
        self.worker_registry = worker_registry
        self._seq = 0
        self._worker = None
        self._closed = False
        self._impact = None

        lay = QVBoxLayout(self)
        head = QLabel(
            f"<b>根目录：</b>{self.root}<br>"
            f"<b>配置变化：</b>{'；'.join(diff.lines()[:1])}<br>"
            + ("<br>".join(diff.lines()[1:]))
        )
        head.setWordWrap(True)
        head.setStyleSheet("padding:6px;background:#E3F2FD;border-radius:4px;")
        lay.addWidget(head)

        detail = []
        if diff.added_items:
            detail.append("新增：" + "；".join(
                f"{GROUP_LABELS.get(e.group, e.group)} {e.label}"
                for e in diff.added_items[:8]))
        if diff.modified_items:
            detail.append("修改：" + "；".join(
                f"{GROUP_LABELS.get(e.group, e.group)} {e.label}"
                for e in diff.modified_items[:8]))
        if diff.removed_items:
            detail.append("删除：" + "；".join(
                f"{GROUP_LABELS.get(e.group, e.group)} {e.label}"
                for e in diff.removed_items[:8]))
        if detail:
            d = QLabel("<br>".join(detail))
            d.setWordWrap(True)
            lay.addWidget(d)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.impact_view = QTextEdit()
        self.impact_view.setReadOnly(True)
        lay.addWidget(self.impact_view, 1)

        note = QLabel(
            "应用后会重新绑定当前根目录；保护设置、历史记录和实际文件保持不变。"
            "旧预览将失效，需要重新生成计划后才能执行。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#37474F;")
        lay.addWidget(note)

        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("确认应用")
        self.bb.button(QDialogButtonBox.Cancel).setText("取消")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

        self._start_impact()

    # ---- 影响计算 ----
    def _start_impact(self):
        if self.scan_result is None:
            self.status.setText(
                "未扫描：暂不能计算文件影响；仍可查看上面的配置差异并应用。")
            self.impact_view.setPlainText(
                "（当前目录尚未扫描或扫描结果已失效，应用前无法给出命中与目标变化。）")
            self.bb.button(QDialogButtonBox.Ok).setEnabled(True)
            return
        try:
            self._seq += 1
            seq = self._seq
            worker = PreviewWorker(seq, self.root, self.scan_result,
                                   self.draft_config, self.base_config,
                                   self.mode, self.rules)
        except Exception as e:
            self.status.setText(f"影响预览无法启动：{e}")
            self.bb.button(QDialogButtonBox.Ok).setEnabled(True)
            return
        self._worker = worker
        self.bb.button(QDialogButtonBox.Ok).setEnabled(False)
        self.status.setText("正在后台计算文件影响（不移动任何文件）……")
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(lambda w=worker: self._release(w))
        if self.worker_registry is not None:
            try:
                self.worker_registry.register_preview_worker(worker)
            except Exception:
                pass
        worker.start()

    def _seq_ok(self, seq, root) -> bool:
        return (not self._closed and seq == self._seq
                and os.path.realpath(root) == os.path.realpath(self.root))

    def _on_progress(self, seq, root, cur, total, path):
        if not self._seq_ok(seq, root):
            return
        self.status.setText(f"正在后台计算文件影响…… {cur}/{total}")

    def _on_done(self, seq, root, result):
        if not self._seq_ok(seq, root):
            return
        self._impact = result
        lines = result.summary_lines()
        lines.append("")
        if result.representatives:
            lines.append("代表样本：")
            for rep in result.representatives[:10]:
                lines.append(f"· {rep.rel_path}")
                lines.append(f"   旧目标：{rep.old_target}")
                lines.append(f"   新目标：{rep.new_target}")
                lines.append(f"   依据：{rep.new_reason}")
        else:
            lines.append("（没有文件的目标位置会发生变化）")
        self.impact_view.setPlainText("\n".join(lines))
        self.status.setText(
            f"命中 {sum(h.count for h in result.hits)} 个文件；"
            f"分类或目标变化 {result.changed_count} 个。")
        self.bb.button(QDialogButtonBox.Ok).setEnabled(True)

    def _on_failed(self, seq, root, err):
        if not self._seq_ok(seq, root):
            return
        self.status.setText("影响计算失败；可以取消后重试。")
        self.impact_view.setPlainText(f"⚠ {err}")
        self.bb.button(QDialogButtonBox.Ok).setEnabled(True)

    def _on_cancelled(self, seq, root):
        if not self._seq_ok(seq, root):
            return
        self.status.setText("影响计算已取消。")

    def _release(self, worker):
        if self.worker_registry is not None:
            try:
                self.worker_registry.unregister_preview_worker(worker)
            except Exception:
                pass
        if self._worker is worker:
            self._worker = None

    def reject(self):
        self._closed = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
        super().reject()

    def accept(self):
        self._closed = True
        super().accept()


# ======================== 方案库 ========================

class SchemeCenterDialog(QDialog):
    """整理方案库：保存 / 复制 / 改名 / 删除 / 导入 / 导出 / 应用 / 恢复备份。"""

    def __init__(self, *, root: str,
                 current_config: ClassificationConfig,
                 apply_callback: Callable[[ClassificationConfig], None],
                 restore_callback: Callable[[], str],
                 scan_result=None, worker_registry=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("整理方案库 — 导入、导出与应用")
        self.resize(1000, 680)
        self.root = os.path.abspath(root)
        self.current_config = clone_config(current_config)
        self.apply_callback = apply_callback
        self.restore_callback = restore_callback
        self.scan_result = scan_result
        self.worker_registry = worker_registry
        self._records = []

        lay = QVBoxLayout(self)
        top = QLabel(
            f"<b>当前目录：</b>{self.root}<br>"
            f"<b>当前推荐模式配置：</b>{config_summary(self.current_config)}<br>"
            "<span style='color:#37474F;'>方案库只保存推荐模式与长期修正规则；"
            "应用时复制到当前目录，目录之间不共享可变配置。</span>")
        top.setWordWrap(True)
        top.setStyleSheet("padding:6px;background:#ECEFF1;border-radius:4px;")
        lay.addWidget(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["方案名称", "说明", "规则数量", "更新时间", "类型"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "color:#B71C1C;background:#FFEBEE;padding:6px;border-radius:4px;")
        self.error_label.hide()
        lay.addWidget(self.error_label)

        row1 = QHBoxLayout()
        self.btn_save_current = QPushButton("从当前配置保存为方案…")
        self.btn_save_current.clicked.connect(self.on_save_current)
        row1.addWidget(self.btn_save_current)
        self.btn_import = QPushButton("导入方案文件…")
        self.btn_import.clicked.connect(self.on_import)
        row1.addWidget(self.btn_import)
        self.btn_export_current = QPushButton("导出当前配置…")
        self.btn_export_current.clicked.connect(self.on_export_current)
        row1.addWidget(self.btn_export_current)
        row1.addStretch(1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_apply = QPushButton("查看差异与影响并应用…")
        self.btn_apply.clicked.connect(self.on_apply)
        row2.addWidget(self.btn_apply)
        self.btn_duplicate = QPushButton("复制…")
        self.btn_duplicate.clicked.connect(self.on_duplicate)
        row2.addWidget(self.btn_duplicate)
        self.btn_rename = QPushButton("修改名称/说明…")
        self.btn_rename.clicked.connect(self.on_rename)
        row2.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self.on_delete)
        row2.addWidget(self.btn_delete)
        self.btn_export = QPushButton("导出所选方案…")
        self.btn_export.clicked.connect(self.on_export_selected)
        row2.addWidget(self.btn_export)
        row2.addStretch(1)
        lay.addLayout(row2)

        backup_row = QHBoxLayout()
        self.backup_label = QLabel(backup_summary(self.root))
        self.backup_label.setWordWrap(True)
        backup_row.addWidget(self.backup_label, 1)
        self.btn_restore = QPushButton("恢复上一次配置")
        self.btn_restore.clicked.connect(self.on_restore)
        backup_row.addWidget(self.btn_restore)
        lay.addLayout(backup_row)

        end = QDialogButtonBox(QDialogButtonBox.Close)
        end.button(QDialogButtonBox.Close).setText("关闭")
        end.rejected.connect(self.reject)
        end.accepted.connect(self.reject)
        lay.addWidget(end)

        self.refresh_list()

    # ---- 列表 ----
    def refresh_list(self):
        self._records = scheme_store.list_schemes()
        self.table.setRowCount(len(self._records))
        for r, rec in enumerate(self._records):
            self.table.setItem(r, 0, QTableWidgetItem(rec.name))
            desc = rec.description if not rec.error else f"⚠ {rec.error}"
            self.table.setItem(r, 1, QTableWidgetItem(desc))
            self.table.setItem(r, 2, QTableWidgetItem(rec.counts_text))
            self.table.setItem(r, 3, QTableWidgetItem(rec.updated_at or "—"))
            kind = "内置（只读）" if rec.builtin else (
                "不可用" if rec.error else "自定义")
            self.table.setItem(r, 4, QTableWidgetItem(kind))
            self.table.item(r, 0).setData(Qt.UserRole, rec.scheme_id)
        self._update_buttons()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def _update_buttons(self, *_):
        rec = self._selected()
        usable = rec is not None and rec.usable
        self.btn_apply.setEnabled(usable)
        self.btn_duplicate.setEnabled(usable)
        self.btn_export.setEnabled(usable)
        self.btn_rename.setEnabled(usable and not rec.builtin)
        self.btn_delete.setEnabled(usable and not rec.builtin)
        self.btn_restore.setEnabled(bool(list_backups(self.root)))

    def _show_error(self, message: str):
        self.error_label.setText(message)
        self.error_label.show()
        QMessageBox.warning(self, "整理方案", message)

    # ---- 方案操作 ----
    def on_save_current(self):
        name, ok = QInputDialog.getText(self, "保存为方案", "方案名称：")
        if not ok or not (name or "").strip():
            return
        desc, _ = QInputDialog.getText(self, "保存为方案", "方案说明（可留空）：")
        try:
            scheme_store.save_scheme(name, desc or "", self.current_config)
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        self.refresh_list()
        QMessageBox.information(self, "整理方案", "已保存到本地方案库。")

    def on_duplicate(self):
        rec = self._selected()
        if rec is None or not rec.usable:
            return
        name, ok = QInputDialog.getText(
            self, "复制方案", "新方案名称：", text=f"{rec.name} 副本")
        if not ok or not (name or "").strip():
            return
        try:
            scheme_store.duplicate_scheme(rec.scheme_id, name)
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        self.refresh_list()

    def on_rename(self):
        rec = self._selected()
        if rec is None or not rec.usable:
            return
        if rec.builtin:
            QMessageBox.information(self, "整理方案", "内置方案只读，请先复制。")
            return
        name, ok = QInputDialog.getText(
            self, "修改方案", "方案名称：", text=rec.name)
        if not ok or not (name or "").strip():
            return
        desc, ok2 = QInputDialog.getText(
            self, "修改方案", "方案说明：", text=rec.description)
        if not ok2:
            return
        try:
            scheme_store.update_scheme_meta(rec.scheme_id, name, desc or "")
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        self.refresh_list()

    def on_delete(self):
        rec = self._selected()
        if rec is None or not rec.usable:
            return
        if rec.builtin:
            QMessageBox.information(self, "整理方案", "内置方案只读，不能删除。")
            return
        if QMessageBox.question(
                self, "删除方案",
                f"确定删除方案「{rec.name}」吗？\n"
                "已应用到其他目录的配置不会受影响。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            scheme_store.delete_scheme(rec.scheme_id)
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        self.refresh_list()

    def on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入方案文件", os.path.expanduser("~"),
            "归档盒方案 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            rec = scheme_store.import_scheme_file(path)
        except ClassificationConfigError as e:
            self._show_error(f"导入失败：{e}")
            return
        self.refresh_list()
        QMessageBox.information(
            self, "导入方案",
            f"已导入到方案库：{rec.name}\n可在列表中查看差异并应用。")

    def on_export_current(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前配置为方案", os.path.expanduser("~"),
            "归档盒方案 (*.json)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "导出方案", "方案名称：",
                                        text="当前配置方案")
        if not ok or not (name or "").strip():
            return
        try:
            scheme_store.export_current_config(path, self.current_config, name)
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        QMessageBox.information(self, "导出方案", f"已导出：{path}")

    def on_export_selected(self):
        rec = self._selected()
        if rec is None or not rec.usable:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出方案", os.path.expanduser("~"),
            "归档盒方案 (*.json)")
        if not path:
            return
        try:
            scheme_store.export_scheme_to(rec.scheme_id, path)
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        QMessageBox.information(self, "导出方案", f"已导出：{path}")

    def on_apply(self):
        rec = self._selected()
        if rec is None or not rec.usable:
            return
        self._confirm_and_apply(clone_config(rec.config), rec.name)

    def _confirm_and_apply(self, incoming: ClassificationConfig,
                           source_label: str):
        choice = ConfigChoiceDialog(
            self.current_config, incoming, source_label=source_label,
            imported_info=[f"来源：{source_label}"], parent=self)
        if choice.exec() != QDialog.Accepted:
            return
        cfg = choice.chosen_config()
        if cfg is None:
            return
        diff = config_diff(self.current_config, cfg)
        preview = ApplyPreviewDialog(
            root=self.root, base_config=clone_config(self.current_config),
            draft_config=cfg, scan_result=self.scan_result, diff=diff,
            worker_registry=self.worker_registry,
            title=f"应用方案 — {source_label}", parent=self)
        if preview.exec() != QDialog.Accepted:
            return
        try:
            self.apply_callback(cfg)
        except Exception as e:
            QMessageBox.critical(self, "应用方案失败", str(e))
            return
        self.current_config = clone_config(cfg)
        self.refresh_list()
        QMessageBox.information(
            self, "应用方案",
            "方案已应用到当前目录。旧预览已失效，请点击「重新预览」"
            "按新配置生成计划后再执行。")

    def on_restore(self):
        if QMessageBox.question(
                self, "恢复上一次配置",
                "将恢复最近一次应用方案前的分类配置（只改变配置，"
                "不会移动或撤销文件）。\n继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            message = self.restore_callback()
        except ClassificationConfigError as e:
            self._show_error(str(e))
            return
        except Exception as e:
            self._show_error(f"恢复失败：{e}")
            return
        self.refresh_list()
        QMessageBox.information(self, "恢复配置", message)
