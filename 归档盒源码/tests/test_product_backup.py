"""第06批：本地版本备份与安全恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from backup_store import (  # noqa: E402
    load_manifest,
    validate_restore_rel_path,
    vault_paths,
    verify_objects,
)
from backup_service import (  # noqa: E402
    list_backup_versions,
    preview_backup,
    restore_version,
    run_backup,
    verify_version,
)
from tests.manifest_tool import compare_manifests, fs_manifest  # noqa: E402


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class ProductBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_backup_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = os.path.join(self.tmp, "source")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.source)
        _write(self.source, "docs/甲.txt", "第一版内容\n")
        _write(self.source, "docs/乙.txt", "不变文件\n")
        self.sentinel = os.path.join(self.tmp, "范围外哨兵.txt")
        with open(self.sentinel, "w", encoding="utf-8") as f:
            f.write("哨兵\n")
        self.sentinel_sha = _sha(self.sentinel)

    def test_two_versions_restore_match_independent_manifests(self):
        before_v1, _, _ = fs_manifest(self.source)
        r1 = run_backup(self.source, self.vault)
        self.assertTrue(r1.recoverable, r1.errors)
        vid1 = r1.version_id

        path_a = os.path.join(self.source, "docs", "甲.txt")
        with open(path_a, "w", encoding="utf-8") as f:
            f.write("第二版内容\n")
        _write(self.source, "docs/新增.txt", "v2 only\n")
        before_v2, _, _ = fs_manifest(self.source)

        r2 = run_backup(self.source, self.vault)
        self.assertTrue(r2.recoverable, r2.errors)
        vid2 = r2.version_id
        self.assertNotEqual(vid1, vid2)

        dest1 = os.path.join(self.tmp, "restore_v1")
        dest2 = os.path.join(self.tmp, "restore_v2")
        os.makedirs(dest1)
        os.makedirs(dest2)

        rr1 = restore_version(self.vault, vid1, dest1)
        rr2 = restore_version(self.vault, vid2, dest2)
        self.assertTrue(rr1.ok, rr1.errors)
        self.assertTrue(rr2.ok, rr2.errors)

        man1, err1, _ = fs_manifest(dest1)
        man2, err2, _ = fs_manifest(dest2)
        self.assertEqual(err1, [])
        self.assertEqual(err2, [])

        cmp1 = compare_manifests(before_v1, man1)
        cmp2 = compare_manifests(before_v2, man2)
        self.assertTrue(cmp1["ok"], cmp1)
        self.assertTrue(cmp2["ok"], cmp2)

        restored = os.path.join(dest1, "docs", "甲.txt")
        with open(restored, "w", encoding="utf-8") as f:
            f.write("篡改恢复副本\n")
        self.assertNotEqual(_sha(path_a), _sha(restored))
        vpaths = vault_paths(self.vault)
        m1 = load_manifest(vpaths, vid1)
        ok, _ = verify_objects(vpaths, m1)
        self.assertTrue(ok)
        self.assertEqual(_sha(self.sentinel), self.sentinel_sha)

    def test_incremental_reuse_and_source_unchanged(self):
        snap = {}
        for dirpath, _, names in os.walk(self.source):
            for n in names:
                p = os.path.join(dirpath, n)
                snap[os.path.relpath(p, self.source)] = _sha(p)
        r1 = run_backup(self.source, self.vault)
        r2 = run_backup(self.source, self.vault)
        self.assertGreater(r2.reused_objects, 0)
        self.assertEqual(r2.new_bytes, 0)
        for rel, digest in snap.items():
            self.assertEqual(_sha(os.path.join(self.source, rel)), digest)

    def test_cancel_before_manifest_not_recoverable_complete(self):
        cancel = threading.Event()

        def progress(_i, _t, _rel):
            cancel.set()

        report = run_backup(
            self.source, self.vault,
            cancel_event=cancel, progress_cb=progress)
        self.assertTrue(report.cancelled or report.state == "incomplete")
        versions = list_backup_versions(self.vault, recoverable_only=True)
        self.assertEqual(len(versions), 0)
        if report.version_id:
            vr = verify_version(self.vault, report.version_id)
            self.assertFalse(vr.ok)

    def test_interrupt_incomplete_manifest_only(self):
        r1 = run_backup(self.source, self.vault)
        vpaths = vault_paths(self.vault)
        mpath = os.path.join(
            vpaths["versions"], r1.version_id, "manifest.json")
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["state"] = "incomplete"
        bad = mpath + ".incomplete"
        with open(bad, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.remove(mpath)
        except OSError:
            pass
        loaded = load_manifest(vpaths, r1.version_id)
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded.recoverable)

    def test_tampered_object_and_missing_object(self):
        r1 = run_backup(self.source, self.vault)
        vpaths = vault_paths(self.vault)
        m = load_manifest(vpaths, r1.version_id)
        self.assertIsNotNone(m)
        entry = next(e for e in m.files if e.sha256)
        obj = os.path.join(vpaths["objects"], entry.object_ref)
        with open(obj, "wb") as f:
            f.write(b"tampered")
        vr = verify_version(self.vault, r1.version_id)
        self.assertFalse(vr.ok)
        os.remove(obj)
        vr2 = verify_version(self.vault, r1.version_id)
        self.assertFalse(vr2.ok)

    def test_malicious_manifest_path_escape_rejected_on_restore(self):
        r1 = run_backup(self.source, self.vault)
        vpaths = vault_paths(self.vault)
        mpath = os.path.join(
            vpaths["versions"], r1.version_id, "manifest.json")
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["files"].append({
            "rel_path": "../../逃逸.txt",
            "size": 4,
            "sha256": "ab" * 32,
            "mtime_ns": 0,
            "mode": 0,
            "object_ref": "ab/" + "ab" * 32,
            "is_symlink": False,
            "link_target": "",
        })
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(data, f)
        dest = os.path.join(self.tmp, "restore_escape")
        os.makedirs(dest)
        rr = restore_version(self.vault, r1.version_id, dest)
        self.assertFalse(rr.ok)
        self.assertTrue(
            any("逃逸" in e or "路径" in e for e in rr.errors))

    def test_restore_refuses_nonempty_target(self):
        r1 = run_backup(self.source, self.vault)
        dest = os.path.join(self.tmp, "restore_nonempty")
        os.makedirs(dest)
        _write(dest, "已有.txt", "block\n")
        rr = restore_version(self.vault, r1.version_id, dest)
        self.assertFalse(rr.ok)
        self.assertIn("非空", " ".join(rr.errors))

    def test_vault_overlap_rejected(self):
        nested = os.path.join(self.source, "inner_vault")
        os.makedirs(nested)
        prev = preview_backup(self.source, nested)
        self.assertFalse(prev.ok)
        self.assertTrue(prev.placement_errors)

    def test_protected_prefix_excluded_unless_authorized(self):
        prot = os.path.join(self.source, "protected_proj")
        os.makedirs(prot)
        _write(prot, "secret.txt", "protected\n")
        policy = {
            "exclude_dir_names": list(__import__(
                "index_policy", fromlist=["DEFAULT_EXCLUDE_DIR_NAMES"]
            ).DEFAULT_EXCLUDE_DIR_NAMES),
            "exclude_file_names": [],
            "readonly_include": [],
            "metadata_only": [],
            "deny_read": [],
            "protected_prefixes": ["protected_proj"],
        }
        r_no = run_backup(self.source, self.vault, policy=policy)
        self.assertTrue(r_no.recoverable)
        m_no = load_manifest(vault_paths(self.vault), r_no.version_id)
        exc_paths = {x.get("path", "") for x in (m_no.excluded if m_no else [])}
        self.assertIn("protected_proj/secret.txt", exc_paths)
        vault2 = os.path.join(self.tmp, "vault2")
        r_yes = run_backup(
            self.source, vault2, policy=policy,
            protected_allow={"protected_proj"})
        m = load_manifest(vault_paths(vault2), r_yes.version_id)
        paths = {e.rel_path for e in m.files}
        self.assertIn("protected_proj/secret.txt", paths)

    def test_validate_restore_paths(self):
        self.assertTrue(validate_restore_rel_path("../x"))
        self.assertTrue(validate_restore_rel_path("/abs"))
        self.assertTrue(validate_restore_rel_path("ok/../bad"))
        self.assertFalse(validate_restore_rel_path("docs/正常.txt"))

    def test_preview_counts_and_metadata_support(self):
        p = preview_backup(self.source, self.vault)
        self.assertTrue(p.ok, p.errors + p.placement_errors)
        self.assertEqual(p.file_count, 2)
        self.assertGreater(p.total_bytes, 0)
        self.assertIn("note", p.metadata_support)
        self.assertFalse(p.metadata_support.get("xattrs"))


if __name__ == "__main__":
    unittest.main()