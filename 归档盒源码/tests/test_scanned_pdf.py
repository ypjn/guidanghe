"""扫描版 PDF：只在整份没有可复制文字、且用户打开识别时渲染。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from pdf_pages import (  # noqa: E402
    CANCELLED_SCAN_REASON, EMPTY_SCAN_REASON, needs_scan_ocr, recognize_scanned_pdf,
)


class ScannedPdfDecisionTests(unittest.TestCase):
    def test_blank_reason_is_the_only_scan_candidate(self):
        self.assertTrue(needs_scan_ocr("unsupported", EMPTY_SCAN_REASON))
        self.assertFalse(needs_scan_ocr("complete", ""))
        self.assertFalse(needs_scan_ocr("failed", "PDF 已加密，未读取正文"))

    def test_recognize_joins_pages_and_stops_at_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = []

            def render_page(path, page_index, long_edge):
                if page_index > 5:
                    raise AssertionError("不应渲染第 6 页")
                rendered.append(page_index)
                png = os.path.join(tmp, f"{page_index}.png")
                with open(png, "wb") as handle:
                    handle.write(b"png")
                return png

            text, reason = recognize_scanned_pdf(
                "scan.pdf", render_page, lambda png: "高等数学", max_pages=5)
            self.assertEqual(rendered, [1, 2, 3, 4, 5])
            self.assertIn("高等数学", text)
            self.assertEqual(reason, "")
            self.assertFalse(os.path.exists(os.path.join(tmp, "1.png")))

    def test_cancel_before_second_page_drops_text_and_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = threading.Event()

            def render_page(path, page_index, long_edge):
                png = os.path.join(tmp, f"{page_index}.png")
                with open(png, "wb") as handle:
                    handle.write(b"png")
                return png

            def recognize(png):
                cancel.set()
                return "高等数学"

            text, reason = recognize_scanned_pdf(
                "scan.pdf", render_page, recognize, max_pages=5,
                cancel_event=cancel)
            self.assertEqual(text, "")
            self.assertEqual(reason, CANCELLED_SCAN_REASON)
            self.assertFalse(os.path.exists(os.path.join(tmp, "1.png")))


if __name__ == "__main__":
    unittest.main()
