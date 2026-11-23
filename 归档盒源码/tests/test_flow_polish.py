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

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox, QTextEdit

import main
from daily_entry_dialogs import RecentSpacesDialog, _format_opened
from planner import (
    MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE, OrganizePlan, PlanItem, generate_plan,
)
from recent_spaces import touch
from scanner import FileEntry, scan_directory


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

    def test_sidebar_opens_at_top(self):
        window = main.MainWindow()
        window.resize(1280, 800)
        window.show()
        self.app.processEvents()
        self.assertEqual(window.side_scroll.verticalScrollBar().value(), 0)
        window.close()


if __name__ == "__main__":
    unittest.main()
