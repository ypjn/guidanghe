"""窗口顶部的操作反馈。

原来各窗口把结果写在浅灰色小字或底部状态栏里，加载时往往只有一句
「正在…」。这里用色块把同一句话放到窗口上方，加载期间显示进度条。
不抢焦点；下一次操作会换掉上一条，结果会留着直到那时。
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from ui_theme import AMBER, AMBER_BG, RIBBON, RIBBON_SOFT, STAMP, STAMP_SOFT

KIND_INFO = "info"
KIND_OK = "ok"
KIND_WARN = "warn"
KIND_ERROR = "error"
KIND_BUSY = "busy"

_TAG_RE = re.compile(r"<[^>]+>")
_PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

_PALETTE = {
    KIND_INFO: (STAMP_SOFT, "#16324F", STAMP),
    KIND_OK: ("#E5F4EA", "#14532D", "#166534"),
    KIND_WARN: (AMBER_BG, "#7C2D12", AMBER),
    KIND_ERROR: (RIBBON_SOFT, "#7F1D1D", RIBBON),
    KIND_BUSY: (STAMP_SOFT, "#16324F", STAMP),
}


def plain(text: str) -> str:
    raw = _TAG_RE.sub(" ", text or "")
    raw = (
        raw.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )
    return re.sub(r"\s+", " ", raw).strip()


def classify(text: str) -> str:
    """按这句话该怎么被看见来分色。加载中优先于「已…」。"""
    raw = plain(text)
    if not raw:
        return KIND_INFO
    if any(word in raw for word in ("失败", "错误", "无法", "不能", "没能", "打不开", "不可用")):
        return KIND_ERROR
    busy = any(
        word in raw
        for word in ("正在", "进度", "加载", "扫描中", "试运行中", "取消中", "等待中")
    ) or raw.endswith(("……", "...", "…"))
    if busy and "已取消" not in raw:
        return KIND_BUSY
    succeeded = (
        "识别结果" in raw
        or raw.startswith("已")
        or any(word in raw for word in ("完成", "成功", "可恢复"))
    )
    if succeeded:
        if any(word in raw for word in ("已取消", "失效", "不可恢复", "未完成", "作废")):
            return KIND_WARN
        return KIND_OK
    if any(
        word in raw
        for word in ("已取消", "请先", "尚未", "没有", "作废", "不完整", "不一致", "失效", "注意", "未完成", "离线")
    ):
        return KIND_WARN
    return KIND_INFO


def _pair(text: str):
    found = None
    for match in _PAIR_RE.finditer(plain(text)):
        found = (int(match.group(1)), int(match.group(2)))
    return found


class OperationFeedback(QWidget):
    """一条可停留的说明，加载时带上进度条。"""

    def __init__(self, parent=None, *, show_bar: bool = True):
        super().__init__(parent)
        self.setObjectName("operationFeedback")
        self.setAccessibleName("操作反馈")
        self._show_bar = show_bar
        self._kind = KIND_INFO
        self._determinate = False
        self._source = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        self.message = QLabel("")
        self.message.setObjectName("operationFeedbackMessage")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message.setAccessibleName("操作反馈")
        self.bar = QProgressBar()
        self.bar.setObjectName("operationFeedbackBar")
        self.bar.setAccessibleName("加载进度")
        self.bar.setFixedHeight(16)
        self.bar.setTextVisible(False)
        self.bar.hide()
        layout.addWidget(self.message)
        layout.addWidget(self.bar)
        self._chrome(KIND_INFO)
        self.hide()

    def reflect(self, text: str) -> None:
        """直接显示一句反馈。空字符串收起。"""
        self._source = None
        self._present(text)

    def follow(self, label: QLabel, *, hide_source: bool = True) -> None:
        """之后这个标签的 setText 同步到顶部反馈。"""
        original = label.setText

        def wrapped(text, *args, **kwargs):
            original(text, *args, **kwargs)
            shown = "" if text is None else str(text)
            if hide_source and plain(shown):
                label.hide()
            if not plain(shown):
                if self._source is label:
                    self.dismiss()
                return
            self._source = label
            self._present(shown)

        label.setText = wrapped

    def set_progress(self, current: int, total: int) -> None:
        """主窗口已有任务进度、但说明文字里没有「当前/总数」时使用。"""
        if not self._show_bar:
            return
        if not plain(self.message.text()):
            self._present("正在处理……")
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        if total > 0:
            self._determinate = True
            self.bar.setRange(0, total)
            self.bar.setValue(min(current, total))
            self.bar.setTextVisible(True)
            self.bar.setFormat("%v / %m")
            self.bar.setAccessibleDescription(f"已完成 {current} / {total}")
        else:
            self._determinate = False
            self.bar.setRange(0, 0)
            self.bar.setTextVisible(False)
            self.bar.setAccessibleDescription("正在加载")
        self.bar.show()
        self.show()

    def clear_progress(self) -> None:
        """任务结束后收起进度条，说明文字先留着。"""
        self._determinate = False
        self.bar.hide()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        if self._kind == KIND_BUSY:
            self._chrome(KIND_INFO)
            self._kind = KIND_INFO

    def dismiss(self) -> None:
        self.clear_progress()
        self._kind = KIND_INFO
        self.message.setText("")
        self.hide()

    def _present(self, text: str) -> None:
        shown = plain(text)
        if not shown:
            self.dismiss()
            return
        kind = classify(shown)
        self._kind = kind
        self._chrome(kind)
        self.message.setText(shown)
        self._sync_bar(kind, shown)
        self.show()

    def _sync_bar(self, kind: str, text: str) -> None:
        if not self._show_bar or kind != KIND_BUSY:
            self._determinate = False
            self.bar.hide()
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            return
        pair = _pair(text)
        if pair is not None and pair[1] > 0:
            current, total = pair
            self._determinate = True
            self.bar.setRange(0, total)
            self.bar.setValue(min(max(0, current), total))
            self.bar.setTextVisible(True)
            self.bar.setFormat("%v / %m")
            self.bar.setAccessibleDescription(f"已完成 {current} / {total}")
            self.bar.show()
            return
        if not self._determinate:
            self.bar.setRange(0, 0)
            self.bar.setTextVisible(False)
            self.bar.setAccessibleDescription("正在加载")
            self.bar.show()

    def _chrome(self, kind: str) -> None:
        background, foreground, accent = _PALETTE.get(kind, _PALETTE[KIND_INFO])
        self.setStyleSheet(
            "QWidget#operationFeedback {"
            f"background:{background};"
            f"border:1px solid {accent};"
            f"border-left:4px solid {accent};"
            "border-radius:2px;"
            "}"
            "QLabel#operationFeedbackMessage {"
            f"color:{foreground};"
            "background:transparent;"
            "font-size:14px;"
            "font-weight:600;"
            "}"
            "QProgressBar#operationFeedbackBar {"
            "border:none;"
            "border-radius:2px;"
            "background:rgba(22,27,34,0.10);"
            "min-height:16px;"
            "max-height:16px;"
            "text-align:center;"
            f"color:{foreground};"
            "font-size:11px;"
            "}"
            "QProgressBar#operationFeedbackBar::chunk {"
            f"background:{accent};"
            "border-radius:2px;"
            "}"
        )


def install(layout, *labels: QLabel, index: int = 0,
            hide_source: bool = True, show_bar: bool = True) -> OperationFeedback:
    """插到布局顶部，并跟随给出的状态标签。"""
    host = layout.parentWidget()
    feedback = OperationFeedback(host, show_bar=show_bar)
    layout.insertWidget(index, feedback)
    for label in labels:
        feedback.follow(label, hide_source=hide_source)
    return feedback
