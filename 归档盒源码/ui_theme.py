"""
归档盒界面主题 — 卷宗纸感，不用通用网页蓝。

主 CTA 用印章红；点缀用卷宗蓝；背景用纸灰。
"""

from __future__ import annotations

# 与官网 website/styles.css 对齐
INK = "#161B22"
MUTE = "#4A5560"
PAPER = "#EEF1F4"
PAPER_DEEP = "#E2E7ED"
WHITE = "#F8FAFC"
LINE = "#C5CCD4"
STAMP = "#245B8C"       # 卷宗蓝
RIBBON = "#C23B22"      # 印章红
RIBBON_HOVER = "#9E2E1A"
STAMP_SOFT = "#E8F0F6"
RIBBON_SOFT = "#FFF7F4"
AMBER = "#B45309"
AMBER_BG = "#FFFBEB"
AMBER_LINE = "#FDE68A"


def app_stylesheet(combo_arrow_url: str) -> str:
    arrow = combo_arrow_url.replace("\\", "/")
    return f"""
        QMainWindow {{ background: {PAPER}; }}
        QTableWidget {{
            background: white;
            color: {INK};
            border: 1px solid {LINE};
            border-radius: 2px;
            gridline-color: {PAPER_DEEP};
            selection-background-color: {STAMP_SOFT};
            selection-color: {INK};
        }}
        QTableWidget::item {{ color: {INK}; }}
        QTableWidget::item:selected {{
            background: {STAMP_SOFT};
            color: {INK};
        }}
        QHeaderView::section {{
            background: {PAPER_DEEP};
            padding: 6px 8px;
            border: none;
            border-bottom: 1px solid {LINE};
            font-weight: 650;
            color: {MUTE};
        }}
        QPushButton {{
            padding: 6px 12px;
            min-height: 28px;
            background: white;
            color: {INK};
            border: 1px solid {LINE};
            border-radius: 2px;
        }}
        QPushButton:hover {{ background: {PAPER_DEEP}; }}
        QPushButton:disabled {{ color: #6B7280; background: {PAPER_DEEP}; }}
        QPushButton[role="primary"] {{
            background: {RIBBON};
            color: #FFF8F6;
            border: 1px solid {RIBBON};
            font-weight: 650;
        }}
        QPushButton[role="primary"]:hover {{ background: {RIBBON_HOVER}; }}
        QPushButton[role="primary"]:disabled {{
            background: #D6DEE6;
            color: #6B7280;
            border-color: #D6DEE6;
        }}
        QPushButton[role="nav"], QToolButton[role="nav"] {{
            text-align: left;
            padding: 6px 10px;
            border: 1px solid {LINE};
            border-radius: 2px;
            background: #FFFFFF;
            color: {INK};
            font-weight: 500;
        }}
        QPushButton[role="nav"]:hover, QToolButton[role="nav"]:hover {{
            background: {PAPER_DEEP};
        }}
        QPushButton[role="nav"]:disabled, QToolButton[role="nav"]:disabled {{
            color: #6B7280; background: {PAPER_DEEP}; border-color: {LINE};
        }}
        QPushButton[role="nav"][current="true"] {{
            background: {STAMP_SOFT};
            color: {STAMP};
            font-weight: 650;
            border-color: {STAMP};
        }}
        QPushButton[role="danger"] {{
            background: white;
            color: {RIBBON};
            border: 1px solid #E8A99A;
        }}
        QPushButton[role="danger"]:hover {{ background: {RIBBON_SOFT}; }}
        QLineEdit {{
            padding: 6px 10px;
            border: 1px solid {LINE};
            border-radius: 2px;
            background: white;
            color: {INK};
        }}
        QComboBox {{
            combobox-popup: 0;
            padding: 4px 22px 4px 8px;
            border: 1px solid {LINE};
            border-radius: 2px;
            background: white;
            color: {INK};
            min-height: 22px;
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 18px;
            border: none;
            background: transparent;
        }}
        QComboBox::down-arrow {{
            image: url("{arrow}");
            width: 8px;
            height: 5px;
        }}
        QComboBox QAbstractItemView {{
            background: white;
            color: {INK};
            border: 1px solid {LINE};
            outline: 0;
            padding: 0px;
            selection-background-color: {STAMP_SOFT};
            selection-color: {INK};
        }}
        QComboBox QAbstractItemView::item {{
            min-height: 26px;
            padding: 4px 10px;
            color: {INK};
            background: white;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background: {PAPER_DEEP};
            color: {INK};
        }}
        QComboBox QAbstractItemView::item:selected {{
            background: {STAMP_SOFT};
            color: {INK};
        }}
        QLineEdit:focus, QComboBox:focus {{ border-color: {STAMP}; }}
        QSplitter#mainSplit::handle {{ background: {LINE}; }}
        QSplitter#mainSplit::handle:hover {{ background: {STAMP}; }}
        QStatusBar {{ background: {PAPER_DEEP}; color: {MUTE}; }}
        QCheckBox, QLabel, QRadioButton {{ color: {INK}; }}
        QCheckBox {{ padding: 4px; spacing: 6px; }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid {MUTE};
            border-radius: 2px;
            background: #FFFFFF;
        }}
        QCheckBox::indicator:checked {{
            background: {STAMP};
            border: 1px solid {STAMP};
        }}
        QListWidget, QTreeView, QTextEdit, QPlainTextEdit {{
            background: #FFFFFF;
            color: {INK};
        }}
        QLabel[role="step"] {{
            color: {STAMP};
            font-size: 11px;
            font-weight: 650;
            padding: 12px 8px 4px 8px;
            letter-spacing: 0.04em;
        }}
    """
