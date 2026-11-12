"""
归档盒 V1.1 — PySide6 界面入口

职责：目录选择 → 扫描 → 分类预览 → 确认 → 实际移动 → 持久化 → 撤销
V1.1 改动：
- 扫描时自动识别项目根目录并整体保护（不逐文件整理内部内容）
- 计划生成 + SHA-256 计算移入后台线程（PlanWorker）
- 过期结果丢弃（seq 计数器），取消后不发布半成品计划
"""

from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTableWidget, QTableWidgetItem,
    QLabel, QHeaderView, QFileDialog, QStatusBar, QProgressBar,
    QMessageBox, QAbstractItemView, QComboBox, QTextEdit,
    QDialog, QDialogButtonBox, QCheckBox,
)

from scanner import scan_directory, ScanResult
from planner import (
    generate_plan, OrganizePlan, CourseRule, MODE_BY_TYPE,
    MODE_BY_COURSE_THEN_TYPE, MODE_LABELS, ALL_MODES,
    DEFAULT_COURSE_RULES, parse_rules_text, rules_to_text,
)
from storage import Storage
from executor import (
    execute_plan, undo_latest_batch, undo_is_available,
    recover_batch, interrupted_batch_id,
)

# 归档盒自身源码目录 —— 默认保护，防止程序移动自己
DEFAULT_PROTECT_APP_SRC = os.path.dirname(os.path.abspath(__file__))


# ======================== 后台线程 ========================

class ScanWorker(QThread):
    """后台扫描线程。"""
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, root_path: str, protect_dirs=None, parent=None):
        super().__init__(parent)
        self.root_path = root_path
        self.protect_dirs = protect_dirs or []

    def run(self):
        try:
            r = scan_directory(self.root_path, protect_dirs=self.protect_dirs)
            self.finished_ok.emit(r)
        except Exception as e:
            self.failed.emit(str(e))


