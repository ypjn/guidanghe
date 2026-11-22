"""备份对话框后台线程：预览、备份、校验、恢复。"""

from __future__ import annotations

import threading
from typing import Optional, Set

from PySide6.QtCore import QThread, Signal

from backup_service import (
    preview_backup,
    restore_version,
    run_backup,
    verify_version,
)


class BackupPreviewWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, seq: int, source: str, vault: str,
                 policy: Optional[dict] = None,
                 protected_allow: Optional[Set[str]] = None,
                 parent=None):
        super().__init__(parent)
        self.seq = seq
        self.source = source
        self.vault = vault
        self.policy = policy
        self.protected_allow = set(protected_allow or ())
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = preview_backup(
                self.source, self.vault,
                policy=self.policy,
                protected_allow=self.protected_allow,
                cancel_event=self.cancel_event,
                progress_cb=lambda c, t, n: self.progress.emit(c, t, n),
            )
            if self.cancel_event.is_set() or getattr(report, "cancelled", False):
                self.cancelled.emit(self.seq)
                return
            self.finished_ok.emit(self.seq, report)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit(self.seq)
            else:
                self.failed.emit(self.seq, str(exc))


class BackupRunWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, seq: int, source: str, vault: str,
                 policy: Optional[dict] = None,
                 protected_allow: Optional[Set[str]] = None,
                 note: str = "",
                 parent=None):
        super().__init__(parent)
        self.seq = seq
        self.source = source
        self.vault = vault
        self.policy = policy
        self.protected_allow = set(protected_allow or ())
        self.note = note or ""
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = run_backup(
                self.source, self.vault,
                policy=self.policy,
                protected_allow=self.protected_allow,
                cancel_event=self.cancel_event,
                progress_cb=lambda c, t, n: self.progress.emit(c, t, n),
                note=self.note,
            )
            if self.cancel_event.is_set() or getattr(report, "cancelled", False):
                self.cancelled.emit(self.seq)
                return
            if not report.recoverable:
                self.failed.emit(
                    self.seq,
                    "；".join(report.errors) or "备份未完成",
                )
                return
            self.finished_ok.emit(self.seq, report)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit(self.seq)
            else:
                self.failed.emit(self.seq, str(exc))


class BackupVerifyWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, seq: int, vault: str, version_id: str, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.vault = vault
        self.version_id = version_id
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = verify_version(
                self.vault, self.version_id,
                cancel_event=self.cancel_event,
                progress_cb=lambda c, t, n: self.progress.emit(c, t, n),
            )
            if self.cancel_event.is_set() or getattr(report, "cancelled", False):
                self.cancelled.emit(self.seq)
                return
            self.finished_ok.emit(self.seq, report)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit(self.seq)
            else:
                self.failed.emit(self.seq, str(exc))


class BackupRestoreWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, seq: int, vault: str, version_id: str,
                 target: str, parent=None):
        super().__init__(parent)
        self.seq = seq
        self.vault = vault
        self.version_id = version_id
        self.target = target
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = restore_version(
                self.vault, self.version_id, self.target,
                cancel_event=self.cancel_event,
                progress_cb=lambda c, t, n: self.progress.emit(c, t, n),
            )
            if self.cancel_event.is_set() or getattr(report, "cancelled", False):
                self.cancelled.emit(self.seq)
                return
            if not report.ok:
                self.failed.emit(
                    self.seq,
                    "；".join(report.errors) or "恢复失败",
                )
                return
            self.finished_ok.emit(self.seq, report)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit(self.seq)
            else:
                self.failed.emit(self.seq, str(exc))
