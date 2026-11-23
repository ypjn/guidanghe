"""AI 整理顾问对话框：扫描结构 → 管理建议 → 多轮调整。"""

from __future__ import annotations

import html
import os
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
)

import ai_consult
from ai_fallback import AiSettings, AiStop, load_settings
from ai_consult_store import ConsultSession


class ScanWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            snap = ai_consult.summarize_space(
                self.root,
                cancel_event=self.cancel_event,
                progress_cb=lambda msg: self.progress.emit(msg),
            )
            if not self.cancel_event.is_set():
                self.done.emit(snap)
        except Exception as exc:  # noqa: BLE001
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))


class ChatWorker(QThread):
    done = Signal(object, str)  # session, reply
    failed = Signal(str)

    def __init__(self, settings: AiSettings, snapshot, goal: str,
                 session: ConsultSession | None, user_text: str = "",
                 parent=None):
        super().__init__(parent)
        self.settings = settings
        self.snapshot = snapshot
        self.goal = goal
        self.session = session
        self.user_text = user_text
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            if self.session is None:
                session, reply = ai_consult.start_consult_session(
                    self.settings, self.snapshot, goal=self.goal,
                    cancel_event=self.cancel_event)
            else:
                session, reply = ai_consult.continue_consult_session(
                    self.settings, self.session, self.user_text,
                    cancel_event=self.cancel_event)
            if not self.cancel_event.is_set():
                self.done.emit(session, reply)
        except AiStop as exc:
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))


class PlanWorker(QThread):
    done = Signal(object)  # ConsultPlanResult
    failed = Signal(str)

    def __init__(self, settings: AiSettings, snapshot, session, goal: str,
                 protect_dirs=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.snapshot = snapshot
        self.session = session
        self.goal = goal
        self.protect_dirs = list(protect_dirs or [])
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = ai_consult.build_preview_plan_from_consult(
                self.settings, self.snapshot, self.session,
                goal=self.goal, protect_dirs=self.protect_dirs,
                cancel_event=self.cancel_event)
            if not self.cancel_event.is_set():
                self.done.emit(result)
        except AiStop as exc:
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))


