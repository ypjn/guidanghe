"""读取待整理文件的正文片段，供分类规则和预览依据使用。

纯文本（.txt/.md）按块读到上限即停；.docx/.pptx/.xlsx 用标准库
zipfile + XML 读取正文，不新增联网或第三方解析依赖。PDF、旧版
Office 和压缩包继续明确标成「未支持」，坏文件只记录原因，后面的
文件继续处理。图片继续交给 ocr_engine。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import os
import re
import zipfile
import xml.etree.ElementTree as ET

from ocr_engine import OCR_EXTENSIONS

#: 正文最多读取的字节数。达到上限立即停止读取。
MAX_TEXT_BYTES = 4096
#: 每次从文件读取的块大小，保证峰值内存只有上限加一个块。
CHUNK_BYTES = 1024

TEXT_EXTENSIONS = {".txt", ".md"}
OOXML_EXTENSIONS = {".docx", ".pptx", ".xlsx"}

#: OOXML 解压后最多读取的字节数，避免压缩包展开后占满内存。
OOXML_READ_BYTES = MAX_TEXT_BYTES * 64

#: 有明确原因、当前阶段不读取正文的格式。
UNSUPPORTED_REASONS = {
    ".pdf": "当前版本没有 PDF 解析库，未读取正文",
    ".doc": "当前版本没有 Word 解析库，未读取正文",
    ".xls": "当前版本没有表格解析库，未读取正文",
    ".ppt": "当前版本没有演示文稿解析库，未读取正文",
    ".zip": "压缩包不读取正文，避免把打包内容当作文本",
    ".rar": "压缩包不读取正文，避免把打包内容当作文本",
    ".7z": "压缩包不读取正文，避免把打包内容当作文本",
}


@dataclass
class ContentExtraction:
    """一次正文提取的结果。

    texts 和 errors 的键都是相对路径；kinds 记录每个文件实际走了哪条路径，
    供预览说明「正文 / 图片文字 / 未支持」使用。
    """

    texts: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False


def is_text_candidate(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in TEXT_EXTENSIONS


def is_image_candidate(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in OCR_EXTENSIONS


def unsupported_reason(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in UNSUPPORTED_REASONS:
        return f"未支持：{UNSUPPORTED_REASONS[ext]}"
    if not ext:
        return "未支持：没有扩展名，不能判断是否为纯文本"
    return f"未支持：当前版本不解析 {ext} 正文"


def read_text_excerpt(path: str, limit: int = MAX_TEXT_BYTES):
    """流式读取纯文本，最多 limit 字节。

    返回 (文本, 解码说明, 是否达到上限)。解码失败使用替换字符并返回说明；
    文件打不开时抛 OSError，由调用方记录原因。
    """
    buf = bytearray()
    with open(path, "rb") as handle:
        while len(buf) < limit:
            need = min(CHUNK_BYTES, limit - len(buf))
            chunk = handle.read(need)
            if not chunk:
                break
            buf.extend(chunk)
    raw = bytes(buf)
    reached_limit = len(raw) >= limit
    decode_note = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        text = raw.decode("utf-8", errors="replace")
        decode_note = f"第 {exc.start} 字节起不是有效 UTF-8，已用替换字符"
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text, decode_note, reached_limit


class OfficeReadError(Exception):
    """OOXML 文件无法读取正文，原因只写短说明，不带绝对路径。"""


def _local_tag(node) -> str:
    tag = node.tag or ""
    return tag.rsplit("}", 1)[-1]


def _read_zip_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise OfficeReadError(f"缺少正文部件 {name}")
    if info.file_size > limit * 16:
        # 仍然只读上限，避免压缩包展开后占满内存；读到上限就交给截断说明。
        pass
    try:
        with zf.open(name) as handle:
            return handle.read(limit)
    except RuntimeError as exc:
        if "encrypted" in str(exc).lower():
            raise OfficeReadError("文件已加密，未读取正文")
        raise OfficeReadError(str(exc))


def _parse_xml(data: bytes):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise OfficeReadError("正文 XML 损坏，无法解析")


def _join_text_nodes(root, local_name: str) -> str:
    return "".join(node.text or "" for node in root.iter()
                   if _local_tag(node) == local_name)


def _read_docx(zf: zipfile.ZipFile, limit: int) -> str:
    root = _parse_xml(_read_zip_member(zf, "word/document.xml", limit))
    paragraphs = []
    for node in root.iter():
        if _local_tag(node) != "p":
            continue
        text = "".join(child.text or "" for child in node.iter()
                       if _local_tag(child) == "t")
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _read_pptx(zf: zipfile.ZipFile, limit: int) -> str:
    slides = []
    for name in zf.namelist():
        match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
        if match:
            slides.append((int(match.group(1)), name))
    slides.sort()
    parts = []
    for _index, name in slides:
        root = _parse_xml(_read_zip_member(zf, name, limit))
        text = _join_text_nodes(root, "t")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _read_xlsx(zf: zipfile.ZipFile, limit: int) -> str:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = _parse_xml(_read_zip_member(zf, "xl/sharedStrings.xml", limit))
        for node in root.iter():
            if _local_tag(node) != "si":
                continue
            shared.append("".join(child.text or ""
                                  for child in node.iter()
                                  if _local_tag(child) == "t"))
    sheets = []
    for name in zf.namelist():
        match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", name)
        if match:
            sheets.append((int(match.group(1)), name))
    sheets.sort()
    parts = []
    for _index, name in sheets:
        root = _parse_xml(_read_zip_member(zf, name, limit))
        cells = []
        for node in root.iter():
            if _local_tag(node) != "c":
                continue
            inline = "".join(child.text or "" for child in node.iter()
                             if _local_tag(child) == "t")
            if inline:
                cells.append(inline)
                continue
            for child in node:
                if _local_tag(child) != "v" or not child.text:
                    continue
                value = child.text.strip()
                if value.isdigit() and int(value) < len(shared):
                    cells.append(shared[int(value)])
                else:
                    cells.append(value)
                break
        if cells:
            parts.append("\t".join(cells))
    return "\n".join(parts)


def read_ooxml_excerpt(path: str, ext: str,
                       limit: int = MAX_TEXT_BYTES) -> tuple[str, str]:
    """读取 .docx/.pptx/.xlsx 的正文，返回 (文本, 截断说明)。

    只读标准库能解开的 OOXML；损坏、加密、缺少正文部件时抛
    OfficeReadError，文件打不开时抛 OSError，由调用方记录原因。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            if ext == ".docx":
                text = _read_docx(zf, OOXML_READ_BYTES)
            elif ext == ".pptx":
                text = _read_pptx(zf, OOXML_READ_BYTES)
            else:
                text = _read_xlsx(zf, OOXML_READ_BYTES)
    except zipfile.BadZipFile:
        raise OfficeReadError("不是有效的 Office 压缩包或文件已损坏")
    except OfficeReadError:
        raise
    except OSError:
        raise
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    note = ""
    if len(text) > limit:
        text = text[:limit]
        note = f"正文较长：只读取了前 {limit} 个字符"
    return text, note


