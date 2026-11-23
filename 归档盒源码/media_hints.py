"""
归档盒 — 轻量媒体提示（可选）

只读图片头部里的拍摄设备与日期，给「沾边归类」作软证据。
不装 Pillow / 读失败 / 非图片时一律返回空，不影响主流程。
不做文件夹监视，也不根据 EXIF 自动移动文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


@dataclass(frozen=True)
class CaptureHint:
    device_key: str = ""
    day_bucket: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.device_key or self.day_bucket)


_IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif",
}


def is_image_path(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in _IMAGE_EXT


@lru_cache(maxsize=512)
def capture_hint(path: str) -> CaptureHint:
    """读一张图的设备与拍摄日；失败返回空提示。"""
    if not path or not is_image_path(path):
        return CaptureHint()
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 12 * 1024 * 1024:
            return CaptureHint()
    except OSError:
        return CaptureHint()
    try:
        from PIL import Image  # type: ignore
        from PIL.ExifTags import TAGS  # type: ignore
    except Exception:
        return CaptureHint()
    try:
        with Image.open(path) as img:
            raw = img.getexif()
    except Exception:
        return CaptureHint()
    if not raw:
        return CaptureHint()
    tagged = {}
    for key, value in raw.items():
        name = TAGS.get(key, str(key))
        tagged[name] = value
    make = str(tagged.get("Make") or "").strip()
    model = str(tagged.get("Model") or "").strip()
    device = "|".join(p for p in (make.lower(), model.lower()) if p)
    day = ""
    for field in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        stamp = str(tagged.get(field) or "").strip()
        if len(stamp) >= 10 and stamp[4] in "-:":
            day = stamp[:10].replace(":", "-")
            break
    return CaptureHint(device_key=device, day_bucket=day)


def clear_hint_cache() -> None:
    capture_hint.cache_clear()
