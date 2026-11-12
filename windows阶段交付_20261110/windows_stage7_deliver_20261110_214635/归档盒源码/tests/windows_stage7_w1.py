"""Stage7 W1 父套件：Windows 原生验收（真实 Qt 控件子进程 + 退出码复核）。

本轮范围（W1 已有功能，不新增产品功能）：
  1 长期规则与临时修正  -> s7_child_rules.py
  2 方案导入导出与回退  -> s7_child_scheme.py
  3 筛选、勾选与执行确认 -> s7_child_select.py
  4 最近空间/待整理清单与范围 -> s7_child_scope.py
  5 重复核对            -> s7_child_dupes.py
  6 历史导出            -> s7_child_history.py

父套件只做汇总与独立复核：重新读取子进程结果 JSON、重新核对退出码、
重新核对产物文件是否存在并计算 SHA-256。断言条数不等于功能覆盖数。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import windows_harness as H  # noqa: E402

SRC = os.path.dirname(HERE)
CHILD_DIR = os.path.join(HERE, "stage7_children")
BASE = os.environ.get("ARCHIVEBOX_TEST_BASE") or os.path.join(
    tempfile.gettempdir(), "archivebox_stage7_w1_" + time.strftime("%Y%m%d_%H%M%S"))
SUITE_OUT = os.path.join(BASE, "suite")
os.makedirs(SUITE_OUT, exist_ok=True)

CHILDREN = [
    ("rules", "长期规则与临时修正", 600,
     ["R1-", "R2-", "R3-", "R4-", "R5-", "R6-", "R7-", "R8-"]),
    ("scheme", "方案导入导出与回退", 600,
     ["S1-", "S2-", "S3-", "S4-", "S5-", "S6-", "S7-", "S8-"]),
    ("select", "筛选、勾选与执行确认", 600,
     ["C1-", "C2-", "C3-", "C4-", "C5-", "C6-"]),
    ("scope", "最近空间、待整理清单及范围", 600,
     ["Z1-", "Z2-", "Z3-", "Z4-", "Z5-", "Z6-", "Z7-"]),
    ("dupes", "重复核对", 600,
     ["D1-", "D2-", "D3-", "D4-", "D5-", "D6-", "D7-", "D8-"]),
    ("history", "历史导出", 600,
     ["H1-", "H2-", "H3-", "H4-", "H5-"]),
]


def child_source(label: str) -> str:
    path = os.path.join(CHILD_DIR, f"s7_child_{label}.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    results = H.Results("windows_stage7_w1")
    evidence = {"suite": "windows_stage7_w1", "base": BASE, "src": SRC,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "children": {}, "coverage_note":
                "断言条数不等于功能覆盖数；覆盖结论见交付报告的功能覆盖表。"}
    print(f"stage7 W1 父套件：base={BASE}")
    print(f"源码根：{SRC}")

    required = []
    for label, title, timeout, families in CHILDREN:
        out_dir = os.path.join(BASE, f"out_{label}")
        s7_out = os.path.join(out_dir, "result.json")
        extra = {
            "ARCHIVEBOX_SRC": SRC,
            "ARCHIVEBOX_TEST_BASE": BASE,
            "ARCHIVEBOX_RESULTS_DIR": out_dir,
            "S7_OUT": s7_out,
            "PYTHONPATH": CHILD_DIR,
            "PYTHONIOENCODING": "utf-8",
        }
        rec = H.run_child(child_source(label), label, out_dir, BASE,
                          timeout=timeout, extra_env=extra)
        parsed, parse_err = {}, ""
        raw = H.kv(rec.get("stdout", ""), "S7_RESULT")
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError as e:
                parse_err = f"结果 JSON 解析失败：{e}"
        else:
            parse_err = "stdout 中没有 S7_RESULT"

        checks = parsed.get("checks", []) if isinstance(parsed, dict) else []
        fails = [c for c in checks if not c.get("ok")]
        errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
        families_missing = [
            fam for fam in families
            if not any(str(c.get("id", "")).startswith(fam) for c in checks)
        ]

        # 独立复核：结果文件必须存在，且条数与 stdout 一致
        file_ok, file_checks = False, []
        if os.path.isfile(s7_out):
            try:
                with open(s7_out, "r", encoding="utf-8") as f:
                    file_checks = json.load(f).get("checks", [])
                file_ok = len(file_checks) == len(checks) and bool(checks)
            except ValueError:
                file_ok = False

        # 独立复核：产物文件与哈希（子进程也记录内联文本，如确认框文案）
        arts = {}
        raw_arts = parsed.get("artifacts", {}) if isinstance(parsed, dict) else {}
        for key, val in raw_arts.items():
            if isinstance(val, str) and os.path.isabs(val):
                exists = os.path.exists(val)
                if os.path.isdir(val):
                    arts[key] = {"path": val, "exists": exists, "kind": "dir",
                                 "entries": sorted(os.listdir(val))[:20]}
                else:
                    arts[key] = {"path": val, "exists": exists, "kind": "file",
                                 "sha256": H.sha256_file(val) if exists else None}
            else:
                arts[key] = {"inline": True, "preview": str(val)[:300]}
        bad_arts = [k for k, v in arts.items()
                    if "exists" in v and not v["exists"]]

        shots_dir = os.path.join(out_dir, "shots")
        shots = sorted(os.listdir(shots_dir)) if os.path.isdir(shots_dir) else []

        ok_child = (rec.get("exit_code") == 0 and not rec.get("timed_out")
                    and not parse_err and not fails and not errors
                    and bool(checks) and file_ok and not bad_arts
                    and not families_missing)
        tid = f"P-{label.upper()}"
        required.append(tid)
        results.check(
            tid, f"{title}：子进程 0 退出且全部子断言通过",
            ok_child,
            f"exit={rec.get('exit_code')} timed_out={rec.get('timed_out')} "
            f"checks={len(checks)} fails={[c['id'] for c in fails]} "
            f"errors={errors[:2]} 结果文件一致={file_ok} "
            f"缺产物={bad_arts} 缺覆盖族={families_missing} {parse_err}")

        # 每个功能面的子断言必须在结果文件里逐条为真（独立于 stdout 自报）
        results.check(
            f"{tid}-FAMILIES", f"{title}：覆盖族 {len(families)} 组且全部 PASS",
            not families_missing and all(c.get("ok") for c in file_checks),
            f"families={families} missing={families_missing} "
            f"file_checks={len(file_checks)}")

        results.check(
            f"{tid}-ARTIFACTS", f"{title}：清单/导出等产物落盘可复核",
            not bad_arts and bool(arts),
            f"artifacts={sorted(arts)} missing={bad_arts}")

        evidence["children"][label] = {
            "title": title,
            "exit_code": rec.get("exit_code"),
            "timed_out": rec.get("timed_out"),
            "elapsed_ms": rec.get("elapsed_ms"),
            "cmd": rec.get("cmd"),
            "env_isolated": rec.get("env_isolated"),
            "stdout_log": rec.get("stdout_log"),
            "stderr_log": rec.get("stderr_log"),
            "process_json": os.path.join(out_dir, f"{label}.process.json"),
            "result_json": s7_out,
            "checks": len(checks),
            "fails": [c["id"] for c in fails],
            "notes": parsed.get("notes", []) if isinstance(parsed, dict) else [],
            "artifacts": arts,
            "screenshots": shots,
        }

    required.append("P-EXITS")
    results.check_child_exits("P-EXITS",
                              title_extra="6 个子进程均为真实 OS 退出码 0")
    required.append("P-EVIDENCE")
    evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    evidence_path = os.path.join(SUITE_OUT, "stage7_evidence.json")
    with open(evidence_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)
    results.check("P-EVIDENCE", "同轮证据索引落盘并逐项计算 SHA-256",
                  os.path.isfile(evidence_path) and bool(evidence["children"]),
                  f"evidence={evidence_path} sha256={H.sha256_file(evidence_path)}")

    results_path = os.path.join(SUITE_OUT, "stage7_results.json")
    return results.finalize(results_path, required_ids=required)


if __name__ == "__main__":
    sys.exit(main())
