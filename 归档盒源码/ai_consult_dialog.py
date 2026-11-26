"""AI 整理顾问对话框：扫描结构 → 管理建议 → 多轮调整。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
)

import ai_consult
from ai_fallback import AiSettings, AiStop, load_settings
from operation_feedback import install
from ai_consult_store import ConsultSession
from consult_chat_format import render_transcript
from ui_theme import INK, LINE, MUTE, STAMP, WHITE


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
    """非模态：先本地扫描，再联网多轮调整建议；不移动文件。

    role=root 是根目录总管家，单独开窗口。
    role=folder 且 embedded 时嵌在主界面右侧，只调整正在分类的这一个文件夹。
    """

    sidebar_close_requested = Signal()

    def __init__(self, main_window, root: str, parent=None, *,
                 role: str = "root", embedded: bool = False):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.root = os.path.abspath(root)
        self.role = role if role in ("root", "folder") else "root"
        self.folder_name = os.path.basename(self.root.rstrip("\\/")) or self.root
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
        self._messages: list[tuple[str, str]] = []
        if self.role == "folder":
            self.scope_note = (
                f"你只负责正在分类的这个文件夹「{self.folder_name}」以及它里面的文件。"
                "只调整这些文件的分类。"
                "不要建议移动这个文件夹以外的内容。"
            )
            self._box_title = "AI 管家"
            self.setWindowTitle(f"AI 管家 · {self.folder_name}")
        else:
            self.scope_note = (
                f"你是根目录「{self.folder_name}」的总管家，负责整个整理空间。"
                "先看整棵目录怎么分工。"
                "用户没有点名某一个文件夹时，不要把方案收成只改一处。"
            )
            self._box_title = "总管家"
            self.setWindowTitle("总管家 · 根目录")

        self.embedded = bool(embedded) and self.role == "folder"
        self.setModal(False)
        if self.embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._build_sidebar_ui()
        else:
            self.resize(1080, 760)
            self._build_ui()
        if self.embedded:
            pass
        elif self.role == "folder":
            self._append_system(
                f"只调整正在分类的文件夹「{self.folder_name}」。"
                "不会改这个文件夹以外的内容。"
                "步骤：扫描这个文件夹 → 按当前预览调整 → "
                "生成可勾选方案 → 回主界面确认。不会直接移动文件。")
        else:
            self._append_system(
                "你是这个根目录的总管家。"
                "步骤：扫描整个根目录 → 给出分工建议 → 多轮调整 → "
                "生成可勾选方案 → 回主界面确认。不会直接移动文件。"
                "只改正在分类的某一个文件夹时，用预览旁边的「AI 管家」。")
        self.start_scan_and_analyze()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        if self.role == "folder":
            intro_text = (
                f"只调整正在分类的「{self.folder_name}」。"
                "建议不会改这个文件夹以外的内容。"
                "真正移动仍要回主界面，在预览里确认。")
            goal_hint = "可选。例如：实验报告和课件分开，代码先别动"
        else:
            intro_text = (
                "看整个根目录怎么分工，再一起调整。"
                "只改正在分类的那一个文件夹时，用预览旁边的「AI 管家」。"
                "真正移动仍要回主界面预览确认。")
            goal_hint = "可选。例如：整个根目录按学期收，项目目录先别动"
        self.intro = QLabel(intro_text)
        self.intro.setWordWrap(True)
        self.intro.setStyleSheet("color:#475569;")
        outer.addWidget(self.intro)

        goal_row = QHBoxLayout()
        goal_row.addWidget(QLabel("我的目标："))
        self.goal_edit = QLineEdit()
        self.goal_edit.setPlaceholderText(goal_hint)
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
        self.snapshot_view.setStyleSheet(
            f"QPlainTextEdit {{ background:#FFFFFF; color:{INK}; "
            f"border:1px solid {LINE}; border-radius:10px; padding:8px; "
            "font-size:13px; }"
        )
        left.addWidget(self.snapshot_view, stretch=1)
        splitter.addWidget(left_widget)

        right = QVBoxLayout()
        right_widget = self._wrap_vbox(right)
        right_title = QLabel("与 AI 的调整对话")
        right_title.setStyleSheet(f"font-weight:600;color:{INK};")
        right.addWidget(right_title)
        right_hint = QLabel("你的话在右侧。顾问按块回复，需要你拍板的问题会单独标出。")
        right_hint.setWordWrap(True)
        right_hint.setStyleSheet(f"color:{MUTE};font-size:12px;")
        right.addWidget(right_hint)
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_view.setStyleSheet(
            f"QTextBrowser {{ background:#FFFFFF; color:{INK}; "
            f"border:1px solid {LINE}; border-radius:10px; padding:12px 14px; "
            "font-size:14px; }"
        )
        self.chat_view.document().setDocumentMargin(8)
        right.addWidget(self.chat_view, stretch=1)

        composer = QFrame()
        composer.setObjectName("consultComposer")
        composer.setStyleSheet(
            "QFrame#consultComposer {"
            f"background:#FFFFFF; border:1px solid {LINE}; border-radius:18px;"
            "}"
        )
        input_row = QHBoxLayout(composer)
        input_row.setContentsMargins(12, 4, 4, 4)
        input_row.setSpacing(6)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText(
            "继续调整，例如：实验报告和代码放一起；先不要动大二下")
        self.input_edit.setStyleSheet(
            "QLineEdit { border:none; background:transparent;"
            f"padding:6px 4px; font-size:14px; color:{INK}; }}"
        )
        self.input_edit.returnPressed.connect(self.send_message)
        input_row.addWidget(self.input_edit, stretch=1)
        self.btn_send = QPushButton("发送")
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setToolTip("发送这轮调整")
        self.btn_send.setStyleSheet(
            "QPushButton {"
            f"background:{STAMP}; color:{WHITE}; border:none; border-radius:15px;"
            "padding:6px 16px; min-height:32px; font-weight:650;"
            "}"
            "QPushButton:hover { background:#1B466C; }"
            "QPushButton:disabled { background:#D6DEE6; color:#6B7280; }"
        )
        self.btn_send.clicked.connect(self.send_message)
        input_row.addWidget(self.btn_send)
        right.addWidget(composer)
        splitter.addWidget(right_widget)
        splitter.setSizes([340, 680])
        outer.addWidget(splitter, stretch=1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#64748B;font-size:12px;")
        outer.addWidget(self.status)

        bottom = QHBoxLayout()
        self.btn_quick = QPushButton("一句话快速查找…")
        if self.role == "folder":
            self.btn_quick.setToolTip("只在这个文件夹里做一次查找，并生成方案")
        else:
            self.btn_quick.setToolTip("在整个根目录里做一次查找，并生成方案")
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
            "只把这个文件夹的调整收成可勾选预览；不会立刻移动文件。"
            if self.role == "folder"
            else "根据整个根目录的摘要与对话，生成可勾选预览；不会立刻移动文件。")
        self.btn_make_plan.clicked.connect(self.generate_preview_plan)
        bottom.addWidget(self.btn_make_plan)
        outer.addLayout(bottom)
        self.feedback = install(outer, self.status)

    def _sidebar_greeting(self) -> str:
        return (
            f"你好，我是「{self.folder_name}」的 AI 管家。\n"
            "左边仍是这个文件夹的分类预览，你可以直接说要改哪里。"
            "我只调整这一个文件夹，也不会直接移动文件。"
        )

    def _build_sidebar_ui(self):
        """右侧滑出的窄栏：对照分类预览聊天，不再另开整页窗口。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        self.setObjectName("folderAiSidebar")
        self.setStyleSheet(f"QDialog#folderAiSidebar {{ background:{WHITE}; }}")

        header = QHBoxLayout()
        header.setSpacing(8)
        mark = QLabel("AI")
        mark.setFixedSize(28, 28)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(
            f"background:{STAMP}; color:{WHITE}; border-radius:14px;"
            "font-size:11px; font-weight:700;"
        )
        header.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("AI 管家")
        title.setStyleSheet(f"color:{INK}; font-size:15px; font-weight:700;")
        subtitle = QLabel(self.folder_name)
        subtitle.setStyleSheet(f"color:{MUTE}; font-size:12px;")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, stretch=1)
        self.btn_restart = QPushButton("新对话")
        self.btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restart.setToolTip("重新看这个文件夹，并开始一轮新对话")
        self.btn_restart.clicked.connect(self.start_scan_and_analyze)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setToolTip("收起右侧对话，分类预览留在左边")
        self.btn_close.clicked.connect(self.sidebar_close_requested.emit)
        flat = (
            "QPushButton { background:transparent; color:#4A5560; border:none;"
            "padding:4px 8px; min-height:28px; font-weight:500; }"
            "QPushButton:hover { background:#EEF1F4; border-radius:8px; }"
            "QPushButton:disabled { color:#94A3B8; }"
        )
        self.btn_restart.setStyleSheet(flat)
        self.btn_close.setStyleSheet(flat)
        header.addWidget(self.btn_restart)
        header.addWidget(self.btn_close)
        outer.addLayout(header)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_view.setStyleSheet(
            f"QTextBrowser {{ background:{WHITE}; color:{INK};"
            "border:none; padding:4px 0; font-size:14px; }"
        )
        self.chat_view.document().setDocumentMargin(4)
        outer.addWidget(self.chat_view, stretch=1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{MUTE}; font-size:12px;")
        outer.addWidget(self.status)

        composer = QFrame()
        composer.setObjectName("folderAiComposer")
        composer.setStyleSheet(
            "QFrame#folderAiComposer {"
            f"background:{WHITE}; border:1px solid {LINE}; border-radius:22px;"
            "}"
        )
        input_row = QHBoxLayout(composer)
        input_row.setContentsMargins(14, 4, 4, 4)
        input_row.setSpacing(6)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("说说这个文件夹要怎么分…")
        self.input_edit.setStyleSheet(
            "QLineEdit { border:none; background:transparent;"
            f"padding:6px 2px; font-size:14px; color:{INK}; }}"
        )
        self.input_edit.returnPressed.connect(self.send_message)
        input_row.addWidget(self.input_edit, stretch=1)
        self.btn_send = QPushButton("↑")
        self.btn_send.setFixedSize(36, 36)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setToolTip("发送")
        self.btn_send.setStyleSheet(
            "QPushButton {"
            f"background:{STAMP}; color:{WHITE}; border:none; border-radius:18px;"
            "padding:0; min-width:36px; max-width:36px; min-height:36px; max-height:36px;"
            "font-size:16px; font-weight:700;"
            "}"
            "QPushButton:hover { background:#1B466C; }"
            "QPushButton:disabled { background:#D6DEE6; color:#6B7280; }"
        )
        self.btn_send.clicked.connect(self._on_send_clicked)
        input_row.addWidget(self.btn_send)
        outer.addWidget(composer)

        self.btn_make_plan = QPushButton("生成预览，放到左边")
        self.btn_make_plan.setEnabled(False)
        self.btn_make_plan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_make_plan.setToolTip(
            "按这轮对话生成可勾选预览，显示在左边。不会立刻移动文件。")
        self.btn_make_plan.clicked.connect(self.generate_preview_plan)
        outer.addWidget(self.btn_make_plan)

        self.goal_edit = QLineEdit()
        self.goal_edit.hide()
        self.btn_cancel_req = QPushButton()
        self.btn_cancel_req.hide()
        self.feedback = None

    def _on_send_clicked(self):
        if self._busy:
            self._cancel_workers()
            self.status.setText("正在停止…")
            return
        self.send_message()

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
            QMessageBox.warning(self, self._box_title, "整理空间不存在。")
            return
        settings = self._settings()
        if not settings.ready():
            if hasattr(self.main_window, "on_ai_settings"):
                self.main_window.on_ai_settings()
                settings = self._settings()
            if not settings.ready():
                if self.embedded and not self._messages:
                    self._append_assistant(self._sidebar_greeting())
                self._append_system("还没有配置 AI 接口。请先点「配置 AI 接口」。")
                self.status.setText("未配置 AI，仅可查看本地扫描摘要。")
                self._start_scan_only()
                return
        self.session = None
        self._messages.clear()
        if self.embedded:
            self._append_assistant(self._sidebar_greeting())
        else:
            place = "正在分类的文件夹" if self.role == "folder" else "根目录"
            self._append_system(
                f"{place}：{self.root}\n先本地扫描，再请 {settings.provider_label()} 给建议。")
        self._set_busy(True, "正在看这个文件夹…" if self.embedded else "正在扫描目录结构…")
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
        view = getattr(self, "snapshot_view", None)
        if view is not None:
            view.setPlainText(snapshot.text)
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
        QMessageBox.warning(self, self._box_title, f"扫描失败：\n{message}")

    def send_message(self):
        if self._busy:
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        if self.session is None or self.snapshot is None:
            self.status.setText(
                "还在看这个文件夹，看完就可以说要怎么改。"
                if self.embedded else "请先完成「重新扫描并分析」。")
            return
        if not self._settings().ready():
            QMessageBox.information(self, self._box_title, "请先配置 AI 接口。")
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
            self._goal_for_model(),
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
        self._busy = False
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
        QMessageBox.warning(self, self._box_title, f"请求失败：\n{message}")

    def generate_preview_plan(self):
        if self._busy:
            return
        if self.snapshot is None:
            self.status.setText("请先完成扫描与分析。")
            return
        if not self._settings().ready():
            QMessageBox.information(self, self._box_title, "请先配置 AI 接口。")
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
            self._goal_for_model(), protect_dirs=protect, parent=self)
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
        if self.role == "folder":
            note = f"这个文件夹「{self.folder_name}」：{result.summary}"
        else:
            note = f"总管家：{result.summary}"
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
        if self.embedded:
            self.status.setText("已放到左边的预览。核对勾选后，再点「开始整理」。")
            return
        self.status.setText("已放入主界面预览，请核对勾选后点「开始整理」。")
        # 等 PlanWorker.finished 后再关闭，避免 closeEvent 等待仍在列表中的自己。
        self._pending_accept_after_plan = True

    def _on_plan_failed(self, message: str):
        if self._closing:
            return
        self._set_busy(False)
        self.status.setText(f"生成预览失败：{message}")
        self._append_system(f"生成预览失败：{message}")
        QMessageBox.warning(self, self._box_title, f"生成预览失败：\n{message}")

    # ---------- UI helpers ----------

    def _goal_for_model(self) -> str:
        """把角色边界放进目标，不改用户在输入框里写的那一句。"""
        parts = [self.scope_note]
        if self.role == "folder":
            getter = getattr(self.main_window, "classification_digest", None)
            digest = getter() if callable(getter) else ""
            if digest:
                parts.append(digest)
        typed = self.goal_edit.text().strip()
        if typed:
            parts.append(typed)
        return "\n".join(parts)

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
        if self.embedded:
            self.btn_send.setEnabled(True)
            self.btn_send.setText("■" if busy else "↑")
            self.btn_send.setToolTip("停止" if busy else "发送")
        if message:
            self.status.setText(message)
        if self.embedded and hasattr(self, "chat_view"):
            self._render_chat()

    def _on_progress(self, message: str):
        if not self._closing:
            self.status.setText(message)

    def _append_system(self, text: str):
        self._append_message("system", text)

    def _append_user(self, text: str):
        self._append_message("user", text)

    def _append_assistant(self, text: str):
        self._append_message("assistant", text)

    def _append_message(self, role: str, text: str):
        self._messages.append((role, text))
        self._render_chat()

    def _render_chat(self):
        messages = list(self._messages)
        if self.embedded and self._busy:
            messages.append(("thinking", "AI 思考中…"))
        self.chat_view.setHtml(render_transcript(messages))
        self.chat_view.moveCursor(QTextCursor.MoveOperation.End)
        bar = self.chat_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _open_quick_agent(self):
        if hasattr(self.main_window, "on_agent_quick"):
            self.main_window.on_agent_quick(self.root)

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
