"""读取待整理文件的正文片段，供分类规则和预览依据使用。

阶段 1 只读取纯文本（.txt/.md）：按块读到上限即停，不把整个文件读进内存。
图片继续交给 ocr_engine；PDF、Word 和其余格式当前没有解析库，明确标成
「未支持」并继续处理后面的文件。本模块不新增任何解析依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import os

from ocr_engine import OCR_EXTENSIONS

#: 正文最多读取的字节数。达到上限立即停止读取。
MAX_TEXT_BYTES = 4096
#: 每次从文件读取的块大小，保证峰值内存只有上限加一个块。
CHUNK_BYTES = 1024

TEXT_EXTENSIONS = {".txt", ".md"}

#: 有明确原因、当前阶段不读取正文的格式。
UNSUPPORTED_REASONS = {
    ".pdf": "当前版本没有 PDF 解析库，未读取正文",
    ".doc": "当前版本没有 Word 解析库，未读取正文",
    ".docx": "当前版本没有 Word 解析库，未读取正文",
    ".xls": "当前版本没有表格解析库，未读取正文",
    ".xlsx": "当前版本没有表格解析库，未读取正文",
    ".ppt": "当前版本没有演示文稿解析库，未读取正文",
    ".pptx": "当前版本没有演示文稿解析库，未读取正文",
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


def extract_contents(files, cancel_event=None, progress_cb=None) -> ContentExtraction:
    """按文件提取正文；单个文件失败不影响其余文件。

    取消时不抛异常，返回已处理的部分，并置 cancelled=True。调用方应丢弃
    这次结果，不把半成品计划交给界面。
    """
    result = ContentExtraction()
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
            try:
                text, decode_note, reached = read_text_excerpt(entry.abs_path)
            except OSError as exc:
                result.errors[entry.rel_path] = f"无法读取正文：{exc}"
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
