"""确认框、作废计数、规则说明、撤销文案和默认勾选。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="archivebox_flow_")
os.environ["ARCHIVEBOX_LOG_DIR"] = os.path.join(
    os.environ["ARCHIVEBOX_APP_DIR"], "logs")
os.makedirs(os.environ["ARCHIVEBOX_LOG_DIR"], exist_ok=True)

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox, QTextEdit

import main
from daily_entry_dialogs import RecentSpacesDialog, _format_opened
from planner import (
    MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE, OrganizePlan, PlanItem, generate_plan,
)
from recent_spaces import touch
from executor import execute_plan, undo_latest_batch
from scanner import FileEntry, ScanResult, scan_directory
from storage import Storage


def _write(root: str, rel: str, text: str = "x") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _item(root: str, rel: str, target_dir: str) -> PlanItem:
    name = os.path.basename(rel)
    entry = FileEntry(
        abs_path=os.path.join(root, rel), rel_path=rel, filename=name,
        file_type="文档", size=1, size_display="1 B",
    )
    return PlanItem(
        file=entry, course="", file_type="文档", rule_applied="按类型模式",
        target_dir_rel=target_dir, target_filename=name,
        target_rel_path=os.path.join(target_dir, name),
    )


class FlowPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_nested_file_stays_unchecked_in_by_type(self):
        root = tempfile.mkdtemp(prefix="archivebox_sel_")
        _write(root, "散页.txt")
        _write(root, os.path.join("下载", "高数.txt"))
        _write(root, os.path.join("已有课程", "线性代数", "讲义.txt"))
        plan = generate_plan(
            scan_directory(root, detect_project_roots=False), MODE_BY_TYPE, [])
        chosen = {item.file.rel_path: item.selected for item in plan.items}
        self.assertTrue(chosen["散页.txt"])
        self.assertTrue(chosen[os.path.join("下载", "高数.txt")])
        nested = os.path.join("已有课程", "线性代数", "讲义.txt")
        self.assertFalse(chosen[nested])
        kept = next(item for item in plan.items if item.file.rel_path == nested)
        self.assertIn("默认不移动", kept.rule_applied)

    def test_recommended_mode_still_checks_course_files(self):
        root = tempfile.mkdtemp(prefix="archivebox_rec_")
        _write(root, os.path.join("大二上", "数据结构", "作业", "a.pdf"), "pdf")
        plan = generate_plan(
            scan_directory(root, detect_project_roots=False),
            MODE_BY_DIRECTION_SEMESTER, [])
        self.assertEqual(len(plan.items), 1)
        self.assertNotIn("已在自有文件夹中", plan.items[0].rule_applied or "")

    def test_confirm_lists_real_targets(self):
        root = tempfile.mkdtemp(prefix="archivebox_confirm_")
        plan = OrganizePlan(
            root=root, mode=MODE_BY_TYPE, rules=[],
            items=[_item(root, "散页.txt", os.path.join("_已整理", "文档"))],
        )
        dialog = main.ExecuteConfirmDialog(root, plan)
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        files = " ".join(
            edit.toPlainText() for edit in dialog.findChildren(QTextEdit))
        self.assertIn(os.path.join(root, "_已整理", "文档"), labels)
        self.assertNotIn("os.path.join", labels)
        self.assertIn("散页.txt", files)
        self.assertIn(os.path.join(root, "_已整理", "文档", "散页.txt"), files)
        box = dialog.findChild(QDialogButtonBox)
        self.assertEqual(box.button(QDialogButtonBox.Cancel).text(), "取消")
        dialog.close()

    def test_rule_sentence_follows_mode(self):
        window = main.MainWindow()
        window.path_edit.setText(tempfile.mkdtemp(prefix="archivebox_mode_"))
        window.mode_combo.setCurrentIndex(0)
        window._update_config_label()
        self.assertNotIn("正在使用", window.config_label.text())
        self.assertIn("按类型", window.config_label.text())
        window.mode_combo.setCurrentIndex(2)
        window._update_config_label()
        self.assertIn("正在使用", window.config_label.text())
        window.close()

    def test_invalid_plan_clears_move_count(self):
        window = main.MainWindow()
        root = tempfile.mkdtemp(prefix="archivebox_count_")
        window.path_edit.setText(root)
        plan = OrganizePlan(
            root=root, mode=MODE_BY_TYPE, rules=[],
            items=[_item(root, "散页.txt", os.path.join("_已整理", "文档"))],
        )
        window.current_plan = plan
        window._plan_valid = True
        window._update_plan_counts()
        self.assertIn("将移动 1", window.impact_label.text())
        window.on_plan_param_changed()
        self.assertEqual(window.impact_label.text(), "")
        self.assertEqual(window.plan_counts_label.text(), "")
        self.assertFalse(window.btn_execute.isEnabled())
        window._plan_valid = True
        window._update_button_states()
        self.assertIn("按类型", window.override_hint.text())
        self.assertFalse(window.btn_override_selected.isEnabled())
        window.close()

    def test_undo_prompt_uses_readable_time_and_buttons(self):
        window = main.MainWindow()
        batch = type("Batch", (), {
            "batch_id": 3,
            "created_at": "2026-11-23T14:40:09",
            "finished_at": "",
        })()
        action = type("Action", (), {"status": "moved"})()
        window._storage = type("Storage", (), {
            "get_undoable_batch": lambda self: batch,
            "get_actions": lambda self, _batch_id: [action],
        })()
        seen = {}

        def fake_exec(box):
            seen["title"] = box.windowTitle()
            seen["text"] = box.text()
            seen["buttons"] = [button.text() for button in box.buttons()]
            return 0

        old_exec = QMessageBox.exec
        old_undo = main.undo_is_available
        old_blocked = main.interrupted_batch_id
        QMessageBox.exec = fake_exec
        main.undo_is_available = lambda _storage: True
        main.interrupted_batch_id = lambda _storage: None
        try:
            window.on_undo()
        finally:
            QMessageBox.exec = old_exec
            main.undo_is_available = old_undo
            main.interrupted_batch_id = old_blocked
        self.assertIn("2026-11-23 14:40", seen["text"])
        self.assertNotIn("T14:40", seen["text"])
        self.assertEqual(seen["buttons"], ["撤销", "留下"])
        window._storage = None
        window.close()

    def test_success_box_does_not_block_and_offers_undo(self):
        window = main.MainWindow()
        window._show_execute_success("整理完成！")
        box = window._execute_result_box
        self.assertFalse(box.isModal())
        labels = [button.text() for button in box.buttons()]
        self.assertEqual(labels, ["知道了", "撤销上一次"])
        box.close()
        window.close()

    def test_recent_space_time_and_headers(self):
        self.assertEqual(_format_opened("2026-11-23T14:40:09.123456"), "2026-11-23 14:40")
        root = tempfile.mkdtemp(prefix="archivebox_recent_")
        touch(root, "样本")
        dialog = RecentSpacesDialog(lambda _path: True)
        headers = [
            label.text() for label in dialog.findChildren(QLabel)
            if label.text() in {"选择", "名称", "完整路径", "最近打开"}
        ]
        self.assertEqual(headers, ["选择", "名称", "完整路径", "最近打开"])
        times = [
            label.text() for label in dialog.findChildren(QLabel)
            if label.text().startswith("20")
        ]
        self.assertTrue(times)
        self.assertNotIn("T", times[0])
        choice = dialog.rows_grid.columnMinimumWidth(0)
        self.assertGreaterEqual(choice, 64)
        dialog.close()

    def test_path_bar_commits_a_typed_folder(self):
        window = main.MainWindow()
        first = tempfile.mkdtemp(prefix="archivebox_path_a_")
        second = tempfile.mkdtemp(prefix="archivebox_path_b_")
        window.path_edit.setText(first)
        window._path_edit_session = True
        window._path_edit_origin = first
        window.current_plan = "keep"
        window.path_edit.setText(first + os.sep)
        window._commit_typed_path()
        self.assertEqual(window.current_plan, "keep")
        self.assertEqual(window.path_edit.text(), first)
        window._path_edit_session = True
        window.path_edit.setText(second)
        window._commit_typed_path()
        self.assertEqual(os.path.normpath(window.path_edit.text()), os.path.normpath(second))
        self.assertIsNone(window.current_plan)
        self.assertTrue(window.btn_scan.isEnabled())
        window._path_edit_session = True
        window.path_edit.setText(os.path.join(second, "没有这个文件夹"))
        window._commit_typed_path()
        self.assertEqual(os.path.normpath(window.path_edit.text()), os.path.normpath(second))
        self.assertIn("打不开", window.statusBar().currentMessage())
        window._working = True
        window._path_edit_session = True
        third = tempfile.mkdtemp(prefix="archivebox_path_c_")
        window.path_edit.setText(third)
        window._commit_typed_path()
        self.assertEqual(os.path.normpath(window.path_edit.text()), os.path.normpath(second))
        self.assertIn("不能切换", window.statusBar().currentMessage())
        self.assertFalse(window.path_edit.isReadOnly())
        window._working = False
        window._path_edit_session = True
        window._path_edit_origin = os.path.normpath(second)
        window.current_plan = "keep"
        window.path_edit.setText(third)
        window.path_edit.setSelection(0, 4)
        window._remember_path_edit_state(window.path_edit)
        window._keep_path_edit_highlight()
        self.assertEqual(window.path_edit.text(), third)
        self.assertEqual(window.current_plan, "keep")
        self.assertGreater(window.path_edit.selectionEnd(), window.path_edit.selectionStart())
        window.close()

    def test_header_select_all_toggles_visible_rows(self):
        window = main.MainWindow()
        root = tempfile.mkdtemp(prefix="archivebox_all_")
        first = _item(root, "甲.txt", os.path.join("_已整理", "文档"))
        second = _item(root, "乙.txt", os.path.join("_已整理", "文档"))
        second.selected = False
        plan = OrganizePlan(
            root=root, mode=MODE_BY_TYPE, rules=[], items=[first, second])
        window.current_plan = plan
        window._plan_valid = True
        window._populate_table(plan, reset_filters=True)
        self.assertEqual(window.header_select_all.text(), "全选")
        self.assertEqual(
            window.header_select_all.checkState(), Qt.CheckState.PartiallyChecked)
        window.resize(1100, 700)
        window.show()
        self.app.processEvents()
        self.assertGreater(window.header_select_all.width(), 0)
        self.assertLess(window.header_select_all.x(), window.table.columnWidth(0))
        window.plan_search.setText("乙")
        window._on_header_select_all()
        self.assertTrue(first.selected)
        self.assertTrue(second.selected)
        window.plan_search.clear()
        self.assertEqual(
            window.header_select_all.checkState(), Qt.CheckState.Checked)
        window._on_header_select_all()
        self.assertFalse(first.selected)
        self.assertFalse(second.selected)
        window._on_header_select_all()
        self.assertTrue(first.selected)
        self.assertTrue(second.selected)
        self.assertEqual(
            window.header_select_all.checkState(), Qt.CheckState.Checked)
        window._on_header_select_all()
        self.assertFalse(first.selected)
        self.assertFalse(second.selected)
        window.close()

    def test_scan_notice_caps_protected_roots(self):
        root = tempfile.mkdtemp(prefix="archivebox_notice_")
        result = ScanResult(root=root)
        result.protected_roots = {}
        for index in range(30):
            path = os.path.join(root, f"project-{index:02d}")
            os.makedirs(path)
            result.protected_roots[path] = "自动识别（项目标记 package.json 存在）"
        result.protected_kinds = {path: "auto" for path in result.protected_roots}
        result.protected_count = 400
        text = "\n".join(main.build_scan_notices(result))
        self.assertIn("project-00", text)
        self.assertIn("project-19", text)
        self.assertNotIn("project-20", text)
        self.assertNotIn(os.path.join(root, "project-00"), text)
        self.assertIn("还有 10 个文件夹未列出", text)
        self.assertIn("不拆开", text)

    def test_protected_folder_is_one_package(self):
        root = tempfile.mkdtemp(prefix="archivebox_pkg_")
        project = os.path.join(root, "mathorcup")
        os.makedirs(os.path.join(project, ".git"))
        os.makedirs(os.path.join(project, "web"))
        with open(os.path.join(project, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("print(1)\n")
        with open(os.path.join(project, "data.csv"), "w", encoding="utf-8") as handle:
            handle.write("a,b\n")
        with open(os.path.join(project, "web", "package.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(project, "web", "index.js"), "w", encoding="utf-8") as handle:
            handle.write("console.log(1)\n")
        with open(os.path.join(root, "散页.txt"), "w", encoding="utf-8") as handle:
            handle.write("note\n")

        plan = generate_plan(scan_directory(root), MODE_BY_TYPE, [])
        rels = [item.file.rel_path for item in plan.items]
        self.assertIn("mathorcup", rels)
        self.assertNotIn(os.path.join("mathorcup", "main.py"), rels)
        self.assertNotIn(os.path.join("mathorcup", "web"), rels)
        self.assertTrue(any(item.file.filename == "散页.txt" for item in plan.items))
        package = next(item for item in plan.items if item.file.filename == "mathorcup")
        self.assertTrue(package.is_package)
        self.assertEqual(package.file.filename, "mathorcup")
        self.assertIn("项目", package.target_rel_path)
        self.assertTrue(package.target_rel_path.endswith("mathorcup"))
        self.assertTrue(package.selected)

        for item in plan.items:
            item.selected = item.is_package
        storage = Storage(root)
        try:
            executed = execute_plan(plan, storage)
            self.assertEqual(executed.moved, 1, executed.errors)
            self.assertFalse(os.path.exists(project))
            moved = os.path.join(root, "_已整理", "项目", "mathorcup")
            self.assertTrue(os.path.isfile(os.path.join(moved, "main.py")))
            self.assertTrue(os.path.isfile(os.path.join(moved, "web", "index.js")))
            undone = undo_latest_batch(storage)
            self.assertEqual(undone.restored, 1, undone.errors_list)
            self.assertTrue(os.path.isfile(os.path.join(project, "main.py")))
            self.assertTrue(os.path.isfile(os.path.join(project, "web", "index.js")))
            self.assertFalse(os.path.exists(moved))
        finally:
            storage.close()

    def test_scan_notice_dialog_keeps_confirm_on_screen(self):
        dialog = main.ScanNoticeDialog("保护目录\n" * 800)
        dialog.show()
        self.app.processEvents()
        screen = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
        self.assertLessEqual(dialog.frameGeometry().width(), screen.width())
        button = dialog.findChild(QDialogButtonBox)
        self.assertEqual(button.button(QDialogButtonBox.Ok).text(), "知道了")
        self.assertLess(button.geometry().bottom(), dialog.height())
        self.assertGreaterEqual(button.geometry().top(), 0)
        dialog.close()

    def test_protect_dialog_stays_within_screen(self):
        root = tempfile.mkdtemp(prefix="archivebox_protect_")
        readonly = [
            (os.path.join(root, f"project-{index:03d}"), "auto",
             "自动识别（项目标记 package.json 存在）")
            for index in range(200)
        ]
        dialog = main.ProtectDirDialog(root, [], readonly, skipped_count=2000)
        dialog.show()
        self.app.processEvents()
        screen = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
        box = dialog.findChild(QDialogButtonBox)
        self.assertEqual(box.button(QDialogButtonBox.Ok).text(), "应用")
        self.assertLess(box.geometry().bottom(), dialog.height())
        dialog.close()

    def test_sidebar_opens_at_top(self):
        window = main.MainWindow()
        window.resize(1280, 800)
        window.show()
        self.app.processEvents()
        self.assertEqual(window.side_scroll.verticalScrollBar().value(), 0)
        window.close()


if __name__ == "__main__":
    unittest.main()
