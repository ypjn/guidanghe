"""生成归档盒桌面图标，并写成 macOS .icns。

在仓库根目录执行：
  "$ARCHIVEBOX_BUILD_PYTHON" 打包/make_icon.py
需要本机的 iconutil。没有 PySide6 时直接失败，不生成空图标。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication


def _draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.08
    radius = size * 0.22
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0F766E"))
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
        radius, radius)

    box = QRectF(size * 0.24, size * 0.30, size * 0.52, size * 0.40)
    painter.setBrush(QColor("#F8FAFC"))
    painter.drawRoundedRect(box, size * 0.04, size * 0.04)
    tab = QRectF(size * 0.24, size * 0.22, size * 0.24, size * 0.12)
    painter.drawRoundedRect(tab, size * 0.03, size * 0.03)

    painter.setPen(QPen(QColor("#F59E0B"), max(2.0, size * 0.035)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    y = size * 0.46
    for _ in range(3):
        painter.drawLine(
            int(size * 0.32), int(y), int(size * 0.68), int(y))
        y += size * 0.08
    painter.end()
    return image


def main() -> int:
    QApplication.instance() or QApplication([])
    root = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(root, "assets")
    os.makedirs(assets, exist_ok=True)
    master = _draw(1024)
    png_path = os.path.join(assets, "归档盒-1024.png")
    if not master.save(png_path, "PNG"):
        print("无法写入 PNG", file=sys.stderr)
        return 1
    sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    with tempfile.TemporaryDirectory(prefix="archivebox-icon-") as tmp:
        iconset = os.path.join(tmp, "ArchiveBox.iconset")
        os.makedirs(iconset)
        for name, side in sizes.items():
            scaled = master.scaled(
                side, side,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            if not scaled.save(os.path.join(iconset, name), "PNG"):
                print(f"无法写入 {name}", file=sys.stderr)
                return 1
        icns = os.path.join(assets, "归档盒.icns")
        subprocess.check_call(["iconutil", "-c", "icns", iconset, "-o", icns])
    print(icns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
