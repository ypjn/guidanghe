"""S1-10 行为验证（单次渲染）：深色调色板下计划表文字是否可见。

用法：python verify_table_contrast.py <app目录> <fixed|bug>
  fixed = 产品当前全局 QSS
  bug   = 去掉 QTableWidget::item 文字颜色（还原旧缺陷）
每次进程只渲染一次，避免 Qt 样式表重复应用时的缓存干扰。
"""
from __future__ import annotations

import os
import re
import sys

APP = sys.argv[1]
MODE = (sys.argv[2] if len(sys.argv) > 2 else "fixed").lower()
sys.path.insert(0, APP)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPalette                     # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem  # noqa: E402

with open(os.path.join(APP, "main.py"), "r", encoding="utf-8") as f:
    src = f.read()
QSS = re.search(r'setStyleSheet\("""(.*?)"""\)', src, re.S).group(1)
if MODE == "bug":
    # 还原旧缺陷：既没有 QTableWidget::item 规则，也没有表格自身的深色 color
    QSS = re.sub(r"QTableWidget::item\s*\{[^}]*\}", "", QSS)
    QSS = re.sub(r"QTableWidget::item:selected\s*\{[^}]*\}", "", QSS)
    QSS = QSS.replace("color: #212121;", "")

app = QApplication([])
app.setStyle("Fusion")
pal = app.palette()
pal.setColor(QPalette.WindowText, QColor("#FFFFFF"))
pal.setColor(QPalette.Text, QColor("#FFFFFF"))
pal.setColor(QPalette.ButtonText, QColor("#FFFFFF"))
pal.setColor(QPalette.Base, QColor("#1E1E1E"))
pal.setColor(QPalette.Window, QColor("#1E1E1E"))
app.setPalette(pal)
app.setStyleSheet(QSS)

tbl = QTableWidget(1, 2)
tbl.horizontalHeader().setVisible(False)
tbl.verticalHeader().setVisible(False)
tbl.setItem(0, 0, QTableWidgetItem("期末复习提纲.pdf"))
tbl.setItem(0, 1, QTableWidgetItem("_已整理/课程/期末复习提纲.pdf"))
tbl.resize(420, 60)
tbl.show()
app.processEvents()
img = tbl.grab().toImage().convertToFormat(QImage.Format_RGB32)

dark = text_like = 0
for y in range(img.height()):
    for x in range(img.width()):
        c = QColor(img.pixel(x, y))
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        if lum < 110:
            dark += 1
        if abs(c.red() - 33) < 16 and abs(c.green() - 33) < 16 and abs(c.blue() - 33) < 16:
            text_like += 1

print(f"MODE={MODE} DARK={dark} TEXTLIKE={text_like} SIZE={img.width()}x{img.height()}")
sys.stdout.flush()
os._exit(0)
