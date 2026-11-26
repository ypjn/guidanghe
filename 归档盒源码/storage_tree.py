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

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtWidgets import QFrame, QStyle, QTreeWidget, QTreeWidgetItem

from intake_model import is_within, normalize_path

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_LOADED = Qt.ItemDataRole.UserRole + 2
ROLE_LOADING = Qt.ItemDataRole.UserRole + 3

# 这些名字会出现在 /Volumes，但访达侧栏不把它们当成用户磁盘。
_SKIP_VOLUMES = {"Recovery", "VM", "Preboot", "Update"}


@dataclass(frozen=True)
class Place:
    name: str
    path: str
    kind: str  # home / drive


def list_child_dirs(path: str) -> list[tuple[str, str]]:
    """只返回这一层的可见子目录，不递归。

    S4 / misc F-19：不再按 Windows 隐藏属性过滤——用户自己的文件夹
    可能带隐藏属性，在树里凭空消失会让人以为磁盘内容丢了。
    """
    children = []
    with os.scandir(path) as it:
        for entry in it:
            name = entry.name
            if not name or name.startswith("."):
                continue
            try:
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
    """磁盘显示名。S4 / misc F-21：不逐个同步查询离线网络盘/光驱。

    GetDriveTypeW 很快且不访问介质；只有本地固定磁盘才去读卷标。
    """
    try:
        import ctypes
        root = path if path.endswith("\\") else path + "\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        if drive_type == 4:          # DRIVE_REMOTE
            return f"网络驱动器 ({letter})"
        if drive_type == 5:          # DRIVE_CDROM
            return f"光驱 ({letter})"
        if drive_type not in (2, 3, 6):   # 可移动/固定/内存盘之外不查卷标
            return f"本地磁盘 ({letter})"
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


class _TreeLoadWorker(QThread):
    """S4 / misc F-20：目录展开放到后台线程，主线程不再同步 scandir。"""

    loaded = Signal(object, object, str)

    def __init__(self, item: QTreeWidgetItem, path: str, parent=None):
        super().__init__(parent)
        self.item = item
        self.path = path

    def run(self):
        try:
            children = list_child_dirs(self.path)
            error = ""
        except OSError as exc:
            children = []
            error = str(exc)
        self.loaded.emit(self.item, children, error)


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
        self.setToolTip(
            "单击查看路径。双击把资料夹设为整理位置。"
            "磁盘、主目录和系统目录不能选。")
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
        self._loaders: list[_TreeLoadWorker] = []
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
            self._load(current, block=True)
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

    def _load(self, item: QTreeWidgetItem, block: bool = False):
        """加载下一级目录。

        S4 / misc F-20：默认在后台线程读取，主线程不卡顿；
        block=True 仅用于 reveal（明确的路径导航）。失败时不标记已加载，
        折叠后重新展开即可重试。
        """
        if item.data(0, ROLE_LOADED) or item.data(0, ROLE_LOADING):
            return
        path = item.data(0, ROLE_PATH)
        if block:
            try:
                children = list_child_dirs(path)
                error = ""
            except OSError as exc:
                children, error = [], str(exc)
            self._apply_children(item, children, error)
            return
        item.setData(0, ROLE_LOADING, True)
        worker = _TreeLoadWorker(item, path, self)
        worker.loaded.connect(self._on_loaded)
        worker.finished.connect(lambda w=worker: self._release_loader(w))
        self._loaders.append(worker)
        worker.start()

    def _release_loader(self, worker):
        try:
            self._loaders.remove(worker)
        except ValueError:
            pass

    def _on_loaded(self, item, children, error):
        self._apply_children(item, children, error)

    def _apply_children(self, item: QTreeWidgetItem, children, error: str):
        try:
            item.setData(0, ROLE_LOADING, False)
            item.takeChildren()
        except RuntimeError:
            return  # 条目已被清空/窗口已关闭
        path = item.data(0, ROLE_PATH)
        if error:
            # 失败：保持未加载状态，允许再次展开重试
            item.setData(0, ROLE_LOADED, False)
            item.setToolTip(0, f"无法读取：{path}\n{error}\n折叠后重新展开可重试")
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            return
        if not children:
            item.setData(0, ROLE_LOADED, True)
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
            return
        for name, child_path in children:
            item.addChild(self._make_item(name, child_path, "folder"))
        item.setData(0, ROLE_LOADED, True)
