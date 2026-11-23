"""手册规则可以交给模型，分类结果会留下，下次打开仍用这次的规则。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from handbook_session import (  # noqa: E402
    PRIORITY, HandbookSession, fill_path, parse_handbook,
)

HANDBOOK = os.path.join(
    os.path.dirname(SRC), "docs", "handbooks",
    "2026-11-23-classification-handbook.md",
)


def _move(category: str, evidence: str = "文件名对得上标准名") -> str:
    return json.dumps({
        "category": category,
        "person": "张三" if "张三" in evidence else "",
        "action": "move",
        "evidence": evidence,
    }, ensure_ascii=False)


class HandbookSessionTest(unittest.TestCase):
    def test_parse_keeps_records_and_leaves_school_pack_out(self) -> None:
        rules = parse_handbook(HANDBOOK)
        names = [rule.name for rule in rules]
        self.assertEqual(len(names), 39)
        self.assertEqual(names[0], "发票")
        invoice = rules[0]
        self.assertEqual(invoice.pack, "财务")
        self.assertEqual(invoice.path_template, "_已整理/财务/发票/{年}/")
        self.assertIn("增值税电子普通发票", invoice.aliases)
        self.assertNotIn("高等数学", names)
        self.assertIn("房屋保险", names)
        self.assertIn("出行", names)

    def test_prompt_has_aliases_without_answer_key_or_paths(self) -> None:
        session = HandbookSession.create(
            os.path.join(tempfile.mkdtemp(), "session.json"), HANDBOOK)
        message = session.messages("待分类.pdf", "")
        text = message[0]["content"] + "\n" + message[1]["content"]
        self.assertIn("标准名：发票", text)
        self.assertIn("增值税电子普通发票", text)
        self.assertIn("财务优先", text)
        self.assertNotIn("京东", text)
        self.assertNotIn("Johnny", text)
        self.assertNotIn("_已整理", text)
        self.assertNotIn("全家福", text)

    def test_program_fills_path_from_filename(self) -> None:
        rules = {rule.name: rule.path_template for rule in parse_handbook(HANDBOOK)}
        self.assertEqual(
            fill_path(rules["发票"], "2026-03-14 京东 发票.pdf"),
            "_已整理/财务/发票/2026/",
        )
        self.assertEqual(
            fill_path(rules["发票"], "滴滴出行电子发票.pdf"),
            "_已整理/财务/发票/",
        )
        self.assertEqual(
            fill_path(rules["发票"], "滴滴出行电子发票.pdf", "开票日期：2024年8月2日"),
            "_已整理/财务/发票/2024/",
        )
        self.assertEqual(
            fill_path(rules["身份证件"], "张三 身份证.jpg", person="张三"),
            "_已整理/证件/身份证件/张三/",
        )
        self.assertEqual(
            fill_path(rules["身份证件"], "身份证.jpg", person="张三"),
            "_已整理/证件/身份证件/",
        )
        self.assertEqual(
            fill_path(rules["体检"], "2026 张三 体检报告.pdf", person="张三"),
            "_已整理/健康/体检/张三/2026/",
        )
        self.assertEqual(
            fill_path(rules["照片"], "2026-05-01 全家福.jpg"),
            "_已整理/照片/2026/2026-05/",
        )
        self.assertIsNone(fill_path(rules["照片"], "全家福.jpg"))
        self.assertEqual(
            fill_path(rules["出行"], "2026-04-18 酒店预订单.pdf"),
            "_已整理/出行/2026-04-18/",
        )
        self.assertIsNone(fill_path(rules["出行"], "酒店预订单.pdf"))

    def test_model_choice_is_saved_and_reused_on_next_open(self) -> None:
        root = tempfile.mkdtemp()
        store = os.path.join(root, "session.json")
        calls = []

        def ask(messages):
            calls.append(messages)
            text = messages[1]["content"]
            if "京东 发票" in text:
                return _move("发票", "文件名里有发票")
            if "不存在的抽屉" in text:
                return _move("综合档案", "模型自造了一个类")
            return json.dumps({"category": "", "action": "skip", "evidence": "对不上"})

        first = HandbookSession.create(store, HANDBOOK)
        invoice = first.classify("2026-03-14 京东 发票.pdf", "", ask)
        self.assertEqual(invoice.action, "move")
        self.assertEqual(invoice.category, "发票")
        self.assertEqual(invoice.path, "_已整理/财务/发票/2026/")
        self.assertFalse(invoice.reused)
        invented = first.classify("不存在的抽屉.pdf", "", ask)
        self.assertEqual(invented.action, "rejected")
        self.assertEqual(invented.path, "")

        with open(store, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("api_key", json.dumps(saved))
        self.assertEqual(saved["priority"], PRIORITY)
        self.assertEqual(len(saved["rules"]), 39)
        saved["priority"] = "规则甲：只许按已保存的标准名"
        with open(store, "w", encoding="utf-8") as handle:
            json.dump(saved, handle, ensure_ascii=False)

        def forbidden(_messages):
            raise AssertionError("已经留下的文件不应再问模型")

        second = HandbookSession.load(store)
        again = second.classify("2026-03-14 京东 发票.pdf", "", forbidden)
        self.assertTrue(again.reused)
        self.assertEqual(again.category, "发票")
        self.assertEqual(again.path, "_已整理/财务/发票/2026/")
        reused_reject = second.classify("不存在的抽屉.pdf", "", forbidden)
        self.assertTrue(reused_reject.reused)
        self.assertEqual(reused_reject.action, "rejected")

        def ask_new(messages):
            calls.append(messages)
            self.assertIn("规则甲：只许按已保存的标准名", messages[1]["content"])
            self.assertNotIn("即使对方是医院", messages[1]["content"])
            self.assertIn("标准名：说明书", messages[1]["content"])
            return _move("说明书", "文件名里有说明书")

        fresh = second.classify("洗衣机 说明书.pdf", "", ask_new)
        self.assertEqual(fresh.category, "说明书")
        self.assertEqual(fresh.path, "_已整理/购物/说明书/")
        self.assertEqual(len(calls), 3)

        third = HandbookSession.load(store)
        stuck = third.classify("洗衣机 说明书.pdf", "", forbidden)
        self.assertTrue(stuck.reused)
        self.assertEqual(stuck.path, "_已整理/购物/说明书/")


if __name__ == "__main__":
    unittest.main()
