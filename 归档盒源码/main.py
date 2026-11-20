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
import base64
import copy
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QCoreApplication, QEvent, QItemSelectionModel,
    QRect, QPoint, QSize,
)
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLayout,
    QPushButton, QLineEdit, QTableWidget, QTableWidgetItem,
    QLabel, QHeaderView, QFileDialog, QStatusBar, QProgressBar,
    QMessageBox, QAbstractItemView, QComboBox, QTextEdit,
    QDialog, QDialogButtonBox, QCheckBox, QInputDialog, QListWidget,
    QScrollArea, QFrame, QSizePolicy, QSplitter,
)

from scanner import scan_directory, scan_paths, ScanResult
from planner import (
    generate_plan, OrganizePlan, CourseRule, MODE_BY_TYPE,
    MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER,
    MODE_LABELS, ALL_MODES,
    DEFAULT_COURSE_RULES, parse_rules_text, rules_to_text,
    ocr_maps_from_plan,
    resolve_ambiguity, skip_plan_item,
)
from ocr_engine import OcrError, collect_image_text, engine_label, ocr_supported
from content_extract import extract_contents
import scan_cache
from ai_fallback import AiSettings, apply_ai_fallback, load_settings, save_settings
from classification_config import (
    ClassificationConfig, ClassificationConfigError, config_summary,
    default_config, load_config, save_config, config_path,
)
from preferences import (
    Preference, PreferenceError,
    apply_preferences_to_plan, effective_preferences_resilient,
    ensure_migrated, list_all_preferences_resilient,
    persist_preferences, save_preference,
    sync_space_preferences_from_config,
)
from config_dialog import ClassificationConfigDialog
from protect_config import (
    KIND_LABELS, ProtectConfigError, dedupe_manual_paths,
    load_manual_protect_report, manual_scan_specs,
    normalize_manual_path, save_manual_protect,
)
from storage import Storage
from storage_tree import StorageTree
from duplicate_report import csv_safe
from config_transfer import create_backup, restore_latest_backup
from intake_model import IntakeList, is_within, normalize_path
import recent_spaces
import learning
from executor import (
    execute_plan, undo_latest_batch, undo_is_available,
    recover_batch, interrupted_batch_id, acknowledge_batch_handled,
)

# 归档盒自身源码目录 —— 默认保护，防止程序移动自己
DEFAULT_PROTECT_APP_SRC = os.path.dirname(os.path.abspath(__file__))

# S3 / lead L-07：对外版本号统一为 V1.7（窗口标题、关于、使用说明）
APP_VERSION = "V1.7"
APP_TITLE = f"归档盒 {APP_VERSION}"


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

def _execute_headline(result) -> str:
    """整理完成弹窗标题（S3 / guia F-09：没有移动文件时不能说「整理完成」）。"""
    if getattr(result, "was_cancelled", False):
        return "整理已取消"
    if getattr(result, "moved", 0) > 0:
        return "整理完成！"
    return "没有移动文件"


def _worker_running(worker) -> bool:
    """安全判断后台线程是否在运行（兼容测试里的假 worker）。"""
    if worker is None:
        return False
    try:
        return bool(worker.isRunning())
    except Exception:
        return False


def _describe_exception(e: BaseException) -> str:
    """异常的可读文本：str(e) 为空时至少给出类型名（S3-30）。"""
    text = str(e).strip()
    name = type(e).__name__
    return f"{name}: {text}" if text else name


def _log_worker_failure(task: str, e: BaseException) -> None:
    """后台任务失败：把类型、消息与堆栈追加到启动日志（S3-23/S3-30）。"""
    append_startup_log(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {task}失败："
        f"{_describe_exception(e)}\n"
        + "".join(traceback.format_exception(type(e), e, e.__traceback__)))


class ScanWorker(QThread):
    """后台扫描线程。进度信号带 seq，旧扫描不能写入新扫描的界面状态。"""
    progress = Signal(int, int, str)        # seq, 已发现文件数, 当前相对路径
    stage = Signal(int, str)                # seq, 阶段说明
    finished_ok = Signal(int, object)       # seq, ScanResult
    failed = Signal(int, str)               # seq, error

    def __init__(self, root_path: str, protect_dirs=None, only_paths=None,
                 parent=None, seq: int = 0):
        super().__init__(parent)
        self.root_path = root_path
        self.protect_dirs = protect_dirs or []
        self.only_paths = list(only_paths or [])
        self.seq = seq
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def _progress(self, seen: int, current: str):
        self.progress.emit(self.seq, seen, current)

    def run(self):
        try:
            self.stage.emit(self.seq, "扫描工作区")
            if self.only_paths:
                r = scan_paths(self.root_path, self.only_paths,
                               protect_dirs=self.protect_dirs,
                               cancel_event=self.cancel_event,
                               progress_cb=self._progress)
            else:
                r = scan_directory(self.root_path, protect_dirs=self.protect_dirs,
                                   cancel_event=self.cancel_event,
                                   progress_cb=self._progress)
            self.finished_ok.emit(self.seq, r)
        except Exception as e:
            _log_worker_failure("扫描", e)
            self.failed.emit(self.seq, _describe_exception(e))


class PlanWorker(QThread):
    """后台计划生成线程（含 SHA-256 计算 + 重复检测）。

    所有对外信号都携带本任务自己的 seq，界面按 seq 校验来源，
    过期任务的结果/进度/失败/取消/结束都不会影响新任务状态。
    """
    progress = Signal(int, int, int, str)   # seq, current, total, current_filename
    stage = Signal(int, str)                # seq, 阶段说明
    finished_ok = Signal(int, object)       # seq, OrganizePlan
    cancelled = Signal(int)                 # seq — 用户取消（不返回半成品）
    failed = Signal(int, str)               # seq, error
    finished_seq = Signal(int)              # seq — run() 结束（含异常/取消）

    def __init__(self, seq: int, scan_result: ScanResult, mode: str,
                 rules, config=None, ocr_enabled: bool = False,
                 ai_settings: AiSettings | None = None, parent=None,
                 scan_cache_entries: dict | None = None,
                 cache_note: str = "", merge_cache: bool = False):
        super().__init__(parent)
        self.seq = seq
        self.scan_result = scan_result
        self.mode = mode
        self.rules = rules
        # V1.3：推荐模式的配置快照（深拷贝，后台线程不引用可编辑草稿）
        self.config = config
        self.ocr_enabled = ocr_enabled
        self.ai_settings = ai_settings
        self.scan_cache_entries = scan_cache_entries or {}
        self.cache_note = cache_note
        self.merge_cache = merge_cache
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def _cache_inputs(self):
        """把缓存里身份仍一致的结果拆成可复用部分和待重算列表。"""
        entries = self.scan_cache_entries or {}
        identities = {}
        known_sha = {}
        texts = {}
        errors = {}
        kinds = {}
        notes = {}
        to_extract = []
        hits = 0
        misses = 0
        for entry in self.scan_result.files:
            rel = entry.rel_path
            identity = scan_cache.file_identity(entry.abs_path)
            if identity is None:
                # 读不出身份的项不套用旧结果，交给本轮重新处理。
                misses += 1
                to_extract.append(entry)
                continue
            identities[rel] = identity
            record = entries.get(rel)
            if not scan_cache.identity_matches(record, identity):
                misses += 1
                to_extract.append(entry)
                continue
            hits += 1
            sha = record.get("sha256")
            if isinstance(sha, str) and sha:
                known_sha[rel] = sha
            kind = record.get("kind")
            if not isinstance(kind, str):
                kind = ""
            if kind:
                kinds[rel] = kind
            text = record.get("text")
            has_text = isinstance(text, str)
            if has_text:
                texts[rel] = text
            note = record.get("note")
            if isinstance(note, str) and note:
                notes[rel] = note
            if kind in ("图片", "空文件", "未支持"):
                continue
            if kind == "正文" and has_text:
                continue
            to_extract.append(entry)
        return {
            "identities": identities,
            "known_sha": known_sha,
            "texts": texts,
            "errors": errors,
            "kinds": kinds,
            "notes": notes,
            "to_extract": to_extract,
            "hits": hits,
            "misses": misses,
        }

    def _save_cache(self, inputs, plan, texts, errors, kinds, stats):
        """把本轮确认过的身份、指纹和正文写成下一轮可复用的缓存。"""
        sha_map = {item.file.rel_path: item.source_sha256
                   for item in plan.items}
        entries = dict(self.scan_cache_entries) if self.merge_cache else {}
        for entry in self.scan_result.files:
            rel = entry.rel_path
            identity = inputs["identities"].get(rel)
            current = scan_cache.file_identity(entry.abs_path)
            if identity is None or not scan_cache.identity_matches(identity, current):
                continue
            kind = kinds.get(rel, "")
            if kind == "读取失败":
                continue
            text = texts.get(rel) if kind == "正文" else None
            note = errors.get(rel, "") if kind in ("正文", "未支持") else ""
            entries[rel] = scan_cache.entry(
                identity, sha_map.get(rel, ""), text, kind, note)
        scan_cache.save(self.scan_result.root, entries)
        stats["cache_files"] = len(entries)
        stats["cache_saved"] = True

    def run(self):
        try:
            def _progress(cur, total, fname):
                self.progress.emit(self.seq, cur, total, fname)

            def _stage(name):
                self.stage.emit(self.seq, name)

            inputs = self._cache_inputs()
            stats = {
                "cache_hits": inputs["hits"],
                "cache_misses": inputs["misses"],
                "content_reused": len(inputs["texts"]),
                "content_read": len(inputs["to_extract"]),
                "cache_note": self.cache_note,
            }

            ocr_texts = None
            ocr_errors = None
            if self.ocr_enabled:
                _stage("识别图片文字")
                try:
                    ocr_texts, ocr_errors = collect_image_text(
                        self.scan_result.files,
                        cancel_event=self.cancel_event,
                        progress_cb=_progress,
                    )
                except OcrError as e:
                    _log_worker_failure("计划生成（图片识别）", e)
                    self.failed.emit(self.seq, _describe_exception(e))
                    return
                if self.cancel_event.is_set():
                    self.cancelled.emit(self.seq)
                    return

            # 未命中缓存的文件才重新读取正文；身份一致的文件直接复用摘录。
            _stage("读取正文" if inputs["hits"] == 0
                   else f"读取正文（复用 {inputs['hits']} 个缓存）")
            extraction = extract_contents(
                inputs["to_extract"],
                cancel_event=self.cancel_event,
                progress_cb=_progress,
                root=self.scan_result.root,
            )
            if self.cancel_event.is_set() or extraction.cancelled:
                # 取消后不发布半成品计划；界面按 seq 丢弃迟到的结果。
                self.cancelled.emit(self.seq)
                return
            texts = dict(inputs["texts"])
            texts.update(extraction.texts)
            kinds = dict(inputs["kinds"])
            kinds.update(extraction.kinds)
            errors = dict(inputs["notes"])
            errors.update(extraction.errors)
            stats["content_reused"] = len(inputs["texts"])
            stats["content_read"] = len(extraction.texts) + len(
                [rel for rel in extraction.errors
                 if kinds.get(rel) == "读取失败"])

            plan = generate_plan(
                self.scan_result, self.mode, self.rules,
                cancel_event=self.cancel_event,
                progress_cb=_progress,
                config=self.config,
                ocr_texts=ocr_texts,
                ocr_errors=ocr_errors,
                content_texts=texts,
                content_errors=errors,
                known_sha=inputs["known_sha"],
                cache_stats=stats,
                stage_cb=_stage,
            )

            if self.cancel_event.is_set():
                # 取消后不发布半成品计划
                self.cancelled.emit(self.seq)
                return

            if self.ai_settings is not None and self.mode != MODE_BY_TYPE:
                _stage("AI 补充分类")
                apply_ai_fallback(
                    plan, self.ai_settings,
                    cancel_event=self.cancel_event,
                    progress_cb=_progress,
                    config=self.config,
                    require_evidence=True,
                )
                if self.cancel_event.is_set():
                    self.cancelled.emit(self.seq)
                    return

            try:
                self._save_cache(inputs, plan, texts, errors, kinds, stats)
            except OSError as exc:
                stats["cache_saved"] = False
                stats["cache_error"] = str(exc)
                append_startup_log(f"增量缓存写入失败，本次计划不受影响：{exc}")
            plan.cache_stats = dict(stats)
            self.finished_ok.emit(self.seq, plan)
        except Exception as e:
            _log_worker_failure("计划生成", e)
            self.failed.emit(self.seq, _describe_exception(e))
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
            _log_worker_failure("整理", e)
            self.failed.emit(_describe_exception(e))
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
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        storage = None
        try:
            storage = Storage(self.root)
            result = undo_latest_batch(storage, cancel_event=self.cancel_event)
            self.finished_ok.emit(result)
        except Exception as e:
            _log_worker_failure("撤销", e)
            self.failed.emit(_describe_exception(e))
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
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        storage = None
        try:
            storage = Storage(self.root)
            result = recover_batch(
                storage, self.batch_id,
                progress_cb=lambda cur, total, path:
                    self.progress.emit(self.seq, self.root, cur, total, path),
                cancel_event=self.cancel_event,
            )
            self.finished_ok.emit(self.seq, self.root, result)
        except Exception as e:
            _log_worker_failure("核对恢复", e)
            self.failed.emit(self.seq, self.root, _describe_exception(e))
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
        info_label = QLabel(
            f"<b>根目录：</b>{root}<br>"
            f"<b>分类模式：</b>{MODE_LABELS.get(plan.mode, plan.mode)}<br>"
            f"<b>已选文件：</b>{plan.selected_count} / {plan.total}"
            f"{extra_lines}{scope_html}"
            + (
                f"<br><b>将跳过：</b>{plan.total - plan.selected_count} 个未勾选文件"
                if plan.total > plan.selected_count else ""
            )
            + "<br>"
            f"<b>目标目录：</b>{os.path.join(root, '_已整理')}/…<br>"
        )
        info_label.setWordWrap(True)
        info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_label.setStyleSheet("color:#0F172A; background:#FFFFFF;")
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
        cancel_btn.setAutoDefault(True)
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()
        self._cancel_btn = cancel_btn
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def showEvent(self, event):
        super().showEvent(event)
        self._cancel_btn.setAutoDefault(True)
        self._cancel_btn.setDefault(True)
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


def _fit_dialog(dlg, width: int, height: int,
                min_width: int = 820, min_height: int = 440) -> None:
    """S3 / guib F-05/F-06：窗口按可用屏幕缩小，并设置最小尺寸。

    1366×768 / 150% 缩放下也保证主按钮在屏幕内；放不下时由内部滚动兜底。
    """
    screen = dlg.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry() if screen is not None else None
    dlg.setMinimumSize(min_width, min_height)
    if avail is None:
        dlg.resize(width, height)
        return
    w = max(min_width, min(width, avail.width() - 30))
    h = max(min_height, min(height, avail.height() - 60))
    dlg.resize(w, h)


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


