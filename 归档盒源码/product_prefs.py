"""
归档盒 — 产品级偏好（应用数据目录 JSON）

与分类配置、AI Key 分开。当前持久化「文件夹监视」开关：
默认关闭。打开后只在本程序运行、且已选择空间时监听该空间。
新文件不会自动移动。
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
    # 用户是否启用文件夹监视。默认关。只在程序运行且已选择空间时生效。
    folder_watch_enabled: bool = False
    folder_watch_note: str = (
        "监视默认关闭。打开后只在本程序运行、且已选择空间时监听该空间。"
        "新文件不会自动移动。"
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
