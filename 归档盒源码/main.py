"""
归档盒 V1.2 — PySide6 界面入口

职责：目录选择 → 扫描 → 分类预览 → 确认 → 实际移动 → 持久化 → 撤销
V1.2 第一轮改动：
- 统一任务状态来源（TaskContext）：扫描/计划/执行/撤销/恢复共用一份
  任务上下文，按钮、进度条、取消按钮从同一状态刷新
- 完整操作历史页面：批次列表（真实完成状态与各状态计数）+
  逐项明细（搜索/状态筛选/分页/系统定位）+ 导出 CSV/JSON
- 历史闭环：允许的撤销、中断批次核对恢复入口，全部来自持久化记录
V1.1 改动：
- 扫描时自动识别项目根目录并整体保护（不逐文件整理内部内容）
- 计划生成 + SHA-256 计算移入后台线程（PlanWorker）
- 过期结果丢弃（seq 计数器），取消后不发布半成品计划
"""

from __future__ import annotations

import atexit
import copy
import csv
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QCoreApplication, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTableWidget, QTableWidgetItem,
    QLabel, QHeaderView, QFileDialog, QStatusBar, QProgressBar,
    QMessageBox, QAbstractItemView, QComboBox, QTextEdit,
    QDialog, QDialogButtonBox, QCheckBox, QListWidget,
)

from scanner import scan_directory, ScanResult
from planner import (
    generate_plan, OrganizePlan, CourseRule, MODE_BY_TYPE,
    MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER,
    MODE_LABELS, ALL_MODES,
    DEFAULT_COURSE_RULES, parse_rules_text, rules_to_text,
)
from classification_config import (
    ClassificationConfig, ClassificationConfigError, config_summary,
    default_config, load_config, save_config,
)
from config_dialog import ClassificationConfigDialog
from protect_config import (
    KIND_LABELS, ProtectConfigError, dedupe_manual_paths,
    load_manual_protect, manual_scan_specs, normalize_manual_path,
    save_manual_protect,
)
from storage import Storage
from config_transfer import create_backup, restore_latest_backup
from intake_model import IntakeList
import recent_spaces
from executor import (
    execute_plan, undo_latest_batch, undo_is_available,
    recover_batch, interrupted_batch_id,
)

# 归档盒自身源码目录 —— 默认保护，防止程序移动自己
DEFAULT_PROTECT_APP_SRC = os.path.dirname(os.path.abspath(__file__))


# ======================== 统一任务状态（V1.2） ========================

TASK_KIND_LABELS = {
    "scan": "扫描",
    "plan": "生成计划",
    "execute": "整理",
    "undo": "撤销",
    "recover": "核对恢复",
    "duplicates": "核对重复文件",
}

BATCH_STATUS_LABELS = {
    "planning": "计划中",
    "executing": "执行中",
    "done": "已完成",
    "partial": "部分完成",
    "cancelled": "已取消",
    "cancelled_clean": "已取消（未移动）",
    "recovered_partial": "恢复后部分完成",
    "undone": "已撤销",
    "partial_undone": "部分撤销",
    "partial_failed_undone": "部分撤销（有失败）",
}

# 这些批次状态表示存在需要核对或人工处理的情况
BATCH_NEEDS_ATTENTION = {
    "partial", "recovered_partial", "partial_undone",
    "partial_failed_undone", "executing", "planning",
}

ACTION_STATUS_LABELS = {
    "planned": "未执行",
    "moved": "已移动",
    "failed": "失败",
    "skipped": "已跳过",
    "cancelled": "已取消",
    "undone": "已撤销",
    "conflict": "冲突",
    "paused": "暂停（待人工确认）",
}


