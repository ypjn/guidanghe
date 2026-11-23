"""当前整理空间的文件夹监视。

只判断哪些新路径值得告诉用户去重新扫描。不移动文件，不写预览。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from scanner import SKIP_DIRS, _is_junction_or_reparse

_ACCEPTED_KINDS = frozenset({"created", "moved_in"})
_SKIP = SKIP_DIRS | {name.lower() for name in SKIP_DIRS}


def accept_watch_path(root: str, path: str, kind: str) -> bool:
    """新文件或移入文件仍在空间内、且不是自身产物或越界链接时返回 True。"""
    if kind not in _ACCEPTED_KINDS or not root or not path:
        return False
    root_real = os.path.realpath(root)
    if not root_real:
        return False
    if _crosses_link_or_junction(root, path):
        return False
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath([root_real, resolved]) != root_real:
            return False
    except ValueError:
        return False
    rel = os.path.relpath(resolved, root_real)
    if rel in (".", os.curdir):
        return False
    for segment in rel.split(os.sep):
        if segment in _SKIP or segment.lower() in _SKIP:
            return False
    return True


def _crosses_link_or_junction(root: str, path: str) -> bool:
    """只检查空间内部的每一段。空间外面的系统目录链接不算越界。"""
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        rel = os.path.relpath(path_abs, root_abs)
    except ValueError:
        return False
    if rel.startswith(".."):
        return False
    cursor = root_abs
    for segment in rel.split(os.sep):
        if segment in ("", os.curdir):
            continue
        cursor = os.path.join(cursor, segment)
        if os.path.islink(cursor):
            return True
        if os.path.lexists(cursor) and _is_junction_or_reparse(cursor):
            return True
    return False


@dataclass
class WatchBatch:
    root: str
    paths: list[str]
    overflow: int


def coalesce_watch(events: list[tuple[str, str]], root: str, cap: int = 200) -> WatchBatch:
    """按出现顺序去重。超过 cap 的可接受路径只计入 overflow。"""
    accepted: list[str] = []
    seen: set[str] = set()
    overflow = 0
    for path, kind in events:
        if not accept_watch_path(root, path, kind):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if len(accepted) >= cap:
            overflow += 1
            continue
        accepted.append(path)
    return WatchBatch(root=root, paths=accepted, overflow=overflow)


class FolderWatch:
    """监听一个空间。测试可注入 observer_factory，避免启动系统监视。"""

    def __init__(self, observer_factory=None, debounce_s: float = 1.5):
        self._factory = observer_factory
        self._debounce_s = debounce_s
        self._observer = None
        self._root = ""
        self._pending: list[tuple[str, str, float]] = []
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        return self._root

    def start(self, root: str) -> None:
        self.stop()
        if not root or not os.path.isdir(root):
            return
        self._root = os.path.abspath(root)
        if self._factory is None:
            from watchdog.observers import Observer
            observer = Observer()
        else:
            observer = self._factory()
        observer.schedule(_event_handler(self), self._root, recursive=True)
        starter = getattr(observer, "start", None)
        if callable(starter):
            starter()
        self._observer = observer

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        self._root = ""
        if observer is not None:
            stopper = getattr(observer, "stop", None)
            if callable(stopper):
                stopper()
            join = getattr(observer, "join", None)
            if callable(join):
                try:
                    join(timeout=2)
                except TypeError:
                    join()
                except Exception:
                    pass
        with self._lock:
            self._pending.clear()

    def note(self, path: str, kind: str) -> None:
        if not self._root or not path:
            return
        with self._lock:
            self._pending.append((path, kind, time.monotonic()))

    def flush(self) -> WatchBatch | None:
        with self._lock:
            if not self._root or not self._pending:
                return None
            latest = max(stamp for _, _, stamp in self._pending)
            if time.monotonic() - latest < self._debounce_s:
                return None
            events = [(path, kind) for path, kind, _ in self._pending]
            root = self._root
            self._pending.clear()
        batch = coalesce_watch(events, root)
        if not batch.paths and not batch.overflow:
            return None
        return batch


def _event_handler(watch: FolderWatch):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            watch.note(getattr(event, "src_path", ""), "created")

        def on_moved(self, event):
            dest = getattr(event, "dest_path", "") or ""
            if dest:
                watch.note(dest, "moved_in")

    return _Handler()