def history_reveal_target(root: str, source_rel: str, target_rel: str) -> str:
    """历史定位的当前路径：优先目标，其次原位置，都不存在返回空串。

    只接受解析后仍在 realpath(整理空间) 内的相对路径。绝对路径、../ 和
    指向空间外的符号链接一律返回空串，避免定位到空间外文件；也不按文件名
    搜索其他同名文件。
    """
    root_real = os.path.realpath(os.path.abspath(root or "."))
    for rel in (target_rel, source_rel):
        if not rel or os.path.isabs(rel):
            continue
        candidate = os.path.realpath(os.path.join(root_real, rel))
        try:
            if os.path.commonpath([candidate, root_real]) != root_real:
                continue
        except ValueError:
            continue
        if os.path.exists(candidate):
            return candidate
    return ""


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
        # S3 / guib F-06：按可用屏幕缩小，并设置最小尺寸
        _fit_dialog(self, 1080, 720)
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
        # S3 / guib F-12：注意/模式/状态列有 tooltip，长文本不挤掉其他列
        for col in range(len(_HISTORY_BATCH_COLUMNS)):
            self.batch_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.Interactive)
        self.batch_table.setColumnWidth(0, 70)
        self.batch_table.setColumnWidth(1, 150)
        self.batch_table.setColumnWidth(2, 170)
        self.batch_table.setColumnWidth(3, 110)
        for col in (4, 5, 6, 7, 8):
            self.batch_table.setColumnWidth(col, 60)
        self.batch_table.setColumnWidth(9, 240)
        self.batch_table.itemSelectionChanged.connect(self._on_batch_selected)
        self.batch_table.setMaximumHeight(160)
        lay.addWidget(self.batch_table, 0)

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
        # Qt 6.12 的 macOS 辅助功能在「选中真实单元格替换占位符」时会删除
        # 父表接口，导致 NSAccessibility 读取时崩溃（QTBUG-149612）。
        # 这里不让表格维护 selected children，改用 current row 表示选中行；
        # 辅助功能和鼠标选中都会走 currentCellChanged，进程不再进入该路径。
        self.item_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.item_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.item_table.verticalHeader().setVisible(False)
        self._item_wrappers = []
        self._batch_wrappers = []
        # 行选择按钮不能放在单元格里：QAccessibleTable 只把虚拟单元格暴露给
        # VoiceOver，setCellWidget 的按钮在辅助功能树里不可见。改为把按钮
        # 挂到对话框上，按第 0 列单元格的位置覆盖显示，辅助功能可直接按下。
        self._row_pickers = []
        # S3 / guib F-13：状态/原因列不能太窄，长文本用 tooltip
        self.item_table.setColumnWidth(0, 46)
        self.item_table.setColumnWidth(3, 70)
        self.item_table.setColumnWidth(4, 140)
        self.item_table.setColumnWidth(5, 200)
        self.item_table.setColumnWidth(6, 80)
        self.item_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.item_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.item_table.currentCellChanged.connect(
            lambda *_: self._on_item_selection_changed())
        self.item_table.cellClicked.connect(
            lambda row, _col: self._select_item_row(row))
        self.item_table.itemActivated.connect(
            lambda item: self._select_item_row(item.row()))
        self.item_table.doubleClicked.connect(self._on_reveal)
        self.item_table.verticalScrollBar().valueChanged.connect(
            lambda _=None: self._layout_row_pickers())
        self.item_table.horizontalScrollBar().valueChanged.connect(
            lambda _=None: self._layout_row_pickers())
        # 明细表内部尺寸变化（筛选栏换行、分页栏显隐）不一定伴随对话框
        # 缩放，单独盯住 viewport 的尺寸变化，保证按钮裁切范围同步。
        self._item_viewport = self.item_table.viewport()
        self._item_viewport.installEventFilter(self)
        lay.addWidget(self.item_table, 1)

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
        self._clear_row_pickers()
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
        self._batch_wrappers = []
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
            # S4 / guib F-27：时间列保留年份，悬浮提示完整时间
            t_full = b.created_at.replace("T", " ")
            cells = [
                f"#{b.batch_id}", t_full,
                MODE_LABELS.get(b.mode, b.mode),
                BATCH_STATUS_LABELS.get(b.status, b.status),
                str(moved), str(c.get("skipped", 0)), str(c.get("failed", 0)),
                str(c.get("cancelled", 0)), str(undone), attention,
            ]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                self._batch_wrappers.append(it)
                if col in (4, 5, 6, 7, 8):
                    it.setTextAlignment(Qt.AlignCenter)
                if col in (1, 2, 3, 9):
                    it.setToolTip(text)   # 完整文本，避免被列宽截断
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
            self._clear_row_pickers()
            self.page_label.setText("")
            self.batch_config_label.hide()
            # S3 / guib F-14：空历史要有明确说明
            if not self._batches:
                self.batch_table.setRowCount(1)
                empty = QTableWidgetItem("这个文件夹还没有整理记录")
                self._batch_wrappers.append(empty)
                empty.setToolTip(
                    "整理一次后，批次与逐项明细会记录在这里；"
                    "记录保存在该文件夹的 .organizer 里。")
                self.batch_table.setItem(0, 0, empty)
                self.batch_table.setSpan(
                    0, 0, 1, len(_HISTORY_BATCH_COLUMNS))
                self.detail_label.setText(
                    "还没有整理记录。完成一次整理后，这里会显示移动了哪些文件。")
        self._refresh_buttons()   # 即使选中行未变（如撤销后），也要刷新操作按钮

    def _on_batch_selected(self):
        rows = self.batch_table.selectionModel().selectedRows()
        if not rows or not self._batches:
            self.batch_config_label.hide()
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._batches):
            return
        b = self._batches[row]
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
        self._clear_row_pickers()
        self.item_table.setRowCount(len(chunk))
        self._item_wrappers = []
        self.item_table.setCurrentCell(-1, -1)
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
                self._item_wrappers.append(it)
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
                if col == 4:
                    it.setToolTip(text)   # 状态列：暂停/冲突等完整说明
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
            # 辅助功能可直接激活的行选择按钮：挂在对话框上而不是单元格里，
            # 这样 VoiceOver 的辅助功能树能看到并按下「选择第 N 行明细」；
            # 同时不依赖 QTableWidget 的 selected-cell 接口，避免
            # QTBUG-149612 的父表接口被删除导致崩溃。
            pick = QPushButton(str(r + 1), self)
            pick.setFlat(True)
            pick.hide()
            pick.setCursor(Qt.PointingHandCursor)
            pick.setFocusPolicy(Qt.StrongFocus)
            pick.setAccessibleName(f"选择第 {r + 1} 行明细")
            pick.setToolTip("选择这一行，再用「在文件管理器中显示」定位")
            pick.clicked.connect(lambda _=False, row=r: self._select_item_row(row))
            self._row_pickers.append(pick)
        self.item_table.viewport().update()
        QTimer.singleShot(0, self._layout_row_pickers)
        self.page_label.setText(
            f"第 {self._page + 1}/{pages} 页 · 共 {len(items)} 项"
            if items else "无匹配项")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page < pages - 1)
        self._refresh_buttons()
        self._on_item_selection_changed()

    def _clear_row_pickers(self):
        for btn in getattr(self, "_row_pickers", []):
            btn.hide()
            btn.deleteLater()
        self._row_pickers = []

    def _layout_row_pickers(self):
        """把行选择按钮裁到明细表当前可见区域内，滚出视口的按钮隐藏。"""
        pickers = getattr(self, "_row_pickers", None)
        if not pickers:
            return
        vp = self.item_table.viewport()
        vp_rect = vp.rect()
        for r, btn in enumerate(pickers):
            item = self.item_table.item(r, 0)
            if item is None:
                btn.hide()
                continue
            rect = self.item_table.visualItemRect(item)
            # 行已滚出视口或只露出边缘时，按钮不能伸到表格外去盖住
            # 「刷新」「搜索」「上一页」等控件；只保留与视口相交的部分。
            visible = rect.intersected(vp_rect)
            if rect.isEmpty() or visible.isEmpty():
                btn.hide()
                continue
            top_left = vp.mapTo(self, visible.topLeft())
            geometry = (top_left.x(), top_left.y(),
                        visible.width(), visible.height())
            was_visible = btn.isVisible()
            if (btn.x(), btn.y(), btn.width(), btn.height()) != geometry:
                btn.setGeometry(*geometry)
            btn.show()
            if not was_visible:
                btn.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_row_pickers()

    def eventFilter(self, obj, event):
        # viewport 可能只移动不缩放（筛选栏换行、批次配置标签出现时），
        # 只盯 Resize 会让按钮停在旧位置、伸到分页栏上。这里对移动、
        # 显示和重绘也同步裁切，保证按钮始终贴着当前视口。
        if obj is getattr(self, "_item_viewport", None) and event.type() in (
                QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Paint):
            self._layout_row_pickers()
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._layout_row_pickers)

    def _turn_page(self, delta: int):
        self._page = max(0, self._page + delta)
        self._render_items()

    def _on_filter_changed(self):
        self._page = 0
        self._render_items()

    def _select_item_row(self, row: int):
        if row < 0 or row >= self.item_table.rowCount():
            return
        # NoSelection 下用 current cell 表示选中行，避免 macOS 辅助功能
        # 进入 QTableWidget 的 selected-cell 占位符替换路径。
        self.item_table.setCurrentCell(row, 1)
        self._on_item_selection_changed()

    def _on_item_selection_changed(self):
        row = self.item_table.currentRow()
        if row < 0:
            self.detail_label.setText("")
            self.btn_reveal.setEnabled(False)
            return
        idx = row
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
        idx = self.item_table.currentRow()
        if idx < 0:
            return
        items = self._filtered_items()
        start = self._page * HISTORY_PAGE_SIZE
        a = items[start + idx] if start + idx < len(items) else None
        if a is None:
            return
        # 优先定位仍存在的目标文件，其次原位置；两边都不在就明确说明。
        target = history_reveal_target(self.root or "", a.source_rel, a.target_rel)
        if not target:
            QMessageBox.information(
                self, "归档盒",
                "找不到这个文件：原位置和目标位置都不存在。\n"
                "可能已被移动、撤销或删除，请勿按文件名打开其他同名文件。")
            return
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
        # S3 / guib F-16：导出范围与界面筛选一致
        active_filter = bool(self.search_edit.text().strip()
                             or self.status_filter.currentData())
        actions = (self._filtered_items() if active_filter
                   else list(batch.actions))
        if active_filter and not actions:
            QMessageBox.information(
                self, "归档盒",
                "当前筛选没有任何记录，未导出。请先调整搜索或状态筛选。")
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
                        for a in actions
                    ],
                    "exported_at": __import__("time").strftime(
                        "%Y-%m-%d %H:%M:%S"),
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow([csv_safe(v) for v in (
                        "批次", batch.batch_id, "状态", batch.status,
                        "时间", batch.created_at, "模式", batch.mode)])
                    w.writerow(["序号", "原位置", "目标位置", "大小(字节)",
                                "SHA-256", "状态", "原因", "执行时间", "撤销时间",
                                "分类来源", "自动目标", "自动依据", "修正说明",
                                "规则标识", "命中依据"])
                    for a in actions:
                        w.writerow([csv_safe(v) for v in (
                            a.action_index + 1, a.source_rel,
                            a.target_rel, a.source_size,
                            a.source_sha256, a.status, a.error,
                            a.executed_at, a.undone_at,
                            a.classification_source,
                            a.auto_target_rel, a.auto_rule_applied,
                            a.override_note,
                            a.rule_key, a.rule_applied)])
        except OSError as e:
            QMessageBox.warning(self, "归档盒", f"导出失败：{e}")
            return
        scope = ("当前筛选结果" if active_filter else "整批全部记录")
        QMessageBox.information(
            self, "归档盒",
            f"已导出批次 #{batch_id} 的 {len(actions)} 条记录（{scope}）到：\n{path}\n\n"
            "导出包含原位置与目标位置的完整相对路径；"
            "CSV/JSON 里也会带完整路径字段，分享前请自行脱敏。")

    # ---------------- 主窗口回调 ----------------

    def refresh_after_change(self):
        """撤销/恢复/执行完成后由主窗口调用：统一刷新（列表+明细）。"""
        if not self.isVisible():
            return
        self.refresh_all()


# ======================== 主窗口 ========================