class TaskContext:
    """统一的任务状态来源（V1.2）。

    所有后台任务（扫描/计划生成/执行/撤销/核对恢复）在这里登记：
    - kind / root：当前任务类型与所属根目录（切换根目录后，
      旧任务的结果按 seq 与 root 双重校验，不会污染新目录的界面）；
    - working / plan_working：进行中标志（兼容原有两个布尔位）；
    - cancel_requested：用户已请求取消（覆盖“线程已结束、结果
      尚未处理”的边界——取消按界面相位生效，不依赖 isRunning）；
    - progress / phase / current_file：进度与阶段说明；
    - cancelable：该阶段是否支持协作式取消。

    界面的按钮启用、进度条与取消按钮都从这一份状态刷新。
    """

    def __init__(self):
        self.kind: str | None = None
        self.root: str | None = None
        self.working = False          # 扫描/执行/撤销/恢复 进行中
        self.plan_working = False     # 计划生成进行中
        self.cancel_requested = False
        self.cancelable = False
        self.phase = ""
        self.progress_cur = 0
        self.progress_total = 0
        self.current_file = ""

    # ---- 生命周期 ----

    def begin(self, kind: str, root: str, phase: str = "",
              cancelable: bool = False, total: int = 0):
        """任务开始：登记类型/根目录/阶段说明。"""
        self.kind = kind
        self.root = root
        self.phase = phase
        self.cancelable = cancelable
        self.cancel_requested = False
        self.progress_cur = 0
        self.progress_total = total
        self.current_file = ""
        if kind == "plan":
            self.plan_working = True
        else:
            self.working = True

    def end(self, kind: str | None = None):
        """任务结束。

        kind 用于区分“哪个任务结束了”：扫描结束时若计划生成已接着
        开始，只清扫描的忙碌位，不动正在运行的计划任务状态。
        """
        if kind == "plan":
            self.plan_working = False
        elif kind is not None:
            self.working = False
        else:
            self.working = False
            self.plan_working = False
        if kind is None or self.kind == kind:
            self.kind = None
            self.cancelable = False
            self.cancel_requested = False
            self.phase = ""
            self.progress_cur = 0
            self.progress_total = 0
            self.current_file = ""

    def update_progress(self, cur: int, total: int, fname: str = ""):
        self.progress_cur = cur
        self.progress_total = total
        if fname:
            self.current_file = fname

    def request_cancel(self):
        self.cancel_requested = True

    # ---- 查询 ----

    @property
    def busy(self) -> bool:
        return self.working or self.plan_working

    def describe(self) -> str:
        """给用户看的当前任务说明（状态栏/测试断言用）。"""
        if not self.busy:
            return "空闲"
        label = TASK_KIND_LABELS.get(self.kind, self.kind or "任务")
        parts = [f"{label}中"]
        if self.phase:
            parts.append(self.phase)
        if self.progress_total > 0:
            parts.append(f"（{self.progress_cur}/{self.progress_total}）")
        elif self.current_file:
            parts.append(f"（{self.current_file}）")
        text = " · ".join(p for p in parts if p)
        if self.cancel_requested:
            text += "［取消已请求］"
        return text


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
                 rules, config=None, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.scan_result = scan_result
        self.mode = mode
        self.rules = rules
        # V1.3：推荐模式的配置快照（深拷贝，后台线程不引用可编辑草稿）
        self.config = config
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
                config=self.config,
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
    """后台执行线程（实际移动文件）。支持在当前文件操作完成后取消。

    注意：SQLite 连接默认只能在创建它的线程中使用。后台线程必须使用
    自己新建的 Storage 连接，不能复用主线程的 self._storage，否则会抛出
    “SQLite objects created in a thread can only be used in that same thread”。
    """
    progress = Signal(int, int, str)     # current, total, current_filename
    finished_ok = Signal(object)   # ExecuteResult
    failed = Signal(str)

    def __init__(self, plan: OrganizePlan, root: str, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.root = root
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        storage = None
        try:
            storage = Storage(self.root)
            result = execute_plan(
                self.plan, storage,
                progress_cb=lambda cur, total, fname:
                    self.progress.emit(cur, total, fname),
                cancel_event=self.cancel_event,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if storage is not None:
                try:
                    storage.close()
                except Exception:
                    pass


class UndoWorker(QThread):
    """后台撤销线程（同样使用线程内独立连接）。"""
    finished_ok = Signal(object)   # UndoResult
    failed = Signal(str)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self):
        storage = None
        try:
            storage = Storage(self.root)
            result = undo_latest_batch(storage)
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if storage is not None:
                try:
                    storage.close()
                except Exception:
                    pass


class RecoverWorker(QThread):
    """后台核对恢复线程（V1.2 第二轮）。

    语义与原主线程同步实现完全一致：复用唯一的 recover_batch
    核心逻辑，只核对磁盘实际状态、补记已确认移动的项、把状态
    不明确的项暂停待人工确认——不自动重放计划、不把文件搬回。

    线程内自建自关 Storage（SQLite 连接不能跨线程复用）。
    所有信号都携带任务标识 seq 与根目录 root，界面回调按
    “seq + root（规范化比较）”双重校验来源，迟到结果或来自其他
    根目录的结果都不会污染当前界面。
    """
    progress = Signal(int, str, int, int, str)   # seq, root, checked, total, path
    finished_ok = Signal(int, str, object)       # seq, root, RecoverResult
    failed = Signal(int, str, str)               # seq, root, error

    def __init__(self, seq: int, root: str, batch_id: int, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.root = root
        self.batch_id = batch_id

    def run(self):
        storage = None
        try:
            storage = Storage(self.root)
            result = recover_batch(
                storage, self.batch_id,
                progress_cb=lambda cur, total, path:
                    self.progress.emit(self.seq, self.root, cur, total, path),
            )
            self.finished_ok.emit(self.seq, self.root, result)
        except Exception as e:
            self.failed.emit(self.seq, self.root, str(e))
        finally:
            if storage is not None:
                try:
                    storage.close()
                except Exception:
                    pass


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

    def __init__(self, root: str, plan: OrganizePlan, parent=None,
                 hidden_selected: int = 0, scope_note: str = ""):
        super().__init__(parent)
        self.setWindowTitle("确认整理计划")
        self.setMinimumWidth(480)
        self.resize(520, 340)

        layout = QVBoxLayout(self)

        # 只做纯 Python 计数，不 import plan_patch：
        # 该模块在导入时会增加主线程 GC 压力，而本对话框可能在预览线程
        # 尚未完全退出时被打开（见 plan_patch 延迟导入说明）。
        n_override = sum(1 for i in plan.items if i.is_manual_overridden)
        extra_lines = ""
        if n_override > 0:
            extra_lines = (
                f"<br><b>手动修正：</b>{n_override} 个文件"
                "（仅本次计划的临时修正，不写回配置）"
            )
        scope_html = (f"<br><b>本次范围：</b>{scope_note}" if scope_note else "")
        # S3-01：根目录显示完整路径——wordWrap + 可选中，
        # 长路径换行而不是被对话框宽度裁掉。
        info_label = QLabel(
            f"<b>根目录：</b>{root}<br>"
            f"<b>分类模式：</b>{MODE_LABELS.get(plan.mode, plan.mode)}<br>"
            f"<b>已选文件：</b>{plan.selected_count} / {plan.total}"
            f"{extra_lines}{scope_html}<br>"
            f"<b>目标目录：</b>{os.path.join(root, '_已整理')}/…<br>"
        )
        info_label.setWordWrap(True)
        info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(info_label)

        if plan.conflict_count:
            warn = QLabel(f"<span style='color:#E65100;'>⚠ 注意：有 {plan.conflict_count} 个目标路径磁盘上已存在同名文件，计划已自动分配唯一名称。</span>")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        if hidden_selected > 0:
            hidden_warn = QLabel(
                f"<span style='color:#E65100;'>⚠ 当前筛选隐藏了 {hidden_selected} 个"
                "已勾选文件；确认整理会按全部勾选项执行，而不只是当前可见行。</span>")
            hidden_warn.setWordWrap(True)
            layout.addWidget(hidden_warn)

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
        ok_btn = btns.button(QDialogButtonBox.Ok)
        ok_btn.setText("确认开始整理")
        # S3-01：确认按钮用危险色，且不成为默认按钮（回车不会触发移动）
        ok_btn.setStyleSheet(
            "QPushButton{background:#C62828;color:#FFFFFF;font-weight:600;"
            "padding:4px 14px;border:1px solid #8E0000;border-radius:4px;}"
            "QPushButton:hover{background:#D32F2F;}"
            "QPushButton:pressed{background:#8E0000;}"
            "QPushButton:disabled{background:#E0A0A0;color:#FFFFFF;}"
        )
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(False)
        cancel_btn = btns.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        # 默认按钮与初始焦点都放在「取消」上
        cancel_btn.setAutoDefault(True)
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()
        self._cancel_btn = cancel_btn
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def showEvent(self, event):
        # S3-01：对话框显示时 Qt 会把焦点给第一个可聚焦控件（确认按钮），
        # 这里显式把初始焦点拉回「取消」，保证回车不会触发移动。
        super().showEvent(event)
        self._cancel_btn.setFocus(Qt.OtherFocusReason)


# ======================== 保护目录对话框 ========================

class ProtectDirDialog(QDialog):
    """管理手动保护目录。

    默认保护（归档盒源码）与自动识别的项目根只读展示，不能在这里移除；
    手动保护目录可以添加/移除，确定后由主窗口持久化并按根目录生效。
    """

    def __init__(self, root: str, manual_paths, readonly_entries,
                 skipped_count: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("保护目录 / 不拆分")
        self.setMinimumWidth(560)
        self.resize(640, 460)
        self.root = os.path.abspath(root)
        self._manual = list(manual_paths)
        self._readonly_entries = list(readonly_entries)

        layout = QVBoxLayout(self)
        tip = QLabel(
            "被保护的目录及其全部子目录不会进入整理计划，文件留在原位置。\n"
            "默认保护与自动识别项目由程序维护；手动保护只接受当前根目录内的普通目录。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.readonly_view = QTextEdit()
        self.readonly_view.setReadOnly(True)
        self.readonly_view.setStyleSheet("color:#37474F;background:#F5F5F5;")
        layout.addWidget(self.readonly_view, stretch=1)

        layout.addWidget(QLabel(f"手动保护目录（{len(self._manual)} 个）："))
        self.manual_list = QListWidget()
        self.manual_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.manual_list, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_add_dir = QPushButton("添加目录…")
        self.btn_add_dir.clicked.connect(self._on_add)
        btn_row.addWidget(self.btn_add_dir)
        self.btn_remove_dir = QPushButton("移除选中")
        self.btn_remove_dir.clicked.connect(self._on_remove)
        btn_row.addWidget(self.btn_remove_dir)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("应用")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._skipped_count = skipped_count
        self._refresh()

    def _refresh(self):
        lines = []
        for path, kinds, reason in self._readonly_entries:
            tags = " / ".join(KIND_LABELS.get(k, k) for k in kinds.split(",") if k)
            lines.append(f"[{tags}] {path}\n    {reason}")
        if self._skipped_count:
            lines.append(f"\n本次扫描因保护跳过 {self._skipped_count} 个文件。")
        if not lines:
            lines.append("当前没有默认保护或自动识别项目。")
        self.readonly_view.setPlainText("\n".join(lines))
        self.manual_list.clear()
        for p in self._manual:
            rel = os.path.relpath(p, self.root)
            self.manual_list.addItem(f"{rel}    ({p})")

    def _on_add(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "选择要保护的目录", self.root, QFileDialog.ShowDirsOnly,
        )
        if not chosen:
            return
        try:
            ap = normalize_manual_path(self.root, chosen)
        except ProtectConfigError as e:
            QMessageBox.warning(self, "归档盒", str(e))
            return
        if any(os.path.normcase(p) == os.path.normcase(ap) for p in self._manual):
            QMessageBox.information(self, "归档盒", "该目录已在手动保护列表中。")
            return
        self._manual.append(ap)
        self._manual = dedupe_manual_paths(self.root, self._manual)
        self._refresh()

    def _on_remove(self):
        rows = sorted({self.manual_list.row(i) for i in self.manual_list.selectedItems()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "归档盒", "请先在列表中选择要移除的手动保护目录。")
            return
        for r in rows:
            if 0 <= r < len(self._manual):
                del self._manual[r]
        self._refresh()

    def get_paths(self) -> list[str]:
        return list(self._manual)


# ======================== 操作历史页面（V1.2） ========================

HISTORY_PAGE_SIZE = 200        # 逐项明细分页大小，避免一次渲染全部动作
_HISTORY_ITEM_COLUMNS = ["序号", "原位置（相对路径）", "目标位置（相对路径）",
                         "大小", "状态", "原因 / 说明", "分类来源"]
_HISTORY_BATCH_COLUMNS = ["批次", "时间", "分类模式", "状态",
                          "移动", "跳过", "失败", "取消", "已撤销", "注意"]


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _reveal_in_file_manager(path: str) -> str:
    """在系统文件管理器中定位文件。返回空串成功，否则返回错误说明。"""
    path = os.path.abspath(path)
    target = path if os.path.exists(path) else None
    if target is None:
        return "文件当前不存在（可能已被移动、撤销或删除）"
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target) or target])
        return ""
    except Exception as e:
        return f"无法调用系统文件管理器：{e}"


def _norm_root(p: str) -> str:
    """根目录的规范化形式（V1.2 第三轮）。

    用于任务回调的来源校验：统一 absolute + 符号链接解析后比较，
    不用字符串前缀判断、不做大小写变换（macOS 文件系统大小写
    不敏感，但统一小写比较会在大小写敏感卷上误判）。
    """
    try:
        return os.path.realpath(os.path.abspath(p))
    except Exception:
        return os.path.abspath(p)


def _recover_headline(rr) -> str:
    """核对恢复结果的用户可见标题（V1.2 第二轮文案区分）。"""
    if getattr(rr, "paused", 0):
        return "暂停待人工确认"
    if getattr(rr, "recovered_moved", 0) and getattr(rr, "still_planned", 0):
        return "核对完成（补记已移动，其余仍未执行）"
    if getattr(rr, "recovered_moved", 0):
        return "补记已移动"
    if getattr(rr, "still_planned", 0):
        return "仍未执行"
    return "核对完成"


class HistoryDialog(QDialog):
    """完整操作历史页面。

    数据来源：{root}/.organizer/history.db 的持久化实际记录
    （批次元数据 + 逐项动作），不是界面内存或计划数量推算。

    操作边界：
    - 撤销只对「最近一个尚未完全撤销的批次」开放（与主窗口
      「撤销整理」同一语义）；选中旧批次时按钮禁用并说明原因。
    - 中断批次提供「核对恢复」入口：先核对磁盘实际情况，
      不自动重放、不盲目继续移动。
    - 主窗口忙碌或关闭等待时，撤销/恢复/导出不可用，
      防止从历史入口启动重叠业务任务。
    - 刷新失败只在对话框内提示，不影响主窗口的文件操作状态。
    """

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.mw = main_window                      # MainWindow（提供 storage 与任务状态）
        self.root = main_window._current_root()
        self.setWindowTitle(f"操作历史 — {self.root}")
        self.resize(1080, 720)
        self._batches = []                         # BatchRecord（不含动作）
        self._counts = {}                          # {batch_id: {status: n}}
        self._source_counts = {}                   # {batch_id: {"手动"/"长期规则"/"自动": n}}
        self._selected_batch_id = None
        self._items_cache = []                     # 选中批次的全部动作
        self._page = 0

        self._build_ui()
        self._reload()

    # ---------------- 界面 ----------------

    def _build_ui(self):
        lay = QVBoxLayout(self)

        tip = QLabel("批次列表（按整理根目录保存，与计划数量无关，"
                     "全部来自实际持久化记录）")
        lay.addWidget(tip)

        self.batch_table = QTableWidget(0, len(_HISTORY_BATCH_COLUMNS))
        self.batch_table.setHorizontalHeaderLabels(_HISTORY_BATCH_COLUMNS)
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.batch_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.batch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.batch_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents)
        self.batch_table.itemSelectionChanged.connect(self._on_batch_selected)
        lay.addWidget(self.batch_table, 2)

        # V1.3：显示选中批次实际使用的分类配置快照（历史可解释）
        self.batch_config_label = QLabel("")
        self.batch_config_label.setWordWrap(True)
        self.batch_config_label.setStyleSheet(
            "color:#455A64;background:#FAFAFA;padding:2px 8px;"
            "border-radius:3px;font-size:12px;")
        self.batch_config_label.hide()
        lay.addWidget(self.batch_config_label)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        self.btn_undo = QPushButton("撤销该批次")
        self.btn_undo.clicked.connect(self._on_undo)
        self.btn_recover = QPushButton("核对恢复…")
        self.btn_recover.clicked.connect(self._on_recover)
        self.btn_export = QPushButton("导出…")
        self.btn_export.clicked.connect(self._on_export)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        self.undo_hint = QLabel("")
        self.undo_hint.setWordWrap(True)
        for w in (self.btn_refresh, self.btn_undo, self.btn_recover,
                  self.btn_export, self.undo_hint, self.btn_close):
            row.addWidget(w)
        lay.addLayout(row)

        sep = QLabel("逐项明细")
        lay.addWidget(sep)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索原位置 / 目标位置关键词……")
        self.search_edit.textChanged.connect(self._on_filter_changed)
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", "")
        for status, label in ACTION_STATUS_LABELS.items():
            self.status_filter.addItem(label, status)
        self.status_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.btn_reveal = QPushButton("在文件管理器中显示")
        self.btn_reveal.clicked.connect(self._on_reveal)
        filter_row.addWidget(self.search_edit, 3)
        filter_row.addWidget(QLabel("状态："))
        filter_row.addWidget(self.status_filter, 1)
        filter_row.addWidget(self.btn_reveal)
        lay.addLayout(filter_row)

        self.item_table = QTableWidget(0, len(_HISTORY_ITEM_COLUMNS))
        self.item_table.setHorizontalHeaderLabels(_HISTORY_ITEM_COLUMNS)
        self.item_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.item_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.item_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.item_table.verticalHeader().setVisible(False)
        self.item_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.item_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.item_table.itemSelectionChanged.connect(
            self._on_item_selection_changed)
        self.item_table.doubleClicked.connect(self._on_reveal)
        lay.addWidget(self.item_table, 3)

        page_row = QHBoxLayout()
        self.btn_prev = QPushButton("上一页")
        self.btn_prev.clicked.connect(lambda: self._turn_page(-1))
        self.btn_next = QPushButton("下一页")
        self.btn_next.clicked.connect(lambda: self._turn_page(1))
        self.page_label = QLabel("")
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        page_row.addWidget(self.btn_prev)
        page_row.addWidget(self.page_label)
        page_row.addWidget(self.btn_next)
        page_row.addStretch(1)
        page_row.addWidget(self.detail_label, 3)
        lay.addLayout(page_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color:#b00;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        lay.addWidget(self.error_label)

    # ---------------- 数据加载 ----------------

    def _storage(self):
        st = self.mw._storage
        if st is None:
            raise RuntimeError("数据库未打开（请先选择整理根目录并扫描）")
        return st

    def _show_error(self, msg: str):
        """历史读取失败：只在对话框内提示，清空展示，不影响主窗口。"""
        self.error_label.setText(f"历史读取失败：{msg}")
        self.error_label.show()
        self._batches = []
        self._counts = {}
        self._selected_batch_id = None
        self._items_cache = []
        self._page = 0
        self.batch_table.setRowCount(0)
        self.item_table.setRowCount(0)
        self.page_label.setText("")
        self._refresh_buttons()

    def refresh_all(self):
        """统一刷新入口（V1.2 第二轮）。

        无论批次列表、明细缓存还是选中状态，一律重读数据库实际记录。
        适用于：手动点击“刷新”、历史窗口重新打开、撤销/恢复/执行
        完成后的自动刷新——旧实现里 `_reload()` 只更新批次列表、
        `_on_batch_selected()` 遇到相同 batch_id 直接早退，导致
        “关闭历史 → 撤销 → 重开历史”时明细停留在过期状态。

        保留批次选择、搜索关键词、状态筛选与页码；页数减少时
        自动把页码校正到最后一页。批次已不存在或读取失败时，
        清空旧缓存与展示并刷新按钮状态。
        """
        # 批次列表读取失败时 _reload → _show_error 已清理展示
        self._reload()
        if self.error_label.isVisible():
            return
        # 选中批次已不存在时，_render_batches 会自动改选首行
        # （触发 _on_batch_selected 更新选中并加载明细）；
        # 这里对当前选中批次强制重读逐项动作。
        self.load_items(force=True)

    def _reload(self):
        """重新加载批次列表（不含逐项动作）。"""
        try:
            st = self._storage()
            self._batches = st.list_batches(include_actions=False)
            self._counts = st.action_counts_by_batch()
            # 批次列表不加载逐项动作，分类来源计数用聚合查询（V1.5）
            self._source_counts = st.source_counts_by_batch()
            self.error_label.hide()
        except Exception as e:
            self._show_error(e)
            return
        self._render_batches(keep_selection=True)

    def _render_batches(self, keep_selection=False):
        prev = self._selected_batch_id if keep_selection else None
        self.batch_table.setRowCount(len(self._batches))
        for r, b in enumerate(self._batches):
            c = self._counts.get(b.batch_id, {})
            moved = c.get("moved", 0)
            undone = c.get("undone", 0)
            attention = "⚠ 需核对" if (
                b.status in BATCH_NEEDS_ATTENTION
                or c.get("paused", 0) or c.get("conflict", 0)
            ) else ""
            sc = self._source_counts.get(b.batch_id, {})
            if sc.get("手动", 0) > 0:
                attention += " / ⚑ 手动修正"
            if sc.get("长期规则", 0) > 0:
                attention += f" / ⚑ 长期规则×{sc['长期规则']}"
            attention = attention.lstrip(" / ")
            # 时间列缩短显示（去掉年份，避免列宽不足被截断），
            # 悬浮提示仍显示完整时间
            t_full = b.created_at.replace("T", " ")
            t_short = t_full[5:] if len(t_full) >= 16 else t_full
            cells = [
                f"#{b.batch_id}", t_short,
                MODE_LABELS.get(b.mode, b.mode),
                BATCH_STATUS_LABELS.get(b.status, b.status),
                str(moved), str(c.get("skipped", 0)), str(c.get("failed", 0)),
                str(c.get("cancelled", 0)), str(undone), attention,
            ]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if col in (4, 5, 6, 7, 8):
                    it.setTextAlignment(Qt.AlignCenter)
                if col == 1:
                    it.setToolTip(t_full)   # 悬浮显示完整时间
                self.batch_table.setItem(r, col, it)
        # 选中行
        select_row = -1
        if prev is not None:
            for r, b in enumerate(self._batches):
                if b.batch_id == prev:
                    select_row = r
                    break
        if select_row < 0 and self._batches:
            select_row = 0
        if select_row >= 0:
            self.batch_table.selectRow(select_row)
        else:
            self._selected_batch_id = None
            self.item_table.setRowCount(0)
            self.page_label.setText("")
            self.batch_config_label.hide()
        self._refresh_buttons()   # 即使选中行未变（如撤销后），也要刷新操作按钮

    def _on_batch_selected(self):
        rows = self.batch_table.selectionModel().selectedRows()
        if not rows:
            self.batch_config_label.hide()
            return
        b = self._batches[rows[0].row()]
        summary = b.config_summary
        if summary:
            self.batch_config_label.setText(
                f"本批次实际使用的分类配置（快照，不受后来修改影响）：{summary}")
            self.batch_config_label.show()
        else:
            self.batch_config_label.hide()
        if b.batch_id == self._selected_batch_id:
            # V1.2 第二轮：批次未变也允许刷新入口强制重读明细，
            # 这里保持“切换批次才自动加载”的轻量行为；
            # 强制重读统一走 load_items(force=True)
            return
        self._selected_batch_id = b.batch_id
        self._page = 0
        self.load_items(force=True)

    def load_items(self, force: bool = False):
        """按需加载选中批次的逐项动作。

        force=True 时即使 batch_id 未变也重读数据库——
        统一刷新（refresh_all）与撤销/恢复完成后的刷新都依赖它，
        否则明细缓存会停留在操作前的旧状态。
        """
        if self._selected_batch_id is None:
            self._items_cache = []
            self._render_items()
            return
        try:
            st = self._storage()
            self._items_cache = st.get_actions(self._selected_batch_id)
            self.error_label.hide()
        except Exception as e:
            self._items_cache = []
            self._show_error(e)
            return
        self._render_items()

    def _filtered_items(self):
        text = self.search_edit.text().strip().lower()
        status = self.status_filter.currentData()
        out = []
        for a in self._items_cache:
            if status and a.status != status:
                continue
            if text and (text not in a.source_rel.lower()
                         and text not in a.target_rel.lower()):
                continue
            out.append(a)
        return out

    def _render_items(self):
        items = self._filtered_items()
        pages = max(1, (len(items) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        self._page = min(self._page, pages - 1)
        start = self._page * HISTORY_PAGE_SIZE
        chunk = items[start:start + HISTORY_PAGE_SIZE]
        self.item_table.setRowCount(len(chunk))
        root = self.root or ""
        for r, a in enumerate(chunk):
            reason = a.error or ""
            if a.status == "conflict":
                reason = (f"{a.error}（保留现状，请人工核对）" if a.error
                          else "撤销/恢复时目标位置已存在其他文件，保留现状，请人工核对")
            elif a.status == "paused":
                reason = (f"{a.error}（暂停待人工确认）" if a.error
                          else "中断恢复时状态不明确，暂停待人工确认")
            cls_source = a.classification_source
            cells = [
                str(a.action_index + 1), a.source_rel, a.target_rel,
                _fmt_size(a.source_size),
                ACTION_STATUS_LABELS.get(a.status, a.status), reason,
                cls_source,
            ]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if col in (0, 3, 4, 6):
                    it.setTextAlignment(Qt.AlignCenter)
                if col in (1, 2):
                    # 悬浮提示显示完整绝对路径，解决表格截断
                    abs_path = os.path.join(root, a.target_rel if col == 2
                                            else a.source_rel)
                    it.setToolTip(abs_path)
                if col == 5 and text:
                    # 原因列同理：悬浮显示完整原因（V1.2 第三轮小优化）
                    it.setToolTip(text)
                if col == 6:
                    tip = ""
                    if text == "手动":
                        # 手动修正：悬浮显示自动→手动对比
                        tip = "手动修正文件"
                        if a.auto_target_rel:
                            tip += f"\n自动目标：{a.auto_target_rel}"
                        if a.auto_rule_applied:
                            tip += f"\n自动依据：{a.auto_rule_applied}"
                        if a.override_note:
                            tip += f"\n用户说明：{a.override_note}"
                        it.setForeground(QColor("#C62828"))
                    elif text == "长期规则":
                        tip = "命中长期修正规则"
                        if a.rule_key:
                            tip += f"\n规则标识：{a.rule_key}"
                        if a.rule_applied:
                            tip += f"\n命中依据：{a.rule_applied}"
                        it.setForeground(QColor("#1565C0"))
                    elif a.rule_applied:
                        # 普通自动：把当时实际命中的依据也放进悬浮提示
                        tip = f"命中依据：{a.rule_applied}"
                        if a.rule_key:
                            tip += f"\n规则标识：{a.rule_key}"
                    if tip:
                        it.setToolTip(tip)
                self.item_table.setItem(r, col, it)
        self.page_label.setText(
            f"第 {self._page + 1}/{pages} 页 · 共 {len(items)} 项"
            if items else "无匹配项")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page < pages - 1)
        self._refresh_buttons()
        self._on_item_selection_changed()

    def _turn_page(self, delta: int):
        self._page = max(0, self._page + delta)
        self._render_items()

    def _on_filter_changed(self):
        self._page = 0
        self._render_items()

    def _on_item_selection_changed(self):
        rows = self.item_table.selectionModel().selectedRows()
        if not rows:
            self.detail_label.setText("")
            self.btn_reveal.setEnabled(False)
            return
        idx = rows[0].row()
        items = self._filtered_items()
        start = self._page * HISTORY_PAGE_SIZE
        a = items[start + idx] if start + idx < len(items) else None
        if a is None:
            self.detail_label.setText("")
            self.btn_reveal.setEnabled(False)
            return
        self.btn_reveal.setEnabled(True)
        src = os.path.join(self.root or "", a.source_rel)
        tgt = os.path.join(self.root or "", a.target_rel)
        where = "目标位置存在" if os.path.exists(tgt) else (
            "原位置存在" if os.path.exists(src) else "两处均不存在")
        self.detail_label.setText(
            f"原位置：{src}\n目标位置：{tgt}（当前：{where}）")

    # ---------------- 操作 ----------------

    def _pending_batch_id(self):
        try:
            b = self._storage().get_pending_batch()
            return b.batch_id if b else None
        except Exception:
            return None

    def _interrupted_batch_id(self):
        try:
            return interrupted_batch_id(self._storage())
        except Exception:
            return None

    def _refresh_buttons(self):
        """撤销/恢复/导出按钮：任务忙碌、关闭等待或条件不满足时禁用。

        V1.2 第三轮：忙碌/关闭等待走轻量路径——直接禁用全部操作按钮，
        不查询数据库（任务运行中主线程尽量少碰库），也不重载批次/明细。
        """
        blocked = self.mw.task.busy or self.mw._close_pending or self.mw._closing
        if blocked:
            self.btn_refresh.setEnabled(False)
            self.btn_undo.setEnabled(False)
            self.btn_recover.setEnabled(False)
            self.btn_export.setEnabled(False)
            self.undo_hint.setText("主窗口有任务进行中或正在关闭，"
                                   "暂不能从历史启动撤销/恢复。")
            return
        pending = self._pending_batch_id()
        interrupted = self._interrupted_batch_id()
        sel = self._selected_batch_id
        self.btn_refresh.setEnabled(True)
        self.btn_export.setEnabled(sel is not None)
        can_undo = (sel is not None and pending is not None
                    and sel == pending and undo_is_available(self._storage()))
        self.btn_undo.setEnabled(can_undo)
        if sel is not None and pending is not None and sel != pending:
            self.undo_hint.setText(
                f"只能撤销最近一个尚未完全撤销的批次（当前为 #{pending}）；"
                f"批次 #{sel} 不在该撤销顺序内。")
        elif sel is not None and sel == pending and not can_undo:
            self.undo_hint.setText("当前没有可撤销的批次。")
        else:
            self.undo_hint.setText(
                f"可撤销批次：#{pending}" if can_undo else "当前没有可撤销的批次。")
        self.btn_recover.setEnabled(
            sel is not None and interrupted is not None and sel == interrupted)

    def refresh_task_gating(self):
        """任务状态变化时的轻量联动入口（V1.2 第三轮）。

        只刷新操作按钮可用性——忙碌/关闭等待时直接禁用、空闲时按
        数据库实际条件恢复；不重载批次列表与逐项明细，可在任务
        开始/结束/关闭等待的每次状态变化时安全调用。
        """
        self._refresh_buttons()

    def _on_undo(self):
        """撤销入口：复用主窗口 on_undo 的全部守卫与语义。"""
        if not self.btn_undo.isEnabled():
            return
        self.mw.on_undo()
        # 撤销在后台线程运行，完成时主窗口会调用 refresh_after_change()

    def _on_recover(self):
        """核对恢复入口：先核对磁盘实际情况，不自动重放。"""
        if not self.btn_recover.isEnabled():
            return
        batch_id = self._selected_batch_id
        ret = QMessageBox.question(
            self, "归档盒",
            f"将核对批次 #{batch_id} 的磁盘实际状态：\n"
            "• 已实际移动但未记录的项 → 补记为「已移动」\n"
            "• 尚未移动的项 → 保持「未执行」\n"
            "• 状态不明确的项 → 暂停待人工确认\n\n"
            "核对过程不会移动、覆盖或删除任何文件；\n"
            "如需把已移动的文件放回原位置，请在核对后使用「撤销」。\n\n"
            "继续吗？")
        if ret != QMessageBox.Yes:
            return
        self.accept()          # 关闭历史窗口，回到主窗口执行恢复
        self.mw.recover_interrupted_batch(batch_id)

    def _on_reveal(self):
        rows = self.item_table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        items = self._filtered_items()
        start = self._page * HISTORY_PAGE_SIZE
        a = items[start + idx] if start + idx < len(items) else None
        if a is None:
            return
        # 优先定位仍存在的目标文件，其次原位置
        tgt = os.path.join(self.root or "", a.target_rel)
        src = os.path.join(self.root or "", a.source_rel)
        target = tgt if os.path.exists(tgt) else src
        err = _reveal_in_file_manager(target)
        if err:
            QMessageBox.warning(self, "归档盒", err)

    def _on_export(self):
        """导出选中批次的完整记录（CSV / JSON），保存位置由用户选择。"""
        if not self.btn_export.isEnabled() or self._selected_batch_id is None:
            return
        batch_id = self._selected_batch_id
        try:
            st = self._storage()
            batch = st.get_batch(batch_id)
        except Exception as e:
            QMessageBox.warning(self, "归档盒", f"读取批次失败：{e}")
            return
        if batch is None:
            QMessageBox.warning(self, "归档盒", "未找到该批次记录。")
            return
        suggested = f"归档盒_批次{batch_id}_操作历史.csv"
        path, sel_filter = QFileDialog.getSaveFileName(
            self, "导出操作历史", suggested,
            "CSV 文件 (*.csv);;JSON 文件 (*.json)")
        if not path:
            return
        use_json = "JSON" in (sel_filter or "") or path.lower().endswith(".json")
        try:
            if use_json:
                data = {
                    "batch": {
                        "batch_id": batch.batch_id, "root": batch.root,
                        "mode": batch.mode, "status": batch.status,
                        "created_at": batch.created_at,
                        "finished_at": batch.finished_at,
                        "summary": batch.summary,
                    },
                    "actions": [
                        {
                            "action_index": a.action_index,
                            "source_rel": a.source_rel,
                            "target_rel": a.target_rel,
                            "size": a.source_size,
                            "sha256": a.source_sha256,
                            "status": a.status, "error": a.error,
                            "executed_at": a.executed_at,
                            "undone_at": a.undone_at,
                            "classification_source": a.classification_source,
                            "auto_target_rel": a.auto_target_rel,
                            "auto_rule_applied": a.auto_rule_applied,
                            "override_note": a.override_note,
                            # V1.5：当时实际命中的规则标识与依据
                            "rule_key": a.rule_key,
                            "rule_applied": a.rule_applied,
                        }
                        for a in batch.actions
                    ],
                    "exported_at": __import__("time").strftime(
                        "%Y-%m-%d %H:%M:%S"),
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["批次", batch.batch_id, "状态", batch.status,
                                "时间", batch.created_at, "模式", batch.mode])
                    w.writerow(["序号", "原位置", "目标位置", "大小(字节)",
                                "SHA-256", "状态", "原因", "执行时间", "撤销时间",
                                "分类来源", "自动目标", "自动依据", "修正说明",
                                "规则标识", "命中依据"])
                    for a in batch.actions:
                        w.writerow([a.action_index + 1, a.source_rel,
                                    a.target_rel, a.source_size,
                                    a.source_sha256, a.status, a.error,
                                    a.executed_at, a.undone_at,
                                    a.classification_source,
                                    a.auto_target_rel, a.auto_rule_applied,
                                    a.override_note,
                                    a.rule_key, a.rule_applied])
        except OSError as e:
            QMessageBox.warning(self, "归档盒", f"导出失败：{e}")
            return
        QMessageBox.information(
            self, "归档盒",
            f"已导出批次 #{batch_id} 的 {len(batch.actions)} 条记录到：\n{path}")

    # ---------------- 主窗口回调 ----------------

    def refresh_after_change(self):
        """撤销/恢复/执行完成后由主窗口调用：统一刷新（列表+明细）。"""
        if not self.isVisible():
            return
        self.refresh_all()


# ======================== 主窗口 ========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("归档盒 V1.7 — 可配置、可预览、可撤销的课程文件整理器")
        self.resize(1150, 720)
        self._center_window()

        # V1.2: 统一任务状态来源（_working/_plan_working 是它的属性）
        self.task = TaskContext()

        self.scan_result: ScanResult | None = None
        self.current_plan: OrganizePlan | None = None
        self.rules: list[CourseRule] = list(DEFAULT_COURSE_RULES)
        self.mode: str = MODE_BY_COURSE_THEN_TYPE
        self._plan_valid: bool = False
        self._storage: Storage | None = None

        # V1.1: 过期计划结果丢弃计数器
        self._plan_seq_counter = 0
        self._current_plan_seq = 0
        self._plan_cancel_requested = False
        self._plan_cancel_seq: int | None = None    # 用户取消时对应的任务 seq（按相位判定）
        self._plan_workers: list[PlanWorker] = []   # 保留引用，避免运行中的线程被回收
        self._close_pending = False                 # 关闭请求等待后台线程结束
        self._closing = False                       # 已接受关闭，后续异步回调直接忽略
        self._scan_worker = None
        self._plan_worker = None
        self._exec_worker = None
        self._undo_worker = None
        # V1.2 第二轮：核对恢复后台线程 + 结果缓存（供测试与关闭流程读取）
        self._recover_worker = None
        self._recover_seq = 0
        self._recover_result = None

        # V1.1: 手动保护目录（按根目录持久化）
        self.manual_protect: list[str] = []

        # V1.3: 推荐模式分类配置（按根目录加载，切换目录不串用）
        self.classification_config: ClassificationConfig = default_config()
        self._config_source = "default"    # default / custom / error
        self._config_error = ""
        # 规则试运行后台线程（关闭流程需要等待它们结束）
        self._rule_preview_workers: list = []

        # V1.6：预览搜索/筛选/批量勾选与目标树
        self._visible_flags: list[bool] = []
        self._plan_identity: tuple | None = None
        self._tree_dialog = None
        self._tree_refresh_timer = QTimer(self)
        self._tree_refresh_timer.setSingleShot(True)
        self._tree_refresh_timer.timeout.connect(self._on_tree_refresh_timeout)

        # V1.7：日常整理入口与重复文件查看
        self._full_scan_result = None      # 整空间原始扫描（范围筛选前）
        self._plan_scope = "space"         # space / intake
        self._plan_scope_note = ""
        self.intake = IntakeList()
        self._intake_resolution = None
        self._intake_dialog = None
        self._recent_dialog = None
        self._duplicate_dialog = None
        self._duplicate_worker = None
        self._duplicate_seq = 0
        self._duplicate_cache = None
        self._duplicate_cache_key = None
        self._duplicate_cache_entries = None
        self._duplicate_pending_entries = None
        self._scan_generation = 0
        self.setAcceptDrops(True)

        # V1.2: 操作历史窗口（非模态；同一时间只保留当前根目录的实例）
        self._history_dialog: HistoryDialog | None = None

        self._build_ui()

    # V1.2: 统一任务状态 —— 旧布尔位以属性形式委托到 TaskContext
    @property
    def _working(self) -> bool:
        return self.task.working

    @_working.setter
    def _working(self, v: bool):
        self.task.working = v

    @property
    def _plan_working(self) -> bool:
        return self.task.plan_working

    @_plan_working.setter
    def _plan_working(self, v: bool):
        self.task.plan_working = v

    def _current_root(self) -> str:
        """当前整理根目录（历史页面标题与路径拼接用）。"""
        if self._storage is not None:
            return self._storage.root
        t = self.path_edit.text().strip()
        return os.path.abspath(t) if t else ""

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

        self.btn_scan_intake = QPushButton("扫描清单范围")
        self.btn_scan_intake.setToolTip(
            "只扫描「待整理清单」指定的文件/目录范围；\n"
            "仍经过保护与排除机制，范围外的文件不会进入计划")
        self.btn_scan_intake.setEnabled(False)
        self.btn_scan_intake.clicked.connect(self.on_scan_intake)
        top_bar.addWidget(self.btn_scan_intake)

        self.btn_recent = QPushButton("最近空间…")
        self.btn_recent.setToolTip("打开最近使用过的整理空间；可固定、移除记录")
        self.btn_recent.clicked.connect(self.on_recent_spaces)
        top_bar.addWidget(self.btn_recent)

        self.btn_intake = QPushButton("待整理清单…")
        self.btn_intake.setToolTip(
            "登记本次要整理的文件/目录范围；只登记，不移动文件")
        self.btn_intake.clicked.connect(self.on_intake_dialog)
        top_bar.addWidget(self.btn_intake)

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
        self.btn_edit_rules.setToolTip(
            "只影响「按课程再按类型」模式；推荐模式的规则请用「推荐模式配置…」"
        )
        self.btn_edit_rules.clicked.connect(self.on_edit_rules)
        cfg_bar.addWidget(self.btn_edit_rules)

        self.btn_edit_config = QPushButton("推荐模式配置…")
        self.btn_edit_config.setToolTip(
            "管理学期映射、顶层方向、学期内目录、通用前缀与关键词兜底；\n"
            "只影响「按方向/学期/类别（推荐）」模式"
        )
        self.btn_edit_config.clicked.connect(self.on_edit_config)
        cfg_bar.addWidget(self.btn_edit_config)

        self.btn_scheme_center = QPushButton("整理方案…")
        self.btn_scheme_center.setToolTip(
            "导入/导出方案文件、管理本地方案库、查看差异与文件影响、\n"
            "应用方案到当前目录，或恢复上一次配置")
        self.btn_scheme_center.clicked.connect(self.on_scheme_center)
        cfg_bar.addWidget(self.btn_scheme_center)

        self.btn_edit_protect = QPushButton("保护目录…")
        self.btn_edit_protect.setToolTip(
            "手动添加/移除不参与整理的目录；默认保护与自动识别项目只读展示"
        )
        self.btn_edit_protect.clicked.connect(self.on_edit_protect)
        cfg_bar.addWidget(self.btn_edit_protect)

        self.btn_override_selected = QPushButton("修正所选文件分类…")
        self.btn_override_selected.setToolTip(
            "在当前有效计划中手动修正某个文件的方向/学期/类别/内部路径；\n"
            "仅本次计划生效，不写回分类配置；重新扫描/重新预览/切换目录后失效；\n"
            "当前仅支持「按方向/学期/类别（推荐）」模式"
        )
        self.btn_override_selected.setEnabled(False)
        self.btn_override_selected.clicked.connect(self.on_override_selected)
        cfg_bar.addWidget(self.btn_override_selected)

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

        # --- 配置作用范围说明（V1.3） ---
        self.config_label = QLabel(
            "推荐模式配置：请选择目录后加载（只影响「推荐」模式）")
        self.config_label.setWordWrap(True)
        self.config_label.setStyleSheet(
            "color:#37474F;background:#ECEFF1;padding:2px 10px;"
            "border-radius:3px;font-size:12px;")
        outer.addWidget(self.config_label)

        # --- 本次扫描/计划范围（V1.7） ---
        self.scope_label = QLabel("")
        self.scope_label.setWordWrap(True)
        self.scope_label.setStyleSheet(
            "color:#1B5E20;background:#E8F5E9;padding:2px 10px;"
            "border-radius:3px;font-size:12px;")
        self.scope_label.hide()
        outer.addWidget(self.scope_label)

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

        self.btn_history = QPushButton("🕘 操作历史…")
        self.btn_history.setToolTip(
            "查看当前目录的全部整理批次与逐项记录；\n"
            "可从历史发起允许的撤销与中断批次的核对恢复"
        )
        self.btn_history.setEnabled(False)
        self.btn_history.clicked.connect(self.on_history)
        action_bar.addWidget(self.btn_history)

        self.btn_duplicates = QPushButton("重复文件…")
        self.btn_duplicates.setToolTip(
            "查看内容重复、同名不同内容与待核对条目；\n"
            "只读核对，不删除、不合并、不移动，也不改变勾选")
        self.btn_duplicates.setEnabled(False)
        self.btn_duplicates.clicked.connect(self.on_show_duplicates)
        action_bar.addWidget(self.btn_duplicates)

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

        # --- V1.6 预览筛选（只改变视图，不改变计划与勾选） ---
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        self.plan_search = QLineEdit()
        self.plan_search.setPlaceholderText("搜索源路径 / 目标路径 / 分类依据……")
        self.plan_search.textChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(self.plan_search, 3)
        self.type_filter = QComboBox()
        self.type_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(QLabel("类型："))
        filter_bar.addWidget(self.type_filter, 1)
        self.dir_filter = QComboBox()
        self.dir_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(QLabel("方向："))
        filter_bar.addWidget(self.dir_filter, 1)
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部来源", "")
        self.source_filter.addItem("自动", "auto")
        self.source_filter.addItem("长期规则", "long_term")
        self.source_filter.addItem("本次手动修正", "manual")
        self.source_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(QLabel("来源："))
        filter_bar.addWidget(self.source_filter, 1)
        self.pending_check = QCheckBox("仅待定")
        self.pending_check.stateChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(self.pending_check)
        self.conflict_check = QCheckBox("仅重名/改名")
        self.conflict_check.stateChanged.connect(self._apply_plan_filters)
        filter_bar.addWidget(self.conflict_check)
        self.btn_clear_filters = QPushButton("清空筛选")
        self.btn_clear_filters.clicked.connect(self._clear_plan_filters)
        filter_bar.addWidget(self.btn_clear_filters)
        outer.addLayout(filter_bar)

        batch_bar = QHBoxLayout()
        batch_bar.setSpacing(8)
        self.btn_select_visible = QPushButton("勾选可见")
        self.btn_select_visible.clicked.connect(
            lambda: self._set_visible_checked(True))
        batch_bar.addWidget(self.btn_select_visible)
        self.btn_deselect_visible = QPushButton("取消勾选可见")
        self.btn_deselect_visible.clicked.connect(
            lambda: self._set_visible_checked(False))
        batch_bar.addWidget(self.btn_deselect_visible)
        self.btn_invert_visible = QPushButton("反选可见")
        self.btn_invert_visible.clicked.connect(
            lambda: self._set_visible_checked(None, invert=True))
        batch_bar.addWidget(self.btn_invert_visible)
        self.plan_counts_label = QLabel("")
        self.plan_counts_label.setStyleSheet("color:#37474F;")
        batch_bar.addWidget(self.plan_counts_label, 1)
        self.btn_target_tree = QPushButton("查看目标目录结构…")
        self.btn_target_tree.clicked.connect(self.on_show_target_tree)
        batch_bar.addWidget(self.btn_target_tree)
        outer.addLayout(batch_bar)

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

        # --- 保护目录摘要（扫描后更新，明确显示原因类别与跳过数量） ---
        self.protect_label = QLabel("")
        self.protect_label.setWordWrap(True)
        self.protect_label.setStyleSheet(
            "color:#37474F;background:#ECEFF1;padding:2px 10px;border-radius:3px;"
            "font-size:12px;"
        )
        self.protect_label.hide()
        outer.addWidget(self.protect_label)

    # ============== 浏览 & 扫描 ==============

    def on_browse(self):
        if self._close_pending or self._closing or self._working or self._plan_working:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "选择要扫描的文件夹",
            os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly,
        )
        if chosen:
            self.path_edit.setText(chosen)
            self.btn_scan.setEnabled(True)
            self._reset_for_new_dir(chosen)
            try:
                recent_spaces.touch(chosen)
            except Exception:
                pass
            self.statusBar().showMessage(f"已选择：{chosen}")

    def _reset_for_new_dir(self, root: str, keep_intake_dialog: bool = False):
        """选择新目录时重置所有状态。"""
        # 先让仍在跑的计划任务全部过期（新目录 = 全新的计划上下文）
        self._abandon_plan_generation()
        # V1.2: 关闭旧根目录的历史窗口，避免跨目录串用
        if self._history_dialog is not None:
            self._history_dialog.close()
            self._history_dialog = None
        self.scan_result = None
        self._full_scan_result = None
        self._plan_scope = "space"
        self._plan_scope_note = ""
        self.intake.clear()
        self._intake_resolution = None
        self._invalidate_duplicate_cache(close=True)
        if not keep_intake_dialog:
            self._close_intake_dialog()
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self._reset_plan_filters()
        self.invalid_label.hide()
        self.protect_label.hide()
        self._update_scope_label()
        self.btn_regenerate.setEnabled(False)
        self.btn_execute.setEnabled(False)
        self.btn_undo.setEnabled(False)
        self._current_plan_seq = 0
        # 按根目录加载手动保护配置（切换目录不会串用）
        self.manual_protect = load_manual_protect(root)
        # 按根目录加载推荐模式分类配置（只读；不存在时不创建文件）
        self._load_root_config(root)
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
        if self._close_pending or self._closing:
            return  # 关闭等待期间不再弹窗、不再启动撤销等业务任务

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
                # V1.2 第二轮：核对恢复移入后台线程（与历史页同一流程），
                # 结果弹窗由 _on_recover_done 统一处理
                self.recover_interrupted_batch(interrupted)
            return

        pending = self._storage.get_pending_batch()
        # S2-09：状态已是 done 的批次不算「未完成」，打开目录时不再弹撤销建议。
        if pending is not None and pending.status != "done":
            self.btn_undo.setEnabled(True)
            # S2-09：不再默认建议撤销——默认按钮改为「否」。
            reply = QMessageBox.question(
                self, "归档盒",
                f"检测到该目录存在一个未完成的整理批次（#{pending.batch_id}，"
                f"状态：{pending.status}，创建于 {pending.created_at}）。\n\n"
                f"可以选择继续扫描、或者先撤销该批次。\n\n"
                f"是否现在尝试撤销？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.on_undo()

    def on_scan(self):
        if self._close_pending or self._closing:
            return  # 关闭等待期间不启动新的扫描
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return
        self._plan_scope = "space"
        self._plan_scope_note = ""
        self._update_scope_label()
        self._start_scan(root)

    def on_scan_intake(self):
        """只扫描待整理清单指定的范围。"""
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的整理空间。")
            return
        if len(self.intake) == 0:
            QMessageBox.information(
                self, "待整理清单",
                "清单为空。请先通过「待整理清单…」或拖放加入文件/目录。")
            return
        self._plan_scope = "intake"
        self._update_scope_label()
        self._start_scan(root)

    def _start_scan(self, root: str):
        # 已有计划任务在跑时，先让它全部过期，避免旧结果落到新扫描上
        self._abandon_plan_generation()

        # S1-06：新扫描一开始就作废旧计划——扫描期间/扫描失败后，
        # 旧预览都不能再被执行（空表上点「开始整理」会真的移动文件）。
        if self.current_plan is not None or self._plan_valid:
            self._discard_plan("正在重新扫描，旧预览已失效；扫描完成后会重新生成计划")

        self.table.setRowCount(0)
        self.task.begin("scan", root, phase="扫描中……")
        self._set_working(True, "扫描中……")

        # V1.1: 默认保护归档盒源码目录 + 用户手动保护目录
        protect = [(DEFAULT_PROTECT_APP_SRC, "default", "默认保护（归档盒源码）")]
        protect.extend(manual_scan_specs(root, self.manual_protect))

        self._scan_worker = ScanWorker(root, protect_dirs=protect, parent=self)
        self._scan_worker.finished_ok.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(self._on_scan_worker_finished)
        self._scan_worker.start()

    def _on_scan_worker_finished(self):
        """扫描线程结束。只清理扫描态，不能覆盖仍在运行的计划生成态。"""
        self.task.end("scan")     # 若计划生成已接续开始，kind 保持为 plan
        self._set_working(False)

    def _on_scan_done(self, result: ScanResult):
        self._full_scan_result = result
        self._scan_generation += 1
        self._invalidate_duplicate_cache(close=False)
        effective = self._effective_scan_result(result)
        self.scan_result = effective
        self._update_scope_label()

        # 状态栏汇总（V1.1: 加入项目保护信息）
        msg_parts = [f"扫描完成：{result.summary}"]
        if self._plan_scope == "intake":
            msg_parts.append(self._plan_scope_note)
        if result.skipped:
            reasons = {}
            for s in result.skipped:
                r = s.skipped_reason or "其他"
                reasons[r] = reasons.get(r, 0) + 1
            msg_parts.append("跳过：" + "，".join(f"{k} {v}" for k, v in reasons.items()))
        self.statusBar().showMessage(" | ".join(msg_parts))
        self._update_protect_summary(result)

        # 关闭等待期间：只记录结果，不弹窗、不启动新的计划任务
        if self._close_pending or self._closing:
            return

        if self._plan_scope == "intake" and not effective.files:
            message = self._scope_empty_message()
            self._discard_plan(message)
            QMessageBox.information(self, "待整理清单", message)
            return

        # V1.1: 项目保护 — 如果有保护根目录，弹窗提示（含来源类别）
        if result.protected_roots:
            protect_lines = []
            for proot, reason in result.protected_roots.items():
                kinds = [
                    KIND_LABELS.get(k, k)
                    for k in result.protected_kinds.get(proot, "").split(",") if k
                ]
                tag = " / ".join(kinds) or "项目保护"
                protect_lines.append(f"  • [{tag}] {proot}\n      {reason}")
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

        # S1-01：junction / 重解析点被整体跳过——要告诉用户具体是哪些路径，
        # 不能只给一个计数（用户需要知道链接目标里的文件没有被动过）。
        if result.junction_dirs:
            QMessageBox.information(
                self, "归档盒",
                "以下目录是 junction / 重解析点（指向其他位置的链接目录），"
                "已整体跳过，其中的文件不会进入整理计划：\n\n"
                + "\n".join(f"  • {p}" for p in result.junction_dirs[:20])
                + ("\n  …" if len(result.junction_dirs) > 20 else "")
                + "\n\n如需整理这些位置，请对链接目标的真实目录单独扫描。"
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

    def _update_protect_summary(self, result: ScanResult):
        """更新保护摘要：来源类别 + 跳过数量。"""
        roots = result.protected_roots
        if not roots:
            self.protect_label.setText("")
            self.protect_label.hide()
            return
        counts = {"default": 0, "auto": 0, "manual": 0}
        for p in roots:
            for k in result.protected_kinds.get(p, "").split(","):
                if k in counts:
                    counts[k] += 1
        self.protect_label.setText(
            f"受保护目录 {len(roots)} 个"
            f"（默认 {counts['default']} / 自动 {counts['auto']} / 手动 {counts['manual']}），"
            f"本次跳过 {result.protected_count} 个文件"
        )
        self.protect_label.show()

    def _on_scan_failed(self, err: str):
        # S1-06：扫描失败后旧计划必须彻底失效——不能留下「可执行但预览已清空」的
        # 状态，否则在空表上点「开始整理」会按旧计划移动文件。
        self._discard_plan(
            "扫描失败，旧预览已失效；请排除问题后重新扫描生成新预览")
        self.statusBar().showMessage("扫描失败：旧预览已失效")
        if self._close_pending or self._closing:
            return
        QMessageBox.critical(self, "归档盒", f"扫描时发生异常：\n{err}")

    # ============== 扫描 / 待整理范围（V1.7） ==============

    def _effective_scan_result(self, raw):
        """按当前范围（整个空间 / 待整理清单）得到计划实际使用的扫描结果。"""
        if raw is None:
            return None
        if self._plan_scope != "intake":
            self._intake_resolution = None
            self._plan_scope_note = (
                f"整个整理空间（扫描纳入 {len(raw.files)} 个文件）")
            return raw
        res = self.intake.resolve(raw.root, raw)
        self._intake_resolution = res
        note = (f"待整理清单：{len(self.intake)} 项 → "
                f"纳入 {res.included_count} 个文件")
        if res.protected:
            note += f"；受保护/被排除 {res.protected} 项"
        if res.not_scanned:
            note += f"；未进入扫描 {res.not_scanned} 项"
        if res.unsupported:
            note += f"；无效来源 {res.unsupported} 项"
        self._plan_scope_note = note
        effective = copy.copy(raw)
        effective.files = [f for f in raw.files if f.rel_path in res.included]
        return effective

    def _apply_scope_to_scan(self):
        if self._full_scan_result is not None:
            self.scan_result = self._effective_scan_result(self._full_scan_result)
        self._update_scope_label()

    def _scope_empty_message(self) -> str:
        res = self._intake_resolution
        lines = ["待整理清单在当前扫描中没有可纳入的文件。"]
        if res is not None:
            for d in res.details:
                if d.included == 0:
                    lines.append(f"· {d.path}：{d.reason or '没有可纳入文件'}")
        lines.append(
            "范围外的文件不会被整理；请检查来源是否在当前空间内、"
            "是否受保护或已被扫描排除。")
        return "\n".join(lines)

    def _update_scope_label(self):
        if not hasattr(self, "scope_label"):
            return
        root = self.path_edit.text().strip()
        if not root:
            self.scope_label.hide()
            return
        if self._full_scan_result is None and self.scan_result is None:
            kind = "待整理清单" if self._plan_scope == "intake" else "整个整理空间"
            text = f"本次范围：{kind}（尚未扫描）"
        else:
            text = "本次范围：" + (self._plan_scope_note or "整个整理空间")
        self.scope_label.setText(text)
        self.scope_label.show()

    def _invalidate_for_scope_change(self):
        """待整理范围变化：旧计划失效；保留整空间扫描以便重新预览。"""
        self._abandon_plan_generation()
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self._reset_plan_filters()
        self._invalidate_duplicate_cache(close=True)
        self._apply_scope_to_scan()
        self.invalid_label.setText(
            "⚠ 待整理范围已变化：旧预览已失效，请点击「重新预览」重新生成")
        self.invalid_label.show()
        self._update_button_states()
        self.statusBar().showMessage(
            "待整理清单已更新。请点击「重新预览」按新范围生成计划后再执行。")

    # ============== 最近整理空间 / 待整理清单（V1.7） ==============

    def on_recent_spaces(self):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，暂不能切换整理空间。")
            return
        from daily_entry_dialogs import RecentSpacesDialog
        dlg = RecentSpacesDialog(self._open_recent_space, self)
        self._recent_dialog = dlg
        try:
            dlg.exec()
        finally:
            self._recent_dialog = None

    def _open_recent_space(self, path: str) -> bool:
        if not os.path.isdir(path):
            QMessageBox.warning(
                self, "无法打开空间",
                f"目录当前不存在或不可访问：\n{path}\n"
                "不会自动创建该目录。")
            return False
        self.path_edit.setText(path)
        self.btn_scan.setEnabled(True)
        self._reset_for_new_dir(path)
        try:
            recent_spaces.touch(path)
        except Exception:
            pass
        self.statusBar().showMessage(f"已打开整理空间：{path}")
        return True

    def on_intake_dialog(self):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，暂不能修改待整理清单。")
            return
        from daily_entry_dialogs import IntakeDialog
        if self._intake_dialog is not None:
            self._intake_dialog.raise_()
            self._intake_dialog.refresh()
            return
        dlg = IntakeDialog(
            get_root=lambda: self.path_edit.text().strip(),
            get_summary=self._intake_summary,
            get_items=lambda: self.intake,
            add_paths=self._add_intake_paths,
            remove_path=self._remove_intake_path,
            clear_items=self._clear_intake,
            parent=self,
        )
        dlg.finished.connect(lambda _=0: self._clear_intake_dialog_ref(dlg))
        self._intake_dialog = dlg
        dlg.show()

    def _clear_intake_dialog_ref(self, dlg):
        if self._intake_dialog is dlg:
            self._intake_dialog = None

    def _refresh_intake_dialog(self):
        if self._intake_dialog is not None:
            try:
                self._intake_dialog.refresh()
            except RuntimeError:
                self._intake_dialog = None

    def _intake_summary(self) -> str:
        root = self.path_edit.text().strip()
        if not root:
            return "尚未选择整理空间"
        raw = self._full_scan_result or self.scan_result
        if raw is None:
            return "尚未扫描；扫描后显示纳入/跳过数量"
        return self.intake.describe(root, raw)

    def _add_intake_paths(self, paths) -> list:
        """拖放与对话框共用的加入逻辑；返回逐项说明。"""
        messages = []
        if self._close_pending or self._closing:
            return ["窗口正在关闭，未修改待整理清单。"]
        if self._working or self._plan_working:
            return ["当前有后台任务运行，未修改待整理清单。"]
        changed = False
        for path in paths:
            root = self.path_edit.text().strip()
            res = self.intake.add(path, root or None)
            if res.status == "needs_root" and res.candidate_root:
                reply = QMessageBox.question(
                    self, "设为整理空间",
                    f"尚未选择整理空间。\n\n是否将以下目录设为当前整理空间？\n"
                    f"{res.candidate_root}\n\n"
                    "（不会创建目录；其配置与历史仍按该目录隔离）",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if reply == QMessageBox.Yes and os.path.isdir(res.candidate_root):
                    self.path_edit.setText(res.candidate_root)
                    self.btn_scan.setEnabled(True)
                    self._reset_for_new_dir(res.candidate_root,
                                            keep_intake_dialog=True)
                    try:
                        recent_spaces.touch(res.candidate_root)
                    except Exception:
                        pass
                    root = self.path_edit.text().strip()
                    res = self.intake.add(path, root or None)
            messages.append(res.message)
            if res.status in ("added", "coalesced", "duplicate", "covered"):
                changed = changed or res.status in ("added", "coalesced")
        if changed:
            self._on_intake_changed()
        self._refresh_intake_dialog()
        self._update_button_states()
        return messages

    def _remove_intake_path(self, path: str) -> bool:
        if self._close_pending or self._closing:
            return False
        if self._working or self._plan_working:
            return False
        ok = self.intake.remove(path)
        if ok:
            self._on_intake_changed()
            self._refresh_intake_dialog()
        return ok

    def _clear_intake(self):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            return
        self.intake.clear()
        self._on_intake_changed()
        self._refresh_intake_dialog()

    def _on_intake_changed(self):
        self._update_scope_label()
        if self._plan_scope == "intake":
            self._invalidate_for_scope_change()
        self._update_button_states()

    # ---- 拖放 ----
    def dragEnterEvent(self, event):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if not paths:
            return
        event.acceptProposedAction()
        messages = self._add_intake_paths(paths)
        if messages:
            self.statusBar().showMessage("；".join(messages[-3:]))

    def _load_root_config(self, root: str):
        """按根目录加载分类配置。

        不存在 → 内置默认配置（不创建文件）；
        损坏 / 版本不兼容 / 非法字段 → 记录错误并阻止推荐模式生成计划，
        不静默回退后继续执行；原文件保留，由用户主动修复或恢复默认。
        """
        try:
            cfg, source = load_config(root)
            self.classification_config = cfg
            self._config_source = source
            self._config_error = ""
        except ClassificationConfigError as e:
            self.classification_config = default_config()
            self._config_source = "error"
            self._config_error = str(e)
        self._update_config_label()

    def _update_config_label(self):
        if not hasattr(self, "config_label"):
            return
        root = self.path_edit.text().strip()
        if not root:
            self.config_label.setText(
                "推荐模式配置：请选择目录后加载（只影响「推荐」模式）")
            self.config_label.setToolTip("")
            return
        if self._config_error:
            self.config_label.setText(
                "⚠ 推荐模式配置不可用：已阻止生成计划，请点「推荐模式配置…」"
                "修复或恢复默认（原文件保留）")
            self.config_label.setStyleSheet(
                "color:#B71C1C;background:#FFEBEE;padding:2px 10px;"
                "border-radius:3px;font-size:12px;")
            self.config_label.setToolTip(self._config_error)
            return
        which = "内置默认" if self._config_source == "default" else "本目录自定义"
        self.config_label.setText(
            f"推荐模式配置：{which}｜{config_summary(self.classification_config)}"
            "｜仅「推荐」模式使用；按类型/按课程模式不使用此配置")
        self.config_label.setStyleSheet(
            "color:#37474F;background:#ECEFF1;padding:2px 10px;"
            "border-radius:3px;font-size:12px;")
        self.config_label.setToolTip(
            "匹配顺序：学期映射 → 顶层方向 → 学期内目录 → 通用目录前缀 "
            "→ 未登记目录锚定 → 待定（可用「试运行／查看影响」核对）")

    def on_edit_config(self):
        """打开推荐模式配置编辑器（独立草稿，取消不改变生效配置）。"""
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒",
                "当前有后台任务运行，暂不能修改分类配置。请等待任务结束后再操作。")
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return

        if self._config_error:
            reply = QMessageBox.question(
                self, "归档盒",
                "该目录的分类配置当前不可用：\n\n"
                f"{self._config_error}\n\n"
                "是否打开配置编辑器，用内置默认规则作为草稿？\n"
                "（只有你点击「保存并应用」才会覆盖原文件；取消则原文件保持不变）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return
            draft = default_config()
        else:
            draft = copy.deepcopy(self.classification_config)

        dlg = ClassificationConfigDialog(
            root=root,
            config=draft,
            using_defaults=(self._config_source == "default"),
            config_error=self._config_error,
            scan_result=self.scan_result,
            base_config=copy.deepcopy(self.classification_config),
            save_callback=lambda cfg: self._save_classification_config(root, cfg),
            worker_registry=self,
            parent=self,
        )
        dlg.exec()

    def _save_classification_config(self, root: str, cfg: ClassificationConfig):
        """保存配置（由编辑器调用）；失败时抛错，生效规则保持不变。"""
        if self._close_pending or self._closing:
            raise ClassificationConfigError(
                "窗口正在关闭，未保存分类配置；已生效的规则保持不变。")
        current = self.path_edit.text().strip()
        if not current or os.path.realpath(os.path.abspath(current)) != \
                os.path.realpath(os.path.abspath(root)):
            raise ClassificationConfigError(
                "当前根目录已变化，拒绝把配置写入其他目录。请重新选择目录后再编辑。")
        saved_custom = save_config(root, cfg)
        self.classification_config = cfg
        self._config_source = "custom" if saved_custom else "default"
        self._config_error = ""
        self._update_config_label()
        self._invalidate_for_config_change()

    # ============== 整理方案：导入 / 导出 / 方案库 / 回退（V1.6） ==============

    def on_scheme_center(self):
        """打开整理方案库（导入导出、应用、恢复上一次配置）。"""
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒",
                "当前有后台任务运行，暂不能应用或恢复配置。请等待任务结束后再操作。")
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return
        if self._config_error:
            reply = QMessageBox.question(
                self, "归档盒",
                "该目录的分类配置当前不可用：\n\n"
                f"{self._config_error}\n\n"
                "仍可打开方案库并导入方案覆盖它。继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        from transfer_dialogs import SchemeCenterDialog
        dlg = SchemeCenterDialog(
            root=root,
            current_config=copy.deepcopy(self.classification_config),
            apply_callback=self._apply_scheme_config,
            restore_callback=self._restore_last_config,
            scan_result=self.scan_result,
            worker_registry=self,
            parent=self,
        )
        dlg.exec()

    def _require_idle_root(self) -> str:
        if self._close_pending or self._closing:
            raise ClassificationConfigError(
                "窗口正在关闭，未应用方案；当前配置保持不变。")
        if self._working or self._plan_working:
            raise ClassificationConfigError("当前有后台任务运行，请稍后再试。")
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            raise ClassificationConfigError("当前没有有效的整理根目录。")
        if self._storage is not None and \
                _norm_root(self._storage.root) != _norm_root(root):
            raise ClassificationConfigError(
                "当前根目录已变化，拒绝把方案写入其他目录。请重新选择目录。")
        return root

    def _apply_scheme_config(self, cfg):
        """应用方案：先备份当前配置，再原子保存并重新加载、作废旧计划。"""
        root = self._require_idle_root()
        create_backup(root)
        save_config(root, cfg)
        self._load_root_config(root)
        self._invalidate_for_config_change()
        self.statusBar().showMessage(
            "方案已应用到当前目录。旧预览已失效，请重新预览后再执行。")

    def _restore_last_config(self) -> str:
        """恢复上一次应用前的配置（只改配置，不移动/撤销文件）。"""
        root = self._require_idle_root()
        _state, message = restore_latest_backup(root)
        self._load_root_config(root)
        self._invalidate_for_config_change()
        self.statusBar().showMessage(
            message + " 旧预览已失效，请重新预览后再执行。")
        return message

    def _invalidate_for_config_change(self):
        """配置应用成功后：旧预览立即失效，必须重新生成后才能执行。"""
        self._abandon_plan_generation()
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self._reset_plan_filters()
        self._invalidate_duplicate_cache(close=True)
        self.invalid_label.setText(
            "⚠ 分类配置已更新：旧预览已失效，请点击「重新预览」重新生成")
        self.invalid_label.show()
        self._update_button_states()
        self.statusBar().showMessage(
            "分类配置已保存并生效。请点击「重新预览」按新规则生成计划，"
            "确认后再开始整理。")

    # ---- 规则试运行线程登记（关闭时等待，避免线程未结束就释放窗口） ----

    def register_preview_worker(self, worker):
        if worker not in self._rule_preview_workers:
            self._rule_preview_workers.append(worker)
            # 由主窗口负责最终注销：即使编辑器已关闭，线程结束后也能回收登记
            worker.finished.connect(self._on_preview_worker_finished)

    def _on_preview_worker_finished(self):
        worker = self.sender()
        if worker is not None:
            self.unregister_preview_worker(worker)

    def unregister_preview_worker(self, worker):
        try:
            self._rule_preview_workers.remove(worker)
        except ValueError:
            pass

    # ============== 模式 & 规则变更 ==============

    def on_edit_rules(self):
        if self._close_pending or self._closing or self._working or self._plan_working:
            return
        dlg = RuleEditDialog(rules_to_text(self.rules), self)
        if dlg.exec() == QDialog.Accepted:
            new_rules = parse_rules_text(dlg.get_text())
            if new_rules:
                self.rules = new_rules
            self.on_plan_param_changed()

    def on_edit_protect(self):
        """手动保护目录入口。配置变化后旧扫描/旧计划必须重新生成。"""
        if self._close_pending or self._closing or self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒",
                "当前有后台任务运行或正在关闭，暂不能修改保护目录。"
                "请等待任务结束后再操作。",
            )
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return

        readonly = []
        if self.scan_result is not None:
            for proot, reason in self.scan_result.protected_roots.items():
                kinds = self.scan_result.protected_kinds.get(proot, "")
                if "manual" in kinds.split(","):
                    continue  # 手动条目在下方可编辑列表里
                readonly.append((proot, kinds, reason))

        dlg = ProtectDirDialog(
            root, self.manual_protect, readonly,
            skipped_count=(self.scan_result.protected_count
                           if self.scan_result is not None else 0),
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        new_paths = dedupe_manual_paths(root, dlg.get_paths())
        if new_paths == self.manual_protect:
            return
        self.manual_protect = new_paths
        try:
            save_manual_protect(root, new_paths)
        except OSError as e:
            QMessageBox.warning(
                self, "归档盒",
                f"保护配置已在本轮生效，但无法保存到 .organizer：\n{e}\n"
                "重启程序后可能丢失手动保护目录。",
            )
        self._invalidate_for_protect_change()

    def _invalidate_for_protect_change(self):
        """保护配置改变后：旧扫描结果与旧计划都不可信，必须重新扫描。"""
        self._abandon_plan_generation()
        self.scan_result = None
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self.protect_label.hide()
        self.invalid_label.setText("⚠ 保护配置已更改：请重新扫描以生成正确预览")
        self.invalid_label.show()
        self.btn_regenerate.setEnabled(False)
        self._update_button_states()
        self.statusBar().showMessage(
            "保护配置已更改，旧预览已失效。请点击「扫描」重新生成。"
        )

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
        self._apply_scope_to_scan()
        if self._closing or self._close_pending or self.scan_result is None:
            return  # 关闭等待期间不启动新的计划任务

        # 上一个计划任务仍在跑时，请求它停止；它的所有信号都会因 seq 过期被丢弃
        old = getattr(self, "_plan_worker", None)
        if old is not None and old.isRunning():
            old.request_cancel()

        # 递增 seq —— 只有携带这个 seq 的信号才会被界面接受
        self._plan_seq_counter += 1
        seq = self._plan_seq_counter
        self._current_plan_seq = seq
        self._plan_cancel_requested = False
        self._plan_cancel_seq = None

        mode = self.mode_combo.currentData()
        self.mode = mode

        # V1.3：推荐模式必须使用已加载的显式配置；配置损坏时不静默回退
        plan_config = None
        if mode == MODE_BY_DIRECTION_SEMESTER:
            if self._config_error:
                self._discard_plan(
                    "分类配置不可用：请打开「推荐模式配置…」修复或恢复默认")
                QMessageBox.critical(
                    self, "归档盒",
                    "该目录的分类配置不可用，已停止生成计划：\n\n"
                    f"{self._config_error}\n\n"
                    "为避免按错误规则分类，必须先修复配置或选择「恢复默认」。")
                return
            # 深拷贝：后台线程持有固定快照，不引用任何可编辑草稿对象
            plan_config = copy.deepcopy(self.classification_config)

        # 新计划开始后，旧计划立即失效，取消/失败后都不允许执行旧计划
        self._plan_valid = False
        if self.current_plan is not None:
            self.invalid_label.setText("⚠ 正在生成新计划，旧计划暂不可执行")
            self.invalid_label.show()

        self._plan_working = True
        self.task.begin(
            "plan", self.scan_result.root,
            phase="生成计划中（计算 SHA-256 指纹）",
            cancelable=True,
            total=len(self.scan_result.files),
        )
        self._refresh_busy_ui(
            f"生成计划中（计算 SHA-256 指纹，共 {len(self.scan_result.files)} 个文件）…"
        )
        self._update_button_states()

        worker = PlanWorker(seq, self.scan_result, mode, self.rules,
                            config=plan_config, parent=self)
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
        if not self._plan_working:
            return  # 任务已结束，迟到的进度不再污染界面
        self.task.update_progress(cur, total, fname)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(cur)
        self.statusBar().showMessage(f"生成计划中… [{cur}/{total}] {fname}")

    def _on_plan_done(self, seq: int, plan: OrganizePlan):
        if self._closing or self._close_pending:
            return  # 关闭流程中不再创建新的数据库连接、不发布计划
        if seq != self._current_plan_seq:
            return  # 旧任务结果，不能覆盖新任务状态

        # 取消请求发出后任务仍完成了（线程已结束但结果尚未交付的边界）：
        # 按取消处理，绝不发布这份计划
        if self._plan_cancel_requested or self._plan_cancel_seq == seq:
            self._discard_plan("计划生成已取消，请点击「重新预览」重新生成")
            return

        self.current_plan = plan
        self._plan_valid = True
        self._invalidate_duplicate_cache(close=True)
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

        self._populate_table(plan, reset_filters=True)
        self._update_button_states()
        self.statusBar().showMessage(
            f"✓ {plan.summary()}（仍可更改，点击「开始整理」才会移动文件）"
        )

    def _on_plan_cancelled(self, seq: int):
        if self._closing or self._close_pending:
            return
        if seq != self._current_plan_seq:
            return  # 旧任务的取消信号，与当前任务无关
        self._discard_plan("计划生成已取消，请点击「重新预览」重新生成")

    def _on_plan_failed(self, seq: int, err: str):
        if self._closing or self._close_pending:
            return
        if seq != self._current_plan_seq:
            return  # 旧任务的失败信号，不能弹窗污染当前任务
        self._discard_plan("计划生成失败，请点击「重新预览」重新生成")
        QMessageBox.critical(self, "归档盒", f"生成计划失败：\n{err}")

    def _discard_plan(self, message: str):
        """取消/失败后清空计划与预览，保证没有任何可执行计划残留。"""
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self._reset_plan_filters()
        self._invalidate_duplicate_cache(close=True)
        self.invalid_label.setText(f"⚠ {message}")
        self.invalid_label.show()
        self.statusBar().showMessage(message)
        self._update_button_states()

    def _on_plan_worker_finished(self, seq: int):
        if seq != self._current_plan_seq:
            return  # 旧任务结束，不能重置当前任务的忙碌状态
        self._plan_working = False
        self.task.end("plan")
        self._plan_cancel_requested = False
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self._refresh_busy_ui()
        self._update_button_states()

    def _cancel_plan_generation(self):
        """用户点击取消按钮，请求取消计划生成。

        判定依据是界面相位 _plan_working，而不是 worker.isRunning()：
        线程可能已经结束，但成功/结束信号还在事件队列里等待主线程处理，
        此时界面仍显示“生成中”，取消按钮仍有效。

        不递增 seq —— 否则本任务的取消/结束信号会被误判为过期。
        改用 _plan_cancel_seq 记录被取消的 seq：
        - 结果尚未交付：seq 相同的 finished_ok 到达时被丢弃
        - 结果已经交付（finished_ok 已处理、finished_seq 尚未处理）：
          立即撤下当前计划，执行按钮保持禁用
        """
        if not self._plan_working:
            return
        seq = self._current_plan_seq
        self._plan_cancel_seq = seq
        self._plan_cancel_requested = True
        self.task.request_cancel()

        worker = getattr(self, "_plan_worker", None)
        if worker is not None:
            try:
                worker.request_cancel()
            except RuntimeError:
                pass  # 线程对象已被回收，取消标记仍然生效

        self.btn_cancel.setEnabled(False)
        self.statusBar().showMessage("正在取消计划生成，当前文件计算完成后停止……")

        # 成功回调已处理（计划已发布），但结束回调还没处理 —— 立即失效
        if self.current_plan is not None and self._plan_valid:
            self._discard_plan("计划生成已取消，请点击「重新预览」重新生成")

    def _abandon_plan_generation(self):
        """让当前计划任务的所有信号失效（用于切换目录/重新扫描）。"""
        worker = getattr(self, "_plan_worker", None)
        if worker is not None and worker.isRunning():
            worker.request_cancel()
        # 递增 seq：旧任务的进度/成功/失败/取消/结束信号全部过期
        self._current_plan_seq += 1
        self._plan_working = False
        self.task.end("plan")
        self._plan_cancel_requested = False
        self._plan_cancel_seq = None
        self.progress.setRange(0, 0)
        self.progress.setValue(0)

    def _populate_table(self, plan: OrganizePlan, reset_filters: bool = False):
        if reset_filters or self._plan_identity != (plan.root, self._current_plan_seq):
            self._reset_plan_filters()
            self._update_filter_options(plan)
            self._plan_identity = (plan.root, self._current_plan_seq)
        else:
            # 同一计划的条目可能被手动修正改变方向/类型：
            # 刷新筛选项，但保留仍然有效的当前筛选条件。
            self._refresh_filter_options_preserve()
        self._suppress_cell_signals = True
        self.table.setRowCount(len(plan.items))
        for row, item in enumerate(plan.items):
            # ---- 手动修正行：整行浅黄色底 ----
            # 只为手动行创建 QColor/QBrush 并 setBackground；非手动行保持
            # 默认外观。此前对所有行无条件 setBackground(palette().base())
            # 会让每行每列都产生一个 Qt 代理对象，在主线程 GC 与预览
            # 线程并发时放大 shiboken6 mainThreadDeletionHandler 崩溃面。
            manual = item.is_manual_overridden
            row_color = QColor("#FFF9C4") if manual else None
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
            if manual:
                cell_widget.setStyleSheet("background:#FFF9C4;")

            orig = QTableWidgetItem(item.file.rel_path)
            orig.setToolTip(f"源文件：{item.file.abs_path}")
            if manual:
                orig.setBackground(row_color)
                orig.setText("✎ " + item.file.rel_path)
                orig.setToolTip(
                    "手动修正过的文件：\n"
                    f"自动分类：{item.auto_rule_applied}\n"
                    f"当前目标：{item.target_rel_path}\n"
                    f"修正说明：{item.override_note or '(无)'}")
            self.table.setItem(row, 1, orig)

            tgt = QTableWidgetItem(item.target_rel_path)
            if manual:
                tgt.setBackground(row_color)
            if item.conflict_with_existing:
                tgt.setForeground(Qt.red)
                tgt.setToolTip("⚠ 目标磁盘上已存在同名文件，计划已分配唯一名称")
            self.table.setItem(row, 2, tgt)

            reason = QTableWidgetItem(item.rule_applied)
            if manual:
                reason.setBackground(row_color)
            self.table.setItem(row, 3, reason)

            if item.conflict_with_existing:
                status_text = "目标已存在"
            else:
                status_text = "正常"
            st = QTableWidgetItem(status_text)
            if manual:
                st.setBackground(row_color)
            if item.conflict_with_existing:
                st.setForeground(Qt.red)
            self.table.setItem(row, 4, st)

            size_item = QTableWidgetItem(item.file.size_display)
            size_item.setData(Qt.UserRole, item.file.size)
            if manual:
                size_item.setBackground(row_color)
            self.table.setItem(row, 5, size_item)
        self._suppress_cell_signals = False
        self._apply_plan_filters()

    def _on_check_changed(self, row: int, state: int):
        if self._suppress_cell_signals:
            return
        if self.current_plan is None or row >= len(self.current_plan.items):
            return
        self.current_plan.items[row].selected = (state == Qt.Checked)
        self._update_plan_counts()
        self._update_button_states()

    # ============== 预览搜索 / 筛选 / 批量勾选（V1.6） ==============

    @staticmethod
    def _source_kind(item) -> str:
        if item.is_manual_overridden:
            return "manual"
        if (item.rule_key or "").startswith("long_term:"):
            return "long_term"
        return "auto"

    def _update_filter_options(self, plan: OrganizePlan):
        if not hasattr(self, "type_filter"):
            return
        types = sorted({i.file.file_type for i in plan.items if i.file.file_type})
        dirs = sorted({i.direction for i in plan.items if i.direction})
        for combo, values, label in (
                (self.type_filter, types, "全部类型"),
                (self.dir_filter, dirs, "全部方向")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(label, "")
            for v in values:
                combo.addItem(str(v), v)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

    def _refresh_filter_options_preserve(self):
        """刷新类型/方向筛选项；仍有效的当前选择保持不变。"""
        plan = self.current_plan
        if plan is None or not hasattr(self, "type_filter"):
            return
        old_type = self.type_filter.currentData()
        old_dir = self.dir_filter.currentData()
        self._update_filter_options(plan)
        for combo, value in ((self.type_filter, old_type),
                             (self.dir_filter, old_dir)):
            index = combo.findData(value) if value else -1
            combo.blockSignals(True)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _reset_plan_filters(self):
        if not hasattr(self, "plan_search"):
            return
        widgets = (self.plan_search, self.type_filter, self.dir_filter,
                   self.source_filter, self.pending_check, self.conflict_check)
        for w in widgets:
            w.blockSignals(True)
        self.plan_search.clear()
        for combo in (self.type_filter, self.dir_filter, self.source_filter):
            combo.setCurrentIndex(0)
        self.pending_check.setChecked(False)
        self.conflict_check.setChecked(False)
        for w in widgets:
            w.blockSignals(False)
        self._visible_flags = []
        self._plan_identity = None
        if hasattr(self, "plan_counts_label"):
            self.plan_counts_label.setText("")
        self._close_tree_dialog()

    def _clear_plan_filters(self):
        if self._close_pending or self._closing:
            return
        if not hasattr(self, "plan_search"):
            return
        widgets = (self.plan_search, self.type_filter, self.dir_filter,
                   self.source_filter, self.pending_check, self.conflict_check)
        for w in widgets:
            w.blockSignals(True)
        self.plan_search.clear()
        for combo in (self.type_filter, self.dir_filter, self.source_filter):
            combo.setCurrentIndex(0)
        self.pending_check.setChecked(False)
        self.conflict_check.setChecked(False)
        for w in widgets:
            w.blockSignals(False)
        self._apply_plan_filters()

    def _apply_plan_filters(self, *_):
        if not hasattr(self, "plan_search"):
            return
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            self._visible_flags = []
            if hasattr(self, "plan_counts_label"):
                self.plan_counts_label.setText("")
            return
        text = self.plan_search.text().strip().lower()
        ftype = self.type_filter.currentData() or ""
        fdir = self.dir_filter.currentData() or ""
        fsrc = self.source_filter.currentData() or ""
        only_pending = self.pending_check.isChecked()
        only_conflict = self.conflict_check.isChecked()

        flags: list[bool] = []
        for item in plan.items:
            ok = True
            if text and not (
                    text in (item.file.rel_path or "").lower()
                    or text in (item.target_rel_path or "").lower()
                    or text in (item.rule_applied or "").lower()):
                ok = False
            if ok and ftype and item.file.file_type != ftype:
                ok = False
            if ok and fdir and (item.direction or "") != fdir:
                ok = False
            if ok and fsrc and self._source_kind(item) != fsrc:
                ok = False
            if ok and only_pending and (item.direction or "") != "待定":
                ok = False
            if ok and only_conflict and not item.conflict_with_existing:
                ok = False
            flags.append(ok)

        self._visible_flags = flags
        for row, ok in enumerate(flags):
            self.table.setRowHidden(row, not ok)
        self._update_plan_counts()
        self._update_button_states()
        self._schedule_tree_refresh()

    def _update_plan_counts(self):
        if not hasattr(self, "plan_counts_label"):
            return
        plan = self.current_plan
        if plan is None:
            self.plan_counts_label.setText("")
            return
        flags = self._visible_flags
        if len(flags) != plan.total:
            flags = [True] * plan.total
        visible = sum(1 for f in flags if f)
        hidden_selected = sum(
            1 for i, f in enumerate(flags)
            if not f and i < plan.total and plan.items[i].selected)
        self.plan_counts_label.setText(
            f"计划 {plan.total}｜可见 {visible}｜已勾选 {plan.selected_count}"
            f"｜隐藏但仍勾选 {hidden_selected}")

    def _set_visible_checked(self, value, invert: bool = False):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return
        flags = self._visible_flags
        if len(flags) != plan.total:
            flags = [True] * plan.total
        for row, visible in enumerate(flags):
            if not visible:
                continue
            item = plan.items[row]
            new_value = (not item.selected) if invert else bool(value)
            item.selected = new_value
            cell = self.table.cellWidget(row, 0)
            cb = cell.findChild(QCheckBox) if cell is not None else None
            if cb is not None:
                cb.blockSignals(True)
                cb.setChecked(new_value)
                cb.blockSignals(False)
        self._update_plan_counts()
        self._update_button_states()

    def _item_for_table_row(self, row: int):
        """按“当前计划 + 根目录 + 源相对路径”定位条目，不把行号当身份。"""
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return None
        if row < 0 or row >= len(plan.items):
            return None
        item = plan.items[row]
        cell = self.table.item(row, 1)
        shown = (cell.text() if cell is not None else "").replace("✎ ", "")
        if shown != item.file.rel_path:
            return None
        return item

    # ============== 目标目录结构（V1.6） ==============

    def on_show_target_tree(self):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            QMessageBox.information(
                self, "归档盒", "请先生成有效预览后再查看目标目录结构。")
            return
        from target_tree_dialog import TargetTreeDialog
        self._close_tree_dialog()
        flags = self._visible_flags or [True] * plan.total
        dlg = TargetTreeDialog(plan, flags, self)
        dlg.finished.connect(lambda _=0: self._clear_tree_ref(dlg))
        self._tree_dialog = dlg
        dlg.show()

    def _clear_tree_ref(self, dlg):
        if self._tree_dialog is dlg:
            self._tree_dialog = None

    def _close_tree_dialog(self):
        if self._tree_dialog is not None:
            try:
                self._tree_dialog.close()
            except Exception:
                pass
            self._tree_dialog = None

    def _schedule_tree_refresh(self):
        if self._tree_dialog is not None:
            self._tree_refresh_timer.start(300)

    def _on_tree_refresh_timeout(self):
        if self._tree_dialog is not None and self.current_plan is not None:
            try:
                self._tree_dialog.set_visible_flags(self._visible_flags)
            except RuntimeError:
                self._tree_dialog = None

    # ============== 重复文件查看（V1.7） ==============

    def _close_intake_dialog(self):
        if self._intake_dialog is not None:
            try:
                self._intake_dialog.close()
            except Exception:
                pass
            self._intake_dialog = None

    def _invalidate_duplicate_cache(self, close: bool = True):
        self._duplicate_cache = None
        self._duplicate_cache_key = None
        self._duplicate_cache_entries = None
        if close and self._duplicate_dialog is not None:
            try:
                self._duplicate_dialog.close()
            except Exception:
                pass
            self._duplicate_dialog = None

    def _duplicate_cache_key_now(self, root: str):
        return (_norm_root(root), self._scan_generation,
                self._current_plan_seq if self._plan_valid else 0)

    @staticmethod
    def _duplicate_cache_still_valid(entries) -> bool:
        """缓存复用前只做 stat 校验：内容是否变化由大小/修改时间判断。"""
        if not entries:
            return False
        for e in entries:
            try:
                st = os.stat(e.abs_path)
            except OSError:
                return False
            if not os.path.isfile(e.abs_path):
                return False
            if int(st.st_size) != int(e.size):
                return False
            if e.expected_mtime_ns and \
                    int(getattr(st, "st_mtime_ns", 0)) != e.expected_mtime_ns:
                return False
        return True

    def _duplicate_scope_note(self) -> str:
        if self._plan_scope == "intake":
            return (f"待整理清单范围：{len(self.intake)} 项 → "
                    f"扫描纳入 {len(self.scan_result.files) if self.scan_result else 0} 个文件")
        n = len(self.scan_result.files) if self.scan_result else 0
        return f"整个整理空间：当前扫描纳入 {n} 个文件"

    def on_show_duplicates(self):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，请等待任务结束后再查看重复文件。")
            return
        if self.scan_result is None:
            QMessageBox.information(
                self, "归档盒",
                "请先扫描当前整理空间（或待整理清单范围）后再查看重复文件。")
            return
        from duplicate_report import entries_from_plan
        from duplicate_dialog import DuplicateDialog

        root = self.scan_result.root
        note = self._duplicate_scope_note()
        plan = self.current_plan if self._plan_valid else None
        source = "plan" if plan is not None else "scan"
        entries = entries_from_plan(plan, self.scan_result)

        if self._duplicate_dialog is not None:
            try:
                self._duplicate_dialog.close()
            except Exception:
                pass
            self._duplicate_dialog = None
        dlg = DuplicateDialog(root, note, self)
        dlg.cancel_callback = self._cancel_duplicate_check
        dlg.recheck_callback = self._recheck_duplicates
        dlg.finished.connect(lambda _=0: self._on_duplicate_dialog_closed(dlg))
        self._duplicate_dialog = dlg
        dlg.show()

        key = self._duplicate_cache_key_now(root)
        if self._duplicate_cache is not None and self._duplicate_cache_key == key:
            cached_entries = self._duplicate_cache_entries
            if cached_entries and self._duplicate_cache_still_valid(cached_entries):
                dlg.show_report(self._duplicate_cache)
                return
            if cached_entries:
                # 缓存过期：复用条目（未变化项沿用指纹），后台只重核
                # 变化/缺失项；不在界面线程重读内容。
                self._invalidate_duplicate_cache(close=False)
                self._start_duplicate_check(dlg, list(cached_entries),
                                            note, source)
                return
            self._invalidate_duplicate_cache(close=False)
        self._start_duplicate_check(dlg, entries, note, source)

    def _start_duplicate_check(self, dlg, entries, note: str, source: str):
        from duplicate_report import assemble_report
        root = self.scan_result.root if self.scan_result else dlg.root
        if not entries:
            report = assemble_report(root, [], scope_note=note, source=source)
            self._duplicate_cache = report
            self._duplicate_cache_entries = []
            self._duplicate_cache_key = self._duplicate_cache_key_now(root)
            dlg.show_report(report)
            return
        if self._duplicate_worker is not None and self._duplicate_worker.isRunning():
            return
        from duplicate_dialog import DuplicateCheckWorker
        self._duplicate_seq += 1
        seq = self._duplicate_seq
        worker = DuplicateCheckWorker(seq, root, entries, note, source)
        self._duplicate_worker = worker
        self._duplicate_pending_entries = entries
        self._duplicate_pending_key = self._duplicate_cache_key_now(root)
        worker.progress.connect(self._on_duplicate_progress)
        worker.finished_ok.connect(self._on_duplicate_done)
        worker.failed.connect(self._on_duplicate_failed)
        worker.cancelled.connect(self._on_duplicate_cancelled)
        worker.finished.connect(
            lambda w=worker: self._on_duplicate_worker_finished(w))
        self.task.begin("duplicates", root,
                        phase="核对重复文件（读取内容指纹）",
                        cancelable=True, total=len(entries))
        self._set_working(True, f"核对重复文件（{len(entries)} 个）……")
        worker.start()

    def _duplicate_source_ok(self, seq, root) -> bool:
        if self._closing or seq != self._duplicate_seq:
            return False
        cur = self.scan_result.root if self.scan_result else ""
        return bool(cur) and _norm_root(cur) == _norm_root(root)

    def _on_duplicate_progress(self, seq, root, cur, total, path):
        if not self._duplicate_source_ok(seq, root):
            return
        self.task.update_progress(cur, total, path)
        if self._duplicate_dialog is not None:
            self._duplicate_dialog.show_progress(cur, total, path)
        self.statusBar().showMessage(f"核对重复文件… [{cur}/{total}] {path}")

    def _on_duplicate_done(self, seq, root, report):
        if not self._duplicate_source_ok(seq, root):
            return
        self._duplicate_cache = report
        self._duplicate_cache_entries = getattr(
            self._duplicate_worker, "entries", None) or \
            self._duplicate_pending_entries
        self._duplicate_cache_key = getattr(
            self, "_duplicate_pending_key", None) or \
            self._duplicate_cache_key_now(root)
        if self._duplicate_dialog is not None:
            self._duplicate_dialog.show_report(report)
        self.statusBar().showMessage(
            f"重复核对完成：{report.group_count_text}；{report.status_text}")

    def _on_duplicate_failed(self, seq, root, err):
        if not self._duplicate_source_ok(seq, root):
            return
        if self._duplicate_dialog is not None:
            self._duplicate_dialog.show_failure(f"核对失败：{err}")
        self.statusBar().showMessage("核对重复文件失败")

    def _on_duplicate_cancelled(self, seq, root):
        if not self._duplicate_source_ok(seq, root):
            return
        if self._duplicate_dialog is not None:
            self._duplicate_dialog.show_cancelled()
        self.statusBar().showMessage("核对重复文件已取消（未生成结论）")

    def _on_duplicate_worker_finished(self, worker):
        if getattr(worker, "seq", None) != self._duplicate_seq:
            return
        if self._duplicate_worker is worker:
            self._duplicate_worker = None
        self._finish_task("duplicates")

    def _on_duplicate_dialog_closed(self, dlg):
        if self._duplicate_dialog is dlg:
            self._duplicate_dialog = None
        worker = self._duplicate_worker
        if worker is not None and worker.isRunning():
            try:
                worker.request_cancel()
            except Exception:
                pass

    def _cancel_duplicate_check(self):
        worker = self._duplicate_worker
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            self.task.request_cancel()
            if self._duplicate_dialog is not None:
                self._duplicate_dialog.progress_label.setText(
                    "正在取消核对……（当前文件完成后停止）")

    def _recheck_duplicates(self):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            return
        if self._duplicate_dialog is None or self.scan_result is None:
            return
        from duplicate_report import entries_from_plan
        plan = self.current_plan if self._plan_valid else None
        entries = entries_from_plan(plan, self.scan_result)
        for e in entries:
            e.sha256 = ""
            e.status = "pending"
            e.reason = ""
            e.rechecked = False
        self._invalidate_duplicate_cache(close=False)
        self._duplicate_dialog.set_running(True)
        self._start_duplicate_check(
            self._duplicate_dialog, entries, self._duplicate_scope_note(),
            "plan" if plan is not None else "scan")

    def _update_button_states(self):
        blocked = self._close_pending or self._closing
        if blocked:
            # 关闭等待期间不允许重新启用任何可启动业务任务的控件
            self.btn_execute.setEnabled(False)
            self.btn_undo.setEnabled(False)
            self.btn_regenerate.setEnabled(False)
            self.btn_edit_rules.setEnabled(False)
            self.btn_edit_config.setEnabled(False)
            self.btn_edit_protect.setEnabled(False)
            self.btn_override_selected.setEnabled(False)
            self.btn_history.setEnabled(False)
            self.mode_combo.setEnabled(False)
            self.btn_scheme_center.setEnabled(False)
            self.btn_recent.setEnabled(False)
            self.btn_intake.setEnabled(False)
            self.btn_scan_intake.setEnabled(False)
            self.btn_duplicates.setEnabled(False)
            self._set_preview_controls_enabled(False)
            return

        # 历史页面是只读视图，有数据库即可查看；
        # 其内部的撤销/恢复/导出按钮会按任务状态自行禁用
        self.btn_history.setEnabled(self._storage is not None)

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
        # 计划参数 / 保护配置按钮
        idle = not self._working and not self._plan_working
        mode = self.mode_combo.currentData()
        # 配置按钮只在其实际生效的模式下可用，避免“能编辑但当前模式不使用”
        self.btn_edit_rules.setEnabled(idle and mode == MODE_BY_COURSE_THEN_TYPE)
        self.btn_edit_config.setEnabled(
            idle and mode == MODE_BY_DIRECTION_SEMESTER)
        self.btn_edit_protect.setEnabled(idle)
        # 手动修正：有效计划 + 恰好选中一行 + 不忙
        # 本轮只支持推荐模式（其他模式的自动分类快照不含方向/学期/类别，
        # 无法"恢复自动"），与 btn_edit_config 保持同一模式边界。
        selected_rows = {r.row() for r in self.table.selectedIndexes()}
        self.btn_override_selected.setEnabled(
            idle and has_plan
            and plan.mode == MODE_BY_DIRECTION_SEMESTER
            and len(selected_rows) == 1
            and not self.table.isRowHidden(next(iter(selected_rows))))
        self.mode_combo.setEnabled(idle)
        self.btn_scheme_center.setEnabled(
            idle and bool(self.path_edit.text().strip()))
        self.btn_recent.setEnabled(idle)
        self.btn_intake.setEnabled(idle)
        self.btn_scan_intake.setEnabled(
            idle and bool(self.path_edit.text().strip()) and len(self.intake) > 0)
        self.btn_duplicates.setEnabled(idle and self.scan_result is not None)
        self._set_preview_controls_enabled(idle and has_plan)
        # V1.2 第三轮：任务状态变化同步已打开的历史窗口（轻量按钮刷新）
        self._notify_history_gating()

    def _set_preview_controls_enabled(self, enabled: bool):
        for w in (self.plan_search, self.type_filter, self.dir_filter,
                  self.source_filter, self.pending_check,
                  self.conflict_check, self.btn_clear_filters,
                  self.btn_select_visible, self.btn_deselect_visible,
                  self.btn_invert_visible, self.btn_target_tree):
            w.setEnabled(bool(enabled))

    # ============== 执行 & 撤销 ==============

    def _refresh_busy_ui(self, msg: str = ""):
        """根据 _working / _plan_working / 关闭等待 统一刷新进度条、取消按钮、浏览/扫描。

        扫描线程结束与计划线程运行是两件事：扫描结束时只能清掉扫描态，
        不能把仍在运行的计划生成态、取消按钮一起清掉。
        关闭等待期间一律不展示可用的取消按钮，也不重新启用业务控件。
        """
        blocking = self._close_pending or self._closing
        busy = self._working or self._plan_working or blocking
        self.btn_browse.setEnabled(not busy)
        self.btn_scan.setEnabled(bool(not busy and self.path_edit.text()))

        exec_worker = getattr(self, "_exec_worker", None)
        exec_cancelable = (
            not blocking and self._working and not self._plan_working
            and exec_worker is not None and exec_worker.isRunning()
        )
        show_cancel = (not blocking) and (self._plan_working or exec_cancelable)

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
                # 扫描 / 撤销 / 关闭等待等阶段，不展示无效的取消按钮
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
        if self._close_pending or self._closing:
            return  # 关闭等待期间不启动新的整理
        if self._working or self._plan_working:
            return
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

        # 执行前确认（按全部勾选项统计；隐藏但仍勾选的项单独提醒）
        flags = self._visible_flags
        if len(flags) != plan.total:
            flags = [True] * plan.total
        hidden_selected = sum(
            1 for i, f in enumerate(flags)
            if not f and plan.items[i].selected)
        dlg = ExecuteConfirmDialog(plan.root, plan, self,
                                   hidden_selected=hidden_selected,
                                   scope_note=self._plan_scope_note)
        if dlg.exec() != QDialog.Accepted:
            return

        if self._storage is None:
            self._storage = Storage(plan.root)

        self._set_working(True, f"整理中（{len(selected_items)} 项）……")
        self.task.begin(
            "execute", plan.root,
            phase=f"整理中（{len(selected_items)} 项）",
            cancelable=True,
            total=len(selected_items),
        )

        self._exec_worker = ExecuteWorker(plan, plan.root, parent=self)
        self._exec_worker.progress.connect(self._on_execute_progress)
        self._exec_worker.finished_ok.connect(self._on_execute_done)
        self._exec_worker.failed.connect(self._on_execute_failed)
        self._exec_worker.finished.connect(lambda: self._finish_task("execute"))
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.show()
        self._exec_worker.start()

    def _on_execute_progress(self, cur: int, total: int, fname: str):
        """整理进度（V1.2：执行线程逐项汇报）。"""
        if self._closing or not self._working or self.task.kind != "execute":
            return  # 迟到/过期进度，不污染界面
        self.task.update_progress(cur, total, fname)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(cur)
        self.statusBar().showMessage(f"正在整理 [{cur}/{total}] {fname}")

    def on_cancel_execute(self):
        """取消按钮：按界面相位决定取消“生成计划”还是“执行整理”。"""
        # 优先取消计划生成：即使线程已经结束，只要界面仍处于生成相位，
        # 排队中的成功结果也必须被撤销
        if self._plan_working:
            self._cancel_plan_generation()
            return
        # 否则取消执行
        worker = getattr(self, "_exec_worker", None)
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            self.task.request_cancel()
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage("正在取消：将在当前文件操作完成后停止……")

    def _on_execute_done(self, result):
        if self._closing:
            return
        # 文件已移动：旧重复核对结果与目标位置均失效
        self._invalidate_duplicate_cache(close=True)
        # S1-05：执行结束（成功或失败）作废当前计划——不重新扫描生成预览
        # 就不能再执行，避免对同一计划重复执行出「成功 0」的空批次。
        self._discard_plan(
            "整理已结束，本次预览已失效；如需再次整理请重新扫描生成预览")
        # 刷新按钮状态（撤销现在应该可用）
        self._update_button_states()

        if self._close_pending:
            self.statusBar().showMessage(result.summary())
            return  # 关闭等待期间不弹窗

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
        # S1-05：执行异常同样作废当前计划，未重新预览不允许再执行
        self._discard_plan(
            "整理失败，本次预览已失效；请排查后重新扫描生成预览")
        self.statusBar().showMessage("整理失败")
        if self._close_pending or self._closing:
            return
        QMessageBox.critical(self, "归档盒", f"执行整理时发生异常：\n{err}")

    def on_undo(self):
        if self._close_pending or self._closing:
            return  # 关闭等待期间不启动新的撤销
        if self._working or self._plan_working:
            return
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
        self.task.begin("undo", self._storage.root, phase="撤销最近批次")
        self._undo_worker = UndoWorker(self._storage.root, parent=self)
        self._undo_worker.finished_ok.connect(self._on_undo_done)
        self._undo_worker.failed.connect(self._on_undo_failed)
        self._undo_worker.finished.connect(lambda: self._finish_task("undo"))
        self._undo_worker.start()

    def _finish_task(self, kind: str):
        """工作线程真正退出后：结束任务上下文并刷新界面/历史。"""
        self.task.end(kind)
        self._set_working(False)
        self._refresh_history_if_open()

    def _on_undo_done(self, result):
        if self._closing:
            return
        # 撤销后路径变化：旧重复核对结果失效
        self._invalidate_duplicate_cache(close=True)
        self._update_button_states()

        self.statusBar().showMessage(
            f"撤销批次 #{result.batch_id}：{result.summary()}"
        )
        if self._close_pending:
            return  # 关闭等待期间不弹窗

        msg = f"撤销批次 #{result.batch_id}\n\n{result.summary()}"
        if result.errors_list:
            msg += "\n\n冲突/错误详情：\n" + "\n".join(result.errors_list[:20])

        # S1-02/S1-03：撤销被拒绝（批次状态未确定 / 仍有 planned、paused）时，
        # 明确说明原因，不弹「完整撤销成功」。
        if result.refused:
            QMessageBox.warning(self, "归档盒", msg)
        elif result.is_complete:
            QMessageBox.information(self, "归档盒", msg + "\n\n✓ 完整撤销成功！")
        elif result.restored > 0:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 部分撤销完成，有文件因冲突未恢复。")
        else:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 没有任何文件被恢复，不能算完整撤销。")

    def _on_undo_failed(self, err: str):
        self.statusBar().showMessage("撤销失败")
        if self._close_pending or self._closing:
            return
        QMessageBox.critical(self, "归档盒", f"撤销时发生异常：\n{err}")

    # ============== 单文件分类修正（V1.4） ==============

    def on_override_selected(self):
        """对有效计划中所选行打开手动修正对话框。

        门控（按钮禁用时不该被调用；这里再做一次守卫，保证直接调用也安全）：
          - 关闭等待 / 忙碌期间拒绝；
          - 当前有有效计划；
          - 恰好选中一行；
          - 源路径存在于 plan.items 中（防过期/改错对象）。

        对话框取消时不改变计划、磁盘、配置；
        应用到本次计划时：
          - 整批重算唯一名称（避免连续修正后的冲突被漏掉）；
          - 刷新表格（手动修正行高亮、tooltip、目标/依据同步更新）；
          - 状态栏提示"本次计划有 N 个手动修正文件"。

        计划本身仍是有效计划，可继续加勾选/执行；
        重新扫描 / 重新生成 / 配置变化 / 切换根目录时，PlanItem 会
        随 plan 一起被丢弃，修正自然清除——主窗口不需单独维护。
        """
        if self._close_pending or self._closing or self._working or self._plan_working:
            return
        if self.current_plan is None or not self._plan_valid:
            QMessageBox.information(
                self, "归档盒",
                "请先在当前目录生成有效预览后再手动修正文件分类。")
            return
        if self.current_plan.mode != MODE_BY_DIRECTION_SEMESTER:
            QMessageBox.information(
                self, "归档盒",
                "单文件修正当前仅支持「按方向/学期/类别（推荐）」模式。\n"
                "其他模式请使用对应的规则配置入口（编辑课程规则 / 推荐模式配置）。")
            return

        selected_rows = {r.row() for r in self.table.selectedIndexes()}
        if len(selected_rows) != 1:
            QMessageBox.information(
                self, "归档盒",
                "请在预览表格中选中恰好一行后再修正。")
            return
        row = next(iter(selected_rows))
        if self.table.isRowHidden(row):
            return
        item = self._item_for_table_row(row)
        if item is None:
            QMessageBox.warning(
                self, "归档盒",
                "所选行与当前计划不一致（可能已重新筛选或计划已更新），"
                "请重新选择文件后再修正。")
            return
        source_rel = item.file.rel_path

        # 延迟导入：plan_patch 在导入时会新增一批模块/类对象，抬高主线程
        # GC 频率；若此时预览线程刚退出尚未回收，会放大 shiboken6
        # mainThreadDeletionHandler 的崩溃概率。只在用户真正点开修正
        # 对话框时才加载，保证扫描/预览/试运行期间不引入这个负担。
        from plan_patch import (
            ManualOverrideDialog, OverrideValidationError,
            apply_override_to_plan, revert_auto_override,
            summarize_manual_overrides,
        )

        root = self.path_edit.text().strip()
        rule_context = {
            "scan_result": self.scan_result,
            "config": copy.deepcopy(self.classification_config),
            "save_callback": (
                (lambda cfg: self._save_classification_config(root, cfg))
                if root and os.path.isdir(root) else None),
            # 规则试运行在后台线程执行，线程由主窗口登记表保活
            "worker_registry": self,
        }

        try:
            dlg = ManualOverrideDialog(
                self.current_plan, source_rel, mode=self.current_plan.mode,
                rule_context=rule_context)
        except OverrideValidationError as e:
            QMessageBox.warning(self, "归档盒", str(e))
            return

        result = dlg.exec()
        if result != QDialog.Accepted:
            return

        # 用户在修正对话框里选择了「保存为长期规则」并已成功保存：
        # 配置已由 _save_classification_config 写入并使旧计划失效，
        # 这里只补充提示（不会再应用本次的临时修正——计划已作废）。
        if dlg.long_term_saved_config is not None:
            rule = dlg.saved_rule
            name = rule.name if rule is not None else "（未命名）"
            self.invalid_label.setText(
                "⚠ 长期修正规则已保存：旧预览已失效，请点击「重新预览」")
            self.invalid_label.show()
            self.statusBar().showMessage(
                f"已保存长期规则「{name}」。本次临时修正随旧计划一起失效；"
                "请点击「重新预览」按新规则生成计划后再执行。")
            return

        # 用户点了"恢复自动分类"或"应用到本次计划"
        if dlg.revert_on_ok:
            reverted = revert_auto_override(self.current_plan, source_rel)
            if reverted is None:
                QMessageBox.information(
                    self, "归档盒",
                    "该文件没有可用的自动分类快照（可能是旧历史里恢复的），"
                    "无法恢复自动分类。")
                return
            self._populate_table(self.current_plan)
            n, _ = summarize_manual_overrides(self.current_plan)
            self.statusBar().showMessage(
                "已恢复自动分类。"
                + (f"本次计划另有 {n} 个手动修正文件；重新扫描/重新预览后失效。"
                   if n > 0
                   else "本次计划已无手动修正文件。"))
            return

        # 应用手动修正
        try:
            applied = apply_override_to_plan(
                self.current_plan, source_rel, dlg.params)
        except OverrideValidationError as e:
            QMessageBox.warning(self, "归档盒", str(e))
            return

        self._populate_table(self.current_plan)
        n, lines = summarize_manual_overrides(self.current_plan)
        hint = f"本次计划共 {n} 个手动修正文件（仅本次生效）："
        if n <= 5:
            hint += "\n  " + "\n  ".join(lines)
        else:
            hint += f"（详情见历史页面）"
        self.statusBar().showMessage(hint)

    # ============== 操作历史（V1.2） ==============

    def on_history(self):
        """打开完整操作历史页面。

        视图随时可用（忙碌时也可查看）；页面内的撤销/恢复/导出
        按钮会根据任务状态自行禁用，不能从历史启动重叠业务任务。
        """
        if self._closing or self._close_pending:
            return
        if self._storage is None:
            QMessageBox.information(
                self, "归档盒",
                "请先选择整理根目录（并完成扫描）后再查看操作历史。")
            return
        root = self._current_root()
        if (self._history_dialog is None
                or self._history_dialog.root != root):
            if self._history_dialog is not None:
                self._history_dialog.close()
            self._history_dialog = HistoryDialog(self, parent=self)
        else:
            # 复用已创建的窗口：无论当前是否可见都统一重读
            # （批次列表 + 选中批次明细）。V1.2 第二轮修复：
            # 旧实现只调 _reload() 更新批次列表，明细缓存停留在
            # 关闭前的旧状态，且 _on_batch_selected 因 batch_id
            # 相同早退、手动“刷新”也不重读明细。
            self._history_dialog.refresh_all()
        self._history_dialog.show()
        self._history_dialog.raise_()

    def _notify_history_gating(self):
        """任务开始/结束/关闭等待变化时，轻量同步已打开历史窗口的按钮。

        V1.2 第三轮修复：此前历史窗口打开后再启动整理/撤销/核对恢复，
        历史内的撤销/恢复/导出/刷新按钮不会随任务状态禁用（只有重新
        打开历史才恢复正确）。现在 `_update_button_states()` 在每次
        任务状态变化时都会调用这里——只做按钮可用性刷新
        （`refresh_task_gating`），不重载批次与明细，不逐条进度触发。
        """
        d = self._history_dialog
        if d is None or not d.isVisible():
            return
        try:
            d.refresh_task_gating()
        except Exception:
            pass  # 历史按钮刷新失败不影响主窗口任务状态

    def _refresh_history_if_open(self):
        """执行/撤销/恢复完成后，刷新打开中的历史页面。"""
        d = self._history_dialog
        if d is None or not d.isVisible():
            return
        try:
            d.refresh_after_change()
        except Exception:
            pass  # 历史刷新失败不影响主窗口的文件操作状态

    def _do_recover(self, batch_id: int):
        """启动后台核对恢复（启动检查、切换目录检查与历史页共用同一流程）。

        V1.2 第二轮：恢复从主线程同步执行移入后台线程。
        - 复用唯一的 recover_batch 核心逻辑，语义不变：
          只核对磁盘实际状态、补记已确认移动的项、暂停状态不明确的项，
          不自动重放计划、不把文件搬回（搬回是“撤销”的职责）。
        - 工作线程在自身线程内创建并关闭 Storage，不复用主线程连接。
        - 任务携带唯一 seq 与根目录，回调校验来源，迟到结果丢弃。
        - 本阶段恢复不可取消：进度明确显示“正在核对 N/M”，
          不展示无效的取消按钮。
        """
        if self._storage is None:
            return None
        if self._close_pending or self._closing:
            return None   # 关闭等待期间不启动新的业务任务
        if self._working or self._plan_working:
            return None   # 已有任务运行，避免重叠任务

        self._recover_seq += 1
        seq = self._recover_seq
        root = self._storage.root
        self.task.begin("recover", root, phase="核对恢复中……",
                        cancelable=False, total=0)
        self._set_working(True, "正在核对恢复……")
        self._recover_result = None

        self._recover_worker = RecoverWorker(seq, root, int(batch_id),
                                             parent=self)
        self._recover_worker.progress.connect(self._on_recover_progress)
        self._recover_worker.finished_ok.connect(self._on_recover_done)
        self._recover_worker.failed.connect(self._on_recover_failed)
        self._recover_worker.finished.connect(
            lambda: self._finish_task("recover"))
        self._recover_worker.start()
        return self._recover_worker

    def _recover_source_ok(self, seq: int, root: str) -> bool:
        """恢复回调来源校验（V1.2 第三轮）：任务标识 + 所属根目录双重校验。

        - seq 必须等于当前恢复任务标识（丢弃迟到/过期的旧任务结果）；
        - root 必须与当前上下文（当前 Storage 根目录，规范化比较）
          一致——切换根目录后，旧目录任务的回调不会再更新本目录的
          结果、任务状态、进度或状态栏，也不弹窗、不刷新历史；
        - 窗口已进入关闭流程的一律忽略。
        比较使用 _norm_root（abspath + realpath），不用前缀匹配、
        不做大小写变换。
        """
        if self._closing:
            return False
        if seq != self._recover_seq:
            return False
        cur = self._storage.root if self._storage is not None else self.task.root
        if not root or not cur:
            return False
        return _norm_root(root) == _norm_root(cur)

    def _on_recover_progress(self, seq: int, root: str, cur: int, total: int,
                             path: str):
        """核对进度：实际核对数量（待核对动作数）与当前路径。

        来源按 seq + root 双重校验（V1.2 第三轮）。
        """
        if not self._recover_source_ok(seq, root):
            return   # 迟到/过期/其他根目录的进度，不污染界面
        if self.task.kind != "recover":
            return
        self.task.update_progress(cur, total, path)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(cur)
        self.statusBar().showMessage(
            f"正在核对 [{cur}/{total}]（按待核对动作数统计） {path}")

    def _on_recover_done(self, seq: int, root: str, result):
        """核对恢复结果到达（线程可能仍在收尾，真正的退出由
        _finish_task 在线程 finished 信号里处理）。"""
        if not self._recover_source_ok(seq, root):
            return   # 迟到/过期/其他根目录的结果，不更新任何状态
        self._recover_result = result
        self._update_button_states()

        headline = _recover_headline(result)
        self.statusBar().showMessage(f"{headline}：{result.summary()}")
        if self._close_pending:
            return   # 关闭等待期间不弹阻塞退出的业务结果对话框

        msg = f"{headline}\n\n{result.summary()}"
        if result.paused:
            msg += (f"\n\n有 {result.paused} 项状态不明确，已暂停，"
                    "请人工确认文件位置后再撤销。")
        msg += "\n（核对恢复只核对与补记状态，不会移动文件；"
        "把文件放回原位置请使用“撤销”。）"
        QMessageBox.information(self, "归档盒", msg)
        self._refresh_history_if_open()

    def _on_recover_failed(self, seq: int, root: str, err: str):
        """核对恢复异常：保留可复核记录，不误报全部恢复成功。"""
        if not self._recover_source_ok(seq, root):
            return   # 迟到/过期/其他根目录的失败，不弹窗不覆盖状态栏
        self.statusBar().showMessage("核对恢复失败")
        if self._close_pending:
            return
        QMessageBox.critical(self, "归档盒", f"核对恢复时发生异常：\n{err}\n\n"
                            "批次记录保持原状，可稍后重试或人工核对。")

    def recover_interrupted_batch(self, batch_id: int):
        """从历史页发起的中断批次核对恢复（后台执行）。"""
        if self._closing or self._close_pending or self._working or self._plan_working:
            return
        if self._storage is None or batch_id is None:
            return
        self._do_recover(int(batch_id))

    # ============== 关闭时清理 ==============

    def _background_workers(self):
        """收集所有可能正在运行的后台线程（含被新任务顶替但尚未退出的计划线程）。"""
        candidates = [
            getattr(self, "_scan_worker", None),
            getattr(self, "_plan_worker", None),
            getattr(self, "_exec_worker", None),
            getattr(self, "_undo_worker", None),
            getattr(self, "_recover_worker", None),
            getattr(self, "_duplicate_worker", None),
        ]
        candidates.extend(getattr(self, "_plan_workers", []) or [])
        candidates.extend(getattr(self, "_rule_preview_workers", []) or [])
        out, seen = [], set()
        for w in candidates:
            if w is None or id(w) in seen:
                continue
            seen.add(id(w))
            out.append(w)
        return out

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return

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
                # 进入关闭相位：任何迟到的成功结果都不再发布
                self._plan_cancel_seq = self._current_plan_seq
                self._plan_cancel_requested = True
                self._refresh_busy_ui(
                    "后台任务正在结束，窗口将在任务完成且记录安全关闭后自动关闭……"
                )
                self._update_button_states()
                QTimer.singleShot(200, self._retry_close)
            return

        # 所有线程已结束，此时关闭数据库才安全
        self._closing = True
        self._close_pending = False
        if self._history_dialog is not None:
            self._history_dialog.close()
            self._history_dialog = None
        if self._storage is not None:
            try:
                self._storage.close()
            finally:
                self._storage = None
        event.accept()

    def _retry_close(self):
        """等待后台线程全部结束后再真正关闭窗口。"""
        if not self._close_pending or self._closing:
            return
        running = [w for w in self._background_workers() if w.isRunning()]
        if running:
            QTimer.singleShot(200, self._retry_close)
            return
        # 线程已全部结束；此时迟到的回调已被 _close_pending 拦截
        self._close_pending = False
        self.close()


# ======================== 启动日志 / 未捕获异常（S1-11） ========================

def startup_log_path() -> str:
    """免安装包的「启动日志.txt」路径。

    包结构为 <包>/app/main.py + <包>/runtime/…，日志在包根目录，
    与启动器写入的是同一个文件。源码运行时退化为源码目录下的同名文件；
    都不可写时退到系统临时目录，保证异常信息不会完全丢失。
    """
    app_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(app_dir), "启动日志.txt"),
        os.path.join(app_dir, "启动日志.txt"),
    ]
    for path in candidates:
        try:
            parent = os.path.dirname(path)
            if os.path.isdir(parent) and os.access(parent, os.W_OK):
                return path
        except OSError:
            continue
    return os.path.join(os.environ.get("TEMP") or os.getcwd(),
                        "归档盒-启动日志.txt")


def append_startup_log(text: str) -> None:
    """尽力追加一行启动日志；写不进去也不能影响程序运行。"""
    try:
        with open(startup_log_path(), "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except OSError:
        pass


def _install_crash_logging() -> None:
    """把未捕获异常写进启动日志（含时间），再交回默认处理。

    S1-11：启动日志以前是 0 字节，无法区分「没启动」和「启动后异常」。
    pythonw.exe 没有控制台，未捕获异常默认完全不可见，这里落盘到启动日志。
    """
    def _excepthook(exc_type, exc_value, exc_tb):
        append_startup_log(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常：\n"
            + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        append_startup_log(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 线程未捕获异常"
            f"（{getattr(args.thread, 'name', '?')}）：\n"
            + "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


# ======================== 入口 ========================

def main():
    _install_crash_logging()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMainWindow { background: #FAFAFA; }
        QTableWidget {
            background: white;
            color: #212121;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            gridline-color: #EEEEEE;
        }
        /* S1-10：表格背景被刷成白色，必须显式给条目设深色文字；
           深色 Windows 调色板的默认文字是白色，白底白字会让预览行只剩勾选框。 */
        QTableWidget::item {
            color: #212121;
        }
        QTableWidget::item:selected {
            color: #FFFFFF;
            background: #1E88E5;
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
    # S1-11：启动日志不再留 0 字节——正常启动留一行时间戳，
    # 异常时由 excepthook 追加堆栈，两者都能与「启动器根本没运行」区分开。
    append_startup_log(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 归档盒已启动，进入事件循环")
    code = app.exec()
    shutdown_widgets()
    sys.exit(code)


def shutdown_widgets():
    """在 QApplication 仍存活时显式销毁所有顶层窗口。

    避免解释器退出阶段交给 Qt 统一清理控件时出现父子析构顺序问题
    （macOS 上表现为退出期在 QPushButtonWrapper 析构中崩溃）。
    """
    app = QApplication.instance()
    if app is None:
        return
    for w in list(app.topLevelWidgets()):
        try:
            w.close()
            w.deleteLater()
        except RuntimeError:
            pass
    try:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    except Exception:
        pass


# 退出兜底：即使入口未显式调用 shutdown_widgets（例如异常路径或测试脚本），
# 也在解释器最终化前销毁顶层控件，避免 Qt 清理阶段按错误顺序析构控件
# （PySide6 6.12 + Python 3.14 上偶发 SIGSEGV）。atexit 后注册先执行，
# 早于 PySide 自身的 QCoreApplication 清理。
atexit.register(shutdown_widgets)


if __name__ == "__main__":
    main()
