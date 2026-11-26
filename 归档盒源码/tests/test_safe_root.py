"""整理空间不能选到系统目录，软件包也不会被拆开移动。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_APP_DIR = tempfile.mkdtemp(prefix="archivebox_safe_root_")
os.environ["ARCHIVEBOX_APP_DIR"] = _APP_DIR
os.environ["ARCHIVEBOX_LOG_DIR"] = os.path.join(_APP_DIR, "logs")
os.makedirs(os.environ["ARCHIVEBOX_LOG_DIR"], exist_ok=True)

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox

import main
from executor import execute_plan
from index_service import register_space
from index_store import IndexStoreError
from planner import OrganizePlan, PlanItem
from safe_root import (
    affected_reason, posix_root_reason, unsafe_root_reason, windows_root_reason,
)
from scanner import FileEntry, scan_directory, scan_paths
from storage import Storage


def _write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _item(root: str, rel: str, target_rel: str) -> PlanItem:
    abs_path = os.path.join(root, rel)
    return PlanItem(
        file=FileEntry(
            abs_path=abs_path, rel_path=rel,
            filename=os.path.basename(rel), file_type="文档",
            size=1, size_display="1 B",
        ),
        course="", file_type="文档", rule_applied="测试",
        target_dir_rel=os.path.dirname(target_rel),
        target_filename=os.path.basename(target_rel),
        target_rel_path=target_rel,
        selected=True,
    )


class PolicyTest(unittest.TestCase):
    def test_posix_allows_a_folder_inside_home_and_temp(self):
        home = "/Users/me"
        self.assertEqual(posix_root_reason("/Users/me/Desktop", home=home), "")
        self.assertEqual(posix_root_reason("/Users/me/Documents/课程", home=home), "")
        self.assertEqual(
            posix_root_reason("/private/var/folders/ab/T/job", home=home), "")
        self.assertEqual(
            posix_root_reason("/Volumes/Backup/Photos", home=home), "")

    def test_posix_refuses_disk_home_system_and_packages(self):
        home = "/Users/me"
        refused = {
            "/": "磁盘",
            "/Users": "用户",
            "/Users/me": "主目录",
            "/Users/me/Library": "资源库",
            "/Users/me/Library/Preferences": "资源库",
            "/Users/me/Applications": "应用程序",
            "/Applications": "应用程序",
            "/System/Library": "系统",
            "/usr/local": "系统",
            "/private/tmp": "临时",
            "/private/var": "系统",
            "/Volumes/Backup": "磁盘",
            "/Users/me/Desktop/Demo.app": "文件包",
            "/Users/me/.ssh": "隐藏",
        }
        for path, word in refused.items():
            reason = posix_root_reason(path, home=home)
            self.assertTrue(reason, path)
            self.assertIn(word, reason, f"{path} -> {reason}")

    def test_windows_refuses_drive_profile_and_appdata(self):
        home = r"C:\Users\me"
        self.assertEqual(
            windows_root_reason(r"D:\Photos", home=home), "")
        self.assertEqual(
            windows_root_reason(r"C:\Users\me\Documents", home=home), "")
        self.assertEqual(
            windows_root_reason(r"\\nas\share\docs", home=home), "")
        refused = [
            r"C:\\",
            r"C:\Users",
            r"C:\Users\me",
            r"C:\Users\me\AppData\Roaming",
            r"C:\Windows\System32",
            r"C:\Program Files\App",
            r"C:\ProgramData",
            r"\\nas\share",
        ]
        for path in refused:
            self.assertTrue(windows_root_reason(path, home=home), path)

    def test_keynote_file_can_move_but_package_cannot(self):
        self.assertEqual(affected_reason("/Users/me/Desktop/课件.key", is_dir=False), "")
        reason = affected_reason(
            "/Users/me/Desktop/课件.key/index.xml", is_dir=False)
        self.assertIn("文件包", reason)

    def test_this_machine_refuses_system_and_allows_documents(self):
        self.assertIn("磁盘", unsafe_root_reason("/"))
        home = os.path.expanduser("~")
        self.assertIn("主目录", unsafe_root_reason(home))
        self.assertIn("资源库", unsafe_root_reason(os.path.join(home, "Library")))
        self.assertTrue(unsafe_root_reason("/Applications"))
        self.assertTrue(unsafe_root_reason("/System/Library"))
        data = "/System/Volumes/Data"
        if os.path.isdir(data):
            self.assertTrue(unsafe_root_reason(data))
        desktop = os.path.join(home, "Desktop")
        documents = os.path.join(home, "Documents")
        if os.path.isdir(desktop):
            self.assertEqual(unsafe_root_reason(desktop), "")
        if os.path.isdir(documents):
            self.assertEqual(unsafe_root_reason(documents), "")
        data_desktop = "/System/Volumes/Data" + desktop
        if os.path.isdir(data_desktop):
            self.assertEqual(unsafe_root_reason(data_desktop), "")
        data_library = os.path.join("/System/Volumes/Data", home.lstrip("/"), "Library")
        if os.path.isdir(data_library):
            self.assertIn("资源库", unsafe_root_reason(data_library))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(unsafe_root_reason(folder), "")


class ScanAndMoveTest(unittest.TestCase):
    def test_dangerous_roots_are_not_walked(self):
        with patch("scanner.os.walk", side_effect=AssertionError("不应扫描")):
            for path in ("/", os.path.expanduser("~"), "/Applications"):
                result = scan_directory(path)
                self.assertEqual(result.files, [])
                self.assertTrue(result.errors, path)

    def test_scan_keeps_ordinary_files_and_skips_packages(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "note.txt", "n")
            _write(root, "课件.key", "keynote-file")
            _write(root, "Library/book.txt", "b")
            _write(root, "Demo.app/Contents/Info.plist", "secret")
            _write(root, "Notes.pages/index.xml", "pages")
            _write(root, "Backups.backupdb/info.txt", "backup")
            result = scan_directory(root, detect_project_roots=False)
            rels = {item.rel_path.replace("\\", "/") for item in result.files}
            self.assertEqual(rels, {"note.txt", "课件.key", "Library/book.txt"})
            picked = scan_paths(root, [os.path.join(root, "Demo.app", "Contents", "Info.plist")])
            self.assertEqual(picked.files, [])

    def test_execute_skips_package_contents(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "note.txt", "NOTE")
            secret = os.path.join(root, "Demo.app", "Contents", "Info.plist")
            _write(root, "Demo.app/Contents/Info.plist", "SECRET")
            plan = OrganizePlan(
                root=root, mode="by_type", rules=[],
                items=[
                    _item(root, "note.txt", os.path.join("_已整理", "note.txt")),
                    _item(root, "Demo.app/Contents/Info.plist",
                          os.path.join("_已整理", "Info.plist")),
                ],
            )
            result = execute_plan(plan, Storage(root))
            self.assertEqual(result.moved, 1, result.errors)
            self.assertEqual(result.skipped, 1, result.errors)
            self.assertTrue(os.path.isfile(secret))
            with open(secret, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "SECRET")
            self.assertTrue(os.path.isfile(os.path.join(root, "_已整理", "note.txt")))
            self.assertFalse(os.path.isfile(os.path.join(root, "_已整理", "Info.plist")))

    def test_execute_refuses_disk_root_before_any_batch(self):
        with tempfile.TemporaryDirectory() as root:
            plan = OrganizePlan(
                root="/", mode="by_type", rules=[],
                items=[_item("/", "archivebox-safe-root-probe.txt",
                             "archivebox-safe-root-probe-out.txt")],
            )
            before = os.path.exists("/.organizer")
            result = execute_plan(plan, Storage(root))
            self.assertEqual(result.batch_id, 0)
            self.assertEqual(result.moved, 0)
            self.assertTrue(result.errors)
            self.assertEqual(os.path.exists("/.organizer"), before)

    def test_register_space_refuses_home(self):
        with self.assertRaises(IndexStoreError):
            register_space(os.path.expanduser("~"))


class WindowGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rejected_root_restores_previous_space(self):
        window = main.MainWindow()
        self.addCleanup(window.close)
        good = tempfile.mkdtemp(prefix="archivebox_good_")
        self.addCleanup(shutil.rmtree, good, True)
        self.assertTrue(window._reset_for_new_dir(good))
        disk_marker = os.path.exists("/.organizer")
        home = os.path.expanduser("~")
        home_marker = os.path.exists(os.path.join(home, ".organizer"))
        with patch.object(QMessageBox, "warning", return_value=None) as warn:
            self.assertFalse(window._reset_for_new_dir("/"))
            self.assertFalse(window._reset_for_new_dir(home))
            window.path_edit.setText("/")
            window._start_scan("/")
        self.assertGreaterEqual(warn.call_count, 3)
        self.assertEqual(
            os.path.realpath(window.path_edit.text()), os.path.realpath(good))
        self.assertFalse(window.task.working)
        self.assertIsNone(window._scan_worker)
        self.assertEqual(os.path.exists("/.organizer"), disk_marker)
        self.assertEqual(os.path.exists(os.path.join(home, ".organizer")), home_marker)


if __name__ == "__main__":
    unittest.main()
