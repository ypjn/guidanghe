"""阶段三基线 → 阶段四升级样本：创建与验证。

创建必须在阶段三基线源码上运行（PYTHONPATH 指向 5760c5f 的归档盒源码），
验证在候选源码上运行。样本只使用合成文件，不读取本人资料。

创建：
  PYTHONPATH=<baseline>/归档盒源码 python tests/stage4_upgrade.py \
      --mode create --root /tmp/stage4-upgrade

验证：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/stage4_upgrade.py \
      --mode verify --root /tmp/stage4-upgrade
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.append(SRC)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def prepare_env(root):
    appdata = os.path.join(root, "appdata")
    schemes = os.path.join(root, "schemes")
    recent = os.path.join(root, "recent_spaces.json")
    os.makedirs(appdata, exist_ok=True)
    os.makedirs(schemes, exist_ok=True)
    os.environ["ARCHIVEBOX_APP_DIR"] = appdata
    os.environ["ARCHIVEBOX_SCHEME_DIR"] = schemes
    os.environ["ARCHIVEBOX_RECENT_FILE"] = recent
    return appdata, schemes, recent


def create(root):
    prepare_env(root)
    from classification_config import (
        LongTermRule, default_config, save_config,
    )
    from executor import execute_plan
    from planner import (
        CourseRule, MODE_BY_COURSE_THEN_TYPE, generate_plan,
    )
    from preferences import (
        MATCH_DIR_SCOPE, SCOPE_GLOBAL, SCOPE_SPACE, SOURCE_USER,
        Preference, list_global_preferences, list_space_preferences,
        save_preference,
    )
    from protect_config import load_manual_protect, save_manual_protect
    import recent_spaces
    import scheme_store
    from scanner import scan_directory
    from storage import Storage

    space = os.path.join(root, "space")
    space_v1 = os.path.join(root, "space_v1")
    os.makedirs(space)
    os.makedirs(space_v1)
    files = {
        "课程/高等数学/笔记.txt": "高等数学 极限 导数\n",
        "课程/高等数学/习题.txt": "高等数学 第三章习题\n",
        "项目/周报/第1周.txt": "项目周报 第 1 周\n",
        "下载/其他.txt": "普通下载文件\n",
        "保护目录/内部.txt": "保护目录里的文件\n",
    }
    for rel, text in files.items():
        write(space, rel, text)
    write(space_v1, "下载/旧笔记.txt", "旧版本笔记\n")
    before = {
        rel: sha256(os.path.join(space, rel))
        for rel in files
    }

    cfg = default_config()
    cfg.long_term_rules = [LongTermRule(
        rule_id="lt-upgrade-001",
        name="升级空间规则",
        match_type=MATCH_DIR_SCOPE,
        source_path="课程/高等数学",
        direction="课程",
        semester="2026秋-大二上",
        category="高等数学",
        reason="阶段三升级样本",
    )]
    save_config(space, cfg)

    # 版本 1 配置：先写一份自定义配置，再改成 version 1、去掉长期规则。
    save_config(space_v1, cfg)
    cfg_path = os.path.join(space_v1, ".organizer", "classification_config.json")
    with open(cfg_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["version"] = 1
    raw.pop("long_term_rules", None)
    with open(cfg_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)

    space_pref = Preference(
        preference_id="lt-upgrade-space",
        name="升级空间偏好",
        match_type=MATCH_DIR_SCOPE,
        source_path="课程/高等数学",
        direction="课程",
        semester="2026秋-大二上",
        category="高等数学",
        scope=SCOPE_SPACE,
        space_root=space,
        reason="阶段三升级样本",
        confirmed_at="2026-11-19T00:00:00",
        source_kind=SOURCE_USER,
    )
    global_pref = Preference(
        preference_id="lt-upgrade-global",
        name="升级全局偏好",
        match_type=MATCH_DIR_SCOPE,
        source_path="项目/周报",
        direction="项目",
        category="周报",
        scope=SCOPE_GLOBAL,
        space_root="",
        reason="阶段三升级样本",
        confirmed_at="2026-11-19T00:00:00",
        source_kind=SOURCE_USER,
    )
    save_preference(space, space_pref)
    save_preference(space, global_pref)

    protected = os.path.join(space, "保护目录")
    save_manual_protect(space, [protected])
    scheme_store.save_scheme("升级方案", "阶段三升级样本", cfg)
    recent_spaces.touch(space, "升级空间")

    rules = [
        CourseRule(keywords=["高等数学"], course_name="高等数学"),
        CourseRule(keywords=["项目周报"], course_name="项目"),
    ]
    scan = scan_directory(space)
    plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, rules)
    for item in plan.items:
        item.selected = True
    result = execute_plan(plan, Storage(space))

    manifest = {
        "space": space,
        "space_v1": space_v1,
        "files_before": before,
        "batch": {
            "id": result.batch_id,
            "moved": result.moved,
            "skipped": result.skipped,
            "failed": result.failed,
        },
        "preferences_space": [p.preference_id for p in list_space_preferences(space)],
        "preferences_global": [p.preference_id for p in list_global_preferences()],
        "protect": load_manual_protect(space),
        "schemes": [s.name for s in scheme_store.list_schemes()],
        "recent": [s.path for s in recent_spaces.list_spaces()],
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def verify(root):
    prepare_env(root)
    from PySide6.QtWidgets import QApplication, QMessageBox
    import main as main_mod
    from classification_config import (
        ClassificationConfigError, load_config, save_config,
    )
    from executor import undo_latest_batch
    from preferences import (
        list_global_preferences, list_space_preferences,
    )
    from protect_config import load_manual_protect
    import recent_spaces
    import scheme_store
    from storage import Storage

    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)

    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    space = manifest["space"]
    space_v1 = manifest["space_v1"]

    checks = {}
    cfg, source = load_config(space)
    checks["config_v2"] = (
        cfg.version == 2
        and any(rule.rule_id == "lt-upgrade-001"
                for rule in cfg.long_term_rules))
    checks["preferences_space"] = (
        "lt-upgrade-space" in
        [p.preference_id for p in list_space_preferences(space)])
    checks["preferences_global"] = (
        "lt-upgrade-global" in
        [p.preference_id for p in list_global_preferences()])
    checks["protect"] = any("保护目录" in p for p in load_manual_protect(space))
    checks["scheme"] = "升级方案" in [s.name for s in scheme_store.list_schemes()]
    checks["recent"] = recent_spaces.get(space) is not None

    storage = Storage(space)
    batches = storage.list_batches(include_actions=True)
    done = [b for b in batches if b.status == "done"]
    checks["history_done"] = bool(done) and len(done[-1].actions) >= 1

    # 在副本上撤销，保留原始升级样本不被改动。
    copy_root = tempfile.mkdtemp(prefix="stage4_upgrade_copy_")
    copy_space = os.path.join(copy_root, "space")
    shutil.copytree(space, copy_space)
    undo = undo_latest_batch(Storage(copy_space))
    restored = all(
        os.path.isfile(os.path.join(copy_space, rel))
        and sha256(os.path.join(copy_space, rel)) == digest
        for rel, digest in manifest["files_before"].items())
    checks["undo"] = undo.restored > 0 and restored

    # 版本 1 配置：在副本上读取并升级，保留原始样本。
    v1_root = tempfile.mkdtemp(prefix="stage4_upgrade_v1_")
    v1_space = os.path.join(v1_root, "space_v1")
    shutil.copytree(space_v1, v1_space)
    v1_cfg_path = os.path.join(v1_space, ".organizer",
                               "classification_config.json")
    with open(v1_cfg_path, encoding="utf-8") as handle:
        v1_raw = json.load(handle)
    v1_raw["root"] = v1_space
    with open(v1_cfg_path, "w", encoding="utf-8") as handle:
        json.dump(v1_raw, handle, ensure_ascii=False, indent=2)
    cfg1, source1 = load_config(v1_space)
    checks["config_v1_read"] = source1 == "custom" and cfg1.version == 2
    save_config(v1_space, cfg1)
    cfg1b, _ = load_config(v1_space)
    checks["config_v1_upgraded"] = cfg1b.version == 2

    # 损坏配置：原文件保留，其他数据仍可读，恢复后可用。
    corrupt_root = tempfile.mkdtemp(prefix="stage4_upgrade_corrupt_")
    corrupt_space = os.path.join(corrupt_root, "space")
    shutil.copytree(space, corrupt_space)
    cfg_path = os.path.join(corrupt_space, ".organizer", "classification_config.json")
    with open(cfg_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["root"] = corrupt_space
    with open(cfg_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
    original = open(cfg_path, "rb").read()
    with open(cfg_path, "wb") as handle:
        handle.write(b"{not json")
    try:
        load_config(corrupt_space)
        corrupt_raises = False
    except ClassificationConfigError:
        corrupt_raises = True
    corrupt_kept = open(cfg_path, "rb").read() == b"{not json"
    others_readable = (
        "lt-upgrade-space" in
        [p.preference_id for p in list_space_preferences(corrupt_space)]
        and bool(Storage(corrupt_space).list_batches(include_actions=False)))
    window = main_mod.MainWindow()
    window._load_root_config(corrupt_space)
    ui_error = bool(window._config_error)
    window._load_root_config(space)
    ui_recovered = not window._config_error
    window._closing = True
    window.hide()
    window.deleteLater()
    with open(cfg_path, "wb") as handle:
        handle.write(original)
    recovered, _ = load_config(corrupt_space)
    checks["corrupt_recovery"] = (
        corrupt_raises and corrupt_kept and others_readable
        and ui_error and ui_recovered and recovered.version == 2)

    ok = all(checks.values())
    print(json.dumps({
        "ok": ok,
        "checks": checks,
        "config_source": source,
        "config_v1_source": source1,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="阶段三 → 阶段四升级样本")
    parser.add_argument("--mode", choices=["create", "verify"], required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    if args.mode == "create":
        if os.path.exists(args.root):
            shutil.rmtree(args.root)
        os.makedirs(args.root)
        return create(args.root)
    return verify(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
