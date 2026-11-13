"""Windows 免安装包里，在导入界面库之前把动态库目录登记好。"""
from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        base,
        os.path.join(base, "PySide6"),
        os.path.join(base, "shiboken6"),
        os.path.join(base, "numpy.libs"),
        os.path.join(base, "cv2"),
        os.path.join(base, "cv2.libs"),
        os.path.join(base, "onnxruntime", "capi"),
    ]
    plugin_dirs = [
        os.path.join(base, "PySide6", "plugins"),
        os.path.join(base, "PySide6", "Qt", "plugins"),
        os.path.join(base, "PySide6", "qt-plugins"),
    ]
    path_parts = []
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        path_parts.append(folder)
        add = getattr(os, "add_dll_directory", None)
        if add is not None:
            try:
                add(folder)
            except OSError:
                pass
    if path_parts:
        os.environ["PATH"] = os.pathsep.join(path_parts) + os.pathsep + os.environ.get("PATH", "")
    for folder in plugin_dirs:
        if os.path.isdir(os.path.join(folder, "platforms")):
            os.environ.setdefault("QT_PLUGIN_PATH", folder)
            break
