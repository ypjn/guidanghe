"""多空间本地索引。

运行：
  QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover \
    -s tests -p 'test_product_index.py' -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _snapshot(root: str) -> dict:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".organizer"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                found[os.path.relpath(path, root)] = "link:" + os.readlink(path)
            elif os.path.isfile(path):
                found[os.path.relpath(path, root)] = _sha(path)
    return found


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_index_")
        self.db = os.path.join(self.tmp, "index.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["ARCHIVEBOX_INDEX_DB"] = self.db
        os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(self.tmp, "appdata")
        os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(self.tmp, "recent.json")

    def tearDown(self):
        os.environ.pop("ARCHIVEBOX_INDEX_DB", None)
        os.environ.pop("ARCHIVEBOX_APP_DIR", None)
        os.environ.pop("ARCHIVEBOX_RECENT_FILE", None)

    def test_two_spaces_overlap_and_incremental_updates(self):
        import recent_spaces
        from index_service import (
            overlap_notes, register_space, remove_space, update_space,
        )
        from index_store import IndexStore

        space_a = os.path.join(self.tmp, "a")
        space_b = os.path.join(self.tmp, "b")
        nested = os.path.join(space_a, "nested")
        os.makedirs(nested)
        _write(space_a, "note.txt", "甲空间\n")
        _write(space_b, "note.txt", "乙空间\n")
        _write(space_a, "nested/shared.txt", "重叠\n")
        history = os.path.join(space_a, ".organizer", "history.db")
        os.makedirs(os.path.dirname(history))
        with open(history, "wb") as handle:
            handle.write(b"old-history")
        history_sha = _sha(history)
        recent_spaces.touch(space_a, "只是最近打开")
        before = _snapshot(space_a)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        self.assertEqual(store.list_spaces(), [])
        spec_a = register_space(space_a, "甲", store=store)
        spec_b = register_space(space_b, "乙", store=store)
        spec_n = register_space(nested, "嵌套", store=store)
        first = update_space(spec_a.space_id, store=store)
        update_space(spec_b.space_id, store=store)
        update_space(spec_n.space_id, store=store)
        self.assertTrue(first.completed, first.errors)
        self.assertGreater(first.reread, 0)
        self.assertEqual(_snapshot(space_a), before)
        again = update_space(spec_a.space_id, store=store)
        self.assertEqual(again.reread, 0)
        self.assertGreater(again.reused, 0)

        files_a = {item.rel_path: item for item in store.present_files(spec_a.space_id)}
        files_b = {item.rel_path: item for item in store.present_files(spec_b.space_id)}
        self.assertNotEqual(files_a["note.txt"].content_sha256,
                            files_b["note.txt"].content_sha256)
        self.assertNotEqual(files_a["note.txt"].space_id, files_b["note.txt"].space_id)
        notes = overlap_notes(store)
        shared = next(item for item in store.present_files(spec_n.space_id)
                      if item.name == "shared.txt")
        self.assertIn("甲", notes[shared.file_id])

        path = os.path.join(space_a, "note.txt")
        stat = os.stat(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("甲空间改\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed = update_space(spec_a.space_id, store=store)
        self.assertGreaterEqual(changed.reread, 1)
        updated = {item.rel_path: item for item in store.present_files(spec_a.space_id)}
        self.assertNotEqual(updated["note.txt"].content_sha256,
                            files_a["note.txt"].content_sha256)

        os.rename(path, os.path.join(space_a, "renamed.txt"))
        update_space(spec_a.space_id, store=store)
        names = {item.rel_path for item in store.present_files(spec_a.space_id)}
        self.assertIn("renamed.txt", names)
        self.assertNotIn("note.txt", names)
        os.remove(os.path.join(space_a, "renamed.txt"))
        update_space(spec_a.space_id, store=store)
        names = {item.rel_path for item in store.present_files(spec_a.space_id)}
        self.assertNotIn("renamed.txt", names)

        os.rename(space_a, space_a + "-gone")
        missing = update_space(spec_a.space_id, store=store)
        self.assertEqual(missing.coverage, "unavailable")
        self.assertTrue(store.present_files(spec_a.space_id))
        os.rename(space_a + "-gone", space_a)

        self.assertEqual(_sha(history), history_sha)
        self.assertEqual(len(recent_spaces.list_spaces()), 1)
        remove_space(spec_a.space_id, store=store)
        self.assertEqual(store.present_files(spec_a.space_id), [])
        self.assertTrue(os.path.isfile(os.path.join(space_a, "nested/shared.txt")))
        self.assertEqual(_sha(history), history_sha)

    def test_cancel_permission_and_generation_do_not_drop_unseen_files(self):
        from index_service import register_space, update_space
        from index_store import IndexStore
        import index_service

        root = os.path.join(self.tmp, "scan")
        os.makedirs(os.path.join(root, "secret"))
        _write(root, "a.txt", "a\n")
        _write(root, "b.txt", "b\n")
        _write(root, "secret/keep.txt", "keep\n")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "扫描", store=store)
        self.assertTrue(update_space(spec.space_id, store=store).completed)

        cancel = threading.Event()

        def progress(count, _total, _path):
            if count >= 1:
                cancel.set()

        cancelled = update_space(
            spec.space_id, cancel_event=cancel, progress_cb=progress, store=store)
        self.assertTrue(cancelled.cancelled)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("secret/keep.txt", names)
        self.assertIn("a.txt", names)

        real_scandir = index_service.os.scandir

        def blocked(path):
            if str(path).endswith(f"{os.sep}secret"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        index_service.os.scandir = blocked
        try:
            failed = update_space(spec.space_id, store=store)
        finally:
            index_service.os.scandir = real_scandir
        self.assertFalse(failed.completed)
        self.assertTrue(failed.errors)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("secret/keep.txt", names)

        other = IndexStore(self.db)
        self.addCleanup(other.close)
        state = {"ran": False}

        def overlap_progress(count, _total, _path):
            if count == 1 and not state["ran"]:
                state["ran"] = True
                _write(root, "c.txt", "c\n")
                second = update_space(spec.space_id, store=other)
                self.assertTrue(second.completed, second.errors)

        update_space(spec.space_id, progress_cb=overlap_progress, store=store)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("c.txt", names)

    def test_protected_include_does_not_allow_move_and_rebuild_keeps_old_index(self):
        from index_service import register_space, update_space
        from index_store import IndexStore
        from protect_config import load_manual_protect, save_manual_protect
        from scanner import scan_directory

        root = os.path.join(self.tmp, "protect")
        _write(root, "keep.txt", "留下\n")
        _write(root, "demo/pyproject.toml", "[project]\nname='demo'\n")
        _write(root, "demo/src/main.py", "print('demo')\n")
        guarded = os.path.join(root, "manual")
        os.makedirs(guarded)
        _write(root, "manual/secret.txt", "手动保护\n")
        save_manual_protect(root, [guarded])
        before_rules = load_manual_protect(root)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(
            root, "保护", policy={"readonly_include": ["demo"]}, store=store)
        report = update_space(spec.space_id, store=store)
        self.assertTrue(report.completed, report.errors)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("keep.txt", names)
        self.assertIn("demo/src/main.py", names)
        self.assertNotIn("manual/secret.txt", names)
        organized = {item.rel_path.replace("\\", "/") for item in scan_directory(root).files}
        self.assertNotIn("demo/src/main.py", organized)
        self.assertEqual(load_manual_protect(root), before_rules)

        with self.assertRaises(RuntimeError):
            store.rebuild(fail=True)
        self.assertIn("demo/src/main.py",
                      {item.rel_path for item in store.present_files(spec.space_id)})
        store.rebuild()
        self.assertIn("keep.txt",
                      {item.rel_path for item in store.present_files(spec.space_id)})

    def test_links_stay_separate_and_do_not_escape(self):
        from index_service import register_space, update_space
        from index_store import IndexStore

        root = os.path.join(self.tmp, "links")
        outside = os.path.join(self.tmp, "outside.txt")
        os.makedirs(root)
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("哨兵\n")
        original = _write(root, "real.txt", "实体\n")
        os.link(original, os.path.join(root, "hard.txt"))
        os.symlink(outside, os.path.join(root, "escape.txt"))
        before = _sha(outside)
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "链接", store=store)
        update_space(spec.space_id, store=store)
        files = {item.rel_path: item for item in store.present_files(spec.space_id)}
        self.assertEqual(files["real.txt"].content_sha256, files["hard.txt"].content_sha256)
        self.assertNotEqual(files["real.txt"].rel_path, files["hard.txt"].rel_path)
        self.assertEqual(files["escape.txt"].availability, "link")
        self.assertEqual(_sha(outside), before)
        self.assertNotIn(outside, {item.rel_path for item in files.values()})

    def test_dialog_lists_registered_space(self):
        from PySide6.QtWidgets import QApplication
        from index_dialog import IndexSpacesDialog
        from index_service import register_space

        root = os.path.join(self.tmp, "ui")
        os.makedirs(root)
        register_space(root, "界面空间")
        app = QApplication.instance() or QApplication([])
        dialog = IndexSpacesDialog()
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.list.count(), 1)
        self.assertIn("界面空间", dialog.list.item(0).text())
        app.processEvents()

    def test_ten_thousand_files_reuse_unchanged_content(self):
        from index_service import register_space, update_space
        from index_store import IndexStore

        root = os.path.join(self.tmp, "scale")
        os.makedirs(root)
        for bucket in range(100):
            folder = os.path.join(root, f"d{bucket:03d}")
            os.makedirs(folder)
            for index in range(100):
                with open(os.path.join(folder, f"{index}.txt"), "w", encoding="utf-8") as handle:
                    handle.write("x")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "规模", store=store)
        started = time.perf_counter()
        cold = update_space(spec.space_id, store=store)
        cold_s = time.perf_counter() - started
        self.assertTrue(cold.completed, cold.errors)
        self.assertEqual(cold.added, 10000)
        started = time.perf_counter()
        hot = update_space(spec.space_id, store=store)
        hot_s = time.perf_counter() - started
        self.assertEqual(hot.reread, 0)
        self.assertEqual(hot.reused, 10000)
        print(f"\n索引规模 10000 冷={cold_s:.2f}s 热={hot_s:.2f}s "
              f"重读={hot.reread}")
        if cold_s < 15:
            for index in range(40000):
                bucket = index // 400
                folder = os.path.join(root, f"e{bucket:03d}")
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, f"{index}.txt"), "w", encoding="utf-8") as handle:
                    handle.write("y")
            started = time.perf_counter()
            wider = update_space(spec.space_id, store=store)
            wider_s = time.perf_counter() - started
            self.assertEqual(wider.added, 40000)
            hot2 = update_space(spec.space_id, store=store)
            self.assertEqual(hot2.reread, 0)
            print(f"索引规模 50000 增量={wider_s:.2f}s 热重读={hot2.reread}")


if __name__ == "__main__":
    unittest.main()
