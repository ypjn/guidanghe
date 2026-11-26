"""下拉框弹出层至少和控件一样宽，条目更长时再加宽。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox

POPUP_TEXT_PAD = 36


def combo_popup_width(
    box_width: int,
    text_widths: list[int],
    screen_width: int = 0,
    pad: int = POPUP_TEXT_PAD,
) -> int:
    """弹出层宽度：不窄于控件，也不窄于最长一条文字。"""
    width = max(int(box_width), 0)
    for text_w in text_widths:
        width = max(width, int(text_w) + pad)
    if screen_width > 0:
        width = min(width, int(screen_width))
    return width


class FitComboBox(QComboBox):
    def showPopup(self):
        view = self.view()
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        fm = view.fontMetrics()
        text_widths = [
            fm.horizontalAdvance(self.itemText(i)) for i in range(self.count())
        ]
        screen = self.screen().availableGeometry().width() if self.screen() else 0
        view.setMinimumWidth(combo_popup_width(self.width(), text_widths, screen))
        super().showPopup()
