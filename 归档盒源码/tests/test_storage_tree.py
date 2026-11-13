"""
左上角磁盘树：只列出可见目录，单击不切换空间，双击才选定。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_storage_tree.py
"""

from __future__ import annotations

import os
import shutil
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from intake_model import normalize_path
from main import MainWindow
from storage_tree import (
    ROLE_PATH, Place, StorageTree, list_child_dirs, list_places, posix_drives,
)

RESULTS = []
BASE = "/tmp/_storage_tree"


def rec(tid, title, ok, evidence=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    app = QApplication.instance() or QApplication([])
    for name in ("information", "warning", "critical"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)

    try:
        root = os.path.join(BASE, "root")
        volumes = os.path.join(BASE, "Volumes")
        os.makedirs(os.path.join(root, "Users"))
        os.makedirs(volumes)
        os.symlink(root, os.path.join(volumes, "Macintosh HD"))
        os.makedirs(os.path.join(volumes, "Data"))
        os.makedirs(os.path.join(volumes, "Recovery"))
        places = posix_drives(root, volumes)
        names = [p.name for p in places]
        ok = (names[0] == "Macintosh HD"
              and os.path.normcase(places[0].path) == os.path.normcase(os.path.realpath(root))
              and "Data" in names
              and "Recovery" not in names)
        rec("S01", "启动盘用用户看到的名字，恢复分区不进侧栏", ok,
            f"名称={names}")
    except Exception as e:
        rec("S01", "磁盘列表", False, f"异常：{e}")

    try:
        folder = os.path.join(BASE, "kids")
        os.makedirs(os.path.join(folder, "b"))
        os.makedirs(os.path.join(folder, "A"))
        os.makedirs(os.path.join(folder, ".hidden"))
        with open(os.path.join(folder, "note.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        names = [name for name, _path in list_child_dirs(folder)]
        ok = names == ["A", "b"]
        rec("S02", "只列出可见子目录，跳过文件和隐藏目录", ok, f"子目录={names}")
    except Exception as e:
        rec("S02", "子目录", False, f"异常：{e}")

    try:
        disk = os.path.join(BASE, "disk")
        home = os.path.join(disk, "某人")
        nested = os.path.join(home, "桌面")
        os.makedirs(nested)
        os.makedirs(os.path.join(disk, "空文件夹"))
        places = [
            Place("测试盘", disk, "drive"),
            Place("某人", home, "home"),
        ]
        tree = StorageTree(places)
        home_item = tree.topLevelItem(1)
        shown = [home_item.child(i).text(0) for i in range(home_item.childCount())]
        highlighted = []
        activated = []
        tree.folderHighlighted.connect(highlighted.append)
        tree.folderActivated.connect(activated.append)
        tree.itemClicked.emit(home_item, 0)
        click_only = highlighted == [home] and activated == []
        tree.itemDoubleClicked.emit(home_item.child(0), 0)
        opened = activated == [nested]
        tree.reveal(nested)
        current = tree.currentItem()
        revealed = (current is not None
                    and normalize_path(current.data(0, ROLE_PATH)) == normalize_path(nested)
                    and not current.isExpanded())
        empty = tree.topLevelItem(0)
        tree.expandItem(empty)
        empty_child = None
        for i in range(empty.childCount()):
            if empty.child(i).text(0) == "空文件夹":
                empty_child = empty.child(i)
        tree.expandItem(empty_child)
        no_arrow = (empty_child.childCount() == 0
                    and empty_child.childIndicatorPolicy()
                    == empty_child.ChildIndicatorPolicy.DontShowIndicator)
        ok = (home_item.isExpanded() and shown == ["桌面"]
              and click_only and opened and revealed and no_arrow)
        rec("S03", "单击只提示，双击才选定；能展开到目标文件夹", ok,
            f"主目录已展开={home_item.isExpanded()} 子项={shown} "
            f"单击={click_only} 双击={opened} 定位={revealed} 空目录无箭头={no_arrow}")
        tree.close()
    except Exception as e:
        rec("S03", "树交互", False, f"异常：{e}")

    try:
        places = list_places()
        home = os.path.realpath(os.path.expanduser("~"))
        kinds = {p.kind for p in places}
        paths_ok = all(os.path.isdir(p.path) for p in places)
        has_home = any(os.path.normcase(p.path) == os.path.normcase(home)
                       and p.kind == "home" for p in places)
        ok = ("drive" in kinds and has_home and paths_ok and len(places) >= 2)
        rec("S04", "本机列表包含磁盘和主文件夹", ok,
            f"{[(p.kind, p.name, p.path) for p in places]}")
    except Exception as e:
        rec("S04", "本机列表", False, f"异常：{e}")

    try:
        w = MainWindow()
        labels = [w.storage_tree.topLevelItem(i).text(0)
                  for i in range(w.storage_tree.topLevelItemCount())]
        home_name = os.path.basename(os.path.realpath(os.path.expanduser("~")))
        target = os.path.join(BASE, "chosen")
        os.makedirs(target)
        w._open_sidebar_space(target)
        opened = os.path.normcase(os.path.realpath(w.path_edit.text().strip())) == (
            os.path.normcase(os.path.realpath(target)))
        w._working = True
        other = os.path.join(BASE, "other")
        os.makedirs(other)
        w._open_sidebar_space(other)
        blocked = os.path.normcase(os.path.realpath(w.path_edit.text().strip())) == (
            os.path.normcase(os.path.realpath(target)))
        ok = home_name in labels and opened and blocked and "Macintosh HD" in labels or (
            sys.platform == "win32")
        if sys.platform != "win32":
            ok = home_name in labels and "Macintosh HD" in labels and opened and blocked
        rec("S05", "主窗口侧栏显示磁盘，双击入口会切换且忙碌时拒绝", ok,
            f"顶层={labels} 已打开={opened} 忙碌拒绝={blocked}")
        w.close()
        app.processEvents()
    except Exception as e:
        rec("S05", "主窗口侧栏", False, f"异常：{e}")

    failed = sum(1 for ok in RESULTS if not ok)
    print(f"\n合计 {len(RESULTS)} 项：未通过 {failed}")
    return 0 if RESULTS and failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
