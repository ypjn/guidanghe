"""扫描版 PDF 的页渲染与识别编排。

不自己做文字识别。页面变成图片后交给调用方传入的识别函数。
"""
from __future__ import annotations

import os

SCAN_OCR_PAGES = 5
SCAN_OCR_LONG_EDGE = 1600
EMPTY_SCAN_REASON = "没有可提取文字，未做扫描识别"
CANCELLED_SCAN_REASON = "已取消，本次正文未写入"
PAGE_BREAK = "\f"


def needs_scan_ocr(status: str, reason: str) -> bool:
    return status == "unsupported" and reason == EMPTY_SCAN_REASON


def recognize_scanned_pdf(path: str, render_page, recognize, max_pages: int = SCAN_OCR_PAGES,
                          cancel_event=None, page_count: int | None = None,
                          long_edge: int = SCAN_OCR_LONG_EDGE) -> tuple[str, str]:
    """返回 (text, reason)。text 用换页符分隔各页。取消时不保留已识别文字。"""
    parts: list[str] = []
    for page_index in range(1, max_pages + 1):
        if cancel_event is not None and cancel_event.is_set():
            return "", CANCELLED_SCAN_REASON
        png = render_page(path, page_index, long_edge)
        if not png:
            break
        try:
            piece = str(recognize(png) or "").strip()
        finally:
            try:
                os.remove(png)
            except OSError:
                pass
        if piece:
            parts.append(piece)
    if cancel_event is not None and cancel_event.is_set():
        return "", CANCELLED_SCAN_REASON
    if not parts:
        return "", EMPTY_SCAN_REASON
    reason = ""
    if page_count is not None and page_count > max_pages:
        reason = f"已识别前 {max_pages} 页，后面的扫描页未读"
    return PAGE_BREAK.join(parts), reason
