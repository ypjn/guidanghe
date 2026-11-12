"""
归档盒 V1.2 第三轮 — 双击启动入口检查（独立进程）

验证 `启动归档盒.command`：
- L1: 脚本存在、可执行、zsh 语法检查通过
- L2: 缺源码目录时给出清楚说明并退出（不启动任何东西）
- L3: 缺虚拟环境时给出修复指引（不安装、不升级任何依赖）
- L4: 含中文与空格的路径下通过脚本真实启动主窗口（offscreen 子进程，
  存活即认为启动成功，随后终止）
- L5: requirements.txt 与当前实际环境一致（不随意升级依赖）

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_launch_entry.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
PROJECT = os.path.dirname(SRC)                 # aigc2 项目根
ENTRY = os.path.join(PROJECT, "启动归档盒.command")
BASE = "/tmp/_v12r3_launch_check"
RESULTS: list = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
    icon = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}[status]
    print(f"[{icon}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def run_script(path, timeout=15):
    """运行脚本副本，stdin 置空让 read 立即返回，捕获输出与退出码。"""
    try:
        p = subprocess.run(
            ["zsh", path], input="", capture_output=True, text=True,
            timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    t0 = time.time()

    # ---- L1: 存在、可执行、语法 ----
    ok_exec = (os.path.isfile(ENTRY) and os.access(ENTRY, os.X_OK))
    syn = subprocess.run(["zsh", "-n", ENTRY], capture_output=True)
    l1_ok = ok_exec and syn.returncode == 0
    rec("L1", "启动脚本存在、可执行、zsh 语法检查通过",
        "通过" if l1_ok else "未通过",
        f"路径={ENTRY}；可执行={ok_exec}；语法退出码={syn.returncode}")

    # ---- L2: 缺源码目录 → 清楚说明，不启动 ----
    d2 = os.path.join(BASE, "无源码")
    os.makedirs(d2)
    s2 = os.path.join(d2, "启动归档盒.command")
    shutil.copy2(ENTRY, s2)
    rc2, out2 = run_script(s2)
    l2_ok = (rc2 == 1 and "未找到源码目录" in out2
             and "归档盒源码" in out2)
    rec("L2", "缺源码目录：给出清楚说明并退出，不做任何安装/启动",
        "通过" if l2_ok else "未通过",
        f"退出码={rc2}；输出含「未找到源码目录」={ '未找到源码目录' in out2}")

    # ---- L3: 缺虚拟环境 → 修复指引，不自动安装 ----
    d3 = os.path.join(BASE, "无环境目录")
    os.makedirs(os.path.join(d3, "归档盒源码"))
    shutil.copy2(os.path.join(SRC, "main.py"),
                 os.path.join(d3, "归档盒源码", "main.py"))
    s3 = os.path.join(d3, "启动归档盒.command")
    shutil.copy2(ENTRY, s3)
    rc3, out3 = run_script(s3)
    l3_ok = (rc3 == 1 and "未找到虚拟环境解释器" in out3
             and "requirements.txt" in out3
             and "pip install" in out3)
    rec("L3", "缺虚拟环境：给出 venv+requirements 修复指引，不自动升级或安装",
        "通过" if l3_ok else "未通过",
        f"退出码={rc3}；指引包含安装命令={'pip install' in out3}")

    # ---- L4: 中文+空格路径下真实启动主窗口 ----
    # 源码做真实副本；.venv 用符号链接复用主环境（脚本语义即
    # “复用已验证的项目虚拟环境”，也避免复制数百 MB 环境）
    d4 = os.path.join(BASE, "试用 副本 目录")
    os.makedirs(d4)
    shutil.copy2(ENTRY, os.path.join(d4, "启动归档盒.command"))
    shutil.copytree(
        SRC, os.path.join(d4, "归档盒源码"),
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests", ".organizer", ".venv"))
    os.symlink(os.path.join(SRC, ".venv"),
               os.path.join(d4, "归档盒源码", ".venv"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen(
        ["zsh", os.path.join(d4, "启动归档盒.command")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, start_new_session=True)
    time.sleep(4.0)
    alive = proc.poll() is None
    out4 = ""
    if alive:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            out4 = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            out4 = proc.communicate()[0].decode("utf-8", "replace")
    else:
        out4 = proc.communicate()[0].decode("utf-8", "replace")
    rc4 = proc.returncode
    l4_ok = alive and "正在启动主窗口" in out4
    rec("L4", "中文+空格路径：通过脚本真实启动主窗口（offscreen 子进程存活 4s）",
        "通过" if l4_ok else "未通过",
        f"路径={d4}；进程存活={alive}；启动横幅可见={'正在启动主窗口' in out4}；"
        f"结束时退出码={rc4}")

    # ---- L5: requirements 与实际环境一致 ----
    req = open(os.path.join(SRC, "requirements.txt"), encoding="utf-8").read()
    ver = subprocess.run(
        [os.path.join(SRC, ".venv/bin/python"), "-c",
         "import PySide6;print(PySide6.__version__)"],
        capture_output=True, text=True).stdout.strip()
    l5_ok = ("PySide6==" + ver) in req
    rec("L5", "requirements.txt 声明与当前实际依赖一致（PySide6==" + ver + "）",
        "通过" if l5_ok else "未通过",
        f"实际版本={ver}；声明={'PySide6==' + ver in req}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "launch_entry_check",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python "
                   "tests/test_launch_entry.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "entry": ENTRY,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed, "fail": failed},
    }
    with open(os.path.join(here, "results_launch_entry.json"), "w",
              encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_launch_entry.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2 第三轮 — 双击启动入口检查\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"入口: {ENTRY}\n耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
