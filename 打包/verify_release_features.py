#!/usr/bin/env python3
"""用隔离数据目录验证「安装包应包含」的搜索、备份恢复与升级兼容。

不依赖 macOS / hdiutil。把源码目录当作包内 app 入口，用临时 ARCHIVEBOX_* 路径，
核对：
1. 搜索三份已知反例的正常行为
2. 两版备份与分别恢复
3. 索引 schema 2→3 升级后旧空间仍可查

退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "归档盒源码"
REPRO = ROOT / "docs/product-development/windows-handoff/2026-11-22/reproduce_search.py"


def main() -> int:
    if not (SRC / "main.py").is_file():
        print("找不到源码入口", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="archivebox-release-verify-"))
    results = {"work": str(work), "checks": {}}
    try:
        env = os.environ.copy()
        env["ARCHIVEBOX_APP_DIR"] = str(work / "appdata")
        env["ARCHIVEBOX_LOG_DIR"] = str(work / "logs")
        env["ARCHIVEBOX_INDEX_DB"] = str(work / "index.db")
        env["ARCHIVEBOX_RECENT_FILE"] = str(work / "recent.json")
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        py = sys.executable

        # 1) 搜索修复
        search = subprocess.run(
            [py, str(REPRO), "--source", str(SRC), "--output-dir", str(work / "search")],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        results["checks"]["search_exit"] = search.returncode
        results["checks"]["search_ok"] = search.returncode == 0
        if search.returncode != 0:
            results["checks"]["search_stderr"] = search.stderr[-2000:]

        # 2) 备份恢复
        backup = subprocess.run(
            [py, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_product_backup.py", "-v"],
            cwd=str(SRC), env=env, capture_output=True, text=True)
        results["checks"]["backup_exit"] = backup.returncode
        results["checks"]["backup_ok"] = backup.returncode == 0
        if backup.returncode != 0:
            results["checks"]["backup_stderr"] = (backup.stdout + backup.stderr)[-2000:]

        # 3) 升级兼容：volume schema 迁移 + 旧索引回归
        volume = subprocess.run(
            [py, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_product_volume.py", "-v"],
            cwd=str(SRC), env=env, capture_output=True, text=True)
        results["checks"]["upgrade_exit"] = volume.returncode
        results["checks"]["upgrade_ok"] = volume.returncode == 0
        if volume.returncode != 0:
            results["checks"]["upgrade_stderr"] = (volume.stdout + volume.stderr)[-2000:]

        # 4) 新模块可导入（模拟包内 hiddenimports）
        imports = subprocess.run(
            [py, "-c",
             "import backup_service, backup_store, volume_identity, "
             "search_session, content_search, index_store; "
             "from app_metadata import APP_VERSION; print(APP_VERSION)"],
            cwd=str(SRC), env=env, capture_output=True, text=True)
        results["checks"]["imports_exit"] = imports.returncode
        results["checks"]["imports_ok"] = imports.returncode == 0
        results["checks"]["app_version"] = (imports.stdout or "").strip()
        if imports.returncode != 0:
            results["checks"]["imports_stderr"] = imports.stderr[-2000:]

        ok = all(results["checks"].get(key) for key in (
            "search_ok", "backup_ok", "upgrade_ok", "imports_ok"))
        results["ok"] = ok
        out = work / "results.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"结果文件：{out}")
        return 0 if ok else 1
    finally:
        # 保留结果目录方便取证；调用方可自行清理 work。
        pass


if __name__ == "__main__":
    raise SystemExit(main())
