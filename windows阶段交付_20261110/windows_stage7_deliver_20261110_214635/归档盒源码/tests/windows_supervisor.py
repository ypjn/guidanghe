"""外层监督进程（包装器）。

职责：
- 顺序启动各测试套件为独立子进程（sys.executable，不依赖目录层级猜测）
- 记录 实际命令 / cwd / 隔离环境变量 / 起止时间 / 完整 stdout+stderr / 真实退出码 / 是否超时
- 读回子套件自己写的 results.json 做交叉校验，防止“脚本报成功但结果里有 FAIL”
- 汇总 JSON 由实际观测生成，不手填
用法：
    python tests/windows_supervisor.py --out <证据目录> [--only a,b]
退出码：全部子套件 returncode==0 且未超时 且 results.json 无 FAIL/BLOCK => 0，否则 1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TESTS = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    # (name, script, timeout_sec)
    ("patch_verify", "windows_patch_verify.py", 180),
    ("protect_paths", "windows_protect_paths.py", 240),
    ("harness_selftest", "windows_harness_selftest.py", 240),
    ("desktop_part1", "windows_desktop_full.py", 300),
    ("desktop_part2", "windows_desktop_full_part2.py", 300),
    ("launch_exit_10x", "windows_launch_exit_10x.py", 420),
    ("w2_1_cancel_close", "windows_w2_1_cancel_close.py", 480),
    ("w2_2_recovery", "windows_w2_2_recovery.py", 480),
    ("w2_3_config_isolation", "windows_w2_3_config_isolation.py", 420),
    ("s5_plan_scope", "windows_stage5_plan_scope.py", 1500),
    ("s5_names_occupancy", "windows_stage5_names_occupancy.py", 600),
]


def judge(rc: int, timed_out: bool, self_report, summary, results_present: bool):
    """统一判定规则（selftest 复用同一函数，避免两套逻辑分叉）。"""
    problems = []
    if not results_present:
        problems.append("缺少 results.json")
    if summary:
        if summary.get("FAIL", 0) or summary.get("BLOCK", 0):
            problems.append(f"results.json 含 FAIL={summary.get('FAIL')} "
                            f"BLOCK={summary.get('BLOCK')}")
        if summary.get("missing", 0):
            problems.append(f"必测项缺失 {summary.get('missing')}")
    if timed_out:
        problems.append("超时被强制终止")
    if rc != 0:
        problems.append(f"子进程退出码 {rc}")
    if self_report is not None and rc != 0 and self_report == 0:
        problems.append("脚本自报 0 但真实退出码非 0（假成功）")
    return ("PASS" if not problems else "FAIL"), problems


def run_one(name: str, script: str, timeout: int, out_root: str, base: str) -> dict:
    out_dir = os.path.join(out_root, name)
    os.makedirs(out_dir, exist_ok=True)
    script_path = os.path.join(TESTS, script)
    env = os.environ.copy()
    env.pop("QT_QPA_PLATFORM", None)
    env["ARCHIVEBOX_TEST_BASE"] = base
    env["ARCHIVEBOX_EVIDENCE"] = os.path.join(base, "evidence", name)
    env["ARCHIVEBOX_RESULTS_DIR"] = out_dir

    rec = {
        "suite": name,
        "script": script_path,
        "cmd": [sys.executable, script_path],
        "cwd": TESTS,
        "python": sys.executable,
        "timeout_sec": timeout,
        "env_isolated": {k: env.get(k) for k in
                         ("ARCHIVEBOX_TEST_BASE", "ARCHIVEBOX_EVIDENCE",
                          "ARCHIVEBOX_RESULTS_DIR", "QT_QPA_PLATFORM")},
        "start_utc": datetime.now(timezone.utc).isoformat(),
    }
    stdout_path = os.path.join(out_dir, "stdout.log")
    stderr_path = os.path.join(out_dir, "stderr.log")
    t0 = time.time()
    timed_out = False
    try:
        cp = subprocess.run(rec["cmd"], cwd=TESTS, env=env, capture_output=True,
                            text=True, timeout=timeout,
                            creationflags=(subprocess.CREATE_NO_WINDOW
                                           if sys.platform == "win32" else 0))
        rc = cp.returncode
        out, err = cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = -999
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")

    rec["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    rec["end_utc"] = datetime.now(timezone.utc).isoformat()
    rec["exit_code"] = rc
    rec["timed_out"] = timed_out
    # -999 是本框架的超时哨兵，不是操作系统退出码，必须分开表述
    rec["exit_code_kind"] = "timeout_sentinel" if timed_out else "os_exit_code"
    rec["os_exit_code"] = ("未采集（超时被强制终止）" if timed_out else rc)
    with open(stdout_path, "w", encoding="utf-8") as f:
        f.write(out)
    with open(stderr_path, "w", encoding="utf-8") as f:
        f.write(err)
    rec["stdout_log"] = stdout_path
    rec["stderr_log"] = stderr_path

    # 脚本自报退出码（不采信，仅记录并与真实退出码比对）
    self_report = None
    for line in reversed(out.splitlines()):
        if line.strip().startswith("EXIT_CODE="):
            try:
                self_report = int(line.strip().split("=", 1)[1])
            except ValueError:
                pass
            break
    rec["self_reported_exit_code"] = self_report if self_report is not None else "未采集"

    # 读回结构结果
    res_path = os.path.join(out_dir, "results.json")
    summary = None
    if os.path.isfile(res_path):
        with open(res_path, "r", encoding="utf-8") as f:
            res = json.load(f)
        s = res.get("summary", {})
        summary = {k: s.get(k) for k in
                   ("PASS", "FAIL", "BLOCK", "UNTESTED", "missing", "elapsed_sec")}
        rec["results_summary"] = summary
        rec["results_file"] = res_path

    verdict, problems = judge(rc, timed_out, self_report, summary,
                              os.path.isfile(res_path))
    rec["verdict"] = verdict
    rec["problems"] = problems
    if not os.path.isfile(res_path):
        rec["results_file"] = "未产出"
    with open(os.path.join(out_dir, "process.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(f"[supervisor] {name:26s} exit={rc} timeout={timed_out} "
          f"self_report={rec['self_reported_exit_code']} -> {rec['verdict']}")
    for p in problems:
        print(f"    ! {p}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    base = os.path.abspath(args.base or os.path.join(out_root, "runtime"))
    os.makedirs(base, exist_ok=True)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    suites = [s for s in SUITES if not only or s[0] in only]

    records = []
    t0 = time.time()
    for name, script, timeout in suites:
        print(f"\n===== {name} ({script}) =====")
        if not os.path.isfile(os.path.join(TESTS, script)):
            records.append({"suite": name, "script": script, "verdict": "FAIL",
                            "problems": ["脚本不存在"], "exit_code": "未采集",
                            "self_reported_exit_code": "未采集", "timed_out": False})
            print(f"[supervisor] {name} 脚本缺失")
            continue
        records.append(run_one(name, script, timeout, out_root, base))

    payload = {
        "start_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "python": sys.executable,
        "platform": sys.platform,
        "suites": records,
        "verdict": "PASS" if all(r["verdict"] == "PASS" for r in records) else "FAIL",
    }
    with open(os.path.join(out_root, "supervisor.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n===== supervisor 汇总 =====")
    for r in records:
        print(f"  {r['suite']:26s} {r['verdict']:5s} "
              f"exit={r.get('exit_code')} self={r.get('self_reported_exit_code')}")
    print(f"总判定: {payload['verdict']}")
    print(f"EXIT_CODE={0 if payload['verdict'] == 'PASS' else 1}")
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
