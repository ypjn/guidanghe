"""文件夹监视：只报告当前空间里值得重新扫描的新文件。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from folder_watch import accept_watch_path, coalesce_watch  # noqa: E402


class FolderWatchPathTests(unittest.TestCase):
    def test_ignores_own_output_links_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "空间")
            os.makedirs(os.path.join(root, "_已整理"))
            note = os.path.join(root, "新笔记.txt")
            with open(note, "w", encoding="utf-8") as handle:
                handle.write("a")
            outside = os.path.join(tmp, "外面")
            os.makedirs(outside)
            link = os.path.join(root, "逃逸")
            os.symlink(outside, link, target_is_directory=True)
            self.assertTrue(accept_watch_path(root, note, "created"))
            self.assertFalse(accept_watch_path(
                root, os.path.join(root, "_已整理", "a.txt"), "created"))
            self.assertFalse(accept_watch_path(
                root, os.path.join(link, "秘密.txt"), "created"))
            self.assertFalse(accept_watch_path(root, note, "deleted"))


class FolderWatchCoalesceTests(unittest.TestCase):
    def test_coalesce_caps_at_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "空间")
            os.makedirs(root)
            events = []
            for i in range(201):
                path = os.path.join(root, f"{i}.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("a")
                events.append((path, "created"))
            duplicate = events[0]
            events.append(duplicate)
            deleted_only = os.path.join(root, "删掉.txt")
            with open(deleted_only, "w", encoding="utf-8") as handle:
                handle.write("a")
            events.append((deleted_only, "deleted"))
            batch = coalesce_watch(events, root)
            self.assertEqual(len(batch.paths), 200)
            self.assertEqual(batch.overflow, 1)
            self.assertEqual(batch.paths.count(events[0][0]), 1)
            self.assertNotIn(deleted_only, batch.paths)


if __name__ == "__main__":
    unittest.main()
