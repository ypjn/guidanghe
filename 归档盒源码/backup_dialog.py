"""本地版本备份管理对话框。

复用 backup_service / backup_store；预览确认后才创建版本；
恢复写入新的空目录。与「配置备份/恢复上一次配置」语义分离。
"""

from __future__ import annotations

import os
from typing import Optional, Set

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from backup_service import list_backup_versions
from backup_store import validate_vault_placement
from backup_worker import (
    BackupPreviewWorker,
    BackupRestoreWorker,
    BackupRunWorker,
    BackupVerifyWorker,
)


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _state_label(manifest) -> str:
    if getattr(manifest, "recoverable", False):
        return "完整可恢复"
    state = getattr(manifest, "state", "") or "incomplete"
    if state == "complete" and getattr(manifest, "errors", None):
        return "完整但有错误"
    return "未完成/不可恢复"


class BackupManageDialog(QDialog):
    """非模态备份管理。活跃 worker 通过 running_workers() 交给主窗口关闭链。"""

    def __init__(self, main_window, parent=None,
                 default_source: str = ""):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.setWindowTitle("版本备份")
        self.resize(920, 640)
        self.setModal(False)

        self._workers: list = []
        self._worker = None
        self._task_seq = 0
        self._busy = False
        self._closing = False
        self._pending_close = False

        self._preview = None
        self._preview_token: Optional[tuple] = None
        self._preview_seq = 0
        self._verified: dict[str, bool] = {}

        self._build_ui()
        if default_source and os.path.isdir(default_source):
            self.source_edit.setText(os.path.abspath(default_source))
        self._reload_versions()
        self._refresh_actions()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        intro = QLabel(
            "把选定文件夹做成可恢复的本地版本。备份不删源、不改源；"
            "恢复只写入你指定的新空目录。"
            "这里与「配置备份 / 恢复上一次配置」无关。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#475569;")
        root.addWidget(intro)

        paths = QGroupBox("来源与仓库")
        form = QFormLayout(paths)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("要备份的文件夹")
        self.source_edit.textChanged.connect(self._on_inputs_changed)
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_edit, 1)
        btn_src = QPushButton("浏览…")
        btn_src.clicked.connect(self._pick_source)
        src_row.addWidget(btn_src)
        form.addRow("来源", src_row)

        self.vault_edit = QLineEdit()
        self.vault_edit.setPlaceholderText("备份仓库（不能放在来源里面）")
        self.vault_edit.textChanged.connect(self._on_inputs_changed)
        vault_row = QHBoxLayout()
        vault_row.addWidget(self.vault_edit, 1)
        btn_vault = QPushButton("浏览…")
        btn_vault.clicked.connect(self._pick_vault)
        vault_row.addWidget(btn_vault)
        form.addRow("仓库", vault_row)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("可选备注，会写入完成版本的清单")
        form.addRow("备注", self.note_edit)

        self.protect_edit = QLineEdit()
        self.protect_edit.setPlaceholderText(
            "受保护前缀，逗号分隔，例如 私密,作业/未交")
        self.protect_edit.textChanged.connect(self._on_inputs_changed)
        form.addRow("保护前缀", self.protect_edit)

        self.allow_protect = QCheckBox("授权备份上述保护前缀内的文件")
        self.allow_protect.stateChanged.connect(self._on_inputs_changed)
        form.addRow("", self.allow_protect)
        root.addWidget(paths)

        preview_box = QGroupBox("预览（确认前不会创建版本）")
        pv = QVBoxLayout(preview_box)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(110)
        self.preview_text.setPlaceholderText("点「预览」后显示文件数、占用与排除项。")
        pv.addWidget(self.preview_text)
        prow = QHBoxLayout()
        self.btn_preview = QPushButton("预览")
        self.btn_preview.clicked.connect(self.start_preview)
        prow.addWidget(self.btn_preview)
        self.btn_backup = QPushButton("确认备份")
        self.btn_backup.clicked.connect(self.confirm_backup)
        prow.addWidget(self.btn_backup)
        prow.addStretch(1)
        pv.addLayout(prow)
        root.addWidget(preview_box)

        ver_box = QGroupBox("版本列表")
        vv = QVBoxLayout(ver_box)
        self.version_table = QTableWidget(0, 7)
        self.version_table.setHorizontalHeaderLabels([
            "版本", "时间", "状态", "文件数", "占用", "已校验可恢复", "备注",
        ])
        self.version_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.version_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.version_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.version_table.verticalHeader().setVisible(False)
        self.version_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.version_table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch)
        self.version_table.itemSelectionChanged.connect(self._refresh_actions)
        vv.addWidget(self.version_table)

        vrow = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.clicked.connect(self._reload_versions)
        vrow.addWidget(self.btn_refresh)
        self.btn_verify = QPushButton("完整性检查")
        self.btn_verify.clicked.connect(self.start_verify)
        vrow.addWidget(self.btn_verify)
        self.btn_restore = QPushButton("恢复到新空目录…")
        self.btn_restore.clicked.connect(self.start_restore)
        vrow.addWidget(self.btn_restore)
        vrow.addStretch(1)
        self.btn_cancel_task = QPushButton("取消当前任务")
        self.btn_cancel_task.clicked.connect(self.cancel_current)
        vrow.addWidget(self.btn_cancel_task)
        vv.addLayout(vrow)
        root.addWidget(ver_box, 1)

        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#334155;")
        root.addWidget(self.status)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        bottom.addWidget(self.btn_close)
        root.addLayout(bottom)

    # ---------- helpers ----------

    def running_workers(self):
        return [w for w in self._workers if w is not None]

    def _policy(self) -> Optional[dict]:
        text = self.protect_edit.text().strip()
        if not text:
            return None
        prefixes = [p.strip().replace("\\", "/") for p in text.split(",") if p.strip()]
        if not prefixes:
            return None
        return {"protected_prefixes": prefixes}

    def _protected_allow(self) -> Set[str]:
        if not self.allow_protect.isChecked():
            return set()
        policy = self._policy() or {}
        return set(policy.get("protected_prefixes") or ())

    def _token(self) -> tuple:
        return (
            os.path.abspath(self.source_edit.text().strip()),
            os.path.abspath(self.vault_edit.text().strip()),
            tuple(sorted((self._policy() or {}).get("protected_prefixes") or ())),
            tuple(sorted(self._protected_allow())),
        )

    def _invalidate_preview(self, reason: str = ""):
        self._preview = None
        self._preview_token = None
        self._preview_seq += 1
        if reason:
            self.preview_text.setPlainText(f"预览已作废：{reason}")
        self._refresh_actions()

    def _on_inputs_changed(self, *_):
        if self._preview is not None:
            self._invalidate_preview("来源、仓库、保护范围或授权已改变")
        self._refresh_actions()

    def _pick_source(self):
        path = QFileDialog.getExistingDirectory(self, "选择要备份的文件夹")
        if path:
            self.source_edit.setText(path)

    def _pick_vault(self):
        path = QFileDialog.getExistingDirectory(self, "选择备份仓库目录")
        if path:
            self.vault_edit.setText(path)

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        for btn in (
            self.btn_preview, self.btn_backup, self.btn_refresh,
            self.btn_verify, self.btn_restore,
        ):
            btn.setEnabled(not busy)
        self.btn_cancel_task.setEnabled(busy)
        if message:
            self.status.setText(message)
        if not busy:
            self._refresh_actions()

    def _refresh_actions(self):
        if self._busy:
            return
        source = self.source_edit.text().strip()
        vault = self.vault_edit.text().strip()
        paths_ok = bool(source and vault)
        self.btn_preview.setEnabled(paths_ok)
        preview_ok = (
            self._preview is not None
            and getattr(self._preview, "ok", False)
            and self._preview_token == self._token()
        )
        self.btn_backup.setEnabled(preview_ok)
        row = self.version_table.currentRow()
        has_row = row >= 0
        self.btn_verify.setEnabled(has_row and bool(vault))
        self.btn_restore.setEnabled(has_row and bool(vault))
        self.btn_cancel_task.setEnabled(False)

    def _current_version_id(self) -> Optional[str]:
        row = self.version_table.currentRow()
        if row < 0:
            return None
        item = self.version_table.item(row, 0)
        return item.text() if item else None

    def _reload_versions(self):
        vault = self.vault_edit.text().strip()
        self.version_table.setRowCount(0)
        if not vault or not os.path.isdir(vault):
            return
        try:
            versions = list_backup_versions(vault, recoverable_only=False)
        except Exception as exc:
            self.status.setText(f"读取版本失败：{exc}")
            return
        for manifest in versions:
            row = self.version_table.rowCount()
            self.version_table.insertRow(row)
            verified = self._verified.get(manifest.version_id)
            verified_text = (
                "是" if verified is True
                else ("否" if verified is False else "未检查")
            )
            if not manifest.recoverable:
                verified_text = "不可用"
            values = [
                manifest.version_id,
                manifest.created_at,
                _state_label(manifest),
                str(manifest.file_count),
                _fmt_bytes(manifest.total_bytes),
                verified_text,
                manifest.note or "",
            ]
            for col, text in enumerate(values):
                self.version_table.setItem(row, col, QTableWidgetItem(text))
        self._refresh_actions()

    def _start_worker(self, worker, kind: str):
        self._worker = worker
        self._workers.append(worker)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._set_busy(True, f"正在{kind}……")
        worker.start()

    def _on_progress(self, cur: int, total: int, name: str):
        if self._closing:
            return
        self.status.setText(f"进度 [{cur}/{total}] {name}")

    def _on_worker_finished(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if self._worker is worker:
            self._worker = None
        if self._pending_close and not any(
                w.isRunning() for w in self._workers if w is not None):
            self._pending_close = False
            self._closing = True
            self.close()
            return
        if not self._busy:
            return
        # 忙碌态由各完成回调结束；取消/失败也会回调

    def cancel_current(self):
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            self.status.setText("取消中，等待当前安全步骤结束……")

    # ---------- preview / backup ----------

    def start_preview(self):
        if self._busy:
            return
        source = self.source_edit.text().strip()
        vault = self.vault_edit.text().strip()
        if not source or not vault:
            QMessageBox.warning(self, "版本备份", "请先选择来源和仓库。")
            return
        placement = validate_vault_placement(source, vault)
        if placement:
            QMessageBox.warning(self, "版本备份", "\n".join(placement))
            return
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
        self._task_seq += 1
        seq = self._task_seq
        self._preview_seq = seq
        self._preview = None
        self._preview_token = None
        worker = BackupPreviewWorker(
            seq, source, vault,
            policy=self._policy(),
            protected_allow=self._protected_allow(),
            parent=self,
        )
        worker.finished_ok.connect(self._on_preview_ok)
        worker.failed.connect(self._on_preview_failed)
        worker.cancelled.connect(self._on_preview_cancelled)
        self._start_worker(worker, "预览")

    def _on_preview_ok(self, seq: int, report):
        if self._closing or seq != self._preview_seq:
            return
        self._set_busy(False)
        self._preview = report
        self._preview_token = self._token()
        lines = [
            f"来源：{report.source}",
            f"仓库：{report.vault}",
            f"文件数：{report.file_count}",
            f"总量：{_fmt_bytes(report.total_bytes)}",
            f"预计新增占用：{_fmt_bytes(report.estimated_new_bytes)}",
        ]
        meta = report.metadata_support or {}
        if meta.get("note"):
            lines.append(f"元数据：{meta['note']}")
        if report.exclusions:
            lines.append(f"排除 {len(report.exclusions)} 项：")
            for item in report.exclusions[:12]:
                lines.append(f"  - {item.get('path')}: {item.get('reason')}")
            if len(report.exclusions) > 12:
                lines.append(f"  …另有 {len(report.exclusions) - 12} 项")
        if report.errors or report.placement_errors:
            lines.append("问题：")
            for err in list(report.placement_errors) + list(report.errors):
                lines.append(f"  - {err}")
        self.preview_text.setPlainText("\n".join(lines))
        self.status.setText(
            "预览完成。确认无误后再点「确认备份」。" if report.ok
            else "预览未通过，不能备份。"
        )
        self._refresh_actions()

    def _on_preview_failed(self, seq: int, message: str):
        if self._closing or seq != self._preview_seq:
            return
        self._set_busy(False)
        self._invalidate_preview(message)
        self.status.setText(f"预览失败：{message}")

    def _on_preview_cancelled(self, seq: int):
        if self._closing or seq != self._preview_seq:
            return
        self._set_busy(False)
        self._invalidate_preview("用户取消")
        self.status.setText("预览已取消。")

    def confirm_backup(self):
        if self._busy:
            return
        if (
            self._preview is None
            or not getattr(self._preview, "ok", False)
            or self._preview_token != self._token()
        ):
            QMessageBox.warning(
                self, "版本备份",
                "当前没有有效预览。来源/仓库/策略变化后需要重新预览。")
            return
        reply = QMessageBox.question(
            self, "确认备份",
            f"将创建新版本：\n"
            f"文件 {self._preview.file_count} 个，"
            f"预计新增 {_fmt_bytes(self._preview.estimated_new_bytes)}。\n\n"
            "确认后才写入仓库。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        source = self.source_edit.text().strip()
        vault = self.vault_edit.text().strip()
        self._task_seq += 1
        seq = self._task_seq
        worker = BackupRunWorker(
            seq, source, vault,
            policy=self._policy(),
            protected_allow=self._protected_allow(),
            note=self.note_edit.text().strip(),
            parent=self,
        )
        worker.finished_ok.connect(self._on_backup_ok)
        worker.failed.connect(self._on_backup_failed)
        worker.cancelled.connect(self._on_backup_cancelled)
        self._start_worker(worker, "备份")

    def _on_backup_ok(self, seq: int, report):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self._invalidate_preview("已完成备份，需重新预览后再做下一版")
        self.status.setText(
            f"已创建版本 {report.version_id}（{report.file_count} 个文件）。")
        self._reload_versions()

    def _on_backup_failed(self, seq: int, message: str):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText(f"备份失败：{message}")
        self._reload_versions()

    def _on_backup_cancelled(self, seq: int):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText("备份已取消；未完成版本不会标为可恢复。")
        self._reload_versions()

    # ---------- verify / restore ----------

    def start_verify(self):
        if self._busy:
            return
        vault = self.vault_edit.text().strip()
        version_id = self._current_version_id()
        if not vault or not version_id:
            return
        self._task_seq += 1
        seq = self._task_seq
        worker = BackupVerifyWorker(seq, vault, version_id, parent=self)
        worker.finished_ok.connect(
            lambda s, report, vid=version_id: self._on_verify_ok(s, report, vid))
        worker.failed.connect(self._on_verify_failed)
        worker.cancelled.connect(self._on_verify_cancelled)
        self._start_worker(worker, "校验")

    def _on_verify_ok(self, seq: int, report, version_id: str):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self._verified[version_id] = bool(report.ok)
        if report.ok:
            self.status.setText(f"版本 {version_id} 已校验，对象完整，可恢复。")
        else:
            self.status.setText(
                f"版本 {version_id} 校验未通过："
                + ("；".join(report.errors) or "未知错误")
            )
        self._reload_versions()

    def _on_verify_failed(self, seq: int, message: str):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText(f"校验失败：{message}")

    def _on_verify_cancelled(self, seq: int):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText("校验已取消。")

    def start_restore(self):
        if self._busy:
            return
        vault = self.vault_edit.text().strip()
        version_id = self._current_version_id()
        if not vault or not version_id:
            return
        if self._verified.get(version_id) is False:
            QMessageBox.warning(
                self, "版本备份",
                "该版本最近校验未通过，不能当作已校验可恢复版本。")
            return
        target = QFileDialog.getExistingDirectory(
            self, "选择恢复目标（必须是空目录，或不存在将新建）")
        if not target:
            return
        source = self.source_edit.text().strip()
        if source and os.path.isdir(source):
            try:
                if os.path.commonpath([
                    os.path.realpath(target),
                    os.path.realpath(source),
                ]) == os.path.realpath(source):
                    QMessageBox.warning(
                        self, "版本备份", "恢复目标不能落在来源目录内。")
                    return
            except ValueError:
                pass
        try:
            if os.path.commonpath([
                os.path.realpath(target),
                os.path.realpath(vault),
            ]) == os.path.realpath(vault):
                QMessageBox.warning(
                    self, "版本备份", "恢复目标不能落在备份仓库内。")
                return
        except ValueError:
            pass
        if os.path.isdir(target) and os.listdir(target):
            QMessageBox.warning(
                self, "版本备份", "恢复目标必须是新的空目录，拒绝覆盖已有内容。")
            return
        reply = QMessageBox.question(
            self, "确认恢复",
            f"将版本 {version_id} 恢复到：\n{target}\n\n"
            "不会改动源文件夹和仓库里的原对象。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._task_seq += 1
        seq = self._task_seq
        worker = BackupRestoreWorker(
            seq, vault, version_id, target, parent=self)
        worker.finished_ok.connect(self._on_restore_ok)
        worker.failed.connect(self._on_restore_failed)
        worker.cancelled.connect(self._on_restore_cancelled)
        self._start_worker(worker, "恢复")

    def _on_restore_ok(self, seq: int, report):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText(
            f"已恢复 {report.restored} 个文件到 {report.target}。")

    def _on_restore_failed(self, seq: int, message: str):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        # 取消恢复时 worker 走 cancelled；这里保留已恢复数量提示由服务层 errors
        self.status.setText(f"恢复未完成：{message}")

    def _on_restore_cancelled(self, seq: int):
        if self._closing or seq != self._task_seq:
            return
        self._set_busy(False)
        self.status.setText(
            "恢复已取消。已写出的文件保留在目标目录，不会自动清理未知内容。")

    # ---------- close ----------

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        running = [w for w in self._workers if w is not None and w.isRunning()]
        if running:
            for w in running:
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
            self._pending_close = True
            self.status.setText("正在等待备份任务结束……")
            event.ignore()
            QTimer.singleShot(200, self._retry_close)
            return
        self._closing = True
        event.accept()

    def _retry_close(self):
        if not self._pending_close or self._closing:
            return
        running = [w for w in self._workers if w is not None and w.isRunning()]
        if running:
            QTimer.singleShot(200, self._retry_close)
            return
        self._pending_close = False
        self._closing = True
        self.close()