class AiConsultDialog(QDialog):
    """非模态：先本地扫描，再联网多轮调整建议；不移动文件。"""

    def __init__(self, main_window, root: str, parent=None):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.root = os.path.abspath(root)
        self.snapshot: ai_consult.SpaceSnapshot | None = None
        self.session: ConsultSession | None = None
        self._scan_worker: ScanWorker | None = None
        self._chat_worker: ChatWorker | None = None
        self._plan_worker: PlanWorker | None = None
        self._workers: list[QThread] = []
        self._closing = False
        self._pending_close = False
        self._busy = False
        self._preview_ready = False
        self._pending_accept_after_plan = False

        self.setWindowTitle("AI 整理顾问 · 扫描后一起调整")
        self.resize(980, 720)
        self.setModal(False)
        self._build_ui()
        self._append_system(
            "步骤：1）本地扫描目录结构  2）AI 给出管理建议  "
            "3）你用对话调整  4）生成可勾选预览方案  5）回主界面确认后整理。"
            "顾问不会直接移动文件。")
        self.start_scan_and_analyze()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        intro = QLabel(
            "先看清你的存储结构，再给你管理建议；你可以继续说「按学期」「只收拾下载」"
            "或「先别动项目目录」来调整。真正移动仍要回主界面预览确认。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#475569;")
        outer.addWidget(intro)

        goal_row = QHBoxLayout()
        goal_row.addWidget(QLabel("我的目标："))
        self.goal_edit = QLineEdit()
        self.goal_edit.setPlaceholderText(
            "可选。例如：按大二上课程归类，下载区先收拢，项目目录不要动")
        goal_row.addWidget(self.goal_edit, stretch=1)
        self.btn_restart = QPushButton("重新扫描并分析")
        self.btn_restart.clicked.connect(self.start_scan_and_analyze)
        goal_row.addWidget(self.btn_restart)
        outer.addLayout(goal_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QVBoxLayout()
        left_widget = self._wrap_vbox(left)
        left_title = QLabel("本地结构摘要")
        left_title.setStyleSheet("font-weight:600;color:#0F172A;")
        left.addWidget(left_title)
        self.snapshot_view = QPlainTextEdit()
        self.snapshot_view.setReadOnly(True)
        self.snapshot_view.setPlaceholderText("扫描后会显示目录骨架与类型统计…")
        left.addWidget(self.snapshot_view, stretch=1)
        splitter.addWidget(left_widget)

        right = QVBoxLayout()
        right_widget = self._wrap_vbox(right)
        right_title = QLabel("与 AI 的调整对话")
        right_title.setStyleSheet("font-weight:600;color:#0F172A;")
        right.addWidget(right_title)
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        right.addWidget(self.chat_view, stretch=1)

        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText(
            "继续调整，例如：实验报告和代码放一起；先不要动大二下")
        self.input_edit.returnPressed.connect(self.send_message)
        input_row.addWidget(self.input_edit, stretch=1)
        self.btn_send = QPushButton("发送调整")
        self.btn_send.setProperty("role", "primary")
        self.btn_send.clicked.connect(self.send_message)
        input_row.addWidget(self.btn_send)
        right.addLayout(input_row)
        splitter.addWidget(right_widget)
        splitter.setSizes([380, 580])
        outer.addWidget(splitter, stretch=1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#64748B;font-size:12px;")
        outer.addWidget(self.status)

        bottom = QHBoxLayout()
        self.btn_quick = QPushButton("一句话快速查找…")
        self.btn_quick.setToolTip("打开原来的本地 AI 管家，做单次查找整理方案")
        self.btn_quick.clicked.connect(self._open_quick_agent)
        bottom.addWidget(self.btn_quick)
        bottom.addStretch(1)
        self.btn_cancel_req = QPushButton("取消当前请求")
        self.btn_cancel_req.setEnabled(False)
        self.btn_cancel_req.clicked.connect(self._cancel_workers)
        bottom.addWidget(self.btn_cancel_req)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        bottom.addWidget(self.btn_close)
        self.btn_make_plan = QPushButton("生成可勾选预览方案")
        self.btn_make_plan.setProperty("role", "primary")
        self.btn_make_plan.setEnabled(False)
        self.btn_make_plan.setToolTip(
            "根据结构摘要与对话，生成可在主界面勾选的整理预览；不会立刻移动文件。")
        self.btn_make_plan.clicked.connect(self.generate_preview_plan)
        bottom.addWidget(self.btn_make_plan)
        outer.addLayout(bottom)

    @staticmethod
    def _wrap_vbox(layout: QVBoxLayout):
        from PySide6.QtWidgets import QWidget
        widget = QWidget()
        widget.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return widget

    # ---------- 流程 ----------

    def start_scan_and_analyze(self):
        if self._busy:
            return
        if not os.path.isdir(self.root):
            QMessageBox.warning(self, "AI 整理顾问", "整理空间不存在。")
            return
        settings = self._settings()
        if not settings.ready():
            if hasattr(self.main_window, "on_ai_settings"):
                self.main_window.on_ai_settings()
                settings = self._settings()
            if not settings.ready():
                self._append_system("还没有配置 AI 接口。请先点「配置 AI 接口」。")
                self.status.setText("未配置 AI，仅可查看本地扫描摘要。")
                self._start_scan_only()
                return
        self.session = None
        self.chat_view.clear()
        self._append_system(
            f"整理空间：{self.root}\n先本地扫描，再请 {settings.provider_label()} 给建议。")
        self._set_busy(True, "正在扫描目录结构…")
        self._start_scan(then_analyze=True)

    def _start_scan_only(self):
        self._set_busy(True, "正在扫描目录结构…")
        self._start_scan(then_analyze=False)

    def _start_scan(self, *, then_analyze: bool):
        self._cancel_scan_worker()
        worker = ScanWorker(self.root, self)
        self._scan_worker = worker
        self._workers.append(worker)
        worker.progress.connect(self._on_progress)
        worker.done.connect(
            lambda snap, analyze=then_analyze: self._on_scan_done(snap, analyze))
        worker.failed.connect(self._on_scan_failed)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.start()

    def _on_scan_done(self, snapshot, then_analyze: bool):
        if self._closing:
            return
        self.snapshot = snapshot
        self.snapshot_view.setPlainText(snapshot.text)
        self.status.setText(
            f"扫描完成：约 {snapshot.file_count} 个文件，"
            f"{snapshot.dir_count} 个目录。")
        if not then_analyze:
            self._set_busy(False)
            return
        self._set_busy(True, "正在生成管理建议…")
        self._start_chat(initial=True)

    def _on_scan_failed(self, message: str):
        if self._closing:
            return
        self._set_busy(False)
        self.status.setText(f"扫描失败：{message}")
        QMessageBox.warning(self, "AI 整理顾问", f"扫描失败：\n{message}")

    def send_message(self):
        if self._busy:
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        if self.session is None or self.snapshot is None:
            self.status.setText("请先完成「重新扫描并分析」。")
            return
        if not self._settings().ready():
            QMessageBox.information(self, "AI 整理顾问", "请先配置 AI 接口。")
            return
        self.input_edit.clear()
        self._append_user(text)
        self._set_busy(True, "正在根据你的要求调整建议…")
        self._start_chat(initial=False, user_text=text)

    def _start_chat(self, *, initial: bool, user_text: str = ""):
        self._cancel_chat_worker()
        worker = ChatWorker(
            self._settings(),
            self.snapshot,
            self.goal_edit.text().strip(),
            None if initial else self.session,
            user_text=user_text,
            parent=self,
        )
        self._chat_worker = worker
        self._workers.append(worker)
        worker.done.connect(self._on_chat_done)
        worker.failed.connect(self._on_chat_failed)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.start()

    def _on_chat_done(self, session: ConsultSession, reply: str):
        if self._closing:
            return
        self.session = session
        self._append_assistant(reply)
        self._set_busy(False)
        self.btn_make_plan.setEnabled(True)
        self.status.setText(
            f"会话已保存（{session.id}）。可继续调整，或生成可勾选预览方案。")

    def _on_chat_failed(self, message: str):
        if self._closing:
            return
        self._set_busy(False)
        self.status.setText(f"AI 请求失败：{message}")
        self._append_system(f"请求失败：{message}")
        QMessageBox.warning(self, "AI 整理顾问", f"请求失败：\n{message}")

    def generate_preview_plan(self):
        if self._busy:
            return
        if self.snapshot is None:
            self.status.setText("请先完成扫描与分析。")
            return
        if not self._settings().ready():
            QMessageBox.information(self, "AI 整理顾问", "请先配置 AI 接口。")
            return
        protect = []
        if hasattr(self.main_window, "manual_protect"):
            import main as main_mod
            from protect_config import manual_scan_specs
            protect = [(main_mod.DEFAULT_PROTECT_APP_SRC, "default",
                        "默认保护（归档盒源码）")]
            protect.extend(manual_scan_specs(
                self.root, self.main_window.manual_protect))
        self._set_busy(True, "正在把顾问建议收成可勾选预览方案…")
        self._cancel_plan_worker()
        worker = PlanWorker(
            self._settings(), self.snapshot, self.session,
            self.goal_edit.text().strip(), protect_dirs=protect, parent=self)
        self._plan_worker = worker
        self._workers.append(worker)
        worker.done.connect(self._on_plan_done)
        worker.failed.connect(self._on_plan_failed)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.start()

    def _on_plan_done(self, result):
        if self._closing:
            return
        self._set_busy(False)
        note = f"AI 顾问预览：{result.summary}"
        ok = self.main_window.adopt_agent_plan(
            result.plan, note, scan_result=result.scan)
        if not ok:
            self.status.setText("方案已生成，但未能放入主界面预览。")
            return
        self._preview_ready = True
        detail = note
        if result.rejected:
            detail += f"\n未采纳 {len(result.rejected)} 条（示例：{result.rejected[0]}）"
        self._append_system(detail)
        self.status.setText("已放入主界面预览，请核对勾选后点「开始整理」。")
        # 等 PlanWorker.finished 后再关闭，避免 closeEvent 等待仍在列表中的自己。
        self._pending_accept_after_plan = True

    def _on_plan_failed(self, message: str):
        if self._closing:
            return
        self._set_busy(False)
        self.status.setText(f"生成预览失败：{message}")
        self._append_system(f"生成预览失败：{message}")
        QMessageBox.warning(self, "AI 整理顾问", f"生成预览失败：\n{message}")

    # ---------- UI helpers ----------

    def _settings(self) -> AiSettings:
        if hasattr(self.main_window, "_ai_settings"):
            return self.main_window._ai_settings
        return load_settings()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        self.btn_restart.setEnabled(not busy)
        self.btn_send.setEnabled(not busy)
        self.input_edit.setEnabled(not busy)
        self.goal_edit.setEnabled(not busy)
        self.btn_make_plan.setEnabled(
            (not busy) and self.snapshot is not None and (
                self.session is not None or self._preview_ready))
        self.btn_cancel_req.setEnabled(busy)
        if message:
            self.status.setText(message)

    def _on_progress(self, message: str):
        if not self._closing:
            self.status.setText(message)

    def _append_system(self, text: str):
        self._append_html("系统", text, "#64748B")

    def _append_user(self, text: str):
        self._append_html("我", text, "#1D4ED8")

    def _append_assistant(self, text: str):
        self._append_html("顾问", text, "#0F172A")

    def _append_html(self, who: str, text: str, color: str):
        safe = html.escape(text).replace("\n", "<br>")
        self.chat_view.append(
            f'<p style="margin:8px 0;"><b style="color:{color};">{who}</b><br>{safe}</p>')
        self.chat_view.moveCursor(QTextCursor.MoveOperation.End)

    def _open_quick_agent(self):
        if hasattr(self.main_window, "on_agent_quick"):
            self.main_window.on_agent_quick()

    def _cancel_scan_worker(self):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.request_cancel()

    def _cancel_chat_worker(self):
        if self._chat_worker is not None and self._chat_worker.isRunning():
            self._chat_worker.request_cancel()

    def _cancel_plan_worker(self):
        if self._plan_worker is not None and self._plan_worker.isRunning():
            self._plan_worker.request_cancel()

    def _cancel_workers(self):
        self._cancel_scan_worker()
        self._cancel_chat_worker()
        self._cancel_plan_worker()
        self.status.setText("正在取消…")

    def running_workers(self):
        return [w for w in self._workers if w.isRunning()]

    def _on_worker_finished(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
        if worker is self._scan_worker:
            self._scan_worker = None
        if worker is self._chat_worker:
            self._chat_worker = None
        if worker is self._plan_worker:
            self._plan_worker = None
        if self._pending_accept_after_plan and not self.running_workers():
            self._pending_accept_after_plan = False
            self._closing = True
            QDialog.accept(self)
            return
        if not self._workers and self._pending_close:
            self._pending_close = False
            self._closing = True
            QDialog.reject(self)
            self.deleteLater()

    def reject(self):
        if self.running_workers():
            self._pending_close = True
            self._cancel_workers()
            self.status.setText("等待后台任务结束后关闭…")
            return
        self._closing = True
        super().reject()

    def closeEvent(self, event):
        if self.running_workers():
            event.ignore()
            self.reject()
            return
        self._closing = True
        super().closeEvent(event)
