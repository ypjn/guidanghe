"""把可检索的正文切成带位置的片段。

分类用的短摘录仍走 content_extract。这里按页、幻灯片、工作表或字符区间
读取，并在每个文件自己的时间和大小上限内停下。扫描版 PDF 不送去识别。
"""

from __future__ import annotations

import os
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from content_extract import _is_within_root
from scan_cache import file_identity, identity_matches

CONTENT_PARSER_VERSION = 1
PARSER_NAME = "pypdf"
PDF_PACKAGE_VERSION = "6.4.0"

TEXT_EXTENSIONS = {".txt", ".md"}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs",
    ".json", ".csv", ".xml", ".html", ".css", ".sh", ".yml", ".yaml",
    ".ini", ".toml",
}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
PDF_EXTENSIONS = {".pdf"}


@dataclass
class ContentLimits:
    max_file_bytes: int = 8_000_000
    max_pages: int = 40
    max_chars: int = 80_000
    max_seconds: float = 2.0
    max_member_bytes: int = 1_000_000
    chunk_chars: int = 800


@dataclass
class TextChunk:
    position: int
    location: str
    text: str


@dataclass
class ExtractionReport:
    status: str
    reason: str = ""
    chunks: list[TextChunk] = field(default_factory=list)
    parser_name: str = ""
    parser_version: int = CONTENT_PARSER_VERSION


class _Stop(Exception):
    def __init__(self, status: str, reason: str, chunks: list[TextChunk]):
        self.status = status
        self.reason = reason
        self.chunks = chunks


def extract_document(path: str, root: str, limits: ContentLimits | None = None,
                     cancel_event=None, before_body=None) -> ExtractionReport:
    """读取一个文件。读取前后都核对身份；对不上就丢弃这次正文。"""
    limits = limits or ContentLimits()
    if cancel_event is not None and cancel_event.is_set():
        return ExtractionReport("cancelled", "已取消")
    if os.path.islink(path) or not _is_within_root(path, root):
        return ExtractionReport("failed", "读取目标在空间之外或是符号链接，未读取正文")
    before = file_identity(path)
    if before is None:
        return ExtractionReport("failed", "不是普通文件或暂时读不到身份")
    if before_body is not None:
        before_body(path)
    if os.path.islink(path) or not _is_within_root(path, root):
        return ExtractionReport("stale", "读取前目标已离开空间，未读取正文")
    if not identity_matches(before, file_identity(path)):
        return ExtractionReport("stale", "读取前文件已变化，未读取正文")
    ext = os.path.splitext(path)[1].lower()
    started = time.monotonic()
    try:
        if ext in TEXT_EXTENSIONS or ext in CODE_EXTENSIONS:
            report = _read_plain(path, limits, started, ext in CODE_EXTENSIONS)
        elif ext in OFFICE_EXTENSIONS:
            report = _read_office(path, ext, limits, started, cancel_event)
        elif ext in PDF_EXTENSIONS:
            report = _read_pdf(path, limits, started, cancel_event)
        else:
            report = ExtractionReport("unsupported", "当前不索引这种格式的正文")
    except OSError as exc:
        report = ExtractionReport("failed", exc.strerror or "无法读取")
    after = file_identity(path)
    if report.status in {"complete", "partial"} and (
            os.path.islink(path) or not _is_within_root(path, root)
            or not identity_matches(before, after)):
        return ExtractionReport("stale", "读取过程中文件已变化，这次正文已丢弃")
    return report


def _budget_spent(started: float, limits: ContentLimits) -> bool:
    if limits.max_seconds <= 0:
        return True
    return time.monotonic() - started >= limits.max_seconds


def _split(text: str, location: str, limits: ContentLimits, position: int) -> list[TextChunk]:
    text = text.strip()
    if not text:
        return []
    pieces = []
    step = max(1, limits.chunk_chars)
    for offset in range(0, len(text), step):
        part = text[offset:offset + step]
        label = location if len(text) <= step else (
            f"{location} 字符 {offset + 1}-{offset + len(part)}")
        pieces.append(TextChunk(position + len(pieces), label, part))
    return pieces