class FlowLayout(QLayout):
    """按可用宽度自动换行的布局，窄窗口下不裁切筛选控件。"""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(),
                                  -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # S3 / guib F-05：按可用屏幕缩小，1366×768 / 150% 也能看到「开始整理」
        _fit_dialog(self, 1280, 780, min_width=820, min_height=460)
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
        # 阶段 2：偏好库可用性（不可用时回退到 JSON 长期规则）
        self._preferences_available = False
        self._preference_note = ""
        # 规则试运行后台线程（关闭流程需要等待它们结束）
        self._rule_preview_workers: list = []

        # V1.6：预览搜索/筛选/批量勾选与目标树
        self._visible_flags: list[bool] = []
        self._plan_identity: tuple | None = None
        # 阶段 1：歧义改选后旧确认作废，必须重新确认最终批次。
        self._plan_revision = 0
        self._confirmed_revision: int | None = None
        self._ai_consent_given = False
        self._ambiguity_item = None
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
        self._pending_intake_selection = []
        self._pending_drop_select = []
        # 拖放新文件会重建计划（_discard_plan 清掉旧计划），已跳过信息记在
        # 窗口上，等新计划发布时套用一次，再清空。
        self._carry_skipped_rels = []
        self._quiet_drop_scan = False
        self._recent_dialog = None
        self._duplicate_dialog = None
        self._agent_dialog = None
        self._duplicate_worker = None
        self._duplicate_seq = 0
        self._duplicate_cache = None
        self._duplicate_cache_key = None
        self._duplicate_cache_entries = None
        self._duplicate_pending_entries = None
        self._scan_generation = 0
        self._scan_seq_counter = 0
        self._current_scan_seq = 0
        self._ocr_supported = ocr_supported()
        self._ai_settings = load_settings()
        self.setAcceptDrops(True)

        # V1.2: 操作历史窗口（非模态；同一时间只保留当前根目录的实例）
        self._history_dialog: HistoryDialog | None = None

        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(500)
        self._busy_timer.timeout.connect(self._tick_busy)
        self._busy_started_at = 0.0
        self._interrupted_batch = None
        self._root_mismatch_note = ""

        self._build_ui()
        self._build_menus()
        self._attach_drop_watch(self)
        for child in self.findChildren(QWidget):
            self._attach_drop_watch(child)

    def _build_menus(self):
        """S3 / guib F-32：提供「帮助 / 关于 / 使用说明」入口。"""
        menu = self.menuBar().addMenu("帮助")
        act_guide = menu.addAction("使用说明")
        act_guide.triggered.connect(self._show_guide)
        act_about = menu.addAction("关于归档盒")
        act_about.triggered.connect(self._show_about)

    def _show_about(self):
        from scheme_store import app_data_dir
        QMessageBox.about(
            self, f"关于 {APP_TITLE}",
            f"<b>{APP_TITLE}</b><br><br>"
            "把散落的文件按学期、课程与类别整理到 _已整理，"
            "并在移动前给出可核对的预览。<br><br>"
            f"整理记录保存在：<br>{os.path.join(app_data_dir(), '')}<br>"
            "（每个整理空间的记录在其 .organizer/history.db）<br><br>"
            "AI 补充分类默认关闭；只有你自己打开并填写密钥后才会发请求。")

    def _show_guide(self):
        """打开包内的使用说明；不存在时直接说明要点，不指向不存在的文件。"""
        app_dir = os.path.dirname(os.path.abspath(__file__))
        package_root = os.path.dirname(app_dir)
        candidates = [
            os.path.join(package_root, "使用说明.txt"),
            os.path.join(app_dir, "使用说明.txt"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                try:
                    if os.name == "nt":
                        os.startfile(path)  # noqa: S606 — 打开本程序自带说明
                    else:
                        subprocess.Popen(["xdg-open", path])
                    return
                except OSError:
                    break
        from scheme_store import app_data_dir
        QMessageBox.information(
            self, "使用说明",
            f"{APP_TITLE}\n\n"
            "1. 选择要整理的文件夹，点「重新扫描」。\n"
            "2. 在预览里核对目标位置，取消不想移动的勾选。\n"
            "3. 点「开始整理」，在确认框里再确认一次。\n"
            "4. 需要还原时点「撤销上一次」；中断的批次会提示核对恢复。\n\n"
            "整理记录保存在每个文件夹的 .organizer 里；"
            f"方案库与最近空间在 {app_data_dir()}。\n"
            "AI 补充分类默认关闭，程序不内置任何密钥。\n\n"
            "（当前包内没有附带「使用说明.txt」，以上为内置说明。）")

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

    def _touch_recent_space(self, path: str) -> None:
        """记录最近空间；写失败要提示，不能静默吞掉（S4 / misc F-17）。"""
        try:
            recent_spaces.touch(path)
        except recent_spaces.RecentSpacesError as exc:
            append_startup_log(f"最近空间记录写入失败：{exc}")
            self.statusBar().showMessage(
                "最近整理空间记录没能保存（%APPDATA% 不可写或被占用）；"
                "本次整理不受影响。")

    def _center_window(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)


    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        def fill_panel(widget, color):
            pal = widget.palette()
            pal.setColor(QPalette.ColorRole.Window, QColor(color))
            widget.setAutoFillBackground(True)
            widget.setPalette(pal)

        def side_heading(text):
            lab = QLabel(text)
            lab.setStyleSheet(
                "color:#64748B;font-size:11px;font-weight:600;padding:10px 8px 4px 8px;")
            return lab

        def nav(btn):
            btn.setProperty("role", "nav")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            return btn

        title = QHBoxLayout()
        title.setContentsMargins(14, 8, 14, 8)
        title_name = QLabel("归档盒")
        title_name.setStyleSheet("color:#0F172A;font-weight:600;")
        title.addWidget(title_name)
        title.addStretch(1)
        title_hint = QLabel("选中一行后，右侧说明这个文件")
        title_hint.setStyleSheet("color:#64748B;")
        title.addWidget(title_hint)
        outer.addLayout(title)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet("color:#E2E8F0;")
        outer.addWidget(rule)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplit")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(7)

        side_inner = QWidget()
        side_inner.setObjectName("sideInner")
        fill_panel(side_inner, "#F8FAFC")
        side_lay = QVBoxLayout(side_inner)
        side_lay.setContentsMargins(8, 4, 8, 12)
        side_lay.setSpacing(2)
        side_lay.addWidget(side_heading("文件夹"))
        self.storage_tree = StorageTree()
        self.storage_tree.folderHighlighted.connect(self._preview_sidebar_folder)
        self.storage_tree.folderActivated.connect(self._open_sidebar_space)
        self.storage_tree.setMinimumHeight(96)
        self.storage_tree.setMaximumHeight(160)
        side_lay.addWidget(self.storage_tree)
        folder_hint = QLabel("双击文件夹，设为这次要整理的位置")
        folder_hint.setWordWrap(True)
        folder_hint.setStyleSheet(
            "color:#64748B;font-size:11px;padding:2px 8px 8px 8px;")
        side_lay.addWidget(folder_hint)

        side_lay.addWidget(side_heading("分类"))
        self.mode_combo = QComboBox()
        for m in ALL_MODES:
            self.mode_combo.addItem(MODE_LABELS[m], m)
        self.mode_combo.setCurrentIndex(2)
        self.mode_combo.setToolTip("决定预览里的文件按什么规则归类")
        self.mode_combo.currentIndexChanged.connect(self.on_plan_param_changed)
        mode_text_w = max(
            self.fontMetrics().horizontalAdvance(MODE_LABELS[m]) for m in ALL_MODES)
        self.mode_combo.setMinimumWidth(mode_text_w + 36)
        mode_popup = self.mode_combo.view()
        mode_popup.setTextElideMode(Qt.TextElideMode.ElideNone)
        mode_popup.setMinimumWidth(mode_text_w + 24)
        side_lay.addWidget(self.mode_combo)

        self.ocr_check = QCheckBox("识别图片文字")
        if self._ocr_supported:
            self.ocr_check.setToolTip(
                "生成预览时读取图片里的文字，用来匹配关键词。\n"
                "已经按文件夹归好的文件不会因此改判。\n"
                "「文件名关键词」长期规则会同时看图片里的字。\n"
                + engine_label() + "\n"
                "图片多的时候会变慢。"
            )
        else:
            self.ocr_check.setEnabled(False)
            self.ocr_check.setToolTip(
                "没有可用的文字识别组件。图片仍会正常整理，只是不读取图片里的文字。\n"
                "请确认安装的是完整版归档盒（无需在命令行安装依赖）。"
            )
        self.ocr_check.stateChanged.connect(self.on_plan_param_changed)
        side_lay.addWidget(self.ocr_check)

        self.ai_check = QCheckBox("待定或识别失败时问 DeepSeek")
        self.ai_check.setChecked(self._ai_settings.enabled)
        self.ai_check.setToolTip(
            "默认关闭。打开后，分类结果是「待定」或图片识别失败时，"
            "会把文件名、相对路径和一段图片文字发给 api.deepseek.com。\n"
            "需要填写你自己的密钥。程序不再内置密钥。"
        )
        self.ai_check.stateChanged.connect(self._on_ai_toggled)
        side_lay.addWidget(self.ai_check)

        self.btn_edit_rules = nav(QPushButton("课程关键词"))
        self.btn_edit_rules.setToolTip(
            "只在「按课程再按类型」下生效。\n"
            "当前推荐分类方式请用「学期和目录规则」"
        )
        self.btn_edit_rules.clicked.connect(self.on_edit_rules)
        side_lay.addWidget(self.btn_edit_rules)

        self.btn_edit_config = nav(QPushButton("学期和目录规则"))
        self.btn_edit_config.setToolTip(
            "设置学期、课程目录和关键词。\n"
            "只在「按方向/学期/类别（推荐）」下生效"
        )
        self.btn_edit_config.clicked.connect(self.on_edit_config)
        side_lay.addWidget(self.btn_edit_config)

        self.btn_learning = nav(QPushButton("学习建议"))
        self.btn_learning.setToolTip(
            "根据最近的改单归纳候选偏好；确认前不会写入偏好表，"
            "也不会移动文件")
        self.btn_learning.clicked.connect(self.on_learning_suggestions)
        side_lay.addWidget(self.btn_learning)

        self.btn_scheme_center = nav(QPushButton("导入或保存方案"))
        self.btn_scheme_center.setToolTip(
            "把当前分类规则保存成方案，或把已有方案应用到这个文件夹")
        self.btn_scheme_center.clicked.connect(self.on_scheme_center)
        side_lay.addWidget(self.btn_scheme_center)

        self.btn_regenerate = nav(QPushButton("重新生成预览"))
        self.btn_regenerate.setToolTip(
            "规则或范围变了之后，按当前设置重新计算预览。不会移动文件")
        self.btn_regenerate.setEnabled(False)
        self.btn_regenerate.clicked.connect(self.on_regenerate_plan)
        side_lay.addWidget(self.btn_regenerate)

        side_lay.addWidget(side_heading("勾选"))
        self.btn_select_visible = nav(QPushButton("勾选看到的文件"))
        self.btn_select_visible.setToolTip("只勾选表格里当前显示的文件，被筛选隐藏的不变")
        self.btn_select_visible.clicked.connect(
            lambda: self._set_visible_checked(True))
        side_lay.addWidget(self.btn_select_visible)
        self.btn_deselect_visible = nav(QPushButton("取消这些勾选"))
        self.btn_deselect_visible.setToolTip("取消表格里当前显示文件的勾选，被筛选隐藏的不变")
        self.btn_deselect_visible.clicked.connect(
            lambda: self._set_visible_checked(False))
        side_lay.addWidget(self.btn_deselect_visible)
        self.btn_invert_visible = nav(QPushButton("对调这些勾选"))
        self.btn_invert_visible.setToolTip("对表格里当前显示的文件，已勾选的取消，未勾选的勾上")
        self.btn_invert_visible.clicked.connect(
            lambda: self._set_visible_checked(None, invert=True))
        side_lay.addWidget(self.btn_invert_visible)
        self.btn_recent = nav(QPushButton("最近打开"))
        self.btn_recent.setToolTip("打开最近用过的文件夹，也可以固定或移除记录")
        self.btn_recent.clicked.connect(self.on_recent_spaces)
        side_lay.addWidget(self.btn_recent)

        side_lay.addWidget(side_heading("这次"))
        self.btn_agent = nav(QPushButton("AI 管家"))
        self.btn_agent.setToolTip(
            "用一句话查找文件并生成整理方案。\n"
            "先给你看命中的文件和理由；确认后仍走主界面的预览与「开始整理」")
        self.btn_agent.clicked.connect(self.on_agent)
        side_lay.addWidget(self.btn_agent)

        self.btn_intake = nav(QPushButton("待整理清单"))
        self.btn_intake.setToolTip(
            "登记这次要整理的文件和文件夹。这里只登记，不移动文件")
        self.btn_intake.clicked.connect(self.on_intake_dialog)
        side_lay.addWidget(self.btn_intake)

        self.btn_scan_intake = nav(QPushButton("只扫描清单"))
        self.btn_scan_intake.setToolTip(
            "只扫描「待整理清单」里登记的文件和文件夹；\n"
            "清单以外的文件不会进入预览")
        self.btn_scan_intake.setEnabled(False)
        self.btn_scan_intake.clicked.connect(self.on_scan_intake)
        side_lay.addWidget(self.btn_scan_intake)

        self.btn_duplicates = nav(QPushButton("查看重复文件"))
        self.btn_duplicates.setToolTip(
            "查看内容相同、以及文件名相同但内容不同的文件。\n"
            "这里只查看，不删除、不合并、不移动")
        self.btn_duplicates.setEnabled(False)
        self.btn_duplicates.clicked.connect(self.on_show_duplicates)
        side_lay.addWidget(self.btn_duplicates)

        side_lay.addWidget(side_heading("整理"))
        self.btn_undo = nav(QPushButton("撤销上一次"))
        self.btn_undo.setToolTip("把最近一次整理移走的文件放回原位置")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self.on_undo)
        side_lay.addWidget(self.btn_undo)

        self.btn_history = nav(QPushButton("操作历史"))
        self.btn_history.setToolTip(
            "查看这个文件夹以往的整理记录，也可以从这里撤销或核对中断的批次")
        self.btn_history.setEnabled(False)
        self.btn_history.clicked.connect(self.on_history)
        side_lay.addWidget(self.btn_history)

        self.btn_target_tree = nav(QPushButton("将生成的目录"))
        self.btn_target_tree.setToolTip("按当前预览，查看整理后会生成哪些文件夹")
        self.btn_target_tree.clicked.connect(self.on_show_target_tree)
        side_lay.addWidget(self.btn_target_tree)

        self.btn_edit_protect = nav(QPushButton("不整理的目录"))
        self.btn_edit_protect.setToolTip(
            "指定哪些文件夹不参与整理。项目目录默认跳过，这里可以再手动增减")
        self.btn_edit_protect.clicked.connect(self.on_edit_protect)
        side_lay.addWidget(self.btn_edit_protect)

        side_lay.addStretch(1)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        fill_panel(side_scroll, "#F8FAFC")
        side_scroll.setWidget(side_inner)
        side_need = self._sidebar_width_for(side_inner)
        side_scroll.setMinimumWidth(side_need)
        side_scroll.setMaximumWidth(560)
        splitter.addWidget(side_scroll)

        center_host = QWidget()
        self.center_host = center_host
        fill_panel(center_host, "#FFFFFF")
        center = QVBoxLayout(center_host)
        center.setContentsMargins(12, 10, 12, 10)
        center.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("还没有选择文件夹，也可以把文件拖进窗口")
        self.path_edit.setReadOnly(True)
        toolbar.addWidget(self.path_edit, stretch=1)
        self.btn_browse = QPushButton("选择文件夹")
        self.btn_browse.setToolTip("打开系统对话框，选择这次要整理的文件夹")
        self.btn_browse.clicked.connect(self.on_browse)
        toolbar.addWidget(self.btn_browse)
        self.btn_scan = QPushButton("重新扫描")
        self.btn_scan.setToolTip(
            "扫描所选文件夹里的全部文件，只生成预览，不会移动文件")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.on_scan)
        toolbar.addWidget(self.btn_scan)
        center.addLayout(toolbar)

        filters = FlowLayout(spacing=6)
        filters.addWidget(QLabel("筛选："))
        self.plan_search = QLineEdit()
        self.plan_search.setMinimumWidth(220)
        self.plan_search.setPlaceholderText("搜索原位置、目标位置或分类依据")
        self.plan_search.setToolTip("按文字筛选预览行；只影响显示，不改变勾选")
        self.plan_search.textChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.plan_search)
        self.type_filter = QComboBox()
        self.type_filter.setMinimumWidth(96)
        self.type_filter.setToolTip("按文件类型筛选预览")
        self.type_filter.setAccessibleName("类型筛选")
        self.type_filter.addItem("全部类型", "")
        self.type_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.type_filter)
        self.dir_filter = QComboBox()
        self.dir_filter.setMinimumWidth(96)
        self.dir_filter.setToolTip("按分类目录筛选预览")
        self.dir_filter.setAccessibleName("分类目录筛选")
        self.dir_filter.addItem("全部目录", "")
        self.dir_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.dir_filter)
        self.source_filter = QComboBox()
        self.source_filter.setMinimumWidth(96)
        self.source_filter.setToolTip("按分类来源筛选：自动 / 长期规则 / 本次手动修正")
        self.source_filter.setAccessibleName("分类来源筛选")
        self.source_filter.addItem("全部来源", "")
        self.source_filter.addItem("自动", "auto")
        self.source_filter.addItem("长期规则", "long_term")
        self.source_filter.addItem("本次手动修正", "manual")
        self.source_filter.currentIndexChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.source_filter)
        self.pending_check = QCheckBox("待定")
        self.pending_check.setToolTip("只看还没定分类的文件")
        self.pending_check.stateChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.pending_check)
        self.conflict_check = QCheckBox("将改名")
        self.conflict_check.setToolTip("只看因为重名而会改名的文件")
        self.conflict_check.stateChanged.connect(self._apply_plan_filters)
        filters.addWidget(self.conflict_check)
        self.btn_clear_filters = QPushButton("清除")
        self.btn_clear_filters.setToolTip("清空搜索和筛选项，重新显示全部预览")
        self.btn_clear_filters.clicked.connect(self._clear_plan_filters)
        filters.addWidget(self.btn_clear_filters)
        center.addLayout(filters)

        self.config_label = QLabel("选择文件夹后，这里会显示正在使用的分类规则。")
        self.config_label.setWordWrap(True)
        self.config_label.setStyleSheet("color:#475569;font-size:12px;")
        center.addWidget(self.config_label)

        self.scope_label = QLabel("")
        self.scope_label.setWordWrap(True)
        self.scope_label.setStyleSheet("color:#475569;font-size:12px;")
        self.scope_label.hide()
        center.addWidget(self.scope_label)

        self.invalid_label = QLabel("计划已失效（规则、模式或文件夹已更改）")
        self.invalid_label.setWordWrap(True)
        self.invalid_label.setStyleSheet(
            "color:#B45309;background:#FFFBEB;padding:6px 10px;border-radius:6px;")
        self.invalid_label.hide()
        center.addWidget(self.invalid_label)

        self.plan_hint = QLabel("")
        self.plan_hint.setWordWrap(True)
        self.plan_hint.setStyleSheet(
            "color:#0F172A;background:#FFF7ED;padding:6px 10px;border-radius:6px;")
        self.plan_hint.hide()
        center.addWidget(self.plan_hint)

        # 阶段 1：只对会改变落点的歧义提问。使用预览内的轻量选择，
        # 不与执行确认框（ExecuteConfirmDialog）混用。
        self.ambiguity_panel = QFrame()
        self.ambiguity_panel.setStyleSheet(
            "QFrame{background:#FFFBEB;border:1px solid #FDE68A;"
            "border-radius:6px;}")
        amb_lay = QVBoxLayout(self.ambiguity_panel)
        amb_lay.setContentsMargins(10, 8, 10, 8)
        amb_lay.setSpacing(6)
        self.ambiguity_label = QLabel("")
        self.ambiguity_label.setWordWrap(True)
        self.ambiguity_label.setStyleSheet("color:#92400E;font-size:12px;")
        amb_lay.addWidget(self.ambiguity_label)
        amb_row = QHBoxLayout()
        amb_row.setSpacing(6)
        self.ambiguity_combo = QComboBox()
        self.ambiguity_combo.setAccessibleName("歧义候选")
        amb_row.addWidget(self.ambiguity_combo, 1)
        self.btn_ambiguity_apply = QPushButton("按所选归入")
        self.btn_ambiguity_apply.clicked.connect(self.on_apply_ambiguity_choice)
        amb_row.addWidget(self.btn_ambiguity_apply)
        self.btn_ambiguity_skip = QPushButton("跳过，保持原位")
        self.btn_ambiguity_skip.clicked.connect(self.on_skip_ambiguity)
        amb_row.addWidget(self.btn_ambiguity_skip)
        amb_lay.addLayout(amb_row)
        self.ambiguity_panel.hide()
        center.addWidget(self.ambiguity_panel)

        self.protect_label = QLabel("")
        self.protect_label.setWordWrap(True)
        self.protect_label.setStyleSheet("color:#475569;font-size:12px;")
        self.protect_label.hide()
        center.addWidget(self.protect_label)

        self.progress_row = QWidget()
        progress_lay = QHBoxLayout(self.progress_row)
        progress_lay.setContentsMargins(0, 2, 0, 2)
        progress_lay.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(12)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            "QProgressBar {"
            " border: none; border-radius: 6px;"
            " background: #DBEAFE; min-height: 12px; max-height: 12px;"
            "}"
            "QProgressBar::chunk {"
            " border-radius: 6px; background: #2563EB;"
            "}"
        )
        self.progress_label = QLabel("进行中")
        self.progress_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.progress_label.setMinimumWidth(96)
        self.progress_label.setStyleSheet(
            "color:#1D4ED8;font-weight:650;font-size:13px;")
        progress_lay.addWidget(self.progress, 1)
        progress_lay.addWidget(self.progress_label)
        self.progress_row.hide()
        center.addWidget(self.progress_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["勾选", "原位置", "目标位置", "分类依据", "状态", "大小"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        # S3 / guib F-09：分类依据列固定宽度，长规则不能把状态/大小挤出屏幕
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 200)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        table_pal = self.table.palette()
        table_pal.setColor(QPalette.ColorRole.Highlight, QColor("#EFF6FF"))
        table_pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#0F172A"))
        self.table.setPalette(table_pal)
        self.table.itemSelectionChanged.connect(self._update_button_states)
        self._suppress_cell_signals = False
        center.addWidget(self.table, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.plan_counts_label = QLabel("")
        self.plan_counts_label.setStyleSheet("color:#475569;")
        footer.addWidget(self.plan_counts_label, 1)
        # 阶段 1：影响范围（会移动哪些、跳过哪些）
        self.impact_label = QLabel("")
        self.impact_label.setStyleSheet("color:#475569;")
        footer.addWidget(self.impact_label)
        self.btn_cancel = QPushButton("停止本次整理")
        self.btn_cancel.setProperty("role", "danger")
        self.btn_cancel.setToolTip("停止正在进行的整理。已经移走的文件会记在历史里")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self.on_cancel_execute)
        footer.addWidget(self.btn_cancel)
        self.btn_execute = QPushButton("开始整理")
        self.btn_execute.setToolTip("按预览里勾选的项目移动文件。移动前会再确认一次")
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self.on_execute)
        footer.addWidget(self.btn_execute)
        center.addLayout(footer)
        center_host.setMinimumWidth(420)
        splitter.addWidget(center_host)

        insp_inner = QWidget()
        fill_panel(insp_inner, "#FFFFFF")
        insp = QVBoxLayout(insp_inner)
        insp.setContentsMargins(16, 12, 16, 16)
        insp.setSpacing(8)
        insp_kicker = QLabel("当前文件")
        insp_kicker.setStyleSheet(
            "color:#64748B;font-size:11px;font-weight:600;")
        insp.addWidget(insp_kicker)
        self.inspector_title = QLabel("还没有选中文件")
        self.inspector_title.setWordWrap(True)
        self.inspector_title.setStyleSheet("color:#0F172A;font-size:15px;font-weight:600;")
        insp.addWidget(self.inspector_title)
        self.inspector_meta = QLabel("在中间的表格里选中一行。")
        self.inspector_meta.setWordWrap(True)
        self.inspector_meta.setStyleSheet("color:#64748B;font-size:12px;")
        insp.addWidget(self.inspector_meta)

        def insp_field(caption):
            cap = QLabel(caption)
            cap.setStyleSheet("color:#64748B;font-size:12px;")
            val = QLabel("—")
            val.setWordWrap(True)
            val.setStyleSheet("color:#0F172A;font-weight:600;")
            insp.addWidget(cap)
            insp.addWidget(val)
            return val

        self.inspector_reason = insp_field("分类依据")
        self.inspector_evidence = insp_field("依据来源")
        self.inspector_content_cap = QLabel("正文摘录")
        self.inspector_content_cap.setStyleSheet("color:#64748B;font-size:12px;")
        self.inspector_content = QLabel("—")
        self.inspector_content.setWordWrap(True)
        self.inspector_content.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.inspector_content.setStyleSheet("color:#0F172A;font-weight:400;")
        self.inspector_content_cap.hide()
        self.inspector_content.hide()
        insp.addWidget(self.inspector_content_cap)
        insp.addWidget(self.inspector_content)
        self.inspector_ocr_cap = QLabel("图片文字")
        self.inspector_ocr_cap.setStyleSheet("color:#64748B;font-size:12px;")
        self.inspector_ocr = QLabel("—")
        self.inspector_ocr.setWordWrap(True)
        self.inspector_ocr.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.inspector_ocr.setStyleSheet("color:#0F172A;font-weight:400;")
        self.inspector_ocr_cap.hide()
        self.inspector_ocr.hide()
        insp.addWidget(self.inspector_ocr_cap)
        insp.addWidget(self.inspector_ocr)
        self.inspector_dir = insp_field("将放到")
        self.inspector_name = insp_field("文件名")
        self.inspector_note = QLabel("")
        self.inspector_note.setWordWrap(True)
        self.inspector_note.setStyleSheet(
            "color:#B45309;background:#FFFBEB;border:1px solid #FDE68A;"
            "padding:8px 10px;border-radius:6px;")
        self.inspector_note.hide()
        insp.addWidget(self.inspector_note)
        self.btn_override_selected = QPushButton("修改这一行")
        self.btn_override_selected.setToolTip(
            "改这一个文件的分类。只对本次预览生效，重新扫描或切换文件夹后恢复自动分类。\n"
            "目前只支持「按方向/学期/类别（推荐）」"
        )
        self.btn_override_selected.setEnabled(False)
        self.btn_override_selected.clicked.connect(self.on_override_selected)
        insp.addWidget(self.btn_override_selected)
        quiet = QLabel("现在只是预览。点击「开始整理」才会移动。")
        quiet.setWordWrap(True)
        quiet.setStyleSheet("color:#64748B;font-size:12px;")
        insp.addWidget(quiet)
        insp.addStretch(1)

        # Long filenames and excerpts must reflow within the scroll viewport.
        for label in insp_inner.findChildren(QLabel):
            if label.wordWrap():
                label.setTextFormat(Qt.TextFormat.PlainText)
                policy = label.sizePolicy()
                policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
                label.setSizePolicy(policy)

        insp_scroll = QScrollArea()
        insp_scroll.setWidgetResizable(True)
        insp_scroll.setFrameShape(QFrame.Shape.NoFrame)
        insp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        fill_panel(insp_scroll, "#FFFFFF")
        insp_scroll.setWidget(insp_inner)
        insp_scroll.setMinimumWidth(
            insp.minimumSize().width() + insp_scroll.verticalScrollBar().sizeHint().width())
        insp_scroll.setMaximumWidth(480)
        splitter.addWidget(insp_scroll)
        self.inspector_scroll = insp_scroll
        insp_scroll.hide()
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([side_need, 960, 0])
        self.main_splitter = splitter
        outer.addWidget(splitter, stretch=1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("请先选择文件夹，再重新扫描。")
        self._refresh_folder_list()
        self._update_inspector()
        self._mark_next_action()

    def _sidebar_width_for(self, side_inner: QWidget) -> int:
        """侧栏要能放下最长的一行字，避免分类名和复选框被裁掉。"""
        fm = side_inner.fontMetrics()
        labels = (
            "待定或识别失败时问 DeepSeek",
            "按方向/学期/类别（推荐）",
            "勾选看到的文件",
            "导入或保存方案",
            "重新生成预览",
            "不整理的目录",
            "将生成的目录",
            "查看重复文件",
        )
        text_w = max(fm.horizontalAdvance(text) for text in labels)
        # 复选框方块、按钮内边距、侧栏左右留白
        return text_w + 56

    def _same_folder(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        a = os.path.normcase(os.path.normpath(left))
        b = os.path.normcase(os.path.normpath(right))
        if a == b:
            return True
        try:
            return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
                os.path.realpath(right))
        except OSError:
            return False

    def _polish(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _refresh_folder_list(self):
        tree = getattr(self, "storage_tree", None)
        if tree is None or not hasattr(self, "path_edit"):
            return
        tree.reveal(self.path_edit.text().strip())

    def _preview_sidebar_folder(self, path: str):
        name = os.path.basename(path.rstrip("\\/")) or path
        self.statusBar().showMessage(f"{name}    {path}    双击设为整理空间")

    def _open_sidebar_space(self, path: str):
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，暂不能切换整理空间。")
            return
        if not path or not os.path.isdir(path):
            QMessageBox.warning(
                self, "归档盒", f"这个文件夹现在打不开：\n{path}")
            return
        if self._same_folder(path, self.path_edit.text().strip()):
            return
        self._open_recent_space(path)

    def _update_inspector(self):
        if not hasattr(self, "inspector_title"):
            return
        plan = self.current_plan
        rows = set()
        if hasattr(self, "table"):
            rows = {idx.row() for idx in self.table.selectedIndexes()}
        item = None
        if (plan is not None and len(rows) == 1):
            row = next(iter(rows))
            if 0 <= row < len(plan.items) and not self.table.isRowHidden(row):
                item = plan.items[row]
        if item is None:
            if len(rows) > 1:
                self._set_inspector_detail(self.inspector_title, "选中了多行")
                self._set_inspector_detail(self.inspector_meta, "一次只说明一个文件。请改选一行。")
            else:
                self._set_inspector_detail(self.inspector_title, "还没有选中文件")
                self._set_inspector_detail(self.inspector_meta, "在中间的表格里选中一行。")
            self._set_inspector_detail(self.inspector_reason, "—")
            self._set_inspector_detail(self.inspector_evidence, "—")
            self.inspector_content_cap.hide()
            self.inspector_content.hide()
            self._set_inspector_detail(self.inspector_dir, "—")
            self._set_inspector_detail(self.inspector_name, "—")
            self.inspector_ocr_cap.hide()
            self.inspector_ocr.hide()
            self.inspector_note.hide()
            self._set_inspector_open(len(rows) > 1)
            return
        self._set_inspector_open(True)
        self._set_inspector_detail(self.inspector_title, item.file.filename)
        folder = os.path.dirname(item.file.rel_path) or "所在文件夹的根目录"
        self._set_inspector_detail(self.inspector_meta, f"{folder} · {item.file.size_display}")
        self._set_inspector_detail(self.inspector_reason, item.rule_applied or "—")
        source_label = item.content_source or "未确定"
        extra_source = ""
        if getattr(item, "ambiguity", None):
            extra_source = "｜歧义，待确认"
        elif getattr(item, "user_resolved", False):
            extra_source = "｜用户已改选"
        elif getattr(item, "skipped", False):
            extra_source = "｜用户已跳过"
        self._set_inspector_detail(
            self.inspector_evidence,
            f"{source_label}{extra_source}｜{item.path_evidence or '相对路径'}")
        self._show_inspector_content(item)
        self._show_inspector_ocr(item)
        shown_dir = (item.target_dir_rel or "—").replace(os.sep, " / ")
        self._set_inspector_detail(self.inspector_dir, shown_dir)
        self._set_inspector_detail(self.inspector_name, item.target_filename or "—")
        if item.conflict_with_existing:
            self.inspector_note.setText(
                "同名文件已经存在。整理时改名，不覆盖原来的文件。")
            self.inspector_note.show()
        else:
            self.inspector_note.hide()

    @staticmethod
    def _set_inspector_detail(label: QLabel, text: str):
        # Give Qt wrap opportunities in filenames and paths with no spaces.
        # These display labels do not supply the paths used by file operations.
        label.setText(re.sub(r"[!-~]{7}(?=[!-~])", lambda match: match[0] + "\u200b", text))

    def _set_inspector_open(self, open_pane: bool):
        """按选中状态显示详情栏，让表格使用空闲空间。"""
        inspector = self.inspector_scroll
        if not open_pane:
            inspector.hide()
            return
        if not inspector.isHidden():
            return

        side, table, _ = self.main_splitter.sizes()
        table_min = self.main_splitter.widget(1).minimumWidth()
        preferred = min(280, max(160, table // 4))
        pane_width = max(inspector.minimumWidth(), min(preferred, table - table_min))
        inspector.show()
        self.main_splitter.setSizes([side, max(table_min, table - pane_width), pane_width])

    def _show_inspector_content(self, item):
        """显示正文摘录或读取失败原因；图片走「图片文字」那一栏。"""
        text = (getattr(item, "content_text", "") or "").strip()
        error = (getattr(item, "content_error", "") or "").strip()
        if not text and not error:
            self.inspector_content_cap.hide()
            self.inspector_content.hide()
            return
        self.inspector_content_cap.show()
        self.inspector_content.show()
        if text:
            shown = text if len(text) <= 500 else text[:500] + "…"
            if error:
                shown += f"\n（{error}）"
            self.inspector_content.setText(shown)
        else:
            self.inspector_content.setText(error)

    def _show_inspector_ocr(self, item):
        if item.file.file_type != "图片":
            self.inspector_ocr_cap.hide()
            self.inspector_ocr.hide()
            return
        self.inspector_ocr_cap.show()
        self.inspector_ocr.show()
        if item.ocr_error:
            self.inspector_ocr.setText(f"识别失败：{item.ocr_error}")
        elif item.ocr_text is None:
            self.inspector_ocr.setText(
                "还没识别。勾选左侧「识别图片文字」，再点「重新生成预览」。")
        elif not item.ocr_text.strip():
            self.inspector_ocr.setText("没有识别出文字。")
        else:
            text = item.ocr_text.strip()
            if len(text) > 500:
                text = text[:500] + "…"
            self.inspector_ocr.setText(text)

    def _sync_intake_label(self):
        if not hasattr(self, "btn_intake"):
            return
        count = len(self.intake)
        self.btn_intake.setText(
            "待整理清单" if count == 0 else f"待整理清单  {count}")

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
            self._touch_recent_space(chosen)
            self.statusBar().showMessage(f"已选择：{chosen}")
            self._mark_next_action()

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
        self._pending_intake_selection = []
        self._pending_drop_select = []
        self._quiet_drop_scan = False
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
        # S2 F-07/F-08：目录改名/移动、保护目录暂时不存在时都要说明，
        # 不静默丢弃配置项。
        self.manual_protect, protect_notes = load_manual_protect_report(root)
        if protect_notes and not self._closing:
            QMessageBox.information(
                self, "保护目录配置",
                "手动保护配置已按当前目录继续使用：\n\n" + "\n\n".join(protect_notes))
        # 按根目录加载推荐模式分类配置（只读；不存在时不创建文件）
        self._load_root_config(root)
        # 阶段 2：打开整理空间时完成旧 JSON 长期规则的一次性迁移
        self._refresh_preferences(root)
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
            self._refresh_folder_list()
            self._update_inspector()
            return
        # S3 / filesafe F-14：history.db 损坏时已隔离旧文件并重建，提示用户
        if self._storage.recovery_note and not self._closing:
            append_startup_log(self._storage.recovery_note)
            QMessageBox.warning(self, "整理记录已重建",
                                self._storage.recovery_note)
        # 阶段 5：打开整理空间时把历史里可复用的分类决定导入一次。
        self._import_learning_feedback(root)
        # S2 F-05/F-06：目录改名/移动后历史与撤销仍然可用，但要说清楚
        mismatch = self._storage.root_mismatch_note()
        if mismatch and not self._closing:
            append_startup_log(mismatch)
            self.statusBar().showMessage(
                "整理记录里的根目录与当前目录不一致（目录已改名或移动），"
                "历史与撤销仍按当前目录使用。")
            self._root_mismatch_note = mismatch
        else:
            self._root_mismatch_note = ""
        # 检查是否有未完成的批次
        self._refresh_folder_list()
        self._update_inspector()
        self._check_pending_batch()
        # 打开已有空间后，不必先扫描：有记录库就能看历史，有可撤销批次
        # 就能撤销。_update_button_states 会按 _storage 和 undo 状态刷新。
        self._update_button_states()

    def _check_pending_batch(self):
        """启动/切换目录时检查 .organizer/history.db 中的未完成批次。"""
        if self._storage is None:
            return
        if self._close_pending or self._closing:
            return  # 关闭等待期间不再弹窗、不再启动撤销等业务任务

        # 优先处理「上次运行被中断」的批次：核对磁盘实际状态
        interrupted = interrupted_batch_id(self._storage)
        self._interrupted_batch = interrupted
        if interrupted is not None:
            reply = QMessageBox.question(
                self, "归档盒",
                f"检测到该目录的整理批次 #{interrupted} 上次运行被中断（未完成）。\n\n"
                "现在核对磁盘实际状态并恢复记录吗？\n"
                "（只核对与补记，不会覆盖、删除或再次移动文件）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                # V1.2 第二轮：核对恢复移入后台线程（与历史页同一流程），
                # 结果弹窗由 _on_recover_done 统一处理
                self.recover_interrupted_batch(interrupted)
            return

        pending = self._storage.get_pending_batch()
        if pending is not None and pending.status != "done":
            self.btn_undo.setEnabled(True)
            # S3 / guib F-19：用中文状态、不暴露内部状态码与 ISO 时间；
            # 默认按钮是「否」。
            status_text = BATCH_STATUS_LABELS.get(pending.status, "需要核对")
            reply = QMessageBox.question(
                self, "归档盒",
                f"检测到该目录有一个需要处理的整理批次（#{pending.batch_id}，"
                f"状态：{status_text}）。\n\n"
                "可以先继续扫描，或稍后在主界面点「撤销上一次」处理这个批次。\n\n"
                "现在尝试撤销吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.on_undo()

    def on_scan(self):
        if self._close_pending or self._closing:
            return  # 关闭等待期间不启动新的扫描
        # S3 / guia F-12：扫描不接受重入，避免两个扫描结果互相覆盖
        if self._working or self._plan_working:
            self.statusBar().showMessage("正在处理，请等当前任务结束后再重新扫描。")
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return
        self._pending_drop_select = []
        self._carry_skipped_rels = []
        self._quiet_drop_scan = False
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
                "清单为空。请先通过「编辑待整理清单」或拖放加入文件/目录。")
            return
        self._plan_scope = "intake"
        self._update_scope_label()
        self._start_scan(root)

    def _start_scan(self, root: str, only_paths=None):
        # 已有计划任务在跑时，先让它全部过期，避免旧结果落到新扫描上
        self._abandon_plan_generation()
        if self.current_plan is not None or self._plan_valid:
            self._discard_plan("正在重新扫描，旧预览已失效；扫描完成后会重新生成计划")

        self.table.setRowCount(0)
        phase = "正在计算目标位置……" if only_paths else "扫描中……"
        self._scan_seq_counter += 1
        self._current_scan_seq = self._scan_seq_counter
        seq = self._current_scan_seq
        self.task.begin("scan", root, phase=phase)
        self._set_working(True, phase)

        # V1.1: 默认保护归档盒源码目录 + 用户手动保护目录
        protect = [(DEFAULT_PROTECT_APP_SRC, "default", "默认保护（归档盒源码）")]
        protect.extend(manual_scan_specs(root, self.manual_protect))

        try:
            self._scan_worker = ScanWorker(
                root, protect_dirs=protect, only_paths=only_paths, parent=self,
                seq=seq)
            self._scan_worker.finished_ok.connect(self._on_scan_done)
            self._scan_worker.failed.connect(self._on_scan_failed)
            self._scan_worker.finished.connect(
                lambda s=seq: self._on_scan_worker_finished(s))
            progress_signal = getattr(self._scan_worker, "progress", None)
            if progress_signal is not None:
                progress_signal.connect(self._on_scan_progress)
            stage_signal = getattr(self._scan_worker, "stage", None)
            if stage_signal is not None:
                stage_signal.connect(self._on_scan_stage)
            self._refresh_busy_ui(phase)
            self._scan_worker.start()
        except Exception as e:
            self._abort_task_start("scan", "扫描", e)

    def _on_scan_stage(self, seq: int, stage: str):
        if seq != self._current_scan_seq or self._close_pending or self._closing:
            return
        if self.task.kind != "scan":
            return
        self.task.phase = stage
        self.statusBar().showMessage(f"{stage}…")

    def _on_scan_progress(self, seq: int, seen: int, current: str):
        if seq != self._current_scan_seq:
            return
        if self._close_pending or self._closing:
            return
        if self.task.kind != "scan" or not self._working:
            return
        self.task.update_progress(seen, 0, current)
        self._apply_progress(seen, 0)
        if seen:
            self.progress_label.setText(f"已发现 {seen}")
        suffix = f" · {current}" if current else ""
        self.statusBar().showMessage(f"扫描中：已发现 {seen} 个文件{suffix}")

    def _on_scan_worker_finished(self, seq: int | None = None):
        """扫描线程结束。只清理扫描态，不能覆盖仍在运行的计划生成态。"""
        if seq is not None and seq != self._current_scan_seq:
            return
        self.task.end("scan")     # 若计划生成已接续开始，kind 保持为 plan
        self._set_working(False)

    def _on_scan_done(self, seq_or_result, result: ScanResult | None = None):
        if result is None:
            result = seq_or_result
            seq = self._current_scan_seq
        else:
            seq = seq_or_result
        if seq != self._current_scan_seq:
            return  # 旧扫描的完成信号，不能写入这次扫描的结果
        worker = getattr(self, "_scan_worker", None)
        if worker is not None and getattr(worker, "cancel_event", None) is not None \
                and worker.cancel_event.is_set():
            # S3 / guia F-06：用户取消扫描后不能用半成品结果生成计划
            self._discard_plan("扫描已取消，旧预览已失效；请重新扫描")
            self.statusBar().showMessage("扫描已取消（结果不完整，未生成计划）。")
            return
        self._full_scan_result = result
        self._scan_generation += 1
        quiet_drop = self._quiet_drop_scan
        self._quiet_drop_scan = False
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
            self._pending_drop_select = []
            message = self._scope_empty_message()
            self._discard_plan(message)
            QMessageBox.information(self, "待整理清单", message)
            return

        # 先启动计划生成（后台），再统一弹一个扫描提示，
        # 避免连续 3–4 个模态框挡住已经开始的计划（S3 / guia F-15）。
        self._generate_and_show_plan()

        notices: list[str] = []
        if result.root_project_marker and not quiet_drop:
            notices.append(
                "根目录含有项目标记（"
                + result.root_project_marker
                + "），没有把整个文件夹当成项目。\n"
                "子目录里的项目仍会跳过。根目录里的课程文件会进入预览。"
            )
        if result.protected_roots and not quiet_drop:
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
            notices.append("以下目录被整体保护（不会被整理）：\n\n"
                           + "\n".join(protect_lines))

        if result.junction_dirs:
            notices.append(
                "以下目录是 junction / 重解析点（指向其他位置的链接目录），"
                "已整体跳过，其中的文件不会进入整理计划：\n\n"
                + "\n".join(f"  • {p}" for p in result.junction_dirs[:20])
                + ("\n  …" if len(result.junction_dirs) > 20 else "")
                + "\n\n如需整理这些位置，请对链接目标的真实目录单独扫描。")

        # S3 / filesafe F-12：跳过目录必须给出路径，不能只给一个数字
        user_skips = []
        for path, why in result.skipped_dirs:
            base = os.path.basename(str(path).rstrip("\\/"))
            if why == "保留目录名" and base.lower() in {".organizer", "_已整理"}:
                continue
            user_skips.append((path, why))
        if user_skips:
            notices.append(
                f"以下 {len(user_skips)} 个目录/文件被跳过"
                "（不会进入整理计划）：\n\n"
                + "\n".join(f"  • {p}（{why}）"
                           for p, why in user_skips[:40])
                + ("\n  …" if len(user_skips) > 40 else ""))

        if result.permission_errors:
            notices.append(
                f"有 {result.permission_errors} 个文件/目录无读取权限或读取失败，"
                "已跳过（原因同时写入启动日志）。")
        if result.errors:
            notices.append(
                f"扫描遇到 {len(result.errors)} 个问题：\n\n"
                + "\n".join(result.errors[:10])
                + ("\n…" if len(result.errors) > 10 else ""))

        if notices:
            QMessageBox.information(self, "扫描提示", "\n\n────────\n\n".join(notices))

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

    def _on_scan_failed(self, seq_or_err, err: str | None = None):
        if err is None:
            err = seq_or_err
            seq = self._current_scan_seq
        else:
            seq = seq_or_err
        if seq != self._current_scan_seq:
            return
        root = self.path_edit.text().strip()
        self._discard_plan("扫描失败，旧预览已失效；请排除问题后重新扫描生成新预览")
        # S4 / guia F-25：状态栏说明是哪个目录、失败在扫描阶段
        self.statusBar().showMessage(f"扫描失败（{root or '未选择目录'}）：{err}")
        append_startup_log(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描失败："
            f"{root or '未选择目录'}：{err}")
        if self._close_pending or self._closing:
            return
        QMessageBox.critical(
            self, "归档盒",
            f"扫描时发生异常：\n{root or '未选择目录'}\n\n{err}")

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
            "⚠ 待整理范围已变化：旧预览已失效，请点击「重新生成预览」重新生成")
        self.invalid_label.show()
        self._update_button_states()
        self.statusBar().showMessage(
            "待整理清单已更新。请点击「重新生成预览」按新范围生成计划后再执行。")

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
        self._touch_recent_space(path)
        self.statusBar().showMessage(f"已打开整理空间：{path}")
        self._mark_next_action()
        return True

    def on_agent(self):
        """阶段 6：打开 AI 管家，用一句话查找并生成方案。"""
        if self._close_pending or self._closing:
            return
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，请稍后再用 AI 管家。")
            return
        root = self._current_root()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(
                self, "归档盒", "请先选择一个文件夹，再用 AI 管家查找。")
            return
        from agent_dialog import AgentDialog
        if self._agent_dialog is not None:
            self._agent_dialog.raise_()
            self._agent_dialog.activateWindow()
            return
        # 与普通扫描使用同一套保护规则：默认保护源码目录 + 用户手动保护目录
        protect = [(DEFAULT_PROTECT_APP_SRC, "default", "默认保护（归档盒源码）")]
        protect.extend(manual_scan_specs(root, self.manual_protect))
        dlg = AgentDialog(self, root, protect, parent=self)
        dlg.finished.connect(lambda _=0, d=dlg: self._clear_agent_dialog_ref(d))
        self._agent_dialog = dlg
        dlg.show()

    def _clear_agent_dialog_ref(self, dlg):
        if self._agent_dialog is dlg:
            self._agent_dialog = None

    def adopt_agent_plan(self, plan, note: str = "") -> bool:
        """把 AI 管家方案交给现有预览；执行仍走 on_execute。"""
        if self._close_pending or self._closing:
            return False
        if self._working or self._plan_working:
            QMessageBox.information(
                self, "归档盒", "当前有后台任务运行，方案暂不能放入预览。")
            return False
        if plan is None or not getattr(plan, "items", None):
            QMessageBox.information(self, "归档盒", "这个方案没有需要移动的文件。")
            return False
        current = self._current_root()
        if (not current
                or os.path.realpath(current) != os.path.realpath(plan.root)):
            QMessageBox.warning(
                self, "归档盒",
                "整理空间已经切换，请重新打开 AI 管家生成方案。")
            return False
        if self._storage is None:
            try:
                self._storage = Storage(plan.root)
            except OSError as exc:
                QMessageBox.critical(
                    self, "归档盒",
                    f"无法在该目录建立整理记录（.organizer）：\n{exc}")
                return False
        self.current_plan = plan
        self._plan_valid = True
        self._plan_scope = "space"
        self._plan_scope_note = note or "AI 管家查找结果"
        self._populate_table(plan, reset_filters=True)
        self._invalidate_confirmation()
        self._update_plan_counts()
        self._refresh_ambiguity_panel()
        self._update_button_states()
        self.invalid_label.hide()
        self.statusBar().showMessage(
            f"{self._plan_scope_note}；请核对方案后在主界面点「开始整理」。")
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
            self._apply_pending_intake_selection(refresh_if_empty=True)
            return
        dlg = IntakeDialog(
            get_root=lambda: self.path_edit.text().strip(),
            get_summary=self._intake_summary,
            get_items=lambda: self.intake,
            add_paths=self._add_and_preview_intake,
            remove_path=self._remove_intake_path,
            clear_items=self._clear_intake,
            parent=self,
        )
        dlg.finished.connect(lambda _=0: self._clear_intake_dialog_ref(dlg))
        self._intake_dialog = dlg
        dlg.show()
        self._apply_pending_intake_selection()

    def _clear_intake_dialog_ref(self, dlg):
        if self._intake_dialog is dlg:
            self._intake_dialog = None

    def _refresh_intake_dialog(self):
        if self._intake_dialog is not None:
            try:
                self._intake_dialog.refresh()
            except RuntimeError:
                self._intake_dialog = None

    def _focus_new_intake_items(self, paths):
        """记下刚拖入的路径；清单窗口开着时立刻高亮对应行。"""
        self._pending_intake_selection = [p for p in (paths or []) if p]
        if self._intake_dialog is None or not self._pending_intake_selection:
            return
        try:
            self._intake_dialog.refresh(
                select_paths=self._pending_intake_selection)
            self._intake_dialog.raise_()
            self._pending_intake_selection = []
        except RuntimeError:
            self._intake_dialog = None

    def _apply_pending_intake_selection(self, refresh_if_empty=False):
        dlg = self._intake_dialog
        if dlg is None:
            return
        paths = list(self._pending_intake_selection)
        try:
            if paths:
                dlg.refresh(select_paths=paths)
                self._pending_intake_selection = []
            elif refresh_if_empty:
                dlg.refresh()
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

    def _warn_intake(self, errors):
        """把没能登记的原因弹出来。成功加入不走这里。"""
        lines = []
        for message in errors or []:
            text = (message or "").strip()
            if text and text not in lines:
                lines.append(text)
        if not lines:
            return
        shown = lines[:8]
        body = "\n\n".join(shown)
        if len(lines) > len(shown):
            body += f"\n\n还有 {len(lines) - len(shown)} 条。"
        parent = self._intake_dialog or self
        QMessageBox.warning(parent, "待整理清单", body)

    def _confirm_intake_root(self, candidate_root: str, source_path: str):
        """未选空间时询问是否采用候选目录。

        返回 True/False 表示用户的选择；无法弹出询问时返回 None。
        """
        if not candidate_root or not os.path.isdir(candidate_root):
            return None
        parent = self._intake_dialog or self
        try:
            source = os.path.realpath(os.path.abspath(source_path or ""))
        except (OSError, ValueError):
            source = ""
        if source and source != candidate_root:
            text = (
                "尚未选择整理空间。\n\n"
                "是否将文件所在目录设为当前整理空间，并把所选文件加入待整理清单？\n"
                f"{candidate_root}\n\n"
                "不会创建目录，也不会移动文件。配置与历史按该目录单独保存。")
        else:
            text = (
                "尚未选择整理空间。\n\n"
                "是否将以下目录设为当前整理空间？\n"
                f"{candidate_root}\n\n"
                "（不会创建目录；其配置与历史仍按该目录隔离）")
        reply = QMessageBox.question(
            parent, "设为整理空间", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return reply == QMessageBox.Yes

    def _add_intake_paths(self, paths) -> list:
        """拖放与对话框共用的加入逻辑；返回逐项说明。"""
        messages = []
        if self._close_pending or self._closing:
            message = "窗口正在关闭，未修改待整理清单。"
            self._warn_intake([message])
            return [message]
        if self._working or self._plan_working:
            message = "当前有后台任务运行，未修改待整理清单。"
            self._warn_intake([message])
            return [message]
        changed = False
        declined_roots = set()
        errors = []
        for path in paths:
            root = self.path_edit.text().strip()
            res = self.intake.add(path, root or None)
            explained = False
            if (res.status == "needs_root" and res.candidate_root
                    and res.candidate_root not in declined_roots):
                answer = self._confirm_intake_root(res.candidate_root, path)
                if answer:
                    self.path_edit.setText(res.candidate_root)
                    self.btn_scan.setEnabled(True)
                    self._reset_for_new_dir(res.candidate_root,
                                            keep_intake_dialog=True)
                    self._touch_recent_space(res.candidate_root)
                    root = self.path_edit.text().strip()
                    res = self.intake.add(path, root or None)
                elif answer is False:
                    declined_roots.add(res.candidate_root)
                    explained = True
            elif (res.status == "needs_root" and res.candidate_root
                    and res.candidate_root in declined_roots):
                explained = True
            messages.append(res.message)
            if res.status in ("added", "coalesced"):
                changed = True
            elif not explained:
                errors.append(res.message)
        self._warn_intake(errors)
        if changed:
            self._on_intake_changed()
        self._refresh_intake_dialog()
        self._update_button_states()
        return messages

    def _add_and_preview_intake(self, paths) -> list:
        """选中文件后立刻计算它们会放到哪里，不用再点扫描。"""
        messages = self._add_intake_paths(paths)
        self._schedule_intake_preview(paths)
        return messages

    def _schedule_intake_preview(self, select_paths):
        """等这一轮添加结束后再计算，避免连续选中多个文件时互相打断。"""
        if not hasattr(self, "_scheduled_preview_paths"):
            self._scheduled_preview_paths = []
        for path in select_paths or []:
            if path and path not in self._scheduled_preview_paths:
                self._scheduled_preview_paths.append(path)
        timer = getattr(self, "_preview_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._run_scheduled_preview)
            self._preview_timer = timer
        timer.start(0)

    def _run_scheduled_preview(self):
        paths = list(getattr(self, "_scheduled_preview_paths", []) or [])
        self._scheduled_preview_paths = []
        self._preview_current_intake(select_paths=paths)

    def _preview_current_intake(self, select_paths=None) -> bool:
        """按待整理清单直接生成目标位置预览。返回是否已经开始。"""
        if self._close_pending or self._closing:
            return False
        if self._working or self._plan_working:
            return False
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root) or len(self.intake) == 0:
            return False
        self._plan_scope = "intake"
        chosen = []
        for path in select_paths or []:
            if path and os.path.isfile(path):
                chosen.append(normalize_path(path))
        if not chosen:
            chosen = [item.path for item in self.intake.items if item.kind == "file"]
        self._pending_drop_select = chosen
        self._sync_intake_label()
        self._update_scope_label()
        self._update_button_states()
        if hasattr(self, "invalid_label"):
            self.invalid_label.hide()
        same_scan = (
            self._full_scan_result is not None and
            normalize_path(self._full_scan_result.root) == normalize_path(root))
        if same_scan:
            resolved = self.intake.resolve(root, self._full_scan_result)
            if (resolved.included_count > 0 and not resolved.not_scanned
                    and not resolved.unsupported):
                self._generate_and_show_plan()
                self.statusBar().showMessage(
                    "正在计算这些文件会放到哪里。现在还不会移动。")
                return True
        self._quiet_drop_scan = True
        self._start_scan(root, only_paths=self.intake.paths())
        self.statusBar().showMessage(
            "正在计算这些文件会放到哪里。现在还不会移动。")
        return True

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
            self._warn_intake(["窗口正在关闭，未修改待整理清单。"])
            return
        if self._working or self._plan_working:
            self._warn_intake(["当前有后台任务运行，未修改待整理清单。"])
            return
        self.intake.clear()
        self._pending_intake_selection = []
        self._on_intake_changed()
        self._refresh_intake_dialog()

    def _on_intake_changed(self):
        self._update_scope_label()
        if self._plan_scope == "intake":
            self._invalidate_for_scope_change()
        self._update_button_states()

    # ---- 拖放 ----
    def _attach_drop_watch(self, widget):
        """让文件能落在表格、按钮上，而不只落在窗口空白处。对话框自己处理拖放。"""
        if widget.property("_archivebox_drop_watch"):
            return
        widget.setProperty("_archivebox_drop_watch", True)
        widget.installEventFilter(self)
        if widget is not self and not self._widget_in_dialog(widget):
            widget.setAcceptDrops(True)

    def _widget_in_dialog(self, obj) -> bool:
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None and widget is not self:
            if isinstance(widget, QDialog):
                return True
            widget = widget.parentWidget()
        return False

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._attach_drop_watch(child)
            return False
        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove,
                     QEvent.Type.Drop):
            mime = event.mimeData() if hasattr(event, "mimeData") else None
            has_urls = mime is not None and mime.hasUrls()
            if has_urls and not self._widget_in_dialog(obj):
                if (self._close_pending or self._closing
                        or self._working or self._plan_working):
                    event.ignore()
                elif etype == QEvent.Type.Drop:
                    self.dropEvent(event)
                else:
                    event.acceptProposedAction()
                return True
        return super().eventFilter(obj, event)

    def _wants_file_drop(self, obj, event) -> bool:
        if not hasattr(event, "mimeData"):
            return False
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return False
        if self._widget_in_dialog(obj):
            return False
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return False
        return True

    def dragEnterEvent(self, event):
        if self._wants_file_drop(self, event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._wants_file_drop(self, event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        paths = [p for p in paths if p]
        if not paths:
            return
        event.acceptProposedAction()
        files = [p for p in paths if os.path.isfile(p) and not os.path.isdir(p)]
        dirs = [p for p in paths if os.path.isdir(p)]
        if files and not dirs:
            self._drop_files_to_select(files)
            return
        if not files and not dirs:
            message = "没有拖入可读取的文件或文件夹。"
            self.statusBar().showMessage(message)
            self._warn_intake([message])
            return
        messages = self._add_intake_paths(paths)
        self._focus_new_intake_items(paths)
        started = self._preview_current_intake(select_paths=paths)
        if messages and not started:
            self.statusBar().showMessage("；".join(messages[-3:]))

    def _drop_files_to_select(self, paths):
        """拖入的文件直接成为预览里勾选并选中的行。不会马上移动。"""
        files = [p for p in paths if p and os.path.isfile(p)]
        if not files:
            message = "没有拖入可读取的文件。"
            self.statusBar().showMessage(message)
            self._warn_intake([message])
            return
        matched = self._check_only_dropped_files(files, require_all=True)
        if matched is None:
            self._open_dropped_files(files)
            return
        all_matched, selected = matched
        if not selected:
            # 拖入的全是已跳过文件：保持原位，也不重开预览。
            names = "、".join(f"「{os.path.basename(p)}」" for p in all_matched[:3])
            more = f" 等 {len(all_matched)} 个文件" if len(all_matched) > 3 else ""
            self.statusBar().showMessage(
                f"{names}{more}已跳过，保持原位；其他文件勾选未变。")
            return
        extra = ""
        if len(selected) < len(all_matched):
            extra = f"；{len(all_matched) - len(selected)} 个已跳过的文件保持原位"
        self._announce_selected_files(selected, extra=extra)

    def _open_dropped_files(self, files):
        """当前预览里没有这些文件时，只为它们生成预览并在完成后选中。"""
        normalized = [normalize_path(p) for p in files]
        root = self.path_edit.text().strip()
        root_ok = bool(root) and os.path.isdir(root)
        if root_ok and all(is_within(root, p) for p in normalized):
            target_root = root
        else:
            parents = {os.path.dirname(p) for p in normalized}
            if len(parents) != 1:
                message = ("这几个文件不在同一个文件夹里。请一次拖一个文件，"
                           "或先选择它们所在的文件夹。")
                self.statusBar().showMessage(message)
                self._warn_intake([message])
                return
            target_root = parents.pop()
            if not os.path.isdir(target_root):
                message = "找不到这个文件所在的文件夹。"
                self.statusBar().showMessage(message)
                self._warn_intake([message])
                return
            switched = ((not root_ok) or
                        normalize_path(target_root) != normalize_path(root))
            if switched:
                # S3 / guia F-11 / guib F-22：换根会清空清单与勾选，必须先确认
                reply = QMessageBox.question(
                    self, "切换整理空间？",
                    f"拖入的文件在另一个文件夹：\n{target_root}\n\n"
                    f"当前整理空间：\n{root or '（还没有选择）'}\n\n"
                    "切换后会清空待整理清单、当前预览和勾选，"
                    "并把范围改成「只预览这些文件」。\n"
                    "不会移动、删除任何文件。\n\n要切换到上面的文件夹吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    message = "已取消切换整理空间，拖入的文件没有加入清单。"
                    self.statusBar().showMessage(message)
                    return
                self.path_edit.setText(target_root)
                self.btn_scan.setEnabled(True)
                self._reset_for_new_dir(target_root)
                self._touch_recent_space(target_root)
                if self._working or self._plan_working:
                    message = "这个文件夹有未完成的整理。处理完之后再拖入文件。"
                    self.statusBar().showMessage(message)
                    self._warn_intake([message])
                    return
        self.intake.clear()
        notes = []
        problems = []
        added = 0
        for path in normalized:
            res = self.intake.add(path, target_root)
            notes.append(res.message)
            if res.status in ("added", "duplicate", "covered", "coalesced"):
                added += 1
            else:
                problems.append(res.message)
        if problems:
            self._warn_intake(problems)
        if added == 0:
            message = notes[-1] if notes else "没能加入这个文件。"
            self.statusBar().showMessage(message)
            if not problems:
                self._warn_intake([message])
            return
        self._plan_scope = "intake"
        self._pending_drop_select = list(normalized)
        # 这次预览可能重建计划（新文件不在已完成扫描里），先记下当前计划里
        # 已跳过的相对路径，等新计划发布时套用，避免跳过状态丢失后歧义重现。
        self._carry_skipped_rels = [
            item.file.rel_path for item in (self.current_plan.items
                                            if self.current_plan is not None else [])
            if getattr(item, "skipped", False)
        ]
        self._sync_intake_label()
        self._update_scope_label()
        self._update_button_states()
        self._focus_new_intake_items(normalized)
        self._preview_current_intake(select_paths=normalized)

    def _apply_pending_drop_select(self):
        paths = list(self._pending_drop_select)
        if not paths:
            return
        self._pending_drop_select = []
        matched = self._check_only_dropped_files(paths, require_all=False)
        if matched is None:
            message = ("拖入的文件没有出现在预览里。它可能受保护、被排除，"
                       "或已经不在这个文件夹中。")
            self.statusBar().showMessage(message)
            self._warn_intake([message])
            return
        all_matched, selected = matched
        if not selected:
            names = "、".join(f"「{os.path.basename(p)}」" for p in all_matched)
            self.statusBar().showMessage(f"{names} 已跳过，保持原位。")
            return
        extra = ""
        wanted = {normalize_path(p) for p in paths}
        if len(all_matched) < len(wanted):
            extra = "有的文件没有进入预览。"
        if len(selected) < len(all_matched):
            extra += f"{len(all_matched) - len(selected)} 个已跳过的文件保持原位。"
        self._announce_selected_files(selected, extra=extra)

    def _check_only_dropped_files(self, paths, require_all=True):
        """只勾选这些文件，并选中对应行。对不上时不改当前勾选。

        返回 (匹配到的文件, 实际勾选的文件)；对不上返回 None。
        已跳过（skipped）的条目永远不勾选：拖回它只定位该行，保持原位。
        整批都是已跳过文件时，不改动其他文件的勾选。
        """
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return None
        if self.table.rowCount() != len(plan.items):
            return None
        wanted = []
        for path in paths:
            if path:
                wanted.append(normalize_path(path))
        wanted_set = set(wanted)
        if not wanted_set:
            return None
        rows = []
        for index, item in enumerate(plan.items):
            if normalize_path(item.file.abs_path) in wanted_set:
                rows.append(index)
        if not rows:
            return None
        if require_all and len(rows) != len(wanted_set):
            return None
        if any(self.table.isRowHidden(row) for row in rows):
            self._clear_plan_filters()
        normal_rows = [row for row in rows
                       if not getattr(plan.items[row], "skipped", False)]
        skipped_rows = [row for row in rows if row not in normal_rows]
        self._suppress_cell_signals = True
        try:
            def _sync(index, value):
                cell = self.table.cellWidget(index, 0)
                cb = cell.findChild(QCheckBox) if cell is not None else None
                if cb is not None:
                    cb.blockSignals(True)
                    cb.setChecked(value)
                    cb.blockSignals(False)

            if normal_rows:
                chosen = set(normal_rows)
                for index, item in enumerate(plan.items):
                    # 已跳过的条目保持未勾选，不因拖放重新纳入执行批次。
                    selected = (index in chosen
                                and not getattr(item, "skipped", False))
                    item.selected = selected
                    _sync(index, selected)
            else:
                # 整批都是已跳过文件：不动其他文件的勾选，跳过条目保持未勾选。
                for index in skipped_rows:
                    plan.items[index].selected = False
                    _sync(index, False)
        finally:
            self._suppress_cell_signals = False
        self.table.clearSelection()
        selection = self.table.selectionModel()
        if selection is not None:
            flags = (QItemSelectionModel.SelectionFlag.Select
                     | QItemSelectionModel.SelectionFlag.Rows)
            for row in rows:
                selection.select(self.table.model().index(row, 1), flags)
            selection.setCurrentIndex(
                self.table.model().index(rows[0], 1),
                QItemSelectionModel.SelectionFlag.Current)
        first = self.table.item(rows[0], 1)
        if first is not None:
            self.table.scrollToItem(first)
        self._update_plan_counts()
        self._update_button_states()
        return (
            [plan.items[row].file.abs_path for row in rows],
            [plan.items[row].file.abs_path for row in normal_rows],
        )

    def _announce_selected_files(self, paths, extra=""):
        names = [os.path.basename(p) for p in paths if p]
        if not names:
            return
        if len(names) == 1:
            label = f"「{names[0]}」"
            text = f"已选中{label}。其他文件已取消勾选，点「开始整理」只会移动这一个。"
        elif len(names) <= 3:
            label = "、".join(f"「{name}」" for name in names)
            text = f"已选中{label}。其他文件已取消勾选。"
        else:
            label = "、".join(f"「{name}」" for name in names[:3])
            text = f"已选中{label} 等 {len(names)} 个文件。其他文件已取消勾选。"
        plan = self.current_plan
        if (plan is not None and len(names) == 1 and plan.total == 1
                and plan.selected_count == 1):
            text = f"已选中{label}。预览里只有这一个文件，点「开始整理」才会移动。"
        if extra:
            text += extra
        self.statusBar().showMessage(text)

    def _import_learning_feedback(self, root: str):
        """打开空间 / 执行 / 撤销后，把历史里可复用的记录导入学习反馈。

        只用只读连接；旧库没有学习表或历史库不可读时不影响打开空间、
        读取偏好与生成预览。
        """
        if not root or not os.path.isdir(root):
            return None
        try:
            result = learning.import_history_feedback(root)
        except Exception as e:
            append_startup_log(f"学习反馈暂不可用，已跳过历史导入：{e}")
            return None
        if result.note:
            append_startup_log(result.note)
        return result

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

    def _refresh_preferences(self, root: str) -> bool:
        """打开空间 / 准备生成预览时：做一次性 JSON 迁移并检查偏好库。

        偏好库是长期匹配的唯一来源。空间库打不开时只使用仍可读的全局
        偏好，绝不回退 JSON 里的 long_term_rules；两边都不可用时就不应用
        任何长期规则，并在状态栏与日志中说明原因。
        """
        self._preferences_available = False
        self._preference_note = ""
        if not root or not os.path.isdir(root):
            return False
        notes: list = []

        def add_note(text: str):
            if not text:
                return
            key = text.split("：", 1)[0]
            if any(n.split("：", 1)[0] == key for n in notes):
                return
            notes.append(text)

        try:
            inserted = ensure_migrated(root, self.classification_config)
        except Exception as e:
            inserted = 0
            add_note(f"本空间偏好库不可用：{e}")
        _effective, read_note = effective_preferences_resilient(root)
        for part in (read_note or "").split("；"):
            add_note(part)
        self._preferences_available = True
        self._preference_note = "；".join(notes)
        if inserted:
            append_startup_log(
                f"已把 classification_config.json 中的 {inserted} 条长期规则"
                "一次性迁入本空间偏好库；JSON 原内容保持不变。")
        if self._preference_note:
            append_startup_log(
                self._preference_note + "；本轮不使用 JSON 长期规则。")
        return True

    def _update_config_label(self):
        if not hasattr(self, "config_label"):
            return
        root = self.path_edit.text().strip()
        if not root:
            self.config_label.setText(
                "选择文件夹后，这里会显示正在使用的分类规则。")
            self.config_label.setStyleSheet("color:#475569;font-size:12px;")
            self.config_label.setToolTip("")
            return
        if self._config_error:
            self.config_label.setText(
                "分类规则不可用。请打开「学期和目录规则」修复或恢复默认，原文件已保留。")
            self.config_label.setStyleSheet("color:#B71C1C;font-size:12px;")
            self.config_label.setToolTip(self._config_error)
            return
        which = "内置默认" if self._config_source == "default" else "这个文件夹里保存"
        self.config_label.setText(
            f"正在使用{which}的学期和目录规则。")
        self.config_label.setStyleSheet("color:#475569;font-size:12px;")
        self.config_label.setToolTip(
            config_summary(self.classification_config)
            + "\n只在「按方向/学期/类别（推荐）」下使用。")

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

        ocr_texts, ocr_errors = ocr_maps_from_plan(self.current_plan)
        # 阶段 2：长期修正规则页读写偏好库；先做一次性迁移再展示。
        # 全局库损坏时只展示本空间偏好，保存也只写本空间，不因全局库不可用
        # 而拒绝保存，也不会删除其他窗口刚写入的全局偏好。
        preference_rows = None
        global_error = ""
        try:
            ensure_migrated(root, self.classification_config)
            preference_rows, global_error = list_all_preferences_resilient(root)
        except Exception as e:
            self._preference_note = str(e)
            append_startup_log(f"读取偏好库失败，配置页暂不显示长期偏好：{e}")
        if global_error:
            append_startup_log(global_error + "；配置页只显示本空间偏好。")
        dlg = ClassificationConfigDialog(
            root=root,
            config=draft,
            using_defaults=(self._config_source == "default"),
            config_error=self._config_error,
            scan_result=self.scan_result,
            base_config=copy.deepcopy(self.classification_config),
            save_callback=lambda cfg: self._save_classification_config(root, cfg),
            preference_save_callback=(
                (lambda prefs: self._save_preferences_from_config(root, prefs))
                if preference_rows is not None else None),
            save_all_callback=(
                (lambda cfg, prefs, original_ids, baseline: (
                    self._save_config_and_preferences(
                        root, cfg, prefs, original_ids=original_ids,
                        baseline=baseline,
                        write_global=not bool(global_error))))
                if preference_rows is not None else None),
            preference_ids=(
                [p.preference_id for p in preference_rows]
                if preference_rows is not None else None),
            preferences=preference_rows,
            worker_registry=self,
            parent=self,
            ocr_texts=ocr_texts,
            ocr_errors=ocr_errors,
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
        # S2-36：手动编辑保存前也备份一次——「恢复上一次配置」应当回到
        # 你上一次保存的状态，而不是越过所有手工编辑退回到上次应用方案之前。
        if os.path.isfile(config_path(root)):
            create_backup(root)
        saved_custom = save_config(root, cfg)
        self.classification_config = cfg
        self._config_source = "custom" if saved_custom else "default"
        self._config_error = ""
        self._update_config_label()
        self._invalidate_for_config_change()

    def _save_preference(self, root: str, pref: Preference):
        """由修正对话框确认保存的一条长期偏好。

        空间偏好会先同步该空间 JSON 的 long_term_rules 镜像，再写 SQLite；
        全局偏好只写应用数据目录下的全局偏好库。任何失败都抛错，由对话框
        显示原因且不关闭。
        """
        if self._close_pending or self._closing:
            raise PreferenceError(
                "窗口正在关闭，未保存长期偏好；已生效的偏好保持不变。")
        current = self.path_edit.text().strip()
        if not current or _norm_root(current) != _norm_root(root):
            raise PreferenceError(
                "当前根目录已变化，拒绝把偏好写入其他整理空间。"
                "请重新选择目录后再保存。")
        save_preference(root, pref)
        # JSON 镜像可能已变化，重新加载用于后续试运行/配置页显示
        self._load_root_config(root)
        self._preferences_available = True
        self._preference_note = ""
        self._invalidate_for_config_change()

    def _save_preferences_from_config(self, root: str,
                                      preferences: list):
        """配置页保存：JSON 已由 save_callback 先写入，这里只持久化偏好行。"""
        if self._close_pending or self._closing:
            raise PreferenceError(
                "窗口正在关闭，未保存偏好；已生效的偏好保持不变。")
        current = self.path_edit.text().strip()
        if not current or _norm_root(current) != _norm_root(root):
            raise PreferenceError(
                "当前根目录已变化，拒绝把偏好写入其他整理空间。")
        persist_preferences(root, preferences, sync_json=False)
        self._load_root_config(root)
        self._preferences_available = True
        self._preference_note = ""

    def _save_config_and_preferences(self, root: str,
                                     cfg: ClassificationConfig,
                                     preferences: list,
                                     original_ids=None,
                                     write_global: bool = True,
                                     baseline=None):
        """配置页「保存并应用」：配置 JSON 与偏好库一起原子提交。

        任何一步失败都会回滚两边的库并恢复 JSON；不会出现“报错但新目标
        已经生效”。「按类型」「按课程再按类型」不使用这些长期规则，但配置
        本身仍需保存。
        """
        if self._close_pending or self._closing:
            raise ClassificationConfigError(
                "窗口正在关闭，未保存分类配置；已生效的规则保持不变。")
        current = self.path_edit.text().strip()
        if not current or _norm_root(current) != _norm_root(root):
            raise ClassificationConfigError(
                "当前根目录已变化，拒绝把配置写入其他目录。请重新选择目录后再编辑。")
        if os.path.isfile(config_path(root)):
            create_backup(root)
        persist_preferences(
            root, preferences, sync_json=True,
            original_ids=original_ids,
            write_space=True, write_global=write_global, config=cfg,
            baseline=baseline)
        self._load_root_config(root)
        self._preferences_available = True
        self._preference_note = ""
        self._invalidate_for_config_change()
        return self.classification_config

    # ============== 学习建议（阶段 5） ==============

    def on_learning_suggestions(self):
        """展示由历史改单归纳出的候选；确认前不写偏好表、不移动文件。"""
        if self._close_pending or self._closing:
            return
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "归档盒", "请先选择一个有效的文件夹。")
            return
        self._import_learning_feedback(root)
        try:
            learning.build_candidates(root)
        except Exception as e:
            QMessageBox.warning(self, "学习建议", f"读取学习候选失败：\n{e}")
            return
        from learning_dialog import LearningCandidatesDialog
        dlg = LearningCandidatesDialog(
            root,
            lambda: learning.list_candidates(root),
            self._confirm_learning_candidate,
            lambda cid: learning.reject_candidate(root, cid),
            lambda cid: self._disable_learning_candidate(root, cid),
            parent=self)
        dlg.exec()

    def _confirm_learning_candidate(self, candidate_id: str, scope: str):
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            raise learning.LearningError("当前没有有效的整理空间，未保存。")
        # 复用现有单条保存：校验、路径约束、JSON 镜像与失败回滚都不另写一套。
        learning.confirm_candidate(
            root, candidate_id, scope,
            saver=lambda pref: self._save_preference(root, pref))
        self.statusBar().showMessage(
            "学习建议已保存为偏好；下一次预览生效，文件仍要勾选并确认后才会移动。")

    def _disable_learning_candidate(self, root: str, candidate_id: str):
        learning.disable_candidate(root, candidate_id)
        try:
            self._load_root_config(root)
            if self._plan_valid:
                self._invalidate_for_config_change()
        except Exception as e:
            append_startup_log(f"停用学习偏好后刷新配置失败：{e}")
        self.statusBar().showMessage(
            "已停用该学习偏好；偏好行仍保留，下一次预览不再采用。")

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
        ocr_texts, ocr_errors = ocr_maps_from_plan(self.current_plan)
        dlg = SchemeCenterDialog(
            root=root,
            current_config=copy.deepcopy(self.classification_config),
            apply_callback=self._apply_scheme_config,
            restore_callback=self._restore_last_config,
            scan_result=self.scan_result,
            worker_registry=self,
            parent=self,
            ocr_texts=ocr_texts,
            ocr_errors=ocr_errors,
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
        try:
            previous_cfg, _source = load_config(root)
        except ClassificationConfigError:
            # 当前配置已损坏时允许方案直接覆盖，没有可回滚的旧配置。
            previous_cfg = None
        create_backup(root)
        save_config(root, cfg)
        # 方案里的长期规则必须同步进本空间偏好库；否则正式预览仍会按
        # SQLite 里的旧目标分类。偏好库写入失败时回滚刚写入的 JSON。
        try:
            sync_space_preferences_from_config(root, cfg)
        except Exception:
            if previous_cfg is not None:
                try:
                    save_config(root, previous_cfg)
                except Exception:
                    pass
            raise
        self._load_root_config(root)
        self._invalidate_for_config_change()
        self.statusBar().showMessage(
            "方案已应用到当前目录。旧预览已失效，请点击「重新生成预览」后再执行。")

    def _restore_last_config(self) -> str:
        """恢复上一次应用前的配置（只改配置，不移动/撤销文件）。"""
        root = self._require_idle_root()
        try:
            previous_cfg, _previous_source = load_config(root)
        except ClassificationConfigError:
            previous_cfg = None
        report: dict = {}
        _state, message = restore_latest_backup(root, report=report)
        # 恢复出来的 JSON 长期规则同样要覆盖本空间偏好，否则预览继续用旧目标。
        cfg, _source = load_config(root)
        try:
            sync_space_preferences_from_config(root, cfg)
        except Exception:
            # 偏好库同步失败：把 JSON 写回恢复前，空间库保持恢复前；
            # 删掉本次新产生的备份，保留更早的目标备份和用户原本要恢复的
            # 那一份，免得下一次“恢复”点到刚备份的当前配置。
            if previous_cfg is not None:
                try:
                    save_config(root, previous_cfg)
                except Exception:
                    pass
            new_backup = report.get("new_backup")
            if new_backup:
                try:
                    os.remove(new_backup)
                except OSError:
                    pass
            raise
        self._load_root_config(root)
        self._invalidate_for_config_change()
        self.statusBar().showMessage(
            message + " 旧预览已失效，请点击「重新生成预览」后再执行。")
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
            "⚠ 分类配置已更新：旧预览已失效，请点击「重新生成预览」重新生成")
        self.invalid_label.show()
        self._update_button_states()
        self.statusBar().showMessage(
            "分类配置已保存并生效。请点击「重新生成预览」按新规则生成计划，"
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
            text = dlg.get_text()
            # S3 / guib F-20/F-21：清空或格式全错时明确说「没有保存」
            if not text.strip():
                QMessageBox.information(
                    self, "课程关键词",
                    "规则为空，没有保存。当前仍使用原来的课程关键词规则。\n"
                    "如果要清空，请保留至少一条规则，或直接在配置里改用推荐模式。")
                return
            new_rules = parse_rules_text(text)
            if not new_rules:
                QMessageBox.warning(
                    self, "课程关键词",
                    "没有识别到任何有效规则行，没有保存。\n\n"
                    "每行格式应为：关键词1,关键词2 → 课程名\n"
                    "当前仍使用原来的课程关键词规则。")
                return
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
        # S2 / guia F-04：保护配置改变后，整空间原始扫描也不能再复活
        self._full_scan_result = None
        self.current_plan = None
        self._plan_valid = False
        self.table.setRowCount(0)
        self.protect_label.hide()
        self.invalid_label.setText("⚠ 保护配置已更改：请重新扫描以生成正确预览")
        self.invalid_label.show()
        self.btn_regenerate.setEnabled(False)
        self._update_button_states()
        self.statusBar().showMessage(
            "保护配置已更改，旧预览已失效。请点击「重新扫描」重新生成。"
        )

    def on_plan_param_changed(self, *_):
        if self.current_plan is not None:
            self._plan_valid = False
            self.invalid_label.setText("⚠ 计划已失效（规则/模式/目录已更改）")
            self.invalid_label.show()
            self.statusBar().showMessage("计划参数已更改，请点击「重新生成预览」生成新计划。")
        self._update_button_states()

    def _ai_snapshot(self) -> AiSettings:
        current = self._ai_settings
        return AiSettings(api_key=current.api_key, enabled=bool(current.enabled))

    def _set_ai_checked(self, checked: bool) -> None:
        self.ai_check.blockSignals(True)
        self.ai_check.setChecked(checked)
        self.ai_check.blockSignals(False)

    def _ai_consent_text(self) -> str:
        return (
            "打开后，预览里仍是「待定」或图片识别失败的文件，会把下面这些内容"
            "发给 api.deepseek.com：\n\n"
            "• 文件名\n"
            "• 相对路径\n"
            "• 文件类型\n"
            "• 不超过约 1200 字的正文片段\n"
            "• 不超过约 1200 字的图片识别文字\n\n"
            "不会上传整个文件。可以随时取消勾选关闭。\n"
            "需要使用你自己的密钥，程序里没有内置密钥。\n"
            "文件正文只作为待分析数据，不会被当作指令执行。\n\n"
            "确定打开吗？")

    def _ask_ai_consent(self) -> bool:
        reply = QMessageBox.question(
            self, "打开 DeepSeek 分类", self._ai_consent_text(),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _on_ai_toggled(self, *_):
        checked = self.ai_check.isChecked()
        if checked and not self._ai_settings.enabled:
            if not self._ask_ai_consent():
                self._set_ai_checked(False)
                return
            self._ai_consent_given = True
            if not self._ai_settings.api_key.strip():
                key, ok = QInputDialog.getText(
                    self, "DeepSeek 密钥",
                    "粘贴你自己的 DeepSeek 密钥。留空则保持关闭。",
                )
                if not ok or not str(key).strip():
                    self._set_ai_checked(False)
                    return
                self._ai_settings.api_key = str(key).strip()
        self._ai_settings.enabled = checked
        try:
            save_settings(self._ai_settings)
        except OSError as exc:
            append_startup_log(f"AI 设置保存失败：{exc}")
            QMessageBox.warning(
                self, "AI 设置",
                f"AI 设置没能保存到磁盘（%APPDATA% 不可写或被占用）：\n{exc}\n\n"
                "本次运行仍按当前选择生效，下次启动需要重新设置。")
        self.on_plan_param_changed()

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
                    "分类配置不可用：请打开「学期和目录规则」修复或恢复默认")
                QMessageBox.critical(
                    self, "归档盒",
                    "该目录的分类配置不可用，已停止生成计划：\n\n"
                    f"{self._config_error}\n\n"
                    "为避免按错误规则分类，必须先修复配置或选择「恢复默认」。")
                return
            # 深拷贝：后台线程持有固定快照，不引用任何可编辑草稿对象
            plan_config = copy.deepcopy(self.classification_config)
            # 阶段 2：本空间 / 全局偏好库是长期匹配的唯一来源。
            # 先完成旧 JSON 一次性迁移；偏好库可用时清空配置里的长期规则，
            # 避免同一 preference_id 被 JSON 和 SQLite 各应用一次。
            root_now = self.scan_result.root if self.scan_result else ""
            if self._refresh_preferences(root_now):
                plan_config.long_term_rules = []

        # 新计划开始后，旧计划立即失效，取消/失败后都不允许执行旧计划
        self._plan_valid = False
        if self.current_plan is not None:
            self.invalid_label.setText("⚠ 正在生成新计划，旧计划暂不可执行")
            self.invalid_label.show()

        use_ocr = bool(self._ocr_supported and self.ocr_check.isChecked())
        use_ai = bool(self.ai_check.isChecked() and mode != MODE_BY_TYPE)
        # 启用联网前必须再次说明会发送哪些字段；拒绝则只走本地规则。
        if use_ai and not self._ai_consent_given:
            if not self._ask_ai_consent():
                use_ai = False
                self._set_ai_checked(False)
                self._ai_settings.enabled = False
            else:
                self._ai_consent_given = True
        parts = ["读取正文"]
        if use_ocr:
            parts.append("识别图片文字")
        if use_ai:
            parts.append("对待定或识别失败的文件询问 DeepSeek")
        phase = ("、".join(parts) + "并生成计划" if parts
                 else "生成计划中（计算 SHA-256 指纹）")
        self._plan_working = True
        self.task.begin(
            "plan", self.scan_result.root,
            phase=phase,
            cancelable=True,
            total=len(self.scan_result.files),
        )
        self._refresh_busy_ui(f"{phase}，共 {len(self.scan_result.files)} 个文件…")
        self._update_button_states()

        try:
            cache_entries, cache_note = scan_cache.load(self.scan_result.root)
            if cache_note:
                append_startup_log(f"增量缓存：{cache_note}")
            worker = PlanWorker(
                seq, self.scan_result, mode, self.rules,
                config=plan_config, ocr_enabled=use_ocr,
                ai_settings=(self._ai_snapshot() if use_ai else None),
                parent=self,
                scan_cache_entries=cache_entries,
                cache_note=cache_note,
                merge_cache=(self._plan_scope != "space"))
            self._plan_worker = worker
            self._plan_workers.append(worker)

            worker.progress.connect(self._on_plan_progress)
            worker.stage.connect(self._on_plan_stage)
            worker.finished_ok.connect(self._on_plan_done)
            worker.cancelled.connect(self._on_plan_cancelled)
            worker.failed.connect(self._on_plan_failed)
            worker.finished_seq.connect(self._on_plan_worker_finished)
            # 线程真正结束后再释放引用（此时线程已不在运行）
            worker.finished.connect(lambda w=worker: self._forget_plan_worker(w))
            worker.start()
        except Exception as e:
            # S3 / guia F-24：begin() 之后异常不能让界面永久卡在忙碌态
            self._abort_task_start("plan", "生成计划", e)

    def _abort_task_start(self, kind: str, label: str, exc: BaseException):
        """后台任务启动失败：结束忙碌态、写日志、告诉用户（S3 / F-24）。"""
        self.task.end(kind)
        self._set_working(False)
        _log_worker_failure(f"{label}启动", exc)
        if not (self._close_pending or self._closing):
            QMessageBox.critical(
                self, "归档盒",
                f"{label}没能启动：\n{_describe_exception(exc)}\n\n"
                "界面已恢复空闲，可以重试或换一个目录。")

    def _forget_plan_worker(self, worker):
        try:
            self._plan_workers.remove(worker)
        except ValueError:
            pass

    def _on_plan_stage(self, seq: int, stage: str):
        if seq != self._current_plan_seq or not self._plan_working:
            return
        if self._close_pending or self._closing:
            return
        self.task.phase = stage
        self.statusBar().showMessage(f"{stage}…")

    def _on_plan_progress(self, seq: int, cur: int, total: int, fname: str):
        if seq != self._current_plan_seq:
            return  # 过期进度，忽略
        if not self._plan_working:
            return  # 任务已结束，迟到的进度不再污染界面
        self.task.update_progress(cur, total, fname)
        self._apply_progress(cur, total)
        prefix = self.task.phase or "生成计划"
        self.statusBar().showMessage(f"{prefix}… [{cur}/{total}] {fname}")

    def _on_plan_done(self, seq: int, plan: OrganizePlan):
        if self._closing or self._close_pending:
            return  # 关闭流程中不再创建新的数据库连接、不发布计划
        if seq != self._current_plan_seq:
            return  # 旧任务结果，不能覆盖新任务状态

        # 取消请求发出后任务仍完成了（线程已结束但结果尚未交付的边界）：
        # 按取消处理，绝不发布这份计划
        if self._plan_cancel_requested or self._plan_cancel_seq == seq:
            self._discard_plan("计划生成已取消，请点击「重新生成预览」重新生成")
            return

        # 阶段 2：本轮预览按偏好库的启用偏好做长期匹配。
        # 只能在计划生成的最后一步应用，不能改 executor 的移动协议；
        # 最终是否移动仍由用户勾选 + 执行确认框决定。
        pref_result = None
        if self._preferences_available:
            try:
                effective, global_error = effective_preferences_resilient(plan.root)
                if global_error:
                    self._preference_note = global_error
                    append_startup_log(
                        global_error + "；本轮只使用本空间偏好。")
                pref_result = apply_preferences_to_plan(
                    plan, effective, plan.root)
            except Exception as e:
                self._preference_note = str(e)
                append_startup_log(f"偏好应用失败，本轮按原有分类显示：{e}")

        self.current_plan = plan
        self._plan_valid = True
        # 拖入新文件后重建的计划：把拖放前记下的已跳过文件恢复成跳过，
        # 歧义一并清空，避免「开始整理」再次要求确认。只套用一次。
        self._apply_carried_skips(plan)
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
                self._apply_pending_drop_select()
                QMessageBox.critical(
                    self, "归档盒",
                    f"无法在该目录建立整理记录（.organizer）：\n{e}",
                )
                return

        self._populate_table(plan, reset_filters=True)
        self._invalidate_confirmation()
        self._update_plan_counts()
        self._refresh_ambiguity_panel()
        self._update_button_states()
        ocr_done = sum(1 for i in plan.items if i.ocr_text is not None)
        ocr_fail = sum(1 for i in plan.items if i.ocr_error)
        ocr_msg = ""
        if self._ocr_supported and self.ocr_check.isChecked():
            if ocr_done or ocr_fail:
                ocr_msg = f"（已识别 {ocr_done} 张图片"
                if ocr_fail:
                    ocr_msg += f"，{ocr_fail} 张失败"
                ocr_msg += "）"
            else:
                ocr_msg = "（没有可识别的图片）"
        if plan.ai_note:
            ocr_msg += plan.ai_note
        amb_count = sum(1 for i in plan.items if getattr(i, "ambiguity", None))
        if amb_count:
            ocr_msg += f"；{amb_count} 个歧义待确认（默认不勾选）"
        if pref_result is not None and getattr(pref_result, "adopted", 0):
            ocr_msg += f"；{pref_result.adopted} 个文件采用已保存偏好"
        if pref_result is not None and getattr(pref_result, "conflicts", 0):
            ocr_msg += f"；{pref_result.conflicts} 个偏好冲突待确认"
        if self._preference_note:
            if self._preferences_available:
                ocr_msg += f"；{self._preference_note}"
            else:
                ocr_msg += "；偏好库不可用，已回退到原有长期规则"
        rejected = len(getattr(plan, "ai_rejections", []) or [])
        if rejected:
            ocr_msg += f"；{rejected} 个 AI 建议未通过校验，保持原位"
        if plan.total == 0:
            self.statusBar().showMessage(self._empty_plan_text())
        else:
            self.statusBar().showMessage(
                f"✓ {plan.summary()}{ocr_msg}（仍可更改，点击「开始整理」才会移动文件）"
            )
        self._apply_pending_drop_select()

    def _apply_carried_skips(self, plan) -> int:
        """在新计划上恢复拖放前记下的已跳过文件，然后丢弃记录。

        只按相对路径匹配（同一整理空间内路径稳定）：置 skipped、取消勾选、
        清空 ambiguity，随后由 _populate_table 与 _refresh_ambiguity_panel
        按新状态渲染，歧义面板就不会再要求确认它。
        """
        rels = getattr(self, "_carry_skipped_rels", None)
        if not rels:
            self._carry_skipped_rels = []
            return 0
        self._carry_skipped_rels = []
        wanted = {os.path.normcase(os.path.normpath(rel)) for rel in rels}
        applied = 0
        for item in plan.items:
            key = os.path.normcase(os.path.normpath(item.file.rel_path))
            if key not in wanted:
                continue
            item.skipped = True
            item.selected = False
            item.ambiguity = None
            applied += 1
        return applied

    def _on_plan_cancelled(self, seq: int):
        if self._closing or self._close_pending:
            return
        if seq != self._current_plan_seq:
            return  # 旧任务的取消信号，与当前任务无关
        self._discard_plan("计划生成已取消，请点击「重新生成预览」重新生成")

    def _on_plan_failed(self, seq: int, err: str):
        if self._closing or self._close_pending:
            return
        if seq != self._current_plan_seq:
            return  # 旧任务的失败信号，不能弹窗污染当前任务
        self._discard_plan("计划生成失败，请点击「重新生成预览」重新生成")
        QMessageBox.critical(self, "归档盒", f"生成计划失败：\n{err}")

    def _discard_plan(self, message: str):
        """取消/失败后清空计划与预览，保证没有任何可执行计划残留。"""
        self.current_plan = None
        self._plan_valid = False
        self._ambiguity_item = None
        if hasattr(self, "ambiguity_panel"):
            self.ambiguity_panel.hide()
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
        self._apply_progress(0, 0)
        self._refresh_busy_ui()
        # 计划真正结束、界面空闲后再刷新歧义面板：_on_plan_done 发布计划时
        # _plan_working 仍为真，面板控件会被判定为忙碌而禁用；这里设回空闲
        # 后必须再刷一次，否则「按所选归入」「跳过」等按钮一直不可点。
        self._refresh_ambiguity_panel()
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
            self._discard_plan("计划生成已取消，请点击「重新生成预览」重新生成")

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
        self._apply_progress(0, 0)

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
            # 只给手动修正整行上色。待定只标状态格，避免大表每一格都建代理对象。
            row_color = QColor("#FFF9C4") if manual else None
            pending = (item.direction or "") == "待定"
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
            if row_color is not None:
                cell_widget.setStyleSheet(f"background:{row_color.name()};")

            orig = QTableWidgetItem(item.file.rel_path)
            orig.setToolTip(f"源文件：{item.file.abs_path}")
            if row_color is not None:
                orig.setBackground(row_color)
            if manual:
                orig.setText("✎ " + item.file.rel_path)
                orig.setToolTip(
                    "手动修正过的文件：\n"
                    f"自动分类：{item.auto_rule_applied}\n"
                    f"当前目标：{item.target_rel_path}\n"
                    f"修正说明：{item.override_note or '(无)'}")
            self.table.setItem(row, 1, orig)

            tgt = QTableWidgetItem(item.target_rel_path)
            if row_color is not None:
                tgt.setBackground(row_color)
            # S3 / guib F-10：目标位置始终有完整路径 tooltip
            if item.conflict_with_existing:
                tgt.setForeground(Qt.red)
                tgt.setToolTip(
                    "⚠ 目标磁盘上已存在同名文件，计划已分配唯一名称\n"
                    f"{item.target_rel_path}")
            else:
                tgt.setToolTip(f"目标位置：{item.target_rel_path}")
            self.table.setItem(row, 2, tgt)

            reason = QTableWidgetItem(item.rule_applied)
            reason.setToolTip(item.rule_applied)
            if row_color is not None:
                reason.setBackground(row_color)
            self.table.setItem(row, 3, reason)

            status_text = self._plan_row_status(item)
            st = QTableWidgetItem(status_text)
            if row_color is not None:
                st.setBackground(row_color)
            elif pending:
                st.setBackground(QColor("#FFF7ED"))
            if item.conflict_with_existing:
                st.setForeground(Qt.red)
            self.table.setItem(row, 4, st)

            size_item = QTableWidgetItem(item.file.size_display)
            size_item.setData(Qt.UserRole, item.file.size)
            if row_color is not None:
                size_item.setBackground(row_color)
            self.table.setItem(row, 5, size_item)
        self._suppress_cell_signals = False
        self._apply_plan_filters()

    def _on_check_changed(self, row: int, state: int):
        if self._suppress_cell_signals:
            return
        # S3 / guia F-07：忙碌期间勾选不生效，避免统计与实际移动不一致
        if self.task.busy:
            self._suppress_cell_signals = True
            try:
                cb = self.table.cellWidget(row, 0)
                box = cb.findChild(QCheckBox) if cb is not None else None
                if box is not None:
                    item = (self.current_plan.items[row]
                            if self.current_plan and row < len(self.current_plan.items)
                            else None)
                    box.setChecked(bool(item.selected) if item else box.isChecked())
            finally:
                self._suppress_cell_signals = False
            self.statusBar().showMessage("正在处理，暂时不能修改勾选。")
            return
        if self.current_plan is None or row >= len(self.current_plan.items):
            return
        # S3 / guib F-02：计划失效后勾选不能被静默丢弃
        if not self._plan_valid:
            self._suppress_cell_signals = True
            try:
                cb = self.table.cellWidget(row, 0)
                box = cb.findChild(QCheckBox) if cb is not None else None
                if box is not None:
                    box.setChecked(bool(self.current_plan.items[row].selected))
            finally:
                self._suppress_cell_signals = False
            self.invalid_label.setText(
                "⚠ 计划已失效：勾选没有保存。请先点「重新生成预览」。")
            self.invalid_label.show()
            self.statusBar().showMessage(
                "计划已失效，勾选没有保存；请先重新生成预览。")
            return
        # 已跳过的条目不能被行内复选框重新勾选：弹回未勾选，不进入执行批次。
        item = self.current_plan.items[row]
        checked = Qt.CheckState(state) == Qt.CheckState.Checked
        if checked and getattr(item, "skipped", False):
            self._suppress_cell_signals = True
            try:
                cb = self.table.cellWidget(row, 0)
                box = cb.findChild(QCheckBox) if cb is not None else None
                if box is not None:
                    box.setChecked(False)
            finally:
                self._suppress_cell_signals = False
            self.statusBar().showMessage("该文件已跳过，保持原位；如需整理请重新生成预览。")
            return
        # stateChanged 在 PySide6 中以 int 发出，Qt.Checked 是严格枚举，
        # 必须先转换再比较，否则行内复选框只能取消、不能勾选。
        item.selected = checked
        self._update_plan_counts()
        status_item = self.table.item(row, 4)
        if status_item is not None:
            status_item.setText(self._plan_row_status(self.current_plan.items[row]))
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

    def _ambiguity_items(self):
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return []
        return [i for i in plan.items if getattr(i, "ambiguity", None)]

    def _refresh_ambiguity_panel(self):
        items = self._ambiguity_items()
        if not items:
            self._ambiguity_item = None
            # 没有待确认项时把控件一并禁用，避免隐藏面板残留上一次的可用态。
            self.ambiguity_combo.setEnabled(False)
            self.btn_ambiguity_apply.setEnabled(False)
            self.btn_ambiguity_skip.setEnabled(False)
            if hasattr(self, "ambiguity_panel"):
                self.ambiguity_panel.hide()
            return
        item = items[0]
        amb = item.ambiguity or {}
        self._ambiguity_item = item
        more = f"（还有 {len(items) - 1} 个待确认）" if len(items) > 1 else ""
        self.ambiguity_label.setText(
            f"{item.file.rel_path}：{amb.get('reason', '存在歧义')}。"
            f"请选择去向，或跳过并保持原位{more}")
        self.ambiguity_combo.clear()
        for index, cand in enumerate(amb.get("candidates") or []):
            self.ambiguity_combo.addItem(
                f"{cand.get('label', '')}（{cand.get('evidence', '')}）", index)
        self.ambiguity_combo.setCurrentIndex(0)
        idle = not (self._working or self._plan_working
                    or self._close_pending or self._closing)
        self.ambiguity_combo.setEnabled(idle)
        self.btn_ambiguity_apply.setEnabled(idle)
        self.btn_ambiguity_skip.setEnabled(idle)
        self.ambiguity_panel.show()

    def _invalidate_confirmation(self):
        """改选/跳过后，之前的确认不再代表最终批次。"""
        self._plan_revision += 1
        self._confirmed_revision = None

    def _after_plan_mutation(self, message: str):
        self._invalidate_confirmation()
        plan = self.current_plan
        if plan is not None:
            self._populate_table(plan)
        self._update_plan_counts()
        self._refresh_ambiguity_panel()
        self._update_button_states()
        self.statusBar().showMessage(
            message + "；请重新确认最终批次后再开始整理。")

    def apply_ambiguity_choice(self, source_rel: str | None = None,
                               index: int | None = None) -> bool:
        """预览里的轻量改选：刷新目标与依据，并作废旧确认。"""
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return False
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return False
        item = self._ambiguity_item
        if source_rel:
            item = next((i for i in plan.items
                         if i.file.rel_path == source_rel), None)
        if item is None or not item.ambiguity:
            return False
        choice = index
        if choice is None:
            choice = self.ambiguity_combo.currentData()
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            choice = 0
        updated = resolve_ambiguity(plan, item.file.rel_path, choice)
        if updated is None:
            return False
        self._after_plan_mutation(
            f"已改选 {updated.file.rel_path} → "
            f"{updated.direction}/{updated.category or ''}")
        return True

    def skip_ambiguity(self, source_rel: str | None = None) -> bool:
        """跳过歧义：保持源路径不变，不进入执行批次。"""
        if (self._close_pending or self._closing
                or self._working or self._plan_working):
            return False
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            return False
        item = self._ambiguity_item
        if source_rel:
            item = next((i for i in plan.items
                         if i.file.rel_path == source_rel), None)
        if item is None or not item.ambiguity:
            return False
        before_target = item.target_dir_rel
        updated = skip_plan_item(plan, item.file.rel_path)
        if updated is None:
            return False
        # 阶段 5：「跳过，保持原位」是用户明确决定；记录后不再被候选归入。
        root = self.path_edit.text().strip()
        if root and os.path.isdir(root):
            try:
                learning.record_skip(
                    root, updated.file.rel_path,
                    before_target=before_target or "",
                    rule_key=updated.rule_key or "",
                    rule_applied=updated.rule_applied or "")
            except Exception as e:
                append_startup_log(f"记录学习反馈失败：{e}")
        self._after_plan_mutation(
            f"已跳过 {updated.file.rel_path}，源文件保持原位")
        return True

    def on_apply_ambiguity_choice(self):
        self.apply_ambiguity_choice()

    def on_skip_ambiguity(self):
        self.skip_ambiguity()

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
        pending_amb = sum(1 for i in plan.items if getattr(i, "ambiguity", None))
        skipped = sum(1 for i in plan.items if getattr(i, "skipped", False))
        move_count = plan.selected_count
        if hasattr(self, "impact_label"):
            self.impact_label.setText(
                f"将移动 {move_count}｜跳过 {plan.total - move_count}"
                + (f"｜待确认歧义 {pending_amb}" if pending_amb else "")
                + (f"｜已跳过 {skipped}" if skipped else ""))

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
            # 已跳过的条目保持不勾选：批量勾选/对调都不能把它重新纳入执行批次。
            if getattr(item, "skipped", False):
                continue
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

    def _set_button_role(self, btn, role: str):
        """同一时刻只把下一步操作标成蓝色主按钮。"""
        if (btn.property("role") or "") == role:
            return
        btn.setProperty("role", role)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        btn.update()

    def _mark_next_action(self):
        has_folder = bool(self.path_edit.text().strip())
        browse_next = self.btn_browse.isEnabled() and not has_folder
        execute_next = self.btn_execute.isEnabled()
        scan_next = (
            self.btn_scan.isEnabled() and has_folder and not execute_next)
        self._set_button_role(self.btn_browse, "primary" if browse_next else "")
        self._set_button_role(self.btn_scan, "primary" if scan_next else "")
        self._set_button_role(
            self.btn_execute, "primary" if execute_next else "")

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
            self.ocr_check.setEnabled(False)
            self.ai_check.setEnabled(False)
            self.btn_scheme_center.setEnabled(False)
            self.btn_recent.setEnabled(False)
            self.btn_agent.setEnabled(False)
            self.btn_intake.setEnabled(False)
            self.btn_scan_intake.setEnabled(False)
            self.btn_duplicates.setEnabled(False)
            self._set_preview_controls_enabled(False)
            self._mark_next_action()
            self._sync_intake_label()
            self._refresh_plan_hint()
            self._update_inspector()
            return

        # 历史页面是只读视图，有数据库即可查看；
        # 其内部的撤销/恢复/导出按钮会按任务状态自行禁用
        self.btn_history.setEnabled(self._storage is not None)
        # AI 管家：选好文件夹且空闲时可用；查找本身不移动文件
        self.btn_agent.setEnabled(
            bool(self.path_edit.text().strip())
            and not self._working and not self._plan_working
            and not self._close_pending and not self._closing
        )

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
        self.ocr_check.setEnabled(idle and self._ocr_supported)
        self.ai_check.setEnabled(idle)
        self.btn_scheme_center.setEnabled(
            idle and bool(self.path_edit.text().strip()))
        self.btn_recent.setEnabled(idle)
        self.btn_intake.setEnabled(idle)
        self.btn_scan_intake.setEnabled(
            idle and bool(self.path_edit.text().strip()) and len(self.intake) > 0)
        self.btn_duplicates.setEnabled(idle and self.scan_result is not None)
        self._set_preview_controls_enabled(idle and has_plan)
        # V1.2 第三轮：任务状态变化同步已打开的历史窗口（轻量按钮刷新）
        self._refresh_undo_button_label()
        self._notify_history_gating()
        self._mark_next_action()
        self._sync_intake_label()
        self._refresh_plan_hint()
        self._update_inspector()

    def _empty_plan_text(self) -> str:
        scan = self.scan_result
        if scan is not None and scan.total_files == 0 and scan.protected_count:
            return f"没有可整理的文件：{scan.protected_count} 个文件在保护目录里。"
        return "这个文件夹里没有可整理的文件。换一个文件夹，或把文件拖进来。"

    def _refresh_plan_hint(self):
        label = getattr(self, "plan_hint", None)
        if label is None:
            return
        plan = self.current_plan if self._plan_valid else None
        if plan is None:
            label.hide()
            return
        if plan.total == 0:
            label.setText(self._empty_plan_text())
            label.show()
            return
        pending = sum(1 for item in plan.items if (item.direction or "") == "待定")
        unselected = plan.total - plan.selected_count
        if pending or unselected:
            label.setText(
                f"未分类 {pending}，已选 {plan.selected_count} / {plan.total}。"
                "未勾选的不会移动。")
            label.show()
        else:
            label.hide()

    def _plan_row_status(self, item) -> str:
        if item.conflict_with_existing:
            return "将改名"
        if getattr(item, "skipped", False):
            return "已跳过"
        if getattr(item, "ambiguity", None):
            return "待确认"
        if getattr(item, "user_resolved", False):
            return "已改选"
        if item.is_manual_overridden:
            return "手动修正"
        if (item.direction or "") == "待定":
            return "待定"
        if not item.selected:
            return "未勾选"
        return "将移动"

    def _refresh_undo_button_label(self):
        if not hasattr(self, "btn_undo"):
            return
        batch = None
        if self._storage is not None:
            try:
                batch = self._storage.get_undoable_batch()
            except Exception:
                batch = None
        if batch is None:
            self.btn_undo.setText("撤销上一次")
            return
        latest = batch.batch_id
        try:
            row = self._storage.conn.execute(
                "SELECT MAX(id) FROM batches").fetchone()
            if row and row[0] is not None:
                latest = row[0]
        except Exception:
            pass
        if batch.batch_id != latest:
            self.btn_undo.setText(f"撤销批次 #{batch.batch_id}")
        else:
            self.btn_undo.setText("撤销上一次")

    def _set_preview_controls_enabled(self, enabled: bool):
        for w in (self.plan_search, self.type_filter, self.dir_filter,
                  self.source_filter, self.pending_check,
                  self.conflict_check, self.btn_clear_filters,
                  self.btn_select_visible, self.btn_deselect_visible,
                  self.btn_invert_visible, self.btn_target_tree):
            w.setEnabled(bool(enabled))
        # S3 / guia F-07：忙碌时表格与行内勾选框都不可改；
        # 计划失效（但空闲）时表格仍可滚动查看。
        busy = (self._working or self._plan_working
                or self._close_pending or self._closing)
        self.table.setEnabled(not busy)

    # ============== 执行 & 撤销 ==============

    def _apply_progress(self, cur: int = 0, total: int = 0):
        """刷新表格上方的进度条。有总数时显示「当前 / 总数」，否则保持滚动。"""
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(max(0, min(cur, total)))
            self.progress_label.setText(f"{cur} / {total}")
        else:
            self.progress.setRange(0, 0)
            self.progress.setValue(0)
            self.progress_label.setText("进行中")

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
            and _worker_running(exec_worker)
        )
        scan_worker = getattr(self, "_scan_worker", None)
        scan_cancelable = (
            not blocking and self._working and self.task.kind == "scan"
            and _worker_running(scan_worker)
        )
        undo_worker = getattr(self, "_undo_worker", None)
        undo_cancelable = (
            not blocking and self._working and self.task.kind == "undo"
            and _worker_running(undo_worker)
        )
        recover_worker = getattr(self, "_recover_worker", None)
        recover_cancelable = (
            not blocking and self._working and self.task.kind == "recover"
            and _worker_running(recover_worker)
        )
        show_cancel = (not blocking) and (
            self._plan_working or exec_cancelable or scan_cancelable
            or undo_cancelable or recover_cancelable)

        if busy:
            self._apply_progress(self.task.progress_cur, self.task.progress_total)
            self.progress_row.show()
            self.progress.show()
            if show_cancel:
                self.btn_cancel.show()
                if self._plan_working:
                    self.btn_cancel.setText("停止生成预览")
                elif exec_cancelable:
                    self.btn_cancel.setText("停止本次整理")
                elif scan_cancelable:
                    self.btn_cancel.setText("停止扫描")
                elif undo_cancelable:
                    self.btn_cancel.setText("停止撤销")
                else:
                    self.btn_cancel.setText("停止核对")
                self.btn_cancel.setEnabled(
                    not (self._plan_working and self._plan_cancel_requested))
            else:
                # 关闭等待等阶段，不展示无效的取消按钮
                self.btn_cancel.hide()
                self.btn_cancel.setEnabled(False)
            if not self._busy_timer.isActive():
                self._busy_started_at = time.monotonic()
                self._busy_timer.start()
            if msg:
                self.statusBar().showMessage(msg)
        else:
            self._busy_timer.stop()
            self.progress.hide()
            self.progress_row.hide()
            self._apply_progress(0, 0)
            self.btn_cancel.hide()
            self.btn_cancel.setEnabled(False)
        self._mark_next_action()

    def _tick_busy(self):
        """S3 / guia F-26：等待期间显示任务名与已等待时长，证明程序还活着。"""
        if not self.task.busy and not self._close_pending:
            self._busy_timer.stop()
            return
        elapsed = max(0, int(time.monotonic() - self._busy_started_at))
        if self._close_pending:
            self.statusBar().showMessage(
                f"正在等待：{self.task.describe()}，已等待 {elapsed} 秒"
                "（再次点关闭可强制退出）")
            return
        if self.task.progress_total > 0:
            self.progress_label.setText(
                f"{self.task.progress_cur} / {self.task.progress_total}")
        else:
            label = TASK_KIND_LABELS.get(self.task.kind or "", "任务")
            self.progress_label.setText(f"{label}中 · 已等 {elapsed}s")

    def _set_working(self, working: bool, msg: str = ""):
        self._working = working
        if working:
            self._apply_progress(0, 0)
        self._refresh_busy_ui(msg if working else "")
        self._update_button_states()

    def _revoke_skipped_selection(self, plan) -> int:
        """把 skipped 条目收回未勾选，并同步行内复选框。返回收回数量。

        最终批次只按 selected 组装，跳过的文件必须始终在原位；
        这里再做一次兜底，避免任何路径把它重新勾上后混进执行批次。
        """
        revoked = 0
        self._suppress_cell_signals = True
        try:
            for index, item in enumerate(plan.items):
                if not getattr(item, "skipped", False) or not item.selected:
                    continue
                item.selected = False
                revoked += 1
                cell = self.table.cellWidget(index, 0)
                cb = cell.findChild(QCheckBox) if cell is not None else None
                if cb is not None:
                    cb.blockSignals(True)
                    cb.setChecked(False)
                    cb.blockSignals(False)
        finally:
            self._suppress_cell_signals = False
        if revoked:
            self._update_plan_counts()
            self._update_button_states()
        return revoked

    def on_execute(self):
        if self._close_pending or self._closing:
            return  # 关闭等待期间不启动新的整理
        if self._working or self._plan_working:
            return
        plan = self.current_plan
        if plan is None or not self._plan_valid:
            QMessageBox.warning(self, "归档盒", "请先生成有效的整理计划。")
            return

        # 已跳过的条目即使 selected 因异常路径为真，也不进入最终批次：
        # 进入确认前先收回勾选，让复选框与「已跳过」一致。
        self._revoke_skipped_selection(plan)

        selected_items = [i for i in plan.items if i.selected]
        if not selected_items:
            QMessageBox.warning(self, "归档盒", "没有勾选任何文件。")
            return
        unresolved = [i for i in selected_items if getattr(i, "ambiguity", None)]
        if unresolved:
            QMessageBox.information(
                self, "归档盒",
                f"还有 {len(unresolved)} 个文件存在未确认的歧义。\n"
                "请先在预览上方选择去向或跳过，再开始整理。")
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
        # 用户确认的就是当前这一版最终批次；改选/跳过后 revision 已变化。
        self._confirmed_revision = self._plan_revision

        if self._storage is None:
            self._storage = Storage(plan.root)

        self._set_working(True, f"整理中（{len(selected_items)} 项）……")
        self.task.begin(
            "execute", plan.root,
            phase=f"整理中（{len(selected_items)} 项）",
            cancelable=True,
            total=len(selected_items),
        )

        try:
            self._exec_worker = ExecuteWorker(plan, plan.root, parent=self)
            self._exec_worker.progress.connect(self._on_execute_progress)
            self._exec_worker.finished_ok.connect(self._on_execute_done)
            self._exec_worker.failed.connect(self._on_execute_failed)
            self._exec_worker.finished.connect(lambda: self._finish_task("execute"))
            self.btn_cancel.setEnabled(True)
            self.btn_cancel.show()
            self._exec_worker.start()
        except Exception as e:
            self._abort_task_start("execute", "整理", e)

    def _on_execute_progress(self, cur: int, total: int, fname: str):
        """整理进度（V1.2：执行线程逐项汇报）。"""
        if self._closing or not self._working or self.task.kind != "execute":
            return  # 迟到/过期进度，不污染界面
        self.task.update_progress(cur, total, fname)
        self._apply_progress(cur, total)
        self.statusBar().showMessage(f"正在整理 [{cur}/{total}] {fname}")

    def on_cancel_execute(self):
        """取消按钮：按界面相位决定取消「生成计划」「执行」「扫描/撤销/核对」。"""
        # 优先取消计划生成：即使线程已经结束，只要界面仍处于生成相位，
        # 排队中的成功结果也必须被撤销
        if self._plan_working:
            self._cancel_plan_generation()
            return
        kind = self.task.kind
        worker = None
        if kind == "execute":
            worker = getattr(self, "_exec_worker", None)
        elif kind == "scan":
            worker = getattr(self, "_scan_worker", None)
        elif kind == "undo":
            worker = getattr(self, "_undo_worker", None)
        elif kind == "recover":
            worker = getattr(self, "_recover_worker", None)
        if _worker_running(worker):
            worker.request_cancel()
            self.task.request_cancel()
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage(
                "正在取消：将在当前文件操作完成后停止……")

    def _on_execute_done(self, result):
        if self._closing:
            return
        # 文件已移动：旧重复核对结果与目标位置均失效
        self._invalidate_duplicate_cache(close=True)
        # 阶段 5：现场记下的改单与执行历史合并成同一条判断。
        if self._storage is not None:
            self._import_learning_feedback(self._storage.display_root)
        self._discard_plan("整理已结束，本次预览已失效；如需再次整理请重新扫描生成预览")
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

        # S3-23：即使不弹窗（关闭等待/未开始），跳过与失败详情也要留痕
        if result.errors:
            append_startup_log(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 整理批次 "
                f"#{result.batch_id}：{result.summary()}\n"
                + "\n".join(result.errors))

        # 弹窗报告
        headline = _execute_headline(result)
        msg_lines = [f"{headline}\n\n{summary}\n"]
        if result.moved == 0 and not result.was_cancelled:
            msg_lines.append(
                "本次没有移动任何文件。请查看下面的跳过/失败原因，"
                "处理后重新生成预览再试。")
        if result.skipped:
            msg_lines.append(
                f"跳过 {result.skipped} 项（逐项原因见下方详情）")
        if result.failed:
            msg_lines.append(f"失败 {result.failed} 项（移动错误）")
        msg_lines.append(f"\n批次 #{result.batch_id} 已保存至 .organizer/history.db")
        msg_lines.append(f"关闭程序后仍可重新打开撤销。")
        if result.errors:
            msg_lines.append(
                f"\n\n跳过/失败详情（同时写入启动日志）：\n"
                + "\n".join(result.errors[:20])
                + ("\n…" if len(result.errors) > 20 else ""))

        if result.failed > 0:
            QMessageBox.warning(self, "归档盒", "\n".join(msg_lines))
        else:
            QMessageBox.information(self, "归档盒", "\n".join(msg_lines))

    def _on_execute_failed(self, err: str):
        self._discard_plan("整理失败，本次预览已失效；请排查后重新扫描生成预览")
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

        # S3 / guia F-08：存在未核对的中断批次时，撤销按钮走核对恢复
        blocked = interrupted_batch_id(self._storage)
        if blocked is not None:
            reply = QMessageBox.question(
                self, "有未核对的批次",
                f"批次 #{blocked} 上次运行被中断，状态还没核对。\n\n"
                "直接撤销无法保证不丢文件。要先「核对恢复」确认磁盘实际状态吗？\n"
                "（只核对与补记，不会覆盖、删除或再次移动文件）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self.recover_interrupted_batch(blocked)
            return

        # 确认
        undo_batch = self._storage.get_undoable_batch()
        moved_n = 0
        when = ""
        batch_no = "?"
        if undo_batch is not None:
            batch_no = str(undo_batch.batch_id)
            when = undo_batch.created_at or undo_batch.finished_at or ""
            try:
                actions = self._storage.get_actions(undo_batch.batch_id)
                moved_n = sum(1 for action in actions
                              if action.status in ("moved", "conflict"))
            except Exception:
                moved_n = 0
        reply = QMessageBox.question(
            self, "归档盒",
            f"将撤销批次 #{batch_no}"
            + (f"（{when}）" if when else "")
            + f"，共 {moved_n} 个文件。\n\n"
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
        try:
            self._undo_worker = UndoWorker(self._storage.root, parent=self)
            self._undo_worker.finished_ok.connect(self._on_undo_done)
            self._undo_worker.failed.connect(self._on_undo_failed)
            self._undo_worker.finished.connect(lambda: self._finish_task("undo"))
            self._refresh_busy_ui("正在撤销最近批次……")
            self._undo_worker.start()
        except Exception as e:
            self._abort_task_start("undo", "撤销", e)

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
        # 阶段 5：撤销不是分类决定；只把「无逐文件原因」记为一条待核对线索。
        if self._storage is not None:
            self._import_learning_feedback(self._storage.display_root)
        self._update_button_states()

        self.statusBar().showMessage(
            f"撤销批次 #{result.batch_id}：{result.summary()}"
        )
        msg = f"撤销批次 #{result.batch_id}\n\n{result.summary()}"
        if getattr(result, "notes", None):
            msg += "\n\n说明：\n" + "\n".join(result.notes[:20])
        if result.errors_list:
            msg += "\n\n冲突/错误详情：\n" + "\n".join(result.errors_list[:20])
        # S3-23：撤销的拒绝原因/冲突详情同样落盘，便于事后核对
        if result.errors_list:
            append_startup_log(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 撤销批次 "
                f"#{result.batch_id}：{result.summary()}\n"
                + "\n".join(result.errors_list))
        if self._close_pending:
            return  # 关闭等待期间不弹窗

        if result.refused:
            QMessageBox.warning(self, "归档盒", msg)
        elif result.was_cancelled:
            QMessageBox.information(
                self, "归档盒",
                msg + "\n\n已停止。剩余文件可以再次撤销。")
        elif result.is_complete:
            QMessageBox.information(self, "归档盒", msg + "\n\n✓ 完整撤销成功！")
        elif result.restored > 0:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 部分撤销完成，有文件因冲突未恢复。")
        else:
            QMessageBox.warning(self, "归档盒",
                                msg + "\n\n⚠ 没有任何文件被恢复，不能算完整撤销。")

        # S3 / filesafe F-24：冲突批次要有出口，不能永远问、又永远撤不掉
        if result.conflict > 0 and self._storage is not None:
            conflict_text = "\n".join(result.conflicts[:10]) \
                if getattr(result, "conflicts", None) else ""
            reply = QMessageBox.question(
                self, "冲突批次",
                "有文件因为冲突没有恢复：\n\n"
                + (conflict_text or "\n".join(result.errors_list[:10]))
                + "\n\n这些冲突是否已经人工处理完？\n\n"
                "选「是」：把批次标记为已人工处理，以后打开目录不再反复询问"
                "（不会删除、移动或修改任何文件）。\n"
                "选「否」：保留待处理；修好冲突后可以再次撤销。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                try:
                    acknowledge_batch_handled(self._storage, result.batch_id)
                    self.statusBar().showMessage(
                        f"批次 #{result.batch_id} 已标记为人工处理，不再反复询问。")
                except Exception as e:
                    QMessageBox.warning(
                        self, "归档盒",
                        f"标记人工处理失败：{_describe_exception(e)}")

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
                "其他分类方式请使用「课程关键词」或「学期和目录规则」。")
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
            # 阶段 2：用户确认保存时写入本地偏好库（默认当前空间）
            "preference_save_callback": (
                (lambda pref: self._save_preference(root, pref))
                if root and os.path.isdir(root) else None),
            "space_root": root,
            "space_label": (os.path.basename(os.path.abspath(root))
                            if root else ""),
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

        # 用户在修正对话框里选择了「保存为长期偏好」并已成功保存：
        # 偏好库与 JSON 镜像已更新，旧计划失效，这里只补充提示。
        if (dlg.long_term_saved_preference is not None
                or dlg.long_term_saved_config is not None):
            rule = dlg.saved_rule
            name = rule.name if rule is not None else "（未命名）"
            scope = ""
            if dlg.long_term_saved_preference is not None:
                scope = f"（{dlg.long_term_saved_preference.scope_label}）"
            self.invalid_label.setText(
                "⚠ 长期偏好已保存：旧预览已失效，请点击「重新生成预览」")
            self.invalid_label.show()
            self.statusBar().showMessage(
                f"已保存长期偏好「{name}」{scope}。本次临时修正随旧计划一起失效；"
                "请点击「重新生成预览」按新偏好生成计划后再执行。")
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
            try:
                learning.withdraw_decision(root, source_rel)
            except Exception as e:
                append_startup_log(f"撤回学习反馈失败：{e}")
            self._populate_table(self.current_plan)
            n, _ = summarize_manual_overrides(self.current_plan)
            self.statusBar().showMessage(
                "已恢复自动分类。"
                + (f"本次计划另有 {n} 个手动修正文件；重新扫描/重新预览后失效。"
                   if n > 0
                   else "本次计划已无手动修正文件。"))
            return

        # 应用手动修正
        before_target = next(
            (i.target_dir_rel for i in self.current_plan.items
             if i.file.rel_path == source_rel), "")
        try:
            applied = apply_override_to_plan(
                self.current_plan, source_rel, dlg.params)
        except OverrideValidationError as e:
            QMessageBox.warning(self, "归档盒", str(e))
            return

        # 阶段 5：只在结果与自动快照不同（真正的「仅改本次」）时记一条判断；
        # 改回自动快照则撤回该文件的判断。对话框取消、取消勾选都不写。
        try:
            if applied.is_manual_overridden:
                learning.record_override(
                    root, source_rel,
                    before_target=before_target or "",
                    after_target=applied.target_dir_rel,
                    direction=applied.direction or "",
                    semester=applied.semester or "",
                    category=applied.category or "",
                    rule_key=applied.rule_key or "",
                    rule_applied=applied.rule_applied or "")
            else:
                learning.withdraw_decision(root, source_rel)
        except Exception as e:
            append_startup_log(f"记录学习反馈失败：{e}")

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
        - 本阶段恢复支持协作式取消：进度明确显示“正在核对 N/M”，
          取消后未处理的项目保持原状态。"""
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
                        cancelable=True, total=0)
        self._set_working(True, "正在核对恢复……")
        self._recover_result = None

        try:
            self._recover_worker = RecoverWorker(seq, root, int(batch_id),
                                                 parent=self)
            self._recover_worker.progress.connect(self._on_recover_progress)
            self._recover_worker.finished_ok.connect(self._on_recover_done)
            self._recover_worker.failed.connect(self._on_recover_failed)
            self._recover_worker.finished.connect(
                lambda: self._finish_task("recover"))
            self._refresh_busy_ui("正在核对恢复……")
            self._recover_worker.start()
        except Exception as e:
            self._abort_task_start("recover", "核对恢复", e)
            return None
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
        self._apply_progress(cur, total)
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
        agent_dialog = getattr(self, "_agent_dialog", None)
        if agent_dialog is not None:
            candidates.append(getattr(agent_dialog, "_worker", None))
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

        # 已经在等待后台任务时再次点关闭：说明在等谁、等多久，并允许强制退出
        if self._close_pending:
            elapsed = max(0, int(time.monotonic() - self._busy_started_at))
            reply = QMessageBox.question(
                self, "仍在等待后台任务",
                f"正在等待：{self.task.describe()}\n"
                f"已等待：{elapsed} 秒\n\n"
                "现在强制退出吗？\n\n"
                "强制退出会立刻结束程序。已经移动完的文件留在新位置，"
                "记录在 .organizer/history.db。\n"
                "正在复制的那一个文件可能同时留下源文件和未完成的临时副本"
                "（.archivetmp）。下次打开这个文件夹可以核对。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                append_startup_log(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 用户强制退出："
                    f"{self.task.describe()}（已等待 {elapsed} 秒）")
                self._closing = True
                os._exit(0)
            event.ignore()
            return

        # 请求所有支持协作式取消的后台任务停止
        workers = self._background_workers()
        for w in workers:
            if _worker_running(w) and hasattr(w, "request_cancel"):
                try:
                    w.request_cancel()
                except Exception:
                    pass

        # 只要还有线程在跑，就不关闭窗口、不关闭数据库，等线程真正结束
        running = [w for w in workers if _worker_running(w)]
        if running:
            event.ignore()
            if not self._close_pending:
                self._close_pending = True
                # 进入关闭相位：任何迟到的成功结果都不再发布
                self._plan_cancel_seq = self._current_plan_seq
                self._plan_cancel_requested = True
                self._refresh_busy_ui(
                    f"正在等待后台任务结束：{self.task.describe()}。"
                    "再次点关闭可以强制退出。")
                self._update_button_states()
                QTimer.singleShot(200, self._retry_close)
            return

        # 所有线程已结束，此时关闭数据库才安全
        self._closing = True
        self._close_pending = False
        self._close_child_windows()
        if self._storage is not None:
            try:
                self._storage.close()
            finally:
                self._storage = None
        event.accept()

    def _close_child_windows(self):
        """S3 / guia F-19：关闭主窗口时一并关闭所有非模态子窗口。"""
        for attr in ("_history_dialog", "_intake_dialog", "_tree_dialog",
                     "_duplicate_dialog", "_recent_dialog", "_agent_dialog"):
            dlg = getattr(self, attr, None)
            if dlg is None:
                continue
            try:
                dlg.close()
            except Exception:
                pass
            try:
                setattr(self, attr, None)
            except Exception:
                pass

    def _retry_close(self):
        """等待后台线程全部结束后再真正关闭窗口。"""
        if not self._close_pending or self._closing:
            return
        running = [w for w in self._background_workers() if _worker_running(w)]
        if running:
            QTimer.singleShot(200, self._retry_close)
            return
        # 线程已全部结束；此时迟到的回调已被 _close_pending 拦截
        self._close_pending = False
        self.close()


# ======================== 入口 ========================

def _combo_arrow_url() -> str:
    """下拉箭头。样式表接管 QComboBox 后，系统箭头不会再画。"""
    path = os.path.join(tempfile.gettempdir(), "archivebox-combo-arrow.png")
    if not os.path.exists(path):
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAFCAYAAAB4ka1VAAAAIklEQVR42mNg"
            "QAIpJd3/QZgBG4BJYlWELomiCJckXuuQAQBKGi+ppk1CpwAAAABJRU5ErkJggg=="
        )
        with open(path, "wb") as f:
            f.write(raw)
    return path.replace("\\", "/")


def startup_log_path() -> str:
    """免安装包根目录的「启动日志.txt」。源码运行时写到源码目录。"""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    package_root = os.path.dirname(app_dir)
    packaged = (
        os.path.isdir(os.path.join(package_root, "runtime"))
        or os.path.isfile(os.path.join(package_root, "归档盒.exe"))
        or os.path.isfile(os.path.join(package_root, "启动归档盒.exe"))
    )
    path = os.path.join(package_root if packaged else app_dir, "启动日志.txt")
    parent = os.path.dirname(path)
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return path
    return os.path.join(tempfile.gettempdir(), "归档盒-启动日志.txt")


def append_startup_log(text: str) -> None:
    try:
        with open(startup_log_path(), "a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except OSError:
        pass


def _install_crash_logging() -> None:
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


def _install_qt_chinese(app) -> None:
    """S3 / guib F-18：加载 Qt 自带中文翻译，标准按钮显示「确定/取消/是/否」。

    找不到翻译文件时静默降级（各对话框已单独设置主要按钮文案）。
    """
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        translator = QTranslator(app)
        if translator.load(QLocale(QLocale.Chinese, QLocale.China),
                           "qtbase", "_", path):
            app.installTranslator(translator)
            app._archivebox_translator = translator
    except Exception:
        pass


def main():
    _install_crash_logging()
    app = QApplication(sys.argv)
    _install_qt_chinese(app)
    app.setStyle("Fusion")
    # 界面是浅色。系统深色主题会把勾选框和说明变成白字或黑块，这里固定浅色调色板。
    light = app.palette()
    light.setColor(QPalette.ColorRole.Window, QColor("#F8FAFC"))
    light.setColor(QPalette.ColorRole.WindowText, QColor("#0F172A"))
    light.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    light.setColor(QPalette.ColorRole.AlternateBase, QColor("#F8FAFC"))
    light.setColor(QPalette.ColorRole.Text, QColor("#0F172A"))
    light.setColor(QPalette.ColorRole.Button, QColor("#FFFFFF"))
    light.setColor(QPalette.ColorRole.ButtonText, QColor("#0F172A"))
    light.setColor(QPalette.ColorRole.ToolTipBase, QColor("#FFFFFF"))
    light.setColor(QPalette.ColorRole.ToolTipText, QColor("#0F172A"))
    light.setColor(QPalette.ColorRole.Highlight, QColor("#DBEAFE"))
    light.setColor(QPalette.ColorRole.HighlightedText, QColor("#0F172A"))
    app.setPalette(light)
    ui_font = QFont()
    ui_font.setFamilies(["PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"])
    app.setFont(ui_font)
    app.setStyleSheet("""
        QMainWindow { background: #FAFAFA; }
        QTableWidget {
            background: white;
            color: #212121;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            gridline-color: #EEEEEE;
            selection-background-color: #EFF6FF;
            selection-color: #0F172A;
        }
        QTableWidget::item {
            color: #212121;
        }
        QTableWidget::item:selected {
            background: #EFF6FF;
            color: #0F172A;
        }
        QHeaderView::section {
            background: #F5F5F5;
            padding: 6px 8px;
            border: none;
            border-bottom: 1px solid #E0E0E0;
            font-weight: bold;
            color: #37474F;
        }
        QPushButton {
            padding: 6px 12px;
            min-height: 28px;
            background: white;
            color: #263238;
            border: 1px solid #CFD8DC;
            border-radius: 4px;
        }
        QPushButton:hover { background: #ECEFF1; }
        QPushButton:disabled { color: #5F6B76; background: #EDF1F4; }
        QPushButton[role="primary"] {
            background: #2563EB;
            color: white;
            border: none;
            font-weight: 600;
        }
        QPushButton[role="primary"]:hover { background: #1D4ED8; }
        QPushButton[role="nav"] {
            text-align: left;
            padding: 6px 10px;
            border: 1px solid #D7DEE6;
            border-radius: 6px;
            background: #FFFFFF;
            color: #0F172A;
            font-weight: 500;
        }
        QPushButton[role="nav"]:hover { background: #E2E8F0; }
        QPushButton[role="nav"]:disabled {
            color: #5F6B76; background: #EDF1F4; border-color: #E2E8F0;
        }
        QPushButton[role="nav"][current="true"] {
            background: #EFF6FF;
            color: #1D4ED8;
            font-weight: 600;
        }
        QPushButton[role="primary"]:disabled {
            background: #DCE3E8;
            color: #5F6B76;
        }
        QPushButton[role="danger"] {
            background: white;
            color: #C62828;
            border: 1px solid #EF9A9A;
        }
        QPushButton[role="danger"]:hover { background: #FFEBEE; }
        QLineEdit {
            padding: 6px 10px;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            background: white;
            color: #0F172A;
        }
        QComboBox {
            combobox-popup: 0;
            padding: 4px 22px 4px 8px;
            border: 1px solid #E0E0E0;
            border-radius: 4px;
            background: white;
            color: #0F172A;
            min-height: 22px;
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 18px;
            border: none;
            background: transparent;
        }
        QComboBox::down-arrow {
            image: url("__COMBO_ARROW__");
            width: 8px;
            height: 5px;
        }
        QComboBox QAbstractItemView {
            background: white;
            color: #0F172A;
            border: 1px solid #E2E8F0;
            outline: 0;
            padding: 0px;
            selection-background-color: #EFF6FF;
            selection-color: #0F172A;
        }
        QComboBox QAbstractItemView::item {
            min-height: 26px;
            padding: 4px 10px;
            color: #0F172A;
            background: white;
        }
        QComboBox QAbstractItemView::item:hover {
            background: #F1F5F9;
            color: #0F172A;
        }
        QComboBox QAbstractItemView::item:selected {
            background: #EFF6FF;
            color: #0F172A;
        }
        QLineEdit:focus, QComboBox:focus { border-color: #1E88E5; }
        QSplitter#mainSplit::handle { background: #E2E8F0; }
        QSplitter#mainSplit::handle:hover { background: #93C5FD; }
        QStatusBar { background: #F5F5F5; color: #616161; }
        QCheckBox, QLabel, QRadioButton { color: #0F172A; }
        QCheckBox { padding: 4px; spacing: 6px; }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 1px solid #64748B;
            border-radius: 3px;
            background: #FFFFFF;
        }
        QCheckBox::indicator:checked {
            background: #1D4ED8;
            border: 1px solid #1D4ED8;
        }
        QListWidget, QTreeView, QTextEdit, QPlainTextEdit {
            background: #FFFFFF;
            color: #0F172A;
        }
    """.replace("__COMBO_ARROW__", _combo_arrow_url()))

    win = MainWindow()
    win.show()
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
