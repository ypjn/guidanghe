"""
左上角文件夹树：按磁盘列出本机存储，展开时再读取下一级目录。

不在展开或单击时把目录设为整理空间，因此不会往浏览路径里创建 .organizer。
双击或回车才发出 folderActivated。
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QFrame, QStyle, QTreeWidget, QTreeWidgetItem

from intake_model import is_within, normalize_path

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_LOADED = Qt.ItemDataRole.UserRole + 2

# 这些名字会出现在 /Volumes，但访达侧栏不把它们当成用户磁盘。
_SKIP_VOLUMES = {"Recovery", "VM", "Preboot", "Update"}


@dataclass(frozen=True)
class Place:
    name: str
    path: str
    kind: str  # home / drive


def list_child_dirs(path: str) -> list[tuple[str, str]]:
    """只返回这一层的可见子目录，不递归。"""
    children = []
    hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
    with os.scandir(path) as it:
        for entry in it:
            name = entry.name
            if not name or name.startswith("."):
                continue
            try:
                if hidden and sys.platform == "win32":
                    attrs = entry.stat(follow_symlinks=False).st_file_attributes
                    if attrs & hidden:
                        continue
                if not entry.is_dir(follow_symlinks=True):
                    continue
            except OSError:
                continue
            children.append((name, entry.path))
    children.sort(key=lambda row: row[0].casefold())
    return children


def posix_drives(root: str = "/", volumes: str = "/Volumes") -> list[Place]:
    """macOS / Linux：启动盘在前，其余挂载卷跟在后面。"""
    places = []
    seen = set()
    root_name = "本机磁盘"
    if os.path.isdir(volumes):
        try:
            names = os.listdir(volumes)
        except OSError:
            names = []
        for name in sorted(names, key=str.casefold):
            if not name or name.startswith(".") or name in _SKIP_VOLUMES:
                continue
            path = os.path.join(volumes, name)
            try:
                if not os.path.isdir(path):
                    continue
                real = os.path.realpath(path)
            except OSError:
                continue
            key = os.path.normcase(real)
            if key == os.path.normcase(os.path.realpath(root)):
                root_name = name
                continue
            if key in seen:
                continue
            seen.add(key)
            places.append(Place(name, real, "drive"))
    if os.path.isdir(root):
        real_root = os.path.realpath(root)
        key = os.path.normcase(real_root)
        if key not in seen:
            places.insert(0, Place(root_name, real_root, "drive"))
    return places


def windows_drives() -> list[Place]:
    places = []
    try:
        from PySide6.QtCore import QDir
        infos = QDir.drives()
    except Exception:
        return places
    for info in infos:
        path = info.absoluteFilePath()
        if not path:
            continue
        letter = path.rstrip("\\/").upper() or path
        places.append(Place(_windows_drive_name(path, letter), path, "drive"))
    return places


def _windows_drive_name(path: str, letter: str) -> str:
    try:
        import ctypes
        root = path if path.endswith("\\") else path + "\\"
        buf = ctypes.create_unicode_buffer(256)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, buf, 256, None, None, None, None, 0)
        label = buf.value.strip()
        if ok and label:
            return f"{label} ({letter})"
    except Exception:
        pass
    return f"本地磁盘 ({letter})"


def list_places() -> list[Place]:
    if sys.platform == "win32":
        places = windows_drives()
    else:
        places = posix_drives()
    home = os.path.expanduser("~")
    try:
        home_ok = bool(home) and os.path.isdir(home)
        home_real = os.path.realpath(home) if home_ok else ""
    except OSError:
        home_ok = False
        home_real = ""
    if home_ok and not any(
            os.path.normcase(os.path.realpath(p.path)) == os.path.normcase(home_real)
            for p in places):
        name = os.path.basename(home_real) or "主文件夹"
        places.append(Place(name, home_real, "home"))
    return places


class StorageTree(QTreeWidget):
    """磁盘和文件夹树。单击只提示路径，双击或回车才选定整理空间。"""

    folderHighlighted = Signal(str)
    folderActivated = Signal(str)

    def __init__(self, places: list[Place] | None = None, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setExpandsOnDoubleClick(False)
        self.setAnimated(False)
        self.setUniformRowHeights(True)
        self.setIndentation(16)
        self.setIconSize(QSize(16, 16))
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(240)
        self.setMaximumHeight(280)
        self.setAccessibleName("计算机文件夹")
        self.setAccessibleDescription("展开磁盘和文件夹。双击把文件夹设为整理空间。")
        self.setToolTip("单击查看路径。双击把这个文件夹设为这次要整理的位置。")
        self.setStyleSheet(
            "QTreeWidget { background: #F8FAFC; border: 1px solid #E2E8F0;"
            " border-radius: 6px; color: #0F172A; outline: none; }"
            "QTreeWidget:focus { border: 1px solid #2563EB; }"
            "QTreeWidget::item { padding: 4px 2px; }"
            "QTreeWidget::item:selected { background: #EFF6FF; color: #1D4ED8; }"
        )
        style = self.style()
        self._icon_drive = style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        self._icon_home = style.standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon)
        self._icon_dir = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.itemExpanded.connect(self._on_expanded)
        self.itemClicked.connect(self._on_clicked)
        self.itemDoubleClicked.connect(self._on_double_clicked)
        self._build(places if places is not None else list_places())

    def _build(self, places: list[Place]):
        self.clear()
        home_item = None
        for place in places:
            if not place.path:
                continue
            item = self._make_item(place.name, place.path, place.kind)
            self.addTopLevelItem(item)
            if place.kind == "home":
                home_item = item
        if home_item is not None:
            self.expandItem(home_item)

    def reveal(self, path: str):
        """展开到当前整理空间并选中它，不触发「设为整理空间」。"""
        if not path:
            return
        try:
            if not os.path.isdir(path):
                return
        except OSError:
            return
        best = None
        best_len = -1
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            root = item.data(0, ROLE_PATH)
            if not root or not is_within(root, path):
                continue
            length = len(normalize_path(root))
            if length > best_len:
                best = item
                best_len = length
        if best is None:
            return
        target = os.path.normcase(normalize_path(path))
        current = best
        seen = set()
        while True:
            current_path = current.data(0, ROLE_PATH)
            cursor = os.path.normcase(normalize_path(current_path))
            if cursor == target or cursor in seen:
                break
            seen.add(cursor)
            self._load(current)
            current.setExpanded(True)
            found = None
            for index in range(current.childCount()):
                child = current.child(index)
                child_path = child.data(0, ROLE_PATH)
                if child_path and is_within(child_path, path):
                    found = child
                    break
            if found is None:
                break
            current = found
        self.setCurrentItem(current)
        self.scrollToItem(current)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None:
                self._emit_activated(item)
                return
        super().keyPressEvent(event)

    def _make_item(self, name: str, path: str, kind: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name])
        item.setData(0, ROLE_PATH, path)
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_LOADED, False)
        item.setToolTip(0, path)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if kind == "home":
            item.setIcon(0, self._icon_home)
        elif kind == "drive":
            item.setIcon(0, self._icon_drive)
        else:
            item.setIcon(0, self._icon_dir)
        return item

    def _on_expanded(self, item: QTreeWidgetItem):
        self._load(item)

    def _on_clicked(self, item: QTreeWidgetItem, _column: int):
        path = item.data(0, ROLE_PATH)
        if path:
            self.folderHighlighted.emit(path)

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int):
        self._emit_activated(item)

    def _emit_activated(self, item: QTreeWidgetItem):
        path = item.data(0, ROLE_PATH)
        if path:
            self.folderActivated.emit(path)

    def _load(self, item: QTreeWidgetItem):
        if item.data(0, ROLE_LOADED):
            return
        item.setData(0, ROLE_LOADED, True)
        path = item.data(0, ROLE_PATH)
        try:
            children = list_child_dirs(path)
        except OSError as exc:
            item.setToolTip(0, f"无法读取：{path}\n{exc}")
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
            return
        if not children:
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
            return
        for name, child_path in children:
            item.addChild(self._make_item(name, child_path, "folder"))
