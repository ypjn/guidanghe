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


def _blank_pdf(path: str, password: str = "") -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    with open(path, "wb") as handle:
        writer.write(handle)


def _text_pdf(path: str, text: str) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)
    fonts = DictionaryObject({NameObject("/F1"): font_ref})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    stream = DecodedStreamObject()
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 18 Tf 72 720 Td ({safe}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with open(path, "wb") as handle:
        writer.write(handle)


class ScannedPdfGateTests(unittest.TestCase):
    def test_only_textless_pdf_is_scanned(self):
        from index_content import extract_document

        calls = []

        def scan_ocr(path, cancel_event=None):
            calls.append(os.path.basename(path))
            return "高等数学", ""

        with tempfile.TemporaryDirectory() as tmp:
            blank = os.path.join(tmp, "空白.pdf")
            texted = os.path.join(tmp, "有字.pdf")
            locked = os.path.join(tmp, "加密.pdf")
            huge = os.path.join(tmp, "太大.pdf")
            _blank_pdf(blank)
            _text_pdf(texted, "already")
            _blank_pdf(locked, password="secret")
            with open(huge, "wb") as handle:
                handle.write(b"0" * 8_000_001)
            blank_report = extract_document(blank, tmp, scan_ocr=scan_ocr)
            extract_document(texted, tmp, scan_ocr=scan_ocr)
            locked_report = extract_document(locked, tmp, scan_ocr=scan_ocr)
            extract_document(huge, tmp, scan_ocr=scan_ocr)
            self.assertEqual(calls, ["空白.pdf"])
            self.assertTrue(blank_report.chunks)
            self.assertIn("第1页（扫描）", blank_report.chunks[0].location)
            self.assertEqual(locked_report.reason, "PDF 已加密，未读取正文")


if __name__ == "__main__":
    unittest.main()
