"""读取图片里的文字。不上传图片。

按当前系统选择识别组件，前面的起不来就换下一种：

- macOS：系统 Vision
- Windows：系统中文文字识别（已安装中文语言包时）
- 其余情况，包括 Linux：本地 RapidOCR 模型

调用方决定要不要把识别结果送进分类。本模块只负责「这张图里有哪些字」。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading

# 位图。矢量图、动图和图标不读。
OCR_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".heic",
}
MAX_BYTES = 25 * 1024 * 1024
MAX_CHARS = 8000

_WINDOWS_MODULES = (
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage",
    "winrt.windows.globalization",
)

_cache: dict[tuple, str] = {}
_cache_lock = threading.Lock()
_unavailable: set[str] = set()
_rapid_engine = None
_rapid_lock = threading.Lock()


class OcrError(RuntimeError):
    """这张图没法识别，或这台电脑没有文字识别组件。"""


class EngineUnavailable(OcrError):
    """这种识别组件在这台电脑上用不了，可以换下一种。"""


def _spec_exists(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def engine_names() -> list[str]:
    """当前机器上可用的识别组件，按优先顺序。不加载模型。"""
    names: list[str] = []
    if sys.platform == "darwin" and _spec_exists("Vision"):
        names.append("vision")
    if sys.platform == "win32" and all(_spec_exists(name) for name in _WINDOWS_MODULES):
        names.append("windows")
    if _spec_exists("rapidocr") and _spec_exists("onnxruntime"):
        names.append("rapidocr")
    return names


def ocr_supported() -> bool:
    """至少有一种识别组件。不加载模型，避免拖慢启动。"""
    return bool(engine_names())


def engine_label() -> str:
    """界面上说明这台电脑会用哪种识别。"""
    names = engine_names()
    if not names:
        return "没有可用的文字识别组件。"
    bits = []
    if "vision" in names:
        bits.append("Mac 用系统文字识别")
    if "windows" in names:
        bits.append("Windows 优先用系统中文识别")
    if "rapidocr" in names:
        bits.append("其他情况用同一套本地模型，Windows 和 Linux 也能跑")
    text = "。".join(bits) + "。图片不会上传。"
    if "rapidocr" in names:
        text += "第一次使用本地模型时需要联网下载，之后可以离线。"
    return text


def is_ocr_candidate(filename: str, size: int | None = None) -> bool:
    """文件名和大小是否值得送去识别。size 为 None 时不检查大小。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in OCR_EXTENSIONS:
        return False
    if size is None:
        return True
    return 0 < size <= MAX_BYTES


def model_cache_dir() -> str:
    """本地模型放在用户缓存里，不写进源码目录。"""
    override = os.environ.get("ARCHIVEBOX_OCR_MODELS", "").strip()
    if override:
        path = override
    elif sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(root, "归档盒", "ocr-models")
    elif sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Caches/归档盒/ocr-models")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        path = os.path.join(root, "归档盒", "ocr-models")
    os.makedirs(path, exist_ok=True)
    return path


def recognize_image(path: str, engine: str | None = None) -> str:
    """返回图片中的文字。没有文字时返回空字符串。失败抛出 OcrError。

    engine 为空时按 engine_names() 的顺序尝试。某种组件起不来就换下一种；
    组件已经在跑、但这张图读失败时，不再换组件。
    """
    order = [engine] if engine else engine_names()
    if engine and engine not in ("vision", "windows", "rapidocr"):
        raise OcrError(f"未知识别组件：{engine}")
    failures: list[str] = []
    for name in order:
        if engine is None and name in _unavailable:
            continue
        try:
            if name == "vision":
                text = _recognize_vision(path)
            elif name == "windows":
                text = _recognize_windows(path)
            else:
                text = _recognize_rapidocr(path)
            return text[:MAX_CHARS]
        except EngineUnavailable as exc:
            if engine is None:
                _unavailable.add(name)
            failures.append(str(exc))
    detail = "没有可用的文字识别组件。请安装依赖：pip install -r requirements.txt"
    if failures:
        detail += "\n" + "\n".join(failures)
    raise OcrError(detail)


