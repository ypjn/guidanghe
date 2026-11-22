"""
归档盒 — 产品级偏好（应用数据目录 JSON）

与分类配置、AI Key 分开。当前只持久化「文件夹监视」开关：
默认关闭；打开后也不会在本版本真正挂监视，仅记住用户意愿，
避免未实现的常驻扫描被误开。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from scheme_store import app_data_dir

PREFS_FILENAME = "product_prefs.json"
PREFS_VERSION = 1


@dataclass
class ProductPrefs:
    version: int = PREFS_VERSION
    # 用户是否愿意启用文件夹监视（真正监视器尚未接入；默认关）
    folder_watch_enabled: bool = False
    folder_watch_note: str = (
        "监视默认关闭。打开后下一版本才会真正监听；"
        "当前仅保存开关，不会后台扫描磁盘。"
    )


def prefs_path() -> str:
    return os.path.join(app_data_dir(), PREFS_FILENAME)


def default_prefs() -> ProductPrefs:
    return ProductPrefs()


def load_prefs() -> ProductPrefs:
    path = prefs_path()
    if not os.path.isfile(path):
        return default_prefs()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return default_prefs()
    if not isinstance(data, dict):
        return default_prefs()
    return ProductPrefs(
        version=int(data.get("version") or PREFS_VERSION),
        folder_watch_enabled=bool(data.get("folder_watch_enabled", False)),
        folder_watch_note=str(
            data.get("folder_watch_note")
            or default_prefs().folder_watch_note),
    )


def save_prefs(prefs: ProductPrefs) -> None:
    path = prefs_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload: dict[str, Any] = asdict(prefs)
    payload["version"] = PREFS_VERSION
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def set_folder_watch_enabled(enabled: bool) -> ProductPrefs:
    prefs = load_prefs()
    prefs.folder_watch_enabled = bool(enabled)
    save_prefs(prefs)
    return prefs
