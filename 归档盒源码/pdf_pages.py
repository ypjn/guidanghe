"""扫描版 PDF 的页渲染与识别编排。

不自己做文字识别。页面变成图片后交给调用方传入的识别函数。
"""
from __future__ import annotations

import os
import tempfile

from ocr_engine import OcrError, model_cache_dir

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


def pdf_page_count(path: str) -> int:
    document = _open_pdf(path)
    try:
        return len(document)
    finally:
        document.close()


def render_pdf_page(path: str, page_index: int,
                    long_edge: int = SCAN_OCR_LONG_EDGE) -> str | None:
    """把一页渲染成 PNG。没有这一页时返回 None。"""
    document = _open_pdf(path)
    page = None
    bitmap = None
    try:
        if page_index < 1 or page_index > len(document):
            return None
        page = document[page_index - 1]
        width, height = page.get_size()
        longest = max(float(width), float(height), 1.0)
        scale = min(long_edge / longest, 4.0)
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        folder = os.path.join(model_cache_dir(), "pages")
        os.makedirs(folder, exist_ok=True)
        handle, dest = tempfile.mkstemp(
            prefix=f"page{page_index}-", suffix=".png", dir=folder)
        os.close(handle)
        image.save(dest, format="PNG")
        return dest
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"PDF 无法渲染：{exc}") from exc
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        document.close()


def _open_pdf(path: str):
    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        raise OcrError("缺少 PDF 页面渲染组件。") from exc
    try:
        return pdfium.PdfDocument(path)
    except Exception as exc:
        raise OcrError(f"PDF 无法渲染：{exc}") from exc