def _is_within_root(path: str, root: str) -> bool:
    """解析符号链接后，path 的真实路径是否仍在 root 的真实路径之内。

    与 executor._is_within_root 保持同一套 realpath 规则：整理空间本身
    若是符号链接，仍以 realpath(root) 为基准，只拦截从空间内部逃到空间外
    的路径（含文件或中间目录被换成空间外链接的情况）。
    """
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    root_key = os.path.normcase(real_root)
    path_key = os.path.normcase(real_path)
    return path_key == root_key or path_key.startswith(root_key + os.sep)


def _derive_root(files) -> str | None:
    """从文件条目的 abs_path / rel_path 反推扫描根目录。"""
    for entry in files:
        ap = getattr(entry, "abs_path", None)
        rp = getattr(entry, "rel_path", None)
        if not ap or not rp:
            continue
        parts = rp.split(os.sep)
        root = ap
        for _ in parts:
            root = os.path.dirname(root)
        if root:
            return root
    return None


def _short_error(exc: OSError) -> str:
    """只返回 strerror 这类短原因，绝不把绝对路径写进错误信息。"""
    reason = getattr(exc, "strerror", None)
    if reason:
        return reason
    errno = getattr(exc, "errno", None)
    if errno is not None:
        try:
            return os.strerror(errno)
        except (OSError, ValueError):
            pass
    return type(exc).__name__


