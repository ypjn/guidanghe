"""外层运行器：区分「断言统计」「脚本自报退出码」「操作系统真实退出码」。

问题背景：某些套件在打印“全部通过、exit_code=0”后，会在解释器退出阶段
发生 SIGSEGV，操作系统实际返回 139。只看脚本自报结果会漏掉这类失败，
因此用独立子进程运行并记录真实返回码。

用法：
  .venv/bin/python tests/run_isolated.py tests/test_v15_long_term_rules.py --repeat 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time


def parse_summary(text: str):
    """解析“合计 N 项：通过 A / 未通过 B / 未测 C”，以及脚本自报退出码。

    兼容两种写法：`；退出码 0` 与换行后的 `退出码: 0`。
    """
    m = re.search(
        r"合计\s*(\d+)\s*项：通过\s*(\d+)\s*/\s*未通过\s*(\d+)\s*/"
        r"\s*未测\s*(\d+)", text)
    if not m:
        return None
    e = re.search(r"退出码[:：]?\s*(-?\d+)", text)
    return {
        "total": int(m.group(1)),
        "pass": int(m.group(2)),
        "fail": int(m.group(3)),
        "untested": int(m.group(4)),
        "reported_exit_code": int(e.group(1)) if e else None,
    }


def run_once(script: str, index: int, log_dir: str):
    base = os.path.splitext(os.path.basename(script))[0]
    log_path = os.path.join(log_dir, f"{base}_run{index}.log")
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.run([sys.executable, script],
                              stdout=log, stderr=subprocess.STDOUT,
                              env=os.environ.copy())
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    summary = parse_summary(text)
    # “断言全过、脚本也自报 0，但操作系统返回非 0” = 退出阶段崩溃
    crash_at_exit = (proc.returncode != 0 and summary is not None
                     and summary["fail"] == 0
                     and summary.get("reported_exit_code") == 0)
    return {
        "run": index,
        "os_exit_code": proc.returncode,
        "elapsed_s": round(time.time() - started, 2),
        "log": log_path,
        "summary": summary,
        "crash_at_exit": crash_at_exit,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--log-dir", default="/tmp/archive_isolated")
    args = ap.parse_args()

    script = os.path.abspath(args.script)
    os.makedirs(args.log_dir, exist_ok=True)

    runs = []
    for i in range(1, args.repeat + 1):
        r = run_once(script, i, args.log_dir)
        runs.append(r)
        s = r["summary"] or {}
        print(f"run {i}: OS exit={r['os_exit_code']}  "
              f"断言 {s.get('pass', '?')}/{s.get('total', '?')}  "
              f"脚本自报退出码={s.get('reported_exit_code', '?')}  "
              f"log={r['log']}")

    bad = [r for r in runs if r["os_exit_code"] != 0]
    summary_fail = [r for r in runs
                    if not r["summary"] or r["summary"]["fail"] > 0]
    overall = 0 if not bad and not summary_fail else 1
    out = {
        "script": script,
        "repeat": args.repeat,
        "runs": runs,
        "all_os_exit_zero": not bad,
        "all_assertions_pass": not summary_fail,
        "overall_exit_code": overall,
    }
    out_path = os.path.join(
        args.log_dir, os.path.splitext(os.path.basename(script))[0] + "_runs.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n总体：OS 退出全为 0={not bad}；断言全部通过={not summary_fail}；"
          f"总体退出码={overall}")
    print(f"结果已保存: {out_path}")
    return overall


if __name__ == "__main__":
    sys.exit(main())
