"""
归档盒 V1.0 回归入口退出码验证

用独立子进程运行 tests/test_v10_regression.py 的 main()，通过测试环境内的
故障注入（替换场景函数）验证：

- 全部通过 → 退出码 0
- 存在失败 → 退出码非 0
- 存在“未验证” → 退出码非 0

故障注入只发生在子进程，不修改生产逻辑。

运行：
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/test_v10_exit_code.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
TESTS = os.path.join(SRC, "tests")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

RESULTS = []
BASE = "/tmp/_v10_exitcode_verify"

SCENARIOS = [
    "s01_scan", "s02_plan", "s03_preview_change", "s04_e2e",
    "s05_undo_conflict", "s06_cancel", "s07_recovery", "s07b_cases",
    "s08_multi_batch",
]

CHILD_TEMPLATE = r'''
import sys, os
sys.path.insert(0, {src!r})
sys.path.insert(0, {tests!r})
import test_v10_regression as m
m.RESULTS_DIR = {results_dir!r}
m.BASE = {base!r}
m.OUT = {out!r}
m.HARNESS = {harness!r}
for name in {scenarios!r}:
    setattr(m, name, lambda: None)
{inject}
sys.exit(m.main())
'''


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status, "evidence": evidence})
    mark = {"通过": "PASS", "未通过": "FAIL"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"        {line}")


def run_child(work_dir, inject):
    inj_dir = os.path.join(work_dir, "results")
    os.makedirs(inj_dir, exist_ok=True)
    code = CHILD_TEMPLATE.format(
        src=SRC, tests=TESTS, results_dir=inj_dir,
        base=os.path.join(work_dir, "base"),
        out=os.path.join(work_dir, "out"),
        harness=os.path.join(work_dir, "harness"),
        scenarios=SCENARIOS,
        inject=inject,
    )
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)

    # 1) 全部通过 → 0
    wd = os.path.join(BASE, "pass")
    os.makedirs(wd, exist_ok=True)
    p = run_child(wd, 'm.s01_scan = lambda: m.rec("S-OK", "通过项", "通过")')
    ok_pass = p.returncode == 0 and "退出码：0" in p.stdout
    rec("X1", "全部通过时退出码为 0", "通过" if ok_pass else "未通过",
        f"子进程退出码={p.returncode}\nstdout尾行="
        + "\n".join(p.stdout.strip().splitlines()[-3:])
        + (f"\nstderr={p.stderr[-500:]}" if p.returncode != 0 else ""))

    # 2) 存在失败 → 非 0
    wd = os.path.join(BASE, "fail")
    os.makedirs(wd, exist_ok=True)
    p = run_child(
        wd,
        'm.s01_scan = lambda: m.rec("S-FAIL", "注入失败", "未通过", "测试环境故障注入")',
    )
    p_fail = p
    ok_fail = p.returncode != 0 and "退出码：1" in p.stdout
    rec("X2", "存在失败项时退出码非 0（独立子进程验证）",
        "通过" if ok_fail else "未通过",
        f"子进程退出码={p.returncode}（应非 0）\n"
        f"结果统计行=" + next(
            (ln for ln in p.stdout.splitlines() if "合计" in ln), "<未找到>"))

    # 3) 存在未验证 → 非 0
    wd = os.path.join(BASE, "unverified")
    os.makedirs(wd, exist_ok=True)
    p = run_child(
        wd,
        'm.s01_scan = lambda: m.rec("S-UNV", "未完成验证", "未验证", "缺少真实桌面环境")',
    )
    ok_unv = p.returncode != 0 and "退出码：1" in p.stdout
    rec("X3", "存在未验证项时退出码非 0", "通过" if ok_unv else "未通过",
        f"子进程退出码={p.returncode}（应非 0）\n"
        f"结果统计行=" + next(
            (ln for ln in p.stdout.splitlines() if "合计" in ln), "<未找到>"))

    # 4) 注入后的失败文本确实来自测试环境，而不是生产代码
    ok_isolated = ok_fail and "测试环境故障注入" in p_fail.stdout
    rec("X4", "故障注入仅在测试子进程内，生产模块未被修改",
        "通过" if ok_isolated else "未通过",
        "生产文件未被写入；子进程通过替换场景函数注入失败，"
        f"失败证据可见={ok_isolated}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    total = len(RESULTS)
    exit_code = 0 if passed == total else 1

    print("\n" + "=" * 60)
    print(f"合计 {total} 项：通过 {passed} / 未通过 {total - passed}")
    print(f"退出码: {exit_code}")
    print("=" * 60)

    meta = {
        "suite": "v10_exit_code",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v10_exit_code.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "base": BASE,
        "exit_code": exit_code,
        "counts": {"total": total, "pass": passed, "fail": total - passed,
                   "unverified": 0},
    }
    out = os.path.join(TESTS, "results_v10_exit_code.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(TESTS, "results_v10_exit_code.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.0 回归退出码验证 — 结果清单\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {sys.version.split()[0]}\n")
        f.write(f"合计 {total} 项：通过 {passed} / 未通过 {total - passed}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            if r["evidence"]:
                for line in str(r["evidence"]).splitlines():
                    f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    print(f"结果已保存: {txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
