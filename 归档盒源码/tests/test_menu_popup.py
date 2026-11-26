"""更多工具下拉层贴着按钮，并且不画出屏幕。"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from main import menu_popup_origin
from fit_combo import combo_popup_width


class MenuPopupOriginTests(unittest.TestCase):
    def test_opens_under_the_button_and_matches_its_width(self):
        x, y, width = menu_popup_origin(
            8, 695, 271, 32,
            143, 168,
            0, 0, 1470, 956,
        )
        self.assertEqual((x, y, width), (8, 727, 271))

    def test_opens_above_when_the_button_sits_on_the_screen_bottom(self):
        x, y, width = menu_popup_origin(
            8, 917, 271, 19,
            143, 168,
            0, 0, 1470, 956,
        )
        self.assertEqual(x, 8)
        self.assertEqual(width, 271)
        self.assertEqual(y, 917 - 168)

    def test_shifts_left_when_the_menu_would_leave_the_screen(self):
        x, y, width = menu_popup_origin(
            1400, 100, 120, 32,
            200, 80,
            0, 0, 1470, 956,
        )
        self.assertEqual(width, 200)
        self.assertEqual(x, 1470 - 200)
        self.assertEqual(y, 132)


class ComboPopupWidthTests(unittest.TestCase):
    def test_popup_is_at_least_as_wide_as_the_control(self):
        self.assertEqual(combo_popup_width(271, [80, 140]), 271)

    def test_popup_grows_to_fit_a_longer_item(self):
        self.assertEqual(combo_popup_width(103, [49, 220]), 256)

    def test_popup_does_not_exceed_the_screen(self):
        self.assertEqual(combo_popup_width(400, [900], screen_width=640), 640)
