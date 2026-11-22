"""桌面交付：安装包日志位置、版本身份和旧数据不被覆盖。

运行：
  QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover \
    -s tests -p 'test_product_packaging.py' -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PackagingIdentityTests(unittest.TestCase):
    def test_metadata_matches_stage6_desktop_release(self):
        import app_metadata

        self.assertEqual(app_metadata.APP_NAME, "归档盒")
        self.assertEqual(app_metadata.APP_VERSION, "1.8.1")
        self.assertEqual(app_metadata.APP_VERSION_LABEL, "V1.8.1")
        self.assertEqual(app_metadata.APP_TITLE, "归档盒 V1.8.1")
        self.assertEqual(app_metadata.BUNDLE_ID, "local.ypjn.archivebox")
        self.assertEqual(app_metadata.EXECUTABLE_NAME, "archivebox")
        identity = app_metadata.load_build_identity()
        self.assertEqual(identity["version"], "1.8.1")
        self.assertTrue(identity.get("commit"))

    def test_about_and_export_share_metadata(self):
        import app_metadata
        import main as main_mod
        from classification_config import default_config
        from config_transfer import export_scheme_payload

        payload = export_scheme_payload(default_config(), "身份核对")
        self.assertEqual(main_mod.APP_TITLE, app_metadata.APP_TITLE)
        self.assertEqual(payload["app"]["name"], app_metadata.APP_NAME)
        self.assertEqual(payload["app"]["version"], app_metadata.APP_VERSION)
        text = app_metadata.about_html("/tmp/归档盒数据")
        self.assertIn("1.8.1", text)
        self.assertIn(identity_commit_prefix(app_metadata), text)
        self.assertIn("/tmp/归档盒数据", text)

    def test_build_entry_selects_interpreter_and_pyinstaller_module(self):
        script = _read(os.path.join(ROOT, "打包", "build_mac.sh"))
        spec = _read(os.path.join(ROOT, "打包", "archivebox.spec"))
        self.assertIn("ARCHIVEBOX_BUILD_PYTHON", script)
        self.assertIn("-m PyInstaller", script)
        self.assertNotIn(".venv/bin/pyinstaller", script)
        if ".venv/bin/python" in script:
            self.assertLess(
                script.find("ARCHIVEBOX_BUILD_PYTHON"),
                script.find(".venv/bin/python"))
        self.assertIn("app_metadata", spec)
        self.assertIn("归档盒.icns", spec)
        icon = os.path.join(ROOT, "打包", "assets", "归档盒.icns")
        self.assertTrue(os.path.isfile(icon))
        self.assertGreater(os.path.getsize(icon), 1000)

    def test_packaged_log_stays_outside_app_bundle(self):
        import main as main_mod

        bundle_root = tempfile.mkdtemp(prefix="archivebox_bundle_")
        resources = os.path.join(
            bundle_root, "归档盒.app", "Contents", "Resources")
        os.makedirs(resources)
        fake_main = os.path.join(resources, "main.py")
        with open(fake_main, "w", encoding="utf-8") as handle:
            handle.write("# packaged stand-in\n")
        log_dir = tempfile.mkdtemp(prefix="archivebox_logs_")
        self.addCleanup(shutil.rmtree, bundle_root, True)
        self.addCleanup(shutil.rmtree, log_dir, True)
        with _swap_attr(main_mod, "__file__", fake_main), _env(
                "ARCHIVEBOX_LOG_DIR", log_dir):
            path = main_mod.startup_log_path()
            main_mod.append_startup_log("packaged-log-probe")
        app_root = os.path.join(bundle_root, "归档盒.app")
        self.assertFalse(_is_within(path, app_root))
        self.assertEqual(path, os.path.join(log_dir, "启动日志.txt"))
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(
            os.path.join(resources, "启动日志.txt")))
        with open(path, encoding="utf-8") as handle:
            self.assertIn("packaged-log-probe", handle.read())

    def test_frozen_runtime_uses_user_log_dir_even_from_source_path(self):
        import main as main_mod

        # 强制落在系统临时目录，避免被污染的 TEMP 建到源码树里导致误判。
        log_dir = tempfile.mkdtemp(
            prefix="archivebox_frozen_logs_",
            dir=tempfile.gettempdir())
        self.addCleanup(shutil.rmtree, log_dir, True)
        with _env("ARCHIVEBOX_LOG_DIR", log_dir), _frozen(True):
            path = main_mod.startup_log_path()
        self.assertEqual(path, os.path.join(log_dir, "启动日志.txt"))
        self.assertEqual(os.path.commonpath([path, log_dir]), log_dir)

    def test_source_run_keeps_log_beside_source(self):
        import main as main_mod

        with _env("ARCHIVEBOX_LOG_DIR", None), _frozen(False):
            path = main_mod.startup_log_path()
        self.assertEqual(
            os.path.dirname(os.path.abspath(path)),
            os.path.dirname(os.path.abspath(main_mod.__file__)))

    def test_existing_user_data_is_not_reinitialized(self):
        import recent_spaces
        import scheme_store
        from classification_config import default_config
        from executor import execute_plan, undo_latest_batch
        from planner import (
            DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan,
        )
        from preferences import (
            MATCH_FILENAME_KEYWORD, SCOPE_GLOBAL, Preference,
            list_global_preferences, save_preference,
        )
        from scanner import scan_directory
        from storage import Storage

        app_dir = tempfile.mkdtemp(prefix="archivebox_appdata_")
        space = tempfile.mkdtemp(prefix="archivebox_space_")
        self.addCleanup(shutil.rmtree, app_dir, True)
        self.addCleanup(shutil.rmtree, space, True)
        sample = os.path.join(space, "高数作业.txt")
        payload = "高等数学 极限 作业\n"
        with open(sample, "w", encoding="utf-8") as handle:
            handle.write(payload)
        before_sha = _sha256(sample)

        with _env("ARCHIVEBOX_APP_DIR", app_dir), _env(
                "ARCHIVEBOX_SCHEME_DIR", None), _env(
                "ARCHIVEBOX_RECENT_FILE", None):
            scheme = scheme_store.save_scheme(
                "升级夹具方案", "marker-keep", default_config())
            scheme_path = scheme.path
            scheme_bytes = _read_bytes(scheme_path)
            save_preference(space, Preference(
                preference_id="pref-upgrade-marker",
                name="升级夹具偏好",
                match_type=MATCH_FILENAME_KEYWORD,
                keywords=["升级夹具"],
                direction="项目",
                category="夹具",
                scope=SCOPE_GLOBAL,
                source_kind="user_confirmed",
            ))
            recent_spaces.touch(space, "旧空间")
            recent_path = recent_spaces.store_path()
            recent_bytes = _read_bytes(recent_path)
            scan = scan_directory(space)
            plan = generate_plan(
                scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
            self.assertTrue(plan.items)
            for item in plan.items:
                item.selected = True
            storage = Storage(space)
            executed = execute_plan(plan, storage)
            self.assertEqual(executed.moved, 1, executed.errors)
            self.assertFalse(os.path.exists(sample))
            batches = storage.list_batches()
            storage.close()
            batch_ids = [batch.batch_id for batch in batches]

            listed = scheme_store.list_schemes()
            names = [item.name for item in listed if not item.builtin]
            prefs = list_global_preferences()
            recent = recent_spaces.list_spaces()
            reopened = Storage(space)
            reopened_ids = [batch.batch_id for batch in reopened.list_batches()]
            undone = undo_latest_batch(reopened)
            reopened.close()
            scheme_after = _read_bytes(scheme_path)
            recent_after = _read_bytes(recent_path)

        self.assertEqual(names, ["升级夹具方案"])
        self.assertEqual(scheme_after, scheme_bytes)
        self.assertEqual(recent_after, recent_bytes)
        self.assertTrue(any(item.name == "升级夹具偏好" for item in prefs))
        self.assertEqual([item.path for item in recent], [os.path.realpath(space)])
        self.assertEqual(reopened_ids, batch_ids)
        self.assertEqual(undone.restored, 1, undone.errors_list)
        self.assertEqual(_sha256(sample), before_sha)


def identity_commit_prefix(app_metadata) -> str:
    commit = str(app_metadata.load_build_identity().get("commit") or "")
    return commit[:12]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _is_within(path: str, parent: str) -> bool:
    path = os.path.abspath(path)
    parent = os.path.abspath(parent)
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


class _env:
    def __init__(self, key: str, value):
        self.key = key
        self.value = value
        self.old = os.environ.get(key)

    def __enter__(self):
        if self.value is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.value
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.old
        return False


class _swap_attr:
    def __init__(self, obj, name: str, value):
        self.obj = obj
        self.name = name
        self.value = value
        self.old = getattr(obj, name)

    def __enter__(self):
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, exc_type, exc, tb):
        setattr(self.obj, self.name, self.old)
        return False


class _frozen:
    def __init__(self, value: bool):
        self.value = value
        self.had = hasattr(sys, "frozen")
        self.old = getattr(sys, "frozen", None)

    def __enter__(self):
        sys.frozen = self.value
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.had:
            sys.frozen = self.old
        else:
            delattr(sys, "frozen")
        return False


if __name__ == "__main__":
    unittest.main()