def extract_contents(files, cancel_event=None, progress_cb=None,
                     root: str | None = None) -> ContentExtraction:
    """按文件提取正文；单个文件失败不影响其余文件。

    取消时不抛异常，返回已处理的部分，并置 cancelled=True。调用方应丢弃
    这次结果，不把半成品计划交给界面。

    root 是整理空间根目录；若未提供则从文件条目反推。读取每个正文文件前，
    会用与 executor 相同的 realpath 规则核对真实路径仍在整理空间内，
    防止扫描后文件被换成指向空间外的符号链接而泄露外部字节。
    """
    result = ContentExtraction()
    if root is None:
        root = _derive_root(files)
    candidates = [entry for entry in files if os.path.isfile(entry.abs_path)]
    total = len(candidates)
    for index, entry in enumerate(candidates, start=1):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        if progress_cb is not None:
            progress_cb(index, total, f"读取正文 {entry.filename}")

        name = entry.filename or os.path.basename(entry.abs_path)
        ext = os.path.splitext(name)[1].lower()
        if ext in TEXT_EXTENSIONS:
            # 真正 open 之前重新核对真实路径：扫描后文件或中间目录可能被
            # 换成指向空间外的链接，此时不打开、不读取外部字节。
            if root is not None and not _is_within_root(entry.abs_path, root):
                result.errors[entry.rel_path] = "无法读取正文：越出整理空间"
                result.kinds[entry.rel_path] = "读取失败"
                continue
            try:
                text, decode_note, reached = read_text_excerpt(entry.abs_path)
            except OSError as exc:
                # 只写 strerror 短原因，不把本机绝对路径暴露给界面和模型。
                result.errors[entry.rel_path] = f"无法读取正文：{_short_error(exc)}"
                result.kinds[entry.rel_path] = "读取失败"
                continue
            if decode_note:
                result.errors[entry.rel_path] = (
                    f"正文解码不完整：{decode_note}"
                    + ("；只读取了前 4096 字节" if reached else "")
                )
            else:
                result.kinds[entry.rel_path] = "正文"
                if reached:
                    result.errors[entry.rel_path] = (
                        "正文较长：只读取了前 4096 字节")
            result.texts[entry.rel_path] = text
            continue

        if ext in OOXML_EXTENSIONS:
            if root is not None and not _is_within_root(entry.abs_path, root):
                result.errors[entry.rel_path] = "无法读取正文：越出整理空间"
                result.kinds[entry.rel_path] = "读取失败"
                continue
            try:
                text, note = read_ooxml_excerpt(entry.abs_path, ext)
            except OfficeReadError as exc:
                result.errors[entry.rel_path] = f"无法读取正文：{exc}"
                result.kinds[entry.rel_path] = "读取失败"
                continue
            except OSError as exc:
                result.errors[entry.rel_path] = f"无法读取正文：{_short_error(exc)}"
                result.kinds[entry.rel_path] = "读取失败"
                continue
            result.kinds[entry.rel_path] = "正文"
            if note:
                result.errors[entry.rel_path] = note
            if not text:
                result.errors[entry.rel_path] = (
                    result.errors.get(entry.rel_path)
                    or "正文为空或没有可读取的文字")
            result.texts[entry.rel_path] = text
            continue

        if ext in OCR_EXTENSIONS:
            # 图片由 ocr_engine.collect_image_text 统一识别，这里不另写识别。
            result.kinds[entry.rel_path] = "图片"
            continue

        if entry.size == 0:
            result.kinds[entry.rel_path] = "空文件"
            continue

        result.errors[entry.rel_path] = unsupported_reason(name)
        result.kinds[entry.rel_path] = "未支持"

    return result
