"""跨平台路径定位辅助（Windows 适配层）。

提供从 __file__ 动态定位源码根目录、系统临时目录、
测试隔离目录的统一入口，消除 Mac 硬编码路径。

生产代码不 import 此模块——只在测试脚本中使用。
"""
from __future__ import annotations

import os
import sys
import tempfile


SRC_ROOT = os.path.dirname(os.path.abspath(__file__))


def src_root() -> str:
    """归档盒源码根目录（main.py 所在目录）。"""
    return SRC_ROOT


def source_dir_for_import() -> str:
    """返回应该加到 sys.path 的源码目录。"""
    return SRC_ROOT


def temp_base() -> str:
    """系统临时目录下的归档盒专属子目录。"""
    base = os.path.join(tempfile.gettempdir(), "_archivebox_windows_verify")
    return base


def sanitize(name: str) -> str:
    """把路径段中不适合 Windows 目录名的字符替换掉。"""
    return name.replace("/", "_").replace(":", "_")