def _finish(chunks: list[TextChunk], truncated_reason: str, parser_name: str) -> ExtractionReport:
    if truncated_reason:
        status = "partial" if chunks else "failed"
        return ExtractionReport(status, truncated_reason, chunks, parser_name)
    if not chunks:
        return ExtractionReport("unsupported", "没有可提取文字", [], parser_name)
    return ExtractionReport("complete", "", chunks, parser_name)


def _read_plain(path: str, limits: ContentLimits, started: float,
                code: bool) -> ExtractionReport:
    del started
    size = os.path.getsize(path)
    if size > limits.max_file_bytes:
        return ExtractionReport(
            "partial",
            f"文件有 {size} 字节，超过上限 {limits.max_file_bytes}，未读取正文")
    with open(path, "rb") as handle:
        raw = handle.read(limits.max_chars * 4 + 1)
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    reason = ""
    if len(text) > limits.max_chars or size > len(raw):
        text = text[:limits.max_chars]
        reason = f"已索引前 {limits.max_chars} 个字符，后面未读"
    label = "代码" if code else "正文"
    return _finish(_split(text, label, limits, 1), reason, "text")


def _read_office(path: str, ext: str, limits: ContentLimits, started: float,
                 cancel_event) -> ExtractionReport:
    size = os.path.getsize(path)
    if size > limits.max_file_bytes:
        return ExtractionReport(
            "partial",
            f"文件有 {size} 字节，超过上限 {limits.max_file_bytes}，未读取正文")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return ExtractionReport("failed", "不是有效的 Office 压缩包或文件已损坏")
    chunks: list[TextChunk] = []
    total = 0
    try:
        with archive:
            units = list(_office_units(archive, ext, limits))
            for index, (location, text) in enumerate(units, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    raise _Stop("cancelled", "已取消，本次正文未写入", [])
                if index > 1 and _budget_spent(started, limits):
                    raise _Stop("partial" if chunks else "failed",
                                f"解析超时，{location}及之后未读", chunks)
                if index > limits.max_pages:
                    raise _Stop("partial", f"已索引前 {limits.max_pages} 个部分，后面未读", chunks)
                room = limits.max_chars - total
                if room <= 0:
                    raise _Stop("partial", f"已索引前 {limits.max_chars} 个字符，后面未读", chunks)
                piece = text[:room]
                chunks.extend(_split(piece, location, limits, len(chunks) + 1))
                total += len(piece)
                if len(piece) < len(text):
                    raise _Stop(
                        "partial",
                        f"已索引前 {limits.max_chars} 个字符，{location}之后未完整覆盖",
                        chunks)
    except _Stop as stop:
        return ExtractionReport(stop.status, stop.reason, stop.chunks, "ooxml")
    except KeyError:
        return ExtractionReport("failed", "缺少正文部件")
    except ET.ParseError:
        return ExtractionReport("failed", "正文 XML 损坏，无法解析")
    except RuntimeError as exc:
        if "encrypted" in str(exc).lower():
            return ExtractionReport("failed", "文件已加密，未读取正文")
        return ExtractionReport("failed", "Office 文件无法解析")
    except zipfile.BadZipFile:
        return ExtractionReport("failed", "不是有效的 Office 压缩包或文件已损坏")
    return _finish(chunks, "", "ooxml")


def _office_units(archive: zipfile.ZipFile, ext: str, limits: ContentLimits):
    if ext == ".docx":
        root = _xml_member(archive, "word/document.xml", limits)
        number = 0
        for node in root.iter():
            if _local(node) != "p":
                continue
            number += 1
            text = _text_of(node, "t")
            if text:
                yield f"段落 {number}", text
        return
    if ext == ".pptx":
        slides = []
        for name in archive.namelist():
            marker = "ppt/slides/slide"
            if name.startswith(marker) and name.endswith(".xml"):
                digits = name[len(marker):-4]
                if digits.isdigit():
                    slides.append((int(digits), name))
        for number, name in sorted(slides):
            text = _text_of(_xml_member(archive, name, limits), "t")
            if text:
                yield f"幻灯片 {number}", text
        return
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = _xml_member(archive, "xl/sharedStrings.xml", limits)
        for node in root.iter():
            if _local(node) == "si":
                shared.append(_text_of(node, "t"))
    sheets = []
    for name in archive.namelist():
        marker = "xl/worksheets/sheet"
        if name.startswith(marker) and name.endswith(".xml"):
            digits = name[len(marker):-4]
            if digits.isdigit():
                sheets.append((int(digits), name))
    for number, name in sorted(sheets):
        cells = []
        for node in _xml_member(archive, name, limits).iter():
            if _local(node) != "c":
                continue
            inline = _text_of(node, "t")
            if inline:
                cells.append(inline)
                continue
            for child in node:
                if _local(child) == "v" and child.text:
                    value = child.text.strip()
                    if value.isdigit() and int(value) < len(shared):
                        cells.append(shared[int(value)])
                    else:
                        cells.append(value)
                    break
        if cells:
            yield f"工作表 {number}", "\t".join(cells)


def _xml_member(archive: zipfile.ZipFile, name: str, limits: ContentLimits):
    info = archive.getinfo(name)
    limit = limits.max_member_bytes
    if info.file_size > limit:
        raise _Stop("failed", f"解压量超过上限 {limit} 字节，未读取正文", [])
    with archive.open(name) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _Stop("failed", f"解压量超过上限 {limit} 字节，未读取正文", [])
    return ET.fromstring(data)


def _local(node) -> str:
    return (node.tag or "").rsplit("}", 1)[-1]


def _text_of(node, local_name: str) -> str:
    return "".join(
        child.text or ""
        for child in node.iter()
        if _local(child) == local_name).strip()


def _read_pdf(path: str, limits: ContentLimits, started: float,
              cancel_event) -> ExtractionReport:
    size = os.path.getsize(path)
    if size > limits.max_file_bytes:
        return ExtractionReport(
            "partial",
            f"文件有 {size} 字节，超过上限 {limits.max_file_bytes}，未读取正文")
    try:
        import logging
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
        logging.getLogger("pypdf").setLevel(logging.ERROR)
    except ImportError:
        return ExtractionReport("failed", "未安装 PDF 解析库，未读取正文")
    try:
        reader = PdfReader(path, strict=False)
    except PdfReadError:
        return ExtractionReport("failed", "PDF 无法解析")
    except Exception:
        return ExtractionReport("failed", "PDF 无法解析")
    if reader.is_encrypted:
        return ExtractionReport("failed", "PDF 已加密，未读取正文")
    chunks: list[TextChunk] = []
    total = 0
    try:
        page_total = len(reader.pages)
    except Exception:
        return ExtractionReport("failed", "PDF 无法解析")
    for index in range(1, page_total + 1):
        if cancel_event is not None and cancel_event.is_set():
            return ExtractionReport("cancelled", "已取消，本次正文未写入")
        if index > 1 and _budget_spent(started, limits):
            reason = f"解析超时，第 {index} 页及之后未读"
            return ExtractionReport("partial" if chunks else "failed", reason, chunks, PARSER_NAME)
        if index > limits.max_pages:
            return ExtractionReport(
                "partial", f"已索引前 {limits.max_pages} 页，后面的页未读",
                chunks, PARSER_NAME)
        try:
            text = reader.pages[index - 1].extract_text() or ""
        except Exception:
            return ExtractionReport(
                "partial" if chunks else "failed",
                f"第 {index} 页无法解析，后面的页未读", chunks, PARSER_NAME)
        text = " ".join(text.split())
        if not text:
            continue
        room = limits.max_chars - total
        if room <= 0:
            return ExtractionReport(
                "partial", f"已索引前 {limits.max_chars} 个字符，后面未读",
                chunks, PARSER_NAME)
        piece = text[:room]
        chunks.extend(_split(piece, f"第{index}页", limits, len(chunks) + 1))
        total += len(piece)
        if len(piece) < len(text):
            return ExtractionReport(
                "partial",
                f"已索引前 {limits.max_chars} 个字符，第 {index} 页之后未完整覆盖",
                chunks, PARSER_NAME)
    if not chunks:
        return ExtractionReport(
            "unsupported", "没有可提取文字，未做扫描识别", [], PARSER_NAME)
    return ExtractionReport("complete", "", chunks, PARSER_NAME)

