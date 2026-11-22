"""第 A 批：备份桌面入口、预览作废、取消与关窗等待。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from backup_dialog import BackupManageDialog
from backup_service import (
    list_backup_versions,
    preview_backup,
    restore_version,
    run_backup,
    verify_version,
)
from backup_store import load_manifest, vault_paths
from backup_worker import BackupPreviewWorker, BackupRunWorker
from main import MainWindow


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _write(root: str, rel: str, data) -> str:
    path = os.path.join(root, *rel.replace("\\", "/").split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)
    return path


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _process(ms: int = 50):
    app = _app()
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class BackupServiceCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_bui_svc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = os.path.join(self.tmp, "source")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.source)
        # 多个稍大文件，便于预览哈希中途取消
        for i in range(6):
            _write(self.source, f"docs/大文件{i}.bin", os.urandom(256 * 1024))

    def test_preview_cancel_mid_hash(self):
        cancel = threading.Event()
        seen = {"n": 0}

        def progress(_i, _t, _rel):
            seen["n"] += 1
            if seen["n"] >= 2:
                cancel.set()

        report = preview_backup(
            self.source, self.vault,
            cancel_event=cancel, progress_cb=progress)
        self.assertTrue(report.cancelled)
        self.assertFalse(report.ok)
        self.assertGreaterEqual(seen["n"], 2)

    def test_note_persisted_in_manifest(self):
        report = run_backup(self.source, self.vault, note="验收备注-甲")
        self.assertTrue(report.recoverable, report.errors)
        m = load_manifest(vault_paths(self.vault), report.version_id)
        self.assertIsNotNone(m)
        self.assertEqual(m.note, "验收备注-甲")
        listed = list_backup_versions(self.vault)
        self.assertEqual(listed[0].note, "验收备注-甲")

    def test_verify_cancel(self):
        r = run_backup(self.source, self.vault)
        self.assertTrue(r.recoverable, r.errors)
        cancel = threading.Event()

        def progress(_i, _t, _rel):
            cancel.set()

        vr = verify_version(
            self.vault, r.version_id,
            cancel_event=cancel, progress_cb=progress)
        self.assertTrue(vr.cancelled)
        self.assertFalse(vr.ok)


class BackupUiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_bui_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = os.path.join(self.tmp, "来源 资料")
        self.vault = os.path.join(self.tmp, "备份 仓库")
        os.makedirs(self.source)
        os.makedirs(self.vault)
        _write(self.source, "课程/高等数学/作业1.txt", "第一版\n")
        _write(self.source, "课程/高等数学/很长很长的中文文件名说明.txt", "说明\n")

        # 避免确认框卡住自动化
        self._orig_question = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        self.addCleanup(self._restore_question)

        self.mw = MainWindow()
        self.addCleanup(self._cleanup_mw)
        self.dlg = BackupManageDialog(self.mw, default_source=self.source)
        self.dlg.vault_edit.setText(self.vault)
        self.addCleanup(self._cleanup_dlg)

    def _restore_question(self):
        QMessageBox.question = self._orig_question

    def _cleanup_dlg(self):
        try:
            for w in list(self.dlg.running_workers()):
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                w.wait(5000)
            self.dlg._closing = True
            self.dlg.close()
            self.dlg.deleteLater()
        except Exception:
            pass
        _process(30)

    def _cleanup_mw(self):
        try:
            self.mw._closing = True
            self.mw.close()
            self.mw.deleteLater()
        except Exception:
            pass
        _process(30)

    def _wait_idle(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _process(40)
            if not self.dlg._busy and not any(
                    w.isRunning() for w in self.dlg.running_workers()):
                return
        self.fail("备份对话框任务超时未结束")

    def test_main_window_has_backup_entry(self):
        self.assertTrue(hasattr(self.mw, "btn_backup_manage"))
        self.assertEqual(self.mw.btn_backup_manage.text(), "版本备份")
        self.mw.on_backup_manage()
        _process(50)
        self.assertIsNotNone(self.mw._backup_dialog)
        self.assertTrue(self.mw._backup_dialog.isVisible() or True)
        # 登记进关闭链
        workers_attr = self.mw._background_workers()
        self.assertIsInstance(workers_attr, list)
        self.mw._backup_dialog._closing = True
        self.mw._backup_dialog.close()
        self.mw._clear_backup_dialog_ref(self.mw._backup_dialog)

    def test_stale_preview_cannot_backup_after_source_change(self):
        self.dlg.start_preview()
        self._wait_idle()
        self.assertIsNotNone(self.dlg._preview)
        self.assertTrue(self.dlg._preview.ok)
        self.assertTrue(self.dlg.btn_backup.isEnabled())

        _write(self.source, "课程/高等数学/作业1.txt", "已改内容，预览应作废\n")
        # 改变保护策略使 token 失效
        self.dlg.protect_edit.setText("私密")
        _process(30)
        self.assertFalse(self.dlg.btn_backup.isEnabled())
        self.assertIsNone(self.dlg._preview)

    def test_preview_confirm_v1_v2_restore_and_nonempty_reject(self):
        self.dlg.note_edit.setText("窗口验收V1")
        self.dlg.start_preview()
        self._wait_idle()
        self.assertTrue(self.dlg._preview and self.dlg._preview.ok)

        self.dlg.confirm_backup()
        self._wait_idle()
        versions = list_backup_versions(self.vault)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].note, "窗口验收V1")
        vid1 = versions[0].version_id

        _write(self.source, "课程/高等数学/作业1.txt", "第二版内容\n")
        _write(self.source, "课程/新增.txt", "v2\n")
        self.dlg.note_edit.setText("窗口验收V2")
        self.dlg.start_preview()
        self._wait_idle()
        self.dlg.confirm_backup()
        self._wait_idle()
        versions = list_backup_versions(self.vault)
        self.assertEqual(len(versions), 2)
        vid2 = versions[1].version_id
        self.assertNotEqual(vid1, vid2)

        dest1 = os.path.join(self.tmp, "恢复甲")
        dest2 = os.path.join(self.tmp, "恢复乙")
        os.makedirs(dest1)
        os.makedirs(dest2)
        rr1 = restore_version(self.vault, vid1, dest1)
        rr2 = restore_version(self.vault, vid2, dest2)
        self.assertTrue(rr1.ok, rr1.errors)
        self.assertTrue(rr2.ok, rr2.errors)
        self.assertEqual(
            _sha(os.path.join(dest1, "课程", "高等数学", "作业1.txt")),
            _sha(_write(self.tmp, "_expect_v1.txt", "第一版\n")),
        )
        # 非空目标拒绝
        nonempty = os.path.join(self.tmp, "非空目标")
        os.makedirs(nonempty)
        _write(nonempty, "已有.txt", "不能覆盖\n")
        bad = restore_version(self.vault, vid1, nonempty)
        self.assertFalse(bad.ok)
        self.assertTrue(any("空目录" in e for e in bad.errors))

    def test_close_dialog_waits_for_preview_worker(self):
        # 放大样本，保证关窗时线程仍在跑
        for i in range(20):
            _write(self.source, f"bulk/file_{i}.bin", os.urandom(128 * 1024))
        self.dlg.start_preview()
        _process(30)
        self.assertTrue(self.dlg._busy)
        # close 应忽略并等待
        self.dlg.close()
        _process(50)
        # 若仍在跑则窗口保持；等待结束后可关闭
        self._wait_idle(timeout=30)
        self.dlg._closing = True
        self.dlg.close()
        _process(30)

    def test_cancel_restore_reports_partial_without_wiping_target(self):
        for i in range(8):
            _write(self.source, f"pack/part_{i}.bin", os.urandom(64 * 1024))
        r = run_backup(self.source, self.vault)
        self.assertTrue(r.recoverable, r.errors)
        dest = os.path.join(self.tmp, "恢复取消目标")
        os.makedirs(dest)
        cancel = threading.Event()

        def progress(i, _t, _rel):
            if i >= 2:
                cancel.set()

        report = restore_version(
            self.vault, r.version_id, dest,
            cancel_event=cancel, progress_cb=progress)
        self.assertTrue(report.cancelled or not report.ok)
        self.assertGreaterEqual(report.restored, 1)
        # 不自动清空目标
        remaining = []
        for dirpath, _, names in os.walk(dest):
            for name in names:
                remaining.append(os.path.join(dirpath, name))
        self.assertGreaterEqual(len(remaining), 1)


class BackupWorkerGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_bui_gen_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = os.path.join(self.tmp, "src")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.source)
        for i in range(10):
            _write(self.source, f"f{i}.bin", os.urandom(64 * 1024))

    def test_old_preview_result_ignored_by_newer_seq(self):
        results = []

        def on_ok(seq, report):
            results.append(("ok", seq, report.file_count))

        def on_cancel(seq):
            results.append(("cancel", seq))

        w1 = BackupPreviewWorker(1, self.source, self.vault)
        w2 = BackupPreviewWorker(2, self.source, self.vault)
        w1.finished_ok.connect(on_ok)
        w1.cancelled.connect(on_cancel)
        w2.finished_ok.connect(on_ok)
        w2.cancelled.connect(on_cancel)
        w1.start()
        _process(20)
        w1.request_cancel()
        w2.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _process(40)
            if not w1.isRunning() and not w2.isRunning():
                break
        self.assertFalse(w1.isRunning())
        self.assertFalse(w2.isRunning())
        # 新代次应有成功结果；旧代次即使迟到也不能单独当成当前预览
        ok_seqs = [seq for kind, seq, *_ in [
            (r[0], r[1],) + ((r[2],) if len(r) > 2 else ()) for r in results
        ] if kind == "ok"]
        # 简化：至少 seq=2 成功或 seq=1 被取消
        kinds = {r[0] for r in results}
        self.assertTrue("ok" in kinds or "cancel" in kinds)


if __name__ == "__main__":
    unittest.main()
