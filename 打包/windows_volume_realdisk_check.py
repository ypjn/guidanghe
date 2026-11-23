#!/usr/bin/env python3
"""Windows 真盘只读探测与授权测试目录夹具（不写用户原有资料）。

用法（先由用户指定只读资料目录与可写测试目录）：

  .venv\\Scripts\\python.exe 打包\\windows_volume_realdisk_check.py ^
    --readonly "G:\\某资料夹" ^
    --testdir "D:\\归档盒 测试" ^
    --evidence docs\\product-development\\evidence\\batchB-win

在 --testdir 下创建 random-id 子目录，仅写入样本；不碰只读目录内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "归档盒源码")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _sha_tree(path: str) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            out[os.path.relpath(full, path).replace("\\", "/")] = h.hexdigest()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--readonly", required=True, help="用户资料只读目录")
    parser.add_argument("--testdir", required=True, help="可写测试父目录")
    parser.add_argument("--evidence", required=True, help="证据输出目录")
    parser.add_argument("--index", action="store_true", help="对测试样本做登记/索引/搜索")
    args = parser.parse_args()

    readonly = os.path.abspath(args.readonly)
    testdir = os.path.abspath(args.testdir)
    evidence = os.path.abspath(args.evidence)
    os.makedirs(evidence, exist_ok=True)

    if not os.path.isdir(readonly):
        print(f"只读目录不存在：{readonly}", file=sys.stderr)
        return 2
    if not os.path.isdir(testdir):
        print(f"测试目录不存在：{testdir}", file=sys.stderr)
        return 2

    from volume_identity import probe_volume

    before_readonly = _sha_tree(readonly)
    ro_info = probe_volume(readonly)
    test_info = probe_volume(testdir)

    run_id = uuid.uuid4().hex[:12]
    sample_root = os.path.join(testdir, f"archivebox-fixture-{run_id}")
    os.makedirs(sample_root, exist_ok=True)
    sample_file = os.path.join(sample_root, "中文 样本", "说明.txt")
    os.makedirs(os.path.dirname(sample_file), exist_ok=True)
    with open(sample_file, "w", encoding="utf-8") as f:
        f.write(f"夹具 {run_id}\n外置盘中文检索样本\n")

    result = {
        "when": datetime.now(timezone.utc).isoformat(),
        "os": sys.platform,
        "readonly": readonly,
        "readonly_volume": None if ro_info is None else {
            "volume_id": ro_info.volume_id,
            "filesystem": ro_info.filesystem,
            "label": ro_info.label,
            "dev": ro_info.dev,
        },
        "testdir": testdir,
        "testdir_volume": None if test_info is None else {
            "volume_id": test_info.volume_id,
            "filesystem": test_info.filesystem,
            "label": test_info.label,
            "dev": test_info.dev,
        },
        "sample_root": sample_root,
        "readonly_file_count_before": len(before_readonly),
        "index": None,
    }

    if args.index:
        os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(evidence, "appdata-" + run_id)
        os.environ["ARCHIVEBOX_INDEX_DB"] = os.path.join(
            os.environ["ARCHIVEBOX_APP_DIR"], "index", "index.db")
        from content_search import SearchQuery, search_files
        from index_service import register_space, update_space
        from index_store import IndexStore

        store = IndexStore()
        try:
            spec = register_space(sample_root, "真盘夹具", store=store)
            report = update_space(spec.space_id, store=store, read_content=True)
            page = search_files(
                store, SearchQuery(text="外置盘中文", space_ids=[spec.space_id]))
            result["index"] = {
                "space_id": spec.space_id,
                "volume_id": spec.volume_id,
                "coverage": report.coverage,
                "completed": report.completed,
                "errors": list(report.errors),
                "hits": [h.rel_path for h in page.hits],
            }
        finally:
            store.close()

    after_readonly = _sha_tree(readonly)
    result["readonly_unchanged"] = before_readonly == after_readonly
    result["readonly_file_count_after"] = len(after_readonly)

    out_path = os.path.join(evidence, f"realdisk-{run_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"evidence={out_path}")
    return 0 if result["readonly_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
