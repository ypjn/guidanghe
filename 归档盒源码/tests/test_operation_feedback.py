"""窗口顶部操作反馈：分色说明和加载进度条。"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from operation_feedback import (
    KIND_BUSY, KIND_ERROR, KIND_OK, KIND_WARN, classify, install,
)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify("正在搜索……"), KIND_BUSY)
        self.assertEqual(classify("进度 [2/8] 作业.pdf"), KIND_BUSY)
        self.assertEqual(classify("正在整理 [3/10] a.txt"), KIND_BUSY)
        self.assertEqual(classify("已移动 3 个文件。可在历史里撤销。"), KIND_OK)
        self.assertEqual(classify("试运行完成：样本 4 个"), KIND_OK)
        self.assertEqual(classify("核对完成：2 组；没有需要删除的文件"), KIND_OK)
        self.assertEqual(classify("读取版本失败：磁盘"), KIND_ERROR)
        self.assertEqual(classify("这个文件夹现在打不开：/tmp"), KIND_ERROR)
        self.assertEqual(classify("请先选择一个文件空间。"), KIND_WARN)
        self.assertEqual(classify("预览已取消。"), KIND_WARN)
        self.assertEqual(classify("方案已应用到当前目录。旧预览已失效。"), KIND_WARN)
        self.assertEqual(classify("输入后搜索。"), "info")


class FeedbackWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_progress_then_result(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        status = QLabel("输入后搜索。")
        layout.addWidget(status)
        feedback = install(layout, status)
        host.show()

        self.assertFalse(feedback.isVisible())
        status.setText("正在搜索……")
        self.assertTrue(feedback.isVisible())
        self.assertTrue(feedback.bar.isVisible())
        self.assertEqual(feedback.bar.minimum(), 0)
        self.assertEqual(feedback.bar.maximum(), 0)
        self.assertFalse(status.isVisible())

        status.setText("正在搜索 [2/8] 作业.pdf")
        self.assertEqual(feedback.bar.maximum(), 8)
        self.assertEqual(feedback.bar.value(), 2)
        self.assertTrue(feedback.bar.isTextVisible())

        status.setText("本页 2 条，从第 1 条开始。")
        self.assertFalse(feedback.bar.isVisible())
        self.assertIn("本页 2 条", feedback.message.text())
        self.assertEqual(status.text(), "本页 2 条，从第 1 条开始。")
        host.close()

    def test_external_progress_does_not_erase_message(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        feedback = install(layout)
        host.show()
        feedback.reflect("扫描中：已发现 4 个文件")
        feedback.set_progress(4, 9)
        self.assertEqual(feedback.message.text(), "扫描中：已发现 4 个文件")
        self.assertEqual(feedback.bar.value(), 4)
        self.assertEqual(feedback.bar.maximum(), 9)
        feedback.clear_progress()
        self.assertFalse(feedback.bar.isVisible())
        self.assertIn("扫描中", feedback.message.text())
        host.close()

    def test_html_status_becomes_plain_text(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        feedback = install(layout)
        host.show()
        feedback.reflect("共 3 个文件。<br>该视图只展示计划。")
        self.assertNotIn("<br>", feedback.message.text())
        self.assertIn("共 3 个文件", feedback.message.text())
        host.close()


if __name__ == "__main__":
    unittest.main()