class PlanWorker(QThread):
    """后台计划生成线程（含 SHA-256 计算 + 重复检测）。

    所有对外信号都携带本任务自己的 seq，界面按 seq 校验来源，
    过期任务的结果/进度/失败/取消/结束都不会影响新任务状态。
    """
    progress = Signal(int, int, int, str)   # seq, current, total, current_filename
    finished_ok = Signal(int, object)       # seq, OrganizePlan
    cancelled = Signal(int)                 # seq — 用户取消（不返回半成品）
    failed = Signal(int, str)               # seq, error
    finished_seq = Signal(int)              # seq — run() 结束（含异常/取消）

    def __init__(self, seq: int, scan_result: ScanResult, mode: str,
                 rules, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.scan_result = scan_result
        self.mode = mode
        self.rules = rules
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            def _progress(cur, total, fname):
                self.progress.emit(self.seq, cur, total, fname)

            plan = generate_plan(
                self.scan_result, self.mode, self.rules,
                cancel_event=self.cancel_event,
                progress_cb=_progress,
            )

            if self.cancel_event.is_set():
                # 取消后不发布半成品计划
                self.cancelled.emit(self.seq)
                return

            self.finished_ok.emit(self.seq, plan)
        except Exception as e:
            self.failed.emit(self.seq, str(e))
        finally:
            self.finished_seq.emit(self.seq)


class ExecuteWorker(QThread):
    """后台执行线程（实际移动文件）。支持在当前文件操作完成后取消。"""
    finished_ok = Signal(object)   # ExecuteResult
    failed = Signal(str)

    def __init__(self, plan: OrganizePlan, storage: Storage, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.storage = storage
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = execute_plan(self.plan, self.storage,
                                  cancel_event=self.cancel_event)
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class UndoWorker(QThread):
    """后台撤销线程。"""
    finished_ok = Signal(object)   # UndoResult
    failed = Signal(str)

    def __init__(self, storage: Storage, parent=None):
        super().__init__(parent)
        self.storage = storage

    def run(self):
        try:
            result = undo_latest_batch(self.storage)
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


# ======================== 规则编辑对话框 ========================

class RuleEditDialog(QDialog):
    def __init__(self, current_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑课程分类规则")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        tip = QLabel(
            "每行一条规则，格式：关键词1,关键词2 → 课程名\n"
            "关键词用逗号、分号或斜杠分隔；多条规则按显示顺序取第一条命中。\n"
            "未匹配任何规则的文件归入「未分类课程」。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#616161;font-size:12px;")
        layout.addWidget(tip)
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(current_text)
        self.text_edit.setPlaceholderText(
            "示例：\n高数,高等数学 → 高等数学\nAIGC,生成式人工智能 → AIGC"
        )
        layout.addWidget(self.text_edit, stretch=1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_text(self) -> str:
        return self.text_edit.toPlainText().strip()


# ======================== 执行确认对话框 ========================

class ExecuteConfirmDialog(QDialog):
    """开始整理前的确认对话框。"""

    def __init__(self, root: str, plan: OrganizePlan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认整理计划")
        self.setMinimumWidth(480)
        self.resize(520, 340)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            f"<b>根目录：</b>{root}<br>"
            f"<b>分类模式：</b>{MODE_LABELS.get(plan.mode, plan.mode)}<br>"
            f"<b>已选文件：</b>{plan.selected_count} / {plan.total}<br>"
            f"<b>目标目录：</b>{os.path.basename(root)}/_已整理/…<br>"
        ))

        if plan.conflict_count:
            warn = QLabel(f"<span style='color:#E65100;'>⚠ 注意：有 {plan.conflict_count} 个目标路径磁盘上已存在同名文件，计划已自动分配唯一名称。</span>")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        warn2 = QLabel(
            "<span style='color:#B71C1C;'>重要：将实际移动这些文件！</span><br>"
            "操作将记录在目录下的 .organizer/history.db，可在关闭程序后重新打开撤销最近批次。"
        )
        warn2.setWordWrap(True)
        layout.addWidget(warn2)

        layout.addStretch(1)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.button(QDialogButtonBox.Ok).setText("确认开始整理")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


# ======================== 主窗口 ========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("归档盒 V1.1 — 可预览、可撤销的课程文件整理器")
        self.resize(1150, 720)
        self._center_window()

        self.scan_result: ScanResult | None = None
        self.current_plan: OrganizePlan | None = None
        self.rules: list[CourseRule] = list(DEFAULT_COURSE_RULES)
        self.mode: str = MODE_BY_COURSE_THEN_TYPE
        self._plan_valid: bool = False
        self._working: bool = False           # 扫描/执行/撤销中
        self._plan_working: bool = False      # 计划生成中（独立于 _working）
        self._storage: Storage | None = None

        # V1.1: 过期计划结果丢弃计数器
        self._plan_seq_counter = 0
        self._current_plan_seq = 0
        self._plan_cancel_requested = False
        self._plan_workers: list[PlanWorker] = []   # 保留引用，避免运行中的线程被回收
        self._close_pending = False                 # 关闭请求等待后台线程结束
        self._closing = False                       # 已接受关闭，后续异步回调直接忽略
        self._scan_worker = None
        self._plan_worker = None
        self._exec_worker = None
        self._undo_worker = None

        self._build_ui()

    def _center_window(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        # --- 顶部：目录 ---
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        top_bar.addWidget(QLabel("目录："))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择要扫描的文件夹……")
        self.path_edit.setReadOnly(True)
        self.path_edit.setMinimumWidth(360)
        top_bar.addWidget(self.path_edit, stretch=1)

        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self.on_browse)
        top_bar.addWidget(self.btn_browse)

        self.btn_scan = QPushButton("扫描")
        self.btn_scan.setStyleSheet(
            "QPushButton{background:#1E88E5;color:white;padding:6px 16px;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#1565C0;}"
            "QPushButton:disabled{background:#B0BEC5;}"
        )
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.on_scan)
        top_bar.addWidget(self.btn_scan)

        outer.addLayout(top_bar)

        # --- 第二行：模式 + 规则 ---
        cfg_bar = QHBoxLayout()
        cfg_bar.setSpacing(12)
        cfg_bar.addWidget(QLabel("分类模式："))
        self.mode_combo = QComboBox()
        for m in ALL_MODES:
            self.mode_combo.addItem(MODE_LABELS[m], m)
        self.mode_combo.setCurrentIndex(2)  # 默认：V1.1 推荐模式（方向/学期/类别）
        self.mode_combo.currentIndexChanged.connect(self.on_plan_param_changed)
        cfg_bar.addWidget(self.mode_combo)

        self.btn_edit_rules = QPushButton("编辑课程规则…")
        self.btn_edit_rules.clicked.connect(self.on_edit_rules)
        cfg_bar.addWidget(self.btn_edit_rules)

        self.btn_regenerate = QPushButton("重新预览")
        self.btn_regenerate.setStyleSheet(
            "QPushButton{background:#FB8C00;color:white;padding:6px 14px;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#EF6C00;}"
            "QPushButton:disabled{background:#B0BEC5;}"
        )
        self.btn_regenerate.setEnabled(False)
        self.btn_regenerate.clicked.connect(self.on_regenerate_plan)
        cfg_bar.addWidget(self.btn_regenerate)

        cfg_bar.addStretch(1)
        outer.addLayout(cfg_bar)

        # --- 第三行：整理 + 撤销 ---
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        self.btn_execute = QPushButton("✓ 开始整理")
        self.btn_execute.setStyleSheet(
            "QPushButton{background:#43A047;color:white;padding:7px 22px;"
            "border-radius:4px;font-weight:bold;font-size:14px;}"
            "QPushButton:hover{background:#2E7D32;}"
            "QPushButton:disabled{background:#B0BEC5;}"
        )
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self.on_execute)
        action_bar.addWidget(self.btn_execute)

        self.btn_undo = QPushButton("↩ 撤销最近批次")
        self.btn_undo.setStyleSheet(
            "QPushButton{background:#546E7A;color:white;padding:7px 20px;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#37474F;}"
            "QPushButton:disabled{background:#B0BEC5;}"
        )
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self.on_undo)
        action_bar.addWidget(self.btn_undo)

        self.btn_cancel = QPushButton("✕ 取消整理")
        self.btn_cancel.setStyleSheet(
            "QPushButton{background:#EF5350;color:white;padding:7px 18px;"
            "border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#C62828;}"
            "QPushButton:disabled{background:#B0BEC5;}"
        )
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self.on_cancel_execute)
        action_bar.addWidget(self.btn_cancel)

        action_bar.addStretch(1)
        outer.addLayout(action_bar)

        # --- 进度条 ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.hide()
        outer.addWidget(self.progress)

        # --- 预览表格 ---
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["勾选", "原位置", "目标位置", "分类依据", "状态", "大小"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self._suppress_cell_signals = False
        outer.addWidget(self.table, stretch=1)

        # --- 状态栏 ---
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪。请选择目录开始扫描。")

        # --- 计划失效提示标签 ---
        self.invalid_label = QLabel("⚠ 计划已失效（规则/模式/目录已更改）")
        self.invalid_label.setStyleSheet(
            "color:#E65100;background:#FFF3E0;padding:2px 10px;border-radius:3px;"
            "font-weight:bold;"
        )
        self.invalid_label.hide()
        outer.addWidget(self.invalid_label)

    # ============== 浏览 & 扫描 ==============

    def on_browse(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "选择要扫描的文件夹",
            os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly,
        )
        if chosen:
            self.path_edit.setText(chosen)
            self.btn_scan.setEnabled(True)
            self._reset_for_new_dir(chosen)
            self.statusBar().showMessage(f"已选择：{chosen}")

    def _reset_for_new_dir(self, root: str):
        """选择新目录时重置所有状态。"""
        # 先让仍在跑的计划任务全部过期（新目录 = 全新的计划上下文）
        self._abandon_plan_generation()
        self.scan_result = None
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self.invalid_label.hide()
        self.btn_regenerate.setEnabled(False)
        self.btn_execute.setEnabled(False)
        self.btn_undo.setEnabled(False)
        self._current_plan_seq = 0
        # 打开或切换到该目录的 Storage
        if self._storage is not None:
            try:
                self._storage.close()
            except Exception:
                pass
            self._storage = None
        try:
            self._storage = Storage(root)
        except OSError as e:
            self._storage = None
            QMessageBox.critical(
                self, "归档盒",
                f"无法在该目录建立整理记录（.organizer）：\n{e}\n\n"
                "请确认该目录有写入权限，或换一个目录。\n"
                "（扫描与预览仍可使用，但「开始整理」会被禁用）",
            )
            return
        # 检查是否有未完成的批次
        self._check_pending_batch()

    def _check_pending_batch(self):
        """启动/切换目录时检查 .organizer/history.db 中的未完成批次。"""
        if self._storage is None:
            return

        # 优先处理「上次运行被中断」的批次：核对磁盘实际状态
        interrupted = interrupted_batch_id(self._storage)
        if interrupted is not None:
            reply = QMessageBox.question(
                self, "归档盒",
                f"检测到该目录的整理批次 #{interrupted} 上次运行被中断（未完成）。\n\n"
                "现在核对磁盘实际状态并恢复记录吗？\n"
                "（只核对与补记，不会覆盖、删除或再次移动文件）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                rr = recover_batch(self._storage, interrupted)
                self.statusBar().showMessage(rr.summary())
                msg = rr.summary()
                if rr.paused:
                    msg += (f"\n\n有 {rr.paused} 项状态不明确，已暂停，"
                            "请人工确认文件位置后再撤销。")
                QMessageBox.information(self, "归档盒", msg)
                self._update_button_states()
            return

        pending = self._storage.get_pending_batch()
        if pending is not None:
            self.btn_undo.setEnabled(True)
            reply = QMessageBox.question(
                self, "归档盒",
                f"检测到该目录存在一个未完成的整理批次（#{pending.batch_id}，"
                f"状态：{pending.status}，创建于 {pending.created_at}）。\n\n"
                f"可以选择继续扫描、或者先撤销该批次。\n\n"
                f"是否现在尝试撤销？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self.on_undo()

    def on_scan(self):
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return

        # 已有计划任务在跑时，先让它全部过期，避免旧结果落到新扫描上
        self._abandon_plan_generation()

        self.table.setRowCount(0)
        self._set_working(True, "扫描中……")

        # V1.1: 显式保护归档盒源码目录（防止程序移动自身文件）
        protect = [DEFAULT_PROTECT_APP_SRC]

        self._scan_worker = ScanWorker(root, protect_dirs=protect, parent=self)
        self._scan_worker.finished_ok.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(self._on_scan_worker_finished)
        self._scan_worker.start()

    def _on_scan_worker_finished(self):
        """扫描线程结束。只清理扫描态，不能覆盖仍在运行的计划生成态。"""
        self._set_working(False)

    def _on_scan_done(self, result: ScanResult):
        self.scan_result = result

        # 状态栏汇总（V1.1: 加入项目保护信息）
        msg_parts = [f"扫描完成：{result.summary}"]
        if result.skipped:
            reasons = {}
            for s in result.skipped:
                r = s.skipped_reason or "其他"
                reasons[r] = reasons.get(r, 0) + 1
            msg_parts.append("跳过：" + "，".join(f"{k} {v}" for k, v in reasons.items()))
        self.statusBar().showMessage(" | ".join(msg_parts))

        # V1.1: 项目保护 — 如果有保护根目录，弹窗提示
        if result.protected_roots:
            protect_lines = []
            for proot, reason in result.protected_roots.items():
                # 对默认保护的源码目录做友好提示
                label = "归档盒源码目录（默认保护）" \
                    if proot == DEFAULT_PROTECT_APP_SRC \
                    else os.path.basename(proot)
                protect_lines.append(f"  • {label} — {reason}")
            if result.protected_count > 0:
                protect_lines.append(
                    f"\n共 {result.protected_count} 个文件因项目保护被跳过，"
                    f"不会进入整理计划。"
                )
            QMessageBox.information(
                self, "归档盒",
                "以下目录被整体保护（不会被整理）：\n\n"
                + "\n".join(protect_lines)
            )

        self._generate_and_show_plan()

        if result.permission_errors:
            QMessageBox.information(
                self, "归档盒",
                f"扫描完成，但有 {result.permission_errors} 个文件/目录无读取权限，已跳过。"
            )
        if result.errors:
            QMessageBox.warning(
                self, "归档盒",
                f"扫描遇到 {len(result.errors)} 个问题：\n\n"
                + "\n".join(result.errors[:10])
                + ("\n…" if len(result.errors) > 10 else "")
            )

    def _on_scan_failed(self, err: str):
        self.statusBar().showMessage("扫描失败")
        QMessageBox.critical(self, "归档盒", f"扫描时发生异常：\n{err}")

    # ============== 模式 & 规则变更 ==============

    def on_edit_rules(self):
        dlg = RuleEditDialog(rules_to_text(self.rules), self)
        if dlg.exec() == QDialog.Accepted:
            new_rules = parse_rules_text(dlg.get_text())
            if new_rules:
                self.rules = new_rules
            self.on_plan_param_changed()

    def on_plan_param_changed(self, *_):
        if self.current_plan is not None:
            self._plan_valid = False
            self.invalid_label.setText("⚠ 计划已失效（规则/模式/目录已更改）")
            self.invalid_label.show()
            self.statusBar().showMessage("计划参数已更改，请点击「重新预览」生成新计划。")
        self._update_button_states()

    def on_regenerate_plan(self):
        self._generate_and_show_plan()

    # ============== 生成 & 展示计划（V1.1: 后台线程） ==============

    def _generate_and_show_plan(self):
        """启动后台计划生成线程。"""
        if self._closing or self.scan_result is None:
            return

        # 上一个计划任务仍在跑时，请求它停止；它的所有信号都会因 seq 过期被丢弃
        old = getattr(self, "_plan_worker", None)
        if old is not None and old.isRunning():
            old.request_cancel()

        # 递增 seq —— 只有携带这个 seq 的信号才会被界面接受
        self._plan_seq_counter += 1
        seq = self._plan_seq_counter
        self._current_plan_seq = seq
        self._plan_cancel_requested = False

        mode = self.mode_combo.currentData()
        self.mode = mode

        # 新计划开始后，旧计划立即失效，取消/失败后都不允许执行旧计划
        self._plan_valid = False
        if self.current_plan is not None:
            self.invalid_label.setText("⚠ 正在生成新计划，旧计划暂不可执行")
            self.invalid_label.show()

        self._plan_working = True
        self._refresh_busy_ui(
            f"生成计划中（计算 SHA-256 指纹，共 {len(self.scan_result.files)} 个文件）…"
        )
        self._update_button_states()

        worker = PlanWorker(seq, self.scan_result, mode, self.rules, parent=self)
        self._plan_worker = worker
        self._plan_workers.append(worker)

        worker.progress.connect(self._on_plan_progress)
        worker.finished_ok.connect(self._on_plan_done)
        worker.cancelled.connect(self._on_plan_cancelled)
        worker.failed.connect(self._on_plan_failed)
        worker.finished_seq.connect(self._on_plan_worker_finished)
        # 线程真正结束后再释放引用（此时线程已不在运行）
        worker.finished.connect(lambda w=worker: self._forget_plan_worker(w))
        worker.start()

    def _forget_plan_worker(self, worker):
        try:
            self._plan_workers.remove(worker)
        except ValueError:
            pass

    def _on_plan_progress(self, seq: int, cur: int, total: int, fname: str):
        if seq != self._current_plan_seq:
            return  # 过期进度，忽略
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(cur)
        self.statusBar().showMessage(f"生成计划中… [{cur}/{total}] {fname}")

    def _on_plan_done(self, seq: int, plan: OrganizePlan):
        if self._closing:
            return  # 窗口已进入关闭流程，不再创建新的数据库连接
        if seq != self._current_plan_seq:
            return  # 旧任务结果，不能覆盖新任务状态

        # 取消请求发出后任务仍完成了：按取消处理，绝不发布这份计划
        if self._plan_cancel_requested:
            self._discard_plan("计划生成已取消，请点击「重新预览」重新生成")
            return

        self.current_plan = plan
        self._plan_valid = True
        self.invalid_label.hide()

        # Storage 兜底（如果之前因权限问题没创建）
        if self._storage is None:
            try:
                self._storage = Storage(plan.root)
            except OSError as e:
                self._populate_table(plan)
                self._update_button_states()
                self.statusBar().showMessage("预览可用，但无法写入整理记录，已禁用「开始整理」")
                QMessageBox.critical(
                    self, "归档盒",
                    f"无法在该目录建立整理记录（.organizer）：\n{e}",
                )
                return

        self._populate_table(plan)
        self._update_button_states()
        self.statusBar().showMessage(
            f"✓ {plan.summary()}（仍可更改，点击「开始整理」才会移动文件）"
        )

    def _on_plan_cancelled(self, seq: int):
        if seq != self._current_plan_seq:
            return  # 旧任务的取消信号，与当前任务无关
        self._discard_plan("计划生成已取消，请点击「重新预览」重新生成")

    def _on_plan_failed(self, seq: int, err: str):
        if seq != self._current_plan_seq:
            return  # 旧任务的失败信号，不能弹窗污染当前任务
        self._discard_plan("计划生成失败，请点击「重新预览」重新生成")
        QMessageBox.critical(self, "归档盒", f"生成计划失败：\n{err}")

    def _discard_plan(self, message: str):
        """取消/失败后清空计划与预览，保证没有任何可执行计划残留。"""
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self.invalid_label.setText(f"⚠ {message}")
        self.invalid_label.show()
        self.statusBar().showMessage(message)
        self._update_button_states()

    def _on_plan_worker_finished(self, seq: int):
        if seq != self._current_plan_seq:
            return  # 旧任务结束，不能重置当前任务的忙碌状态
        self._plan_working = False
        self._plan_cancel_requested = False
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self._refresh_busy_ui()
        self._update_button_states()

    def _cancel_plan_generation(self):
        """用户点击取消按钮，请求取消计划生成。

        不递增 seq —— 否则本任务的取消/结束信号会被误判为过期，
        旧计划将残留为「有效」。改用 _plan_cancel_requested 标记，
        即使任务在取消请求后仍完成，结果也会被丢弃。
        """
        worker = getattr(self, "_plan_worker", None)
        if worker is None or not worker.isRunning():
            return
        self._plan_cancel_requested = True
        worker.request_cancel()
        self.btn_cancel.setEnabled(False)
        self.statusBar().showMessage("正在取消计划生成，当前文件计算完成后停止……")

    def _abandon_plan_generation(self):
        """让当前计划任务的所有信号失效（用于切换目录/重新扫描）。"""
        worker = getattr(self, "_plan_worker", None)
        if worker is not None and worker.isRunning():
            worker.request_cancel()
        # 递增 seq：旧任务的进度/成功/失败/取消/结束信号全部过期
        self._current_plan_seq += 1
        self._plan_working = False
        self._plan_cancel_requested = False
        self.progress.setRange(0, 0)
        self.progress.setValue(0)

    def _populate_table(self, plan: OrganizePlan):
        self._suppress_cell_signals = True
        self.table.setRowCount(len(plan.items))
        for row, item in enumerate(plan.items):
            cb = QCheckBox()
            cb.setChecked(item.selected)
            cb.stateChanged.connect(
                lambda state, r=row: self._on_check_changed(r, state))
            cell_widget = QWidget()
            cl = QHBoxLayout(cell_widget)
            cl.addWidget(cb)
            cl.setAlignment(Qt.AlignCenter)
            cl.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, cell_widget)

            orig = QTableWidgetItem(item.file.rel_path)
            orig.setToolTip(f"源文件：{item.file.abs_path}")
            self.table.setItem(row, 1, orig)

            tgt = QTableWidgetItem(item.target_rel_path)
            if item.conflict_with_existing:
                tgt.setForeground(Qt.red)
                tgt.setToolTip("⚠ 目标磁盘上已存在同名文件，计划已分配唯一名称")
            self.table.setItem(row, 2, tgt)

            reason = QTableWidgetItem(item.rule_applied)
            self.table.setItem(row, 3, reason)

            if item.conflict_with_existing:
                status_text = "目标已存在"
            else:
                status_text = "正常"
            st = QTableWidgetItem(status_text)
            if item.conflict_with_existing:
                st.setForeground(Qt.red)
            self.table.setItem(row, 4, st)

            size_item = QTableWidgetItem(item.file.size_display)
            size_item.setData(Qt.UserRole, item.file.size)
            self.table.setItem(row, 5, size_item)
        self._suppress_cell_signals = False

    def _on_check_changed(self, row: int, state: int):
        if self.current_plan is None or row >= len(self.current_plan.items):
            return
        self.current_plan.items[row].selected = (state == Qt.Checked)
        self._update_button_states()

    def _update_button_states(self):
        plan = self.current_plan
        has_plan = plan is not None and self._plan_valid
        if has_plan:
            sel = sum(1 for i in plan.items if i.selected)
            self.btn_execute.setEnabled(
                sel > 0 and not self._working and not self._plan_working
                and self._storage is not None
            )
        else:
            self.btn_execute.setEnabled(False)
        # 撤销按钮
        self.btn_undo.setEnabled(
            self._storage is not None
            and undo_is_available(self._storage)
            and not self._working and not self._plan_working
        )
        # 重新预览：只要已有扫描结果且不忙就可用（取消/失败后可重新生成）
        self.btn_regenerate.setEnabled(
            self.scan_result is not None
            and not self._plan_working and not self._working
        )
        # 计划参数按钮
        self.btn_edit_rules.setEnabled(not self._working and not self._plan_working)
        self.mode_combo.setEnabled(not self._working and not self._plan_working)

    # ============== 执行 & 撤销 ==============

    def _refresh_busy_ui(self, msg: str = ""):
        """根据 _working / _plan_working 统一刷新进度条、取消按钮、浏览/扫描。

        扫描线程结束与计划线程运行是两件事：扫描结束时只能清掉扫描态，
        不能把仍在运行的计划生成态、取消按钮一起清掉。
        """
        busy = self._working or self._plan_working
        self.btn_browse.setEnabled(not busy)
        self.btn_scan.setEnabled(bool(not busy and self.path_edit.text()))

        exec_worker = getattr(self, "_exec_worker", None)
        exec_cancelable = (
            self._working and not self._plan_working
            and exec_worker is not None and exec_worker.isRunning()
        )
        show_cancel = self._plan_working or exec_cancelable

        if busy:
            self.progress.show()
            if show_cancel:
                self.btn_cancel.show()
                if self._plan_working:
                    self.btn_cancel.setText("✕ 取消生成")
                    self.btn_cancel.setEnabled(not self._plan_cancel_requested)
                else:
                    self.btn_cancel.setText("✕ 取消整理")
                    self.btn_cancel.setEnabled(True)
            else:
                # 扫描 / 撤销等不可协作取消的阶段，不展示无效的取消按钮
                self.btn_cancel.hide()
                self.btn_cancel.setEnabled(False)
            if msg:
                self.statusBar().showMessage(msg)
        else:
            self.progress.hide()
            self.progress.setRange(0, 0)
            self.progress.setValue(0)
            self.btn_cancel.hide()
            self.btn_cancel.setEnabled(False)

    def _set_working(self, working: bool, msg: str = ""):
        self._working = working
        if working:
            self.progress.setRange(0, 0)
        self._refresh_busy_ui(msg if working else "")
        self._update_button_states()

    def on_execute(self):
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            QMessageBox.warning(self, "归档盒", "请先生成有效的整理计划。")
            return

        selected_items = [i for i in plan.items if i.selected]
        if not selected_items:
            QMessageBox.warning(self, "归档盒", "没有勾选任何文件。")
            return

        if self._storage is None:
            QMessageBox.warning(
                self, "归档盒",
                "当前目录无法写入整理记录（.organizer），已禁用整理。\n"
                "请确认目录可写，或换一个目录。"
            )
            return

        # 前置条件：存在被中断的批次时先处理它，避免产生重叠批次
        blocked = interrupted_batch_id(self._storage) if self._storage else None
        if blocked is not None:
            QMessageBox.warning(
                self, "归档盒",
                f"批次 #{blocked} 上次运行被中断，尚未核对恢复。\n\n"
                "请先重新选择该目录并确认「核对恢复」，或撤销该批次后再开始新的整理。"
            )
            return

        # 执行前确认
        dlg = ExecuteConfirmDialog(plan.root, plan, self)
        if dlg.exec() != QDialog.Accepted:
            return

        if self._storage is None:
            self._storage = Storage(plan.root)

        self._set_working(True, f"整理中（{len(selected_items)} 项）……")

        self._exec_worker = ExecuteWorker(plan, self._storage, parent=self)
        self._exec_worker.finished_ok.connect(self._on_execute_done)
        self._exec_worker.failed.connect(self._on_execute_failed)
        self._exec_worker.finished.connect(lambda: self._set_working(False))
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.show()
        self._exec_worker.start()

    def on_cancel_execute(self):
        """取消按钮：根据当前状态取消计划生成或执行。"""
        # 优先取消计划生成（如果正在跑）
        plan_worker = getattr(self, "_plan_worker", None)
        if plan_worker is not None and plan_worker.isRunning():
            self._cancel_plan_generation()
            return
        # 否则取消执行
        worker = getattr(self, "_exec_worker", None)
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage("正在取消：将在当前文件操作完成后停止……")

    def _on_execute_done(self, result):
        # 刷新按钮状态（撤销现在应该可用）
        self._update_button_states()

        if result.batch_id == 0 and result.errors:
            self.statusBar().showMessage("未开始整理")
            QMessageBox.warning(self, "归档盒", "\n".join(result.errors))
            return

        summary = result.summary()
        parts = [summary]
        if result.moved > 0:
            parts.append(f"批次 #{result.batch_id} 已记录，可撤销")
        if result.skipped > 0 or result.failed > 0:
            parts.append(f"数据库：{os.path.join(self._storage.db_dir, 'history.db')}")
        self.statusBar().showMessage(" | ".join(parts))

        # 弹窗报告
        headline = "整理已取消" if result.was_cancelled else "整理完成！"
        msg_lines = [f"{headline}\n\n{summary}\n"]
        if result.skipped:
            msg_lines.append(f"跳过 {result.skipped} 项（源已消失/已修改/目标被占用）")
        if result.failed:
            msg_lines.append(f"失败 {result.failed} 项（移动错误）")
        msg_lines.append(f"\n批次 #{result.batch_id} 已保存至 .organizer/history.db")
        msg_lines.append(f"关闭程序后仍可重新打开撤销。")
        if result.errors:
            msg_lines.append(f"\n\n错误详情：\n" + "\n".join(result.errors[:20]))

        if result.failed > 0:
            QMessageBox.warning(self, "归档盒", "\n".join(msg_lines))
        else:
            QMessageBox.information(self, "归档盒", "\n".join(msg_lines))

    def _on_execute_failed(self, err: str):
        self.statusBar().showMessage("整理失败")
        QMessageBox.critical(self, "归档盒", f"执行整理时发生异常：\n{err}")

    def on_undo(self):
        if self._storage is None:
            QMessageBox.warning(self, "归档盒", "没有可撤销的批次。")
            return

        # 重复撤销防呆
        if not undo_is_available(self._storage):
            QMessageBox.information(self, "归档盒", "没有可撤销的批次（已经是最新状态）。")
            return

        # 确认
        reply = QMessageBox.question(
            self, "归档盒",
            "将撤销最近一个尚未完全撤销的整理批次。\n\n"
            "⚠ 注意：\n"
            "• 如果某些目标文件后来被你修改过，撤销时会保留你的修改\n"
            "• 如果原路径现在有新文件，撤销不会覆盖新文件\n"
            "• 只会恢复本批次实际移动成功的文件\n\n"
            "确定要撤销吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._set_working(True, "撤销中……")
        self._undo_worker = UndoWorker(self._storage, parent=self)
        self._undo_worker.finished_ok.connect(self._on_undo_done)
        self._undo_worker.failed.connect(self._on_undo_failed)
        self._undo_worker.finished.connect(lambda: self._set_working(False))
        self._undo_worker.start()

    def _on_undo_done(self, result):
        self._update_button_states()

        self.statusBar().showMessage(
            f"撤销批次 #{result.batch_id}：{result.summary()}"
        )

        msg = f"撤销批次 #{result.batch_id}\n\n{result.summary()}"
        if result.errors_list:
            msg += "\n\n冲突/错误详情：\n" + "\n".join(result.errors_list[:20])

        if result.is_complete:
            QMessageBox.information(self, "归档盒", msg + "\n\n✓ 完整撤销成功！")
        elif result.restored > 0:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 部分撤销完成，有文件因冲突未恢复。")
        else:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 没有任何文件被恢复，可能已全部撤销过。")

    def _on_undo_failed(self, err: str):
        self.statusBar().showMessage("撤销失败")
        QMessageBox.critical(self, "归档盒", f"撤销时发生异常：\n{err}")

    # ============== 关闭时清理 ==============

    def _background_workers(self):
        """收集所有可能正在运行的后台线程（含被新任务顶替但尚未退出的计划线程）。"""
        candidates = [
            getattr(self, "_scan_worker", None),
            getattr(self, "_plan_worker", None),
            getattr(self, "_exec_worker", None),
            getattr(self, "_undo_worker", None),
        ]
        candidates.extend(getattr(self, "_plan_workers", []) or [])
        out, seen = [], set()
        for w in candidates:
            if w is None or id(w) in seen:
                continue
            seen.add(id(w))
            out.append(w)
        return out

    def closeEvent(self, event):
        # 请求所有支持协作式取消的后台任务停止
        workers = self._background_workers()
        for w in workers:
            if w.isRunning() and hasattr(w, "request_cancel"):
                try:
                    w.request_cancel()
                except Exception:
                    pass

        # 只要还有线程在跑，就不关闭窗口、不关闭数据库，等线程真正结束
        running = [w for w in workers if w.isRunning()]
        if running:
            event.ignore()
            if not self._close_pending:
                self._close_pending = True
                self.statusBar().showMessage(
                    "后台任务正在结束，窗口将在任务完成且记录安全关闭后自动关闭……"
                )
                QTimer.singleShot(200, self._retry_close)
            return

        # 所有线程已结束，此时关闭数据库才安全
        self._closing = True
        if self._storage is not None:
            try:
                self._storage.close()
            finally:
                self._storage = None
        self._close_pending = False
        event.accept()

    def _retry_close(self):
        """等待后台线程全部结束后再真正关闭窗口。"""
        if not self._close_pending:
            return
        running = [w for w in self._background_workers() if w.isRunning()]
        if running:
            QTimer.singleShot(200, self._retry_close)
            return
        self._close_pending = False
        self.close()


# ======================== 入口 ========================

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMainWindow { background: #FAFAFA; }
        QTableWidget {
            background: white;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            gridline-color: #EEEEEE;
        }
        QHeaderView::section {
            background: #F5F5F5;
            padding: 6px 8px;
            border: none;
            border-bottom: 1px solid #E0E0E0;
            font-weight: bold;
            color: #37474F;
        }
        QPushButton { padding: 6px 14px; }
        QLineEdit, QComboBox {
            padding: 6px 10px;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            background: white;
        }
        QLineEdit:focus, QComboBox:focus { border-color: #1E88E5; }
        QStatusBar { background: #F5F5F5; color: #616161; }
        QCheckBox { padding: 4px; }
    """)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
