"""外置盘只读索引：离线目录、卷身份与本地清理。

运行：
  QT_QPA_PLATFORM=offscreen /agent/repos/guidanghe/.venv/bin/python -m unittest discover \
    -s tests -p 'test_product_volume.py' -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _sha_tree(root: str) -> dict[str, str]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [n for n in dirnames if n != ".organizer"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and not os.path.islink(path):
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    digest.update(handle.read())
                found[os.path.relpath(path, root)] = digest.hexdigest()
    return found


class VolumeIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_volume_")
        self.db = os.path.join(self.tmp, "index.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["ARCHIVEBOX_INDEX_DB"] = self.db
        os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(self.tmp, "appdata")

    def tearDown(self):
        from volume_identity import set_probe_override
        set_probe_override(None)
        os.environ.pop("ARCHIVEBOX_INDEX_DB", None)
        os.environ.pop("ARCHIVEBOX_APP_DIR", None)

    def test_offline_retains_catalog_and_search_notes(self):
        from content_search import SearchQuery, search_files
        from index_service import register_space, update_space
        from index_store import IndexStore
        from search_session import locate_hit
        from volume_identity import VolumeInfo, set_probe_override

        root = os.path.join(self.tmp, "MyDisk", "data")
        os.makedirs(root)
        _write(root, "readme.txt", "外置资料夹具\n")
        vol = VolumeInfo("test-vol-offline", "ext4", 99, "MyDisk")
        set_probe_override(lambda _path: vol)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "外置盘", store=store)
        report = update_space(spec.space_id, store=store, read_content=True)
        self.assertTrue(report.completed, report.errors)
        self.assertTrue(spec.volume_id)

        os.rename(root, root + "-gone")
        offline = update_space(spec.space_id, store=store)
        self.assertEqual(offline.coverage, "offline")
        self.assertTrue(store.present_files(spec.space_id))
        refreshed = store.get_space(spec.space_id)
        self.assertFalse(refreshed.online)

        page = search_files(store, SearchQuery(text="外置", space_ids=[spec.space_id]))
        self.assertTrue(page.hits)
        hit = page.hits[0]
        self.assertFalse(hit.space_online)
        self.assertEqual(hit.availability, "offline")

        path, reason = locate_hit(store, hit)
        self.assertFalse(path)
        self.assertIn("离线", reason)

        os.rename(root + "-gone", root)
        back = update_space(spec.space_id, store=store)
        self.assertTrue(back.completed, back.errors)
        self.assertTrue(store.get_space(spec.space_id).online)

    def test_same_name_different_volume_identity_mismatch(self):
        from index_service import register_space, update_space
        from index_store import IndexStore
        from volume_identity import VolumeInfo, set_probe_override

        vol_a = os.path.join(self.tmp, "mount", "MyDisk", "data")
        vol_b = os.path.join(self.tmp, "other", "MyDisk", "data")
        os.makedirs(vol_a)
        os.makedirs(vol_b)
        _write(vol_a, "only-a.txt", "卷 A\n")
        _write(vol_b, "only-b.txt", "卷 B\n")

        def probe(path: str):
            real = os.path.realpath(path)
            if real == os.path.realpath(vol_a):
                return VolumeInfo("uuid:disk-a", "ext4", 101, "MyDisk")
            if real == os.path.realpath(vol_b):
                return VolumeInfo("uuid:disk-b", "ext4", 202, "MyDisk")
            return VolumeInfo("uuid:unknown", "", 0, "")

        set_probe_override(probe)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(vol_a, "MyDisk", store=store)
        self.assertEqual(spec.volume_id, "uuid:disk-a")
        first = update_space(spec.space_id, store=store)
        self.assertTrue(first.completed, first.errors)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("only-a.txt", names)
        self.assertNotIn("only-b.txt", names)

        spec.root = vol_b
        store.upsert_space(spec, "2026-11-22T12:00:00")
        mismatch = update_space(spec.space_id, store=store)
        self.assertEqual(mismatch.coverage, "identity_mismatch")
        self.assertTrue(mismatch.errors)
        names_after = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertEqual(names_after, names)
        self.assertNotIn("only-b.txt", names_after)

    def test_incomplete_scan_does_not_delete_unseen(self):
        from index_service import register_space, update_space
        from index_store import IndexStore
        import index_service

        root = os.path.join(self.tmp, "partial")
        os.makedirs(os.path.join(root, "deep"))
        _write(root, "a.txt", "a\n")
        _write(root, "deep/hidden.txt", "hidden\n")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "部分", store=store)
        self.assertTrue(update_space(spec.space_id, store=store).completed)

        cancel = threading.Event()

        def progress(count, _total, _path):
            if count >= 1:
                cancel.set()

        cancelled = update_space(
            spec.space_id, cancel_event=cancel, progress_cb=progress, store=store)
        self.assertTrue(cancelled.cancelled)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("deep/hidden.txt", names)

        real_scandir = index_service.os.scandir

        def blocked(path):
            if str(path).endswith(f"{os.sep}deep"):
                raise OSError(5, "I/O error")
            return real_scandir(path)

        index_service.os.scandir = blocked
        try:
            failed = update_space(spec.space_id, store=store)
        finally:
            index_service.os.scandir = real_scandir
        self.assertFalse(failed.completed)
        names = {item.rel_path for item in store.present_files(spec.space_id)}
        self.assertIn("deep/hidden.txt", names)

    def test_remove_cleans_local_index_only(self):
        from index_service import register_space, remove_space, update_space
        from index_store import IndexStore

        root = os.path.join(self.tmp, "source")
        history = os.path.join(root, ".organizer", "history.db")
        os.makedirs(os.path.dirname(history))
        with open(history, "wb") as handle:
            handle.write(b"keep-history")
        _write(root, "keep.txt", "来源不变\n")
        before = _sha_tree(root)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "来源", store=store)
        update_space(spec.space_id, store=store, read_content=True)
        self.assertTrue(store.present_files(spec.space_id))

        remove_space(spec.space_id, store=store)
        self.assertEqual(store.present_files(spec.space_id), [])
        self.assertIsNone(store.get_space(spec.space_id))
        self.assertEqual(_sha_tree(root), before)
        self.assertTrue(os.path.isfile(history))

    def test_schema_v2_migrates_to_v3(self):
        from index_store import SCHEMA_VERSION, IndexStore
        from volume_identity import VolumeInfo, set_probe_override

        root = os.path.join(self.tmp, "legacy")
        os.makedirs(root)
        _write(root, "old.txt", "old\n")
        set_probe_override(lambda _p: VolumeInfo("legacy-vol", "tmpfs", 7, "legacy"))

        legacy_db = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '2');
            CREATE TABLE spaces (
                space_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                policy_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                path_key TEXT NOT NULL,
                name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL DEFAULT '',
                availability TEXT NOT NULL,
                index_state TEXT NOT NULL,
                parser_version INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL,
                exclude_reason TEXT NOT NULL DEFAULT '',
                present INTEGER NOT NULL DEFAULT 1,
                UNIQUE(space_id, path_key)
            );
            """
        )
        conn.execute(
            "INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)",
            ("sp-legacy", root, "legacy", 1, "{}", "2026-01-01", "2026-01-01", 0, ""),
        )
        conn.execute(
            "INSERT INTO files VALUES ("
            "'f1','sp-legacy','old.txt','old.txt','old.txt','txt',"
            "4,0,0,1,2,'','online','content',0,'2026-01-01','',1)"
        )
        conn.commit()
        conn.close()

        store = IndexStore(legacy_db)
        self.addCleanup(store.close)
        row = store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), SCHEMA_VERSION)
        spec = store.get_space("sp-legacy")
        self.assertEqual(spec.volume_id, "legacy-vol")
        self.assertTrue(spec.online)
        self.assertEqual(len(store.present_files("sp-legacy")), 1)

    def test_reassociate_updates_volume_without_wiping_index(self):
        from index_service import reassociate_space, register_space, update_space
        from index_store import IndexStore
        from volume_identity import VolumeInfo, set_probe_override

        root = os.path.join(self.tmp, "swap")
        os.makedirs(root)
        _write(root, "keep.txt", "保留索引\n")
        set_probe_override(lambda _p: VolumeInfo("win:AAAA", "exFAT", 1, "DiskA"))
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "换盘", store=store)
        self.assertTrue(update_space(spec.space_id, store=store).completed)
        before = {f.rel_path for f in store.present_files(spec.space_id)}
        self.assertIn("keep.txt", before)

        set_probe_override(lambda _p: VolumeInfo("win:BBBB", "exFAT", 2, "DiskB"))
        mismatch = update_space(spec.space_id, store=store)
        self.assertEqual(mismatch.coverage, "identity_mismatch")
        self.assertEqual(
            {f.rel_path for f in store.present_files(spec.space_id)}, before)

        updated = reassociate_space(spec.space_id, store=store)
        self.assertEqual(updated.volume_id, "win:BBBB")
        self.assertEqual(
            {f.rel_path for f in store.present_files(spec.space_id)}, before)

    @unittest.skipUnless(os.name == "nt", "Windows volume serial only")
    def test_windows_probe_uses_volume_serial_not_drive_letter(self):
        from volume_identity import probe_volume

        info = probe_volume(self.tmp)
        self.assertIsNotNone(info)
        self.assertTrue(info.volume_id.startswith("win:"), info)
        self.assertRegex(info.volume_id, r"^win:[0-9A-F]{8}$")
        # 同一卷上的子路径必须得到同一序列号身份
        child = os.path.join(self.tmp, "子目录 测试")
        os.makedirs(child)
        child_info = probe_volume(child)
        self.assertEqual(info.volume_id, child_info.volume_id)
        self.assertEqual(info.filesystem, child_info.filesystem)

    @unittest.skipUnless(sys.platform == "darwin", "macOS volume identity")
    def test_macos_child_path_keeps_same_volume_identity(self):
        """子目录必须和父目录得到同一卷身份，不能只靠文件夹名。"""
        from volume_identity import probe_volume

        info = probe_volume(self.tmp)
        self.assertIsNotNone(info)
        self.assertTrue(info.volume_id.startswith("uuid:"), info.volume_id)
        child = os.path.join(self.tmp, "子目录 测试")
        os.makedirs(child)
        child_info = probe_volume(child)
        self.assertIsNotNone(child_info)
        self.assertEqual(info.volume_id, child_info.volume_id)
        self.assertEqual(info.filesystem, child_info.filesystem)

    @unittest.skipIf(os.name == "nt", "POSIX directory symlink escape check")
    def test_directory_symlink_outside_space_not_indexed(self):
        """目录符号链接指向空间外时，索引不得把哨兵登记成空间内文件。"""
        from index_service import register_space, update_space
        from index_store import IndexStore

        root = os.path.join(self.tmp, "space root")
        outside = os.path.join(self.tmp, "哨兵目录")
        os.makedirs(root)
        os.makedirs(outside)
        _write(outside, "secret.txt", "空间外哨兵\n")
        _write(root, "inside.txt", "空间内\n")
        os.symlink(outside, os.path.join(root, "逃逸链接"), target_is_directory=True)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "symlink", store=store)
        report = update_space(spec.space_id, store=store)
        self.assertTrue(
            report.completed or report.coverage in {"complete", "partial"},
            report.errors)
        names = {item.rel_path.replace("\\", "/") for item in store.present_files(spec.space_id)}
        self.assertIn("inside.txt", names)
        self.assertNotIn("逃逸链接/secret.txt", names)
        self.assertNotIn("secret.txt", names)

    @unittest.skipUnless(os.name == "nt", "Windows junction escape check")
    def test_windows_junction_outside_space_not_followed_as_inside(self):
        """目录 junction 指向空间外哨兵时，索引不得把哨兵登记为空间内文件。"""
        import subprocess

        from index_service import register_space, update_space
        from index_store import IndexStore

        root = os.path.join(self.tmp, "space root")
        outside = os.path.join(self.tmp, "哨兵目录")
        os.makedirs(root)
        os.makedirs(outside)
        _write(outside, "secret.txt", "空间外哨兵\n")
        _write(root, "inside.txt", "空间内\n")
        junction = os.path.join(root, "逃逸链接")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", junction, outside],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False)
        if created.returncode != 0:
            self.skipTest(
                f"无法创建 junction：{(created.stderr or created.stdout or '').strip()}")

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "junction", store=store)
        report = update_space(spec.space_id, store=store)
        self.assertTrue(report.completed or report.coverage in {"complete", "partial"},
                        report.errors)
        names = {item.rel_path.replace("\\", "/") for item in store.present_files(spec.space_id)}
        self.assertIn("inside.txt", names)
        self.assertNotIn("逃逸链接/secret.txt", names)
        self.assertNotIn("secret.txt", names)


if __name__ == "__main__":
    unittest.main()