"""外层监督进程：启动被测子进程，保存完整 stdout/stderr/退出码/时间戳/环境。

用法：
    python tests/windows_runner_v2.py <测试脚本.py> [args...]

产出：
    <out_dir>/process.json    — 退出码、命令、cwd、env、时间戳
    <out_dir>/stdout.log      — 完整 stdout
    <out_dir>/stderr.log      — 完整 stderr
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def run(script: str, extra_args=None, env_overrides=None, timeout=None, out_dir=None) -> dict:
    extra_args = extra_args or []
    env_overrides = env_overrides or {}
    out_dir = out_dir or os.path.dirname(script)
    os.makedirs(out_dir, exist_ok=True)

    py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable

    env = os.environ.copy()
    env.update(env_overrides)
    env.pop("QT_QPA_PLATFORM", None)  # 强制原生

    cmd = [py, script] + list(extra_args)
    start_wall = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    try:
        proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(os.path.abspath(script)),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_bytes, stderr_bytes = proc.communicate()
        exit_code = proc.returncode
        timed_out = True

    elapsed = (time.time() - t0) * 1000.0
    end_wall = datetime.now(timezone.utc).isoformat()

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    with open(os.path.join(out_dir, "stdout.log"), "w", encoding="utf-8") as f:
        f.write(stdout)
    with open(os.path.join(out_dir, "stderr.log"), "w", encoding="utf-8") as f:
        f.write(stderr)

    # 保留关键 env 片段（脱敏）
    env_snapshot = {}
    for k in sorted(env.keys()):
        if any(x in k.upper() for x in ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")):
            v = env[k]
            if len(v) > 300:
                v = v[:300] + "...(truncated)"
            env_snapshot[k] = v

    record = {
        "script": script,
        "extra_args": extra_args,
        "cmd": cmd,
        "cwd": os.path.dirname(os.path.abspath(script)),
        "env_keys": sorted(env.keys()),
        "env_snapshot": env_snapshot,
        "start_wall_utc": start_wall,
        "end_wall_utc": end_wall,
        "elapsed_ms": round(elapsed, 1),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_chars": len(stdout),
        "stderr_chars": len(stderr),
    }
    with open(os.path.join(out_dir, "process.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    print(f"[runner] exit={exit_code} elapsed={elapsed:.0f}ms  stdout={len(stdout)}B  stderr={len(stderr)}B  timeout={timed_out}")
    return record


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: runner_v2.py <script> [args...]")
        sys.exit(2)
    rc = run(sys.argv[1], extra_args=sys.argv[2:], timeout=300)
    sys.exit(0 if rc["exit_code"] == 0 and not rc["timed_out"] else 1)
