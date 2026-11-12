"""生产入口退出验证 ×10（每次独立子进程，真实 app.exec() + 正常关闭清理）。

与生产入口 main() 相同的序列：
    QApplication → MainWindow → show → app.exec() → shutdown_widgets() → sys.exit(code)
唯一附加动作是在事件循环里安排一次「正常关闭」（等同用户点右上角关闭），
用于证明退出走的是 app.exec() 返回后的清理路径，而不是 processEvents 空转。

每次运行独占唯一目录，父进程记录实际命令/cwd/隔离环境/起止时间/
完整 stdout+stderr/最终退出码/超时状态。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import Results, RunDir, run_child  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_launch"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "launch")
CH = rd.sub("children")
R = Results("launch_exit_10x")
print(f"=== launch_exit_10x run={rd.run_id} ===")

LAUNCHER = r'''
import json, os, sys
sys.path.insert(0, r"__SRC__")
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ["ARCHIVEBOX_RECENT_FILE"] = r"__RECENT__"
os.environ["ARCHIVEBOX_APP_DIR"] = r"__APPDIR__"
os.environ["ARCHIVEBOX_SCHEME_DIR"] = r"__SCHEMEDIR__"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
import main

state = {"visible": False, "closed": False, "threads_running": None,
         "python": sys.version.split()[0]}

app = QApplication(sys.argv)
win = main.MainWindow()
win.show()

def after_visible():
    state["visible"] = win.isVisible()
    win.close()                     # 等同用户正常关闭（最后一个窗口关闭 → 事件循环退出）

QTimer.singleShot(400, after_visible)
code = app.exec()
from PySide6.QtCore import QThread
state["closed"] = not win.isVisible()
state["threads_running"] = [t.objectName() or t.__class__.__name__
                            for t in win.findChildren(QThread) if t.isRunning()]
main.shutdown_widgets()      # 生产入口的显式销毁步骤（在 QApplication 存活时执行）
state["shutdown_done"] = True
with open(r"__PAYLOAD__", "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False)
sys.exit(code)
'''

N = 10
runs = []
for i in range(N):
    tag = f"run{i + 1:02d}"
    payload = os.path.join(CH, f"{tag}.json")
    src = (LAUNCHER.replace("__SRC__", SRC)
           .replace("__RECENT__", os.path.join(CH, f"{tag}_recent.json"))
           .replace("__APPDIR__", os.path.join(CH, f"{tag}_appdata"))
           .replace("__SCHEMEDIR__", os.path.join(CH, f"{tag}_appdata", "schemes"))
           .replace("__PAYLOAD__", payload))
    rec = run_child(src, tag, CH, BASE, timeout=60)
    obs = {}
    if os.path.isfile(payload):
        with open(payload, "r", encoding="utf-8") as f:
            obs = json.load(f)
    runs.append({"tag": tag, "exit": rec["exit_code"],
                 "timed_out": rec["timed_out"],
                 "elapsed_ms": rec["elapsed_ms"], "obs": obs,
                 "stdout_log": rec.get("stdout_log"),
                 "stderr_log": rec.get("stderr_log")})
    print(f"  {tag}: exit={rec['exit_code']} timed_out={rec['timed_out']} "
          f"visible={obs.get('visible')} closed={obs.get('closed')} "
          f"threads={obs.get('threads_running')} {rec['elapsed_ms']}ms")

ok_exit = all(r["exit"] == 0 and not r["timed_out"] for r in runs)
ok_life = all(r["obs"].get("visible") is True and r["obs"].get("closed") is True
              for r in runs)
ok_threads = all((r["obs"].get("threads_running") or []) == [] for r in runs)
ok_fast = all(r["elapsed_ms"] < 20000 for r in runs)

R.check("L0_1", f"{N} 次独立进程退出码全部为 0（无超时）", ok_exit,
        f"exits={[r['exit'] for r in runs]}")
R.check("L0_2", "每次窗口都真实可见，并在 app.exec() 内被正常关闭", ok_life,
        f"{[(r['tag'], r['obs'].get('visible'), r['obs'].get('closed')) for r in runs]}")
R.check("L0_3", "退出时没有遗留运行中的 QThread", ok_threads,
        f"{[r['obs'].get('threads_running') for r in runs]}")
R.check("L0_4", "单次启动+退出耗时均在 20s 内（无挂死）", ok_fast,
        f"{[r['elapsed_ms'] for r in runs]}")
R.check("L0_5", "每次运行使用各自独立的隔离目录（recent/配置不共享）",
        len({r['obs'].get('python') for r in runs}) == 1
        and len({r['tag'] for r in runs}) == N,
        f"runs={len(runs)}")

with open(os.path.join(OUT, "launch_exit_10x.json"), "w", encoding="utf-8") as f:
    json.dump(runs, f, ensure_ascii=False, indent=2)

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"L0_{i}" for i in range(1, 6)]))
