#!/usr/bin/env python3
"""用当前系统的 Qt 平台打开主窗口，扫描演示样本并保存窗口画面。

不设置 QT_QPA_PLATFORM。macOS 上应走 cocoa。抓到的是窗口像素，
不是 offscreen 套件里的断言日志。应用数据指向临时目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "归档盒源码"
SHOTS = HERE / "证据" / "截图"
sys.path.insert(0, str(SRC))

os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage3_shot_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage3_shot_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage3_shot_recent_")

from PySide6.QtWidgets import QApplication  # noqa: E402

import 重置演示样本  # noqa: E402
from main import MainWindow  # noqa: E402


def pump(app, seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app, predicate, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    sample = Path(tempfile.mkdtemp(prefix="stage3_shot_sample_"))
    root = 重置演示样本.reset(sample)
    app = QApplication.instance() or QApplication(sys.argv)
    print(f"platform={app.platformName()}")
    window = MainWindow()
    window.resize(1100, 720)
    window.path_edit.setText(str(sample))
    window._reset_for_new_dir(str(sample))
    window.show()
    window.raise_()
    pump(app)
    window.on_scan()
    if not wait_until(app, lambda: (not window._working) and window.scan_result is not None):
        print("扫描没有完成")
        return 1
    window._generate_and_show_plan()
    if not wait_until(
            app, lambda: (not window._plan_working) and window.current_plan is not None):
        print("预览没有完成")
        return 1
    pump(app, 0.8)
    SHOTS.mkdir(parents=True, exist_ok=True)
    image = SHOTS / "01-首次预览.png"
    window.grab().save(str(image))
    rels = [item.file.rel_path for item in window.current_plan.items]
    print(f"saved={image}")
    print(f"plan={rels}")
    print(f"outside={root}")
    if "--hold" in sys.argv:
        pump(app, 25)
    window.close()
    pump(app, 0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
