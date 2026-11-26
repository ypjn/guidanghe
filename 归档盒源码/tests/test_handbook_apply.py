"""主窗口选定一个分类包后，预览按该包改目标，并留下下次还能用的记录。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from handbook_apply import (  # noqa: E402
    SCHOOL_PACK,
    apply_handbook_pack,
    list_pack_choices,
    load_pack_choice,
    save_pack_choice,
)
from handbook_session import open_pack_session  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE, OrganizePlan, PlanItem,
)
from scanner import FileEntry  # noqa: E402


def _item(root: str, rel: str) -> PlanItem:
    name = os.path.basename(rel)
    entry = FileEntry(
        abs_path=os.path.join(root, rel),
        rel_path=rel,
        filename=name,
        file_type="文档",
        size=12,
        size_display="12 B",
    )
    return PlanItem(
        file=entry,
        course="待定",
        file_type="文档",
        rule_applied="规则没有命中",
        target_dir_rel="_已整理/待定",
        target_filename=name,
        target_rel_path=os.path.join("_已整理/待定", name),
        direction="待定",
        category="",
        rule_key="fallback",
        selected=False,
    )


def _plan(root: str, mode: str, items: list[PlanItem]) -> OrganizePlan:
    return OrganizePlan(root=root, mode=mode, rules=[], items=items)


class _Ready:
    enabled = True

    def ready(self) -> bool:
        return True


class HandbookApplyTest(unittest.TestCase):
    def test_pack_list_keeps_school_out(self) -> None:
        names = list_pack_choices()
        self.assertIn("财务", names)
        self.assertIn("证件", names)
        self.assertNotIn(SCHOOL_PACK, names)

    def test_one_pack_prompt_omits_other_packs_and_paths(self) -> None:
        book = os.path.join(
            os.path.dirname(SRC), "docs", "handbooks",
            "2026-11-23-classification-handbook.md")
        store = os.path.join(tempfile.mkdtemp(), "证件.json")
        session = open_pack_session(store, book, "证件")
        text = session.messages("身份证.jpg", "")[1]["content"]
        self.assertIn("标准名：身份证件", text)
        self.assertIn("只从「证件」包", text)
        self.assertNotIn("标准名：发票", text)
        self.assertNotIn("_已整理", text)

    def test_choice_is_saved_per_folder_and_unknown_falls_back(self) -> None:
        root = tempfile.mkdtemp()
        self.assertEqual(load_pack_choice(root), SCHOOL_PACK)
        save_pack_choice(root, "财务")
        self.assertEqual(load_pack_choice(root), "财务")
        path = os.path.join(root, ".organizer", "handbook_pack.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "pack": "不存在的包"}, handle)
        self.assertEqual(load_pack_choice(root), SCHOOL_PACK)

    def test_saved_decision_is_reused_without_asking_again(self) -> None:
        root = tempfile.mkdtemp()
        first = _item(root, "2026-03-14 京东 发票.pdf")
        plan = _plan(root, MODE_BY_DIRECTION_SEMESTER, [first])
        calls = []

        def ask(_settings, messages):
            calls.append(messages)
            return json.dumps({
                "category": "发票",
                "person": "",
                "action": "move",
                "evidence": "文件名写明是发票",
            }, ensure_ascii=False)

        note = apply_handbook_pack(plan, "财务", _Ready(), transport=ask)
        self.assertEqual(len(calls), 1)
        self.assertIn("标准名：发票", calls[0][1]["content"])
        self.assertNotIn("标准名：身份证件", calls[0][1]["content"])
        self.assertNotIn("_已整理", calls[0][1]["content"])
        self.assertTrue(first.selected)
        self.assertEqual(first.target_dir_rel, "_已整理/财务/发票/2026")
        self.assertIn("分类 1 个", note)

        again = _item(root, "2026-03-14 京东 发票.pdf")
        second = _plan(root, MODE_BY_DIRECTION_SEMESTER, [again])

        def refuse(_settings, _messages):
            raise AssertionError("已经分过的文件不应再问模型")

        apply_handbook_pack(second, "财务", _Ready(), transport=refuse)
        self.assertTrue(again.selected)
        self.assertEqual(again.target_dir_rel, "_已整理/财务/发票/2026")
        self.assertIn("沿用已保存", again.rule_applied)

    def test_unknown_category_stays_put_and_type_mode_ignores_pack(self) -> None:
        root = tempfile.mkdtemp()
        item = _item(root, "高等数学 作业.pdf")
        plan = _plan(root, MODE_BY_DIRECTION_SEMESTER, [item])

        def ask(_settings, _messages):
            return json.dumps({
                "category": "综合档案",
                "action": "move",
                "evidence": "想新造一个类别",
            }, ensure_ascii=False)

        apply_handbook_pack(plan, "财务", _Ready(), transport=ask)
        self.assertFalse(item.selected)
        self.assertEqual(item.direction, "待定")
        self.assertEqual(item.target_dir_rel, "")
        self.assertIn("想新造一个类别", item.rule_applied)

        typed = _item(root, "2026 发票.pdf")
        typed.selected = True
        typed.target_dir_rel = "_已整理/文档"
        by_type = _plan(root, MODE_BY_TYPE, [typed])
        note = apply_handbook_pack(by_type, "财务", _Ready(), transport=ask)
        self.assertIn("按类型", note)
        self.assertTrue(typed.selected)
        self.assertEqual(typed.target_dir_rel, "_已整理/文档")

    def test_without_ai_only_saved_files_move(self) -> None:
        root = tempfile.mkdtemp()
        known = _item(root, "2026-03-14 京东 发票.pdf")
        fresh = _item(root, "另一张.pdf")
        plan = _plan(root, MODE_BY_DIRECTION_SEMESTER, [known])

        def ask(_settings, _messages):
            return json.dumps({
                "category": "发票",
                "action": "move",
                "evidence": "文件名写明是发票",
            }, ensure_ascii=False)

        apply_handbook_pack(plan, "财务", _Ready(), transport=ask)
        both = _plan(root, MODE_BY_DIRECTION_SEMESTER, [
            _item(root, "2026-03-14 京东 发票.pdf"),
            fresh,
        ])
        apply_handbook_pack(both, "财务", settings=None, transport=ask)
        self.assertTrue(both.items[0].selected)
        self.assertFalse(fresh.selected)
        self.assertIn("打开 AI", fresh.rule_applied)

    def test_school_pack_does_not_rewrite_the_plan(self) -> None:
        root = tempfile.mkdtemp()
        item = _item(root, "高等数学 作业.pdf")
        item.selected = True
        item.target_dir_rel = "_已整理/课程/高等数学"
        plan = _plan(root, MODE_BY_DIRECTION_SEMESTER, [item])

        def refuse(_settings, _messages):
            raise AssertionError("学习包不应询问分类手册")

        note = apply_handbook_pack(plan, SCHOOL_PACK, _Ready(), transport=refuse)
        self.assertEqual(note, "")
        self.assertTrue(item.selected)
        self.assertEqual(item.target_dir_rel, "_已整理/课程/高等数学")


class HandbookWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="archivebox_pack_")
        os.environ["ARCHIVEBOX_LOG_DIR"] = os.path.join(
            os.environ["ARCHIVEBOX_APP_DIR"], "logs")
        os.makedirs(os.environ["ARCHIVEBOX_LOG_DIR"], exist_ok=True)
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_window_remembers_the_pack_for_that_folder(self) -> None:
        import main
        window = main.MainWindow()
        self.assertEqual(window.pack_combo.currentData(), SCHOOL_PACK)
        self.assertGreaterEqual(window.pack_combo.count(), 2)
        root = tempfile.mkdtemp()
        window.path_edit.setText(root)
        index = window.pack_combo.findData("健康")
        self.assertGreaterEqual(index, 0)
        window.pack_combo.setCurrentIndex(index)
        self.assertEqual(load_pack_choice(root), "健康")
        window._load_pack_choice(root)
        self.assertEqual(window.pack_combo.currentData(), "健康")
        window._update_config_label()
        self.assertIn("健康", window.config_label.text())
        self.assertNotIn("正在使用", window.config_label.text())
        window.close()
