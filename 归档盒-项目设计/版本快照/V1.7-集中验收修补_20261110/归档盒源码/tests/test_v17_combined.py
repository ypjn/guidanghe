"""
归档盒 V1.7 — 组合演练（offscreen，独立临时目录）

1. 打开测试空间；2. 加入部分文件与子目录；3. 应用方案；
4. 查看重复文件并保留副本；5. 筛选/修正分类并选择执行项；
6. 执行范围与实际移动一致；7. 重开历史查看规则依据；8. 撤销；
9. 独立枚举路径、内容 SHA、副本数量，确认范围外文件不变。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v17_combined.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from classification_config import LongTermRule, default_config, load_config, save_config
from duplicate_report import assemble_report, entries_from_plan, verify_entries
from executor import execute_plan, undo_latest_batch
from intake_model import IntakeList
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory
from storage import Storage
from utils import sha256_file
from plan_patch import apply_override_to_plan, OverrideParams

BASE = "/tmp/_v17_combined"
RESULTS = []


def rec(tid, title, ok, evidence=""):
    RESULTS.append({"id": tid, "status": "通过" if ok else "未通过",
                    "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def mk(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))
    return p


def tree_fingerprints(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root)
        for n in files:
            if n == "history.db" or rel_base.startswith(".organizer"):
                continue
            p = os.path.join(base, n)
            rel = os.path.normpath(os.path.join(rel_base, n))
            out[rel] = (os.path.getsize(p), sha256_file(p))
    return out


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    root = os.path.join(BASE, "space")
    mk(root, "included/a.txt", "DUP-CONTENT")
    mk(root, "included/sub/b.txt", "B-CONTENT")
    mk(root, "outside/c.txt", "DUP-CONTENT")
    mk(root, "untouched/d.txt", "UNTOUCHED")
    before_all = tree_fingerprints(root)

    # 1-2. 待整理范围：included 目录 + outside/c.txt
    intake = IntakeList()
    intake.add(os.path.join(root, "included"), root)
    intake.add(os.path.join(root, "outside", "c.txt"), root)

    # 3. 应用方案（长期规则）
    cfg = default_config()
    cfg.long_term_rules.insert(0, LongTermRule(
        rule_id="lt-combined", name="组合规则", match_type="dir_scope",
        source_path="included", include_subdirs=True,
        direction="项目", category="组合"))
    save_config(root, cfg)
    cfg_loaded, _ = load_config(root)

    # 扫描 + 范围筛选
    raw = scan_directory(root, detect_project_roots=False)
    resolution = intake.resolve(root, raw)
    effective_files = [f for f in raw.files if f.rel_path in resolution.included]
    effective = type(raw)(root=raw.root, files=effective_files)
    plan = build_plan(effective, MODE_BY_DIRECTION_SEMESTER, [],
                      config=cfg_loaded, compute_duplicates=True)
    plan_rels = {i.file.rel_path for i in plan.items}
    scope_ok = plan_rels == {"included/a.txt", "included/sub/b.txt",
                             "outside/c.txt"}

    # 4. 重复查看：保留副本
    entries = entries_from_plan(plan, None)
    verify_entries(entries)
    report = assemble_report(root, entries, scope_note="组合演练", source="plan")
    dup_group = report.groups[0] if report.groups else None
    copies_before = sum(1 for v in before_all.values()
                        if v[1] == sha256_file(
                            os.path.join(root, "included/a.txt")))
    dup_ok = (report.content_groups == 1 and report.content_files == 2
              and len(entries) == 3
              and copies_before == 2)

    # 5. 选择执行项 + 修正一个分类
    for item in plan.items:
        item.selected = item.file.rel_path != "included/sub/b.txt"
    apply_override_to_plan(plan, "outside/c.txt", OverrideParams(
        direction="竞赛", semester="", category="组合修正"))
    selected = [i for i in plan.items if i.selected]

    # 6. 执行
    storage = Storage(root)
    result = execute_plan(plan, storage)
    storage.close()
    moved_ok = (result.moved == 2 and len(selected) == 2)

    # 7. 重开历史
    st = Storage(root)
    acts = st.get_actions(result.batch_id)
    by_rel = {a.source_rel: a for a in acts}
    rules_ok = (
        by_rel.get("included/a.txt") is not None
        and by_rel["included/a.txt"].rule_key == "long_term:lt-combined"
        and by_rel.get("outside/c.txt") is not None
        and by_rel["outside/c.txt"].rule_key.startswith("manual_override:")
    )
    history_count = len(acts)
    st.close()

    # 范围外的 untouched/d.txt 不动、未选中的 b.txt 不动
    untouched_ok = (os.path.isfile(os.path.join(root, "untouched/d.txt"))
                    and os.path.isfile(os.path.join(root, "included/sub/b.txt")))

    # 8. 撤销
    undo = undo_latest_batch(Storage(root))
    after_all = tree_fingerprints(root)
    restored = all(after_all.get(rel) == fp for rel, fp in before_all.items())
    undo_ok = undo.is_complete and restored

    ok = (scope_ok and dup_ok and moved_ok and rules_ok
          and untouched_ok and history_count == 2 and undo_ok)
    rec("X01", "组合演练：范围→方案→重复→修正→执行→历史→撤销", ok,
        f"计划范围={sorted(plan_rels)} 范围正确={scope_ok}\n"
        f"重复组={report.content_groups} 份数={report.content_files} 重复检查={dup_ok}\n"
        f"选中={[i.file.rel_path for i in selected]} 移动={result.moved}\n"
        f"历史动作={history_count} 规则依据={rules_ok}\n"
        f"未纳入文件保持={untouched_ok} 撤销完成={undo.is_complete} "
        f"内容/路径恢复={restored}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 else 1
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_v17_combined.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"suite": "test_v17_combined",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "counts": {"total": len(RESULTS), "pass": passed,
                              "fail": failed},
                   "exit_code": exit_code, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；"
          f"退出码 {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