def _recognize_vision(path: str) -> str:
    if sys.platform != "darwin":
        raise EngineUnavailable("系统 Vision 只在 Mac 上可用。")
    try:
        import Vision
        from Foundation import NSURL
    except Exception as exc:
        raise EngineUnavailable("缺少 macOS 文字识别组件。") from exc

    url = NSURL.fileURLWithPath_(os.path.abspath(path))
    if url is None:
        raise OcrError("打不开这张图片。")

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    try:
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    except Exception:
        pass

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        detail = "识别失败"
        if error is not None:
            try:
                detail = str(error.localizedDescription())
            except Exception:
                detail = str(error)
        raise OcrError(detail or "识别失败")

    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        text = str(candidates[0].string() or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _recognize_windows(path: str) -> str:
    if sys.platform != "win32":
        raise EngineUnavailable("Windows 系统文字识别只在 Windows 上可用。")
    try:
        import asyncio

        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import FileAccessMode, StorageFile
    except Exception as exc:
        raise EngineUnavailable("缺少 Windows 文字识别组件。") from exc

    engine = None
    for tag in ("zh-Hans", "zh-CN"):
        language = Language(tag)
        if OcrEngine.is_language_supported(language):
            engine = OcrEngine.try_create_from_language(language)
            if engine is not None:
                break
    if engine is None:
        raise EngineUnavailable("Windows 没有安装中文文字识别，改用本地模型。")

    async def _read() -> str:
        file = await StorageFile.get_file_from_path_async(os.path.abspath(path))
        stream = await file.open_async(FileAccessMode.READ)
        try:
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await engine.recognize_async(bitmap)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return (result.text or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    try:
        return asyncio.run(_read())
    except EngineUnavailable:
        raise
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"Windows 文字识别失败：{exc}") from exc


def _rapid_runner():
    global _rapid_engine
    with _rapid_lock:
        if _rapid_engine is None:
            try:
                from rapidocr import RapidOCR
            except Exception as exc:
                raise EngineUnavailable("缺少本地文字识别模型组件。") from exc
            try:
                _rapid_engine = RapidOCR(params={
                    "Global.model_root_dir": model_cache_dir(),
                    "Global.log_level": "error",
                })
            except Exception as exc:
                raise OcrError(f"本地文字识别模型没有准备好：{exc}") from exc
        return _rapid_engine


def _recognize_rapidocr(path: str) -> str:
    engine = _rapid_runner()
    try:
        result = engine(os.path.abspath(path))
    except Exception as exc:
        message = str(exc)
        if "download" in message.lower() or "url" in message.lower():
            raise OcrError(
                "第一次使用本地模型需要联网下载。下载完成后可以离线识别。\n"
                + message
            ) from exc
        raise OcrError(f"本地模型识别失败：{exc}") from exc
    texts = getattr(result, "txts", None) or ()
    lines = [str(text).strip() for text in texts if str(text).strip()]
    return "\n".join(lines)


def collect_image_text(files, cancel_event=None, progress_cb=None):
    """识别一批扫描结果里的图片。

    返回 (texts, errors)，键都是相对路径。
    texts 的值是识别出的文字，可能是空字符串。
    没有可识别的图片时两个字典都是空的，也不会加载模型。
    用户取消时返回已经识别完的部分。
    """
    candidates = [
        entry for entry in files
        if is_ocr_candidate(entry.filename, entry.size)
        and os.path.isfile(entry.abs_path)
    ]
    texts: dict[str, str] = {}
    errors: dict[str, str] = {}
    if not candidates:
        return texts, errors
    if not ocr_supported():
        raise OcrError(
            "没有可用的文字识别组件。请安装依赖：pip install -r requirements.txt")

    total = len(candidates)
    for index, entry in enumerate(candidates, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb is not None:
            progress_cb(index, total, f"识别图片 {entry.filename}")
        key = (os.path.abspath(entry.abs_path), int(entry.mtime_ns), int(entry.size))
        with _cache_lock:
            cached = _cache.get(key)
        if cached is None:
            try:
                cached = recognize_image(entry.abs_path)
            except OcrError as exc:
                errors[entry.rel_path] = str(exc)
                continue
            with _cache_lock:
                _cache[key] = cached
        texts[entry.rel_path] = cached
    return texts, errors
