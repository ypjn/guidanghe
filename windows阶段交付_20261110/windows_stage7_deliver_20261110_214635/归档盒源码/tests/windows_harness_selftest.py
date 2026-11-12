"""测试框架自检：失败注入，验证“失败一定会被判为失败”。

注入项与正常产品检查分开记录（fault_injection.json），
不计入正常通过率——它们的 PASS 含义是「判定逻辑正确识别了故障」。
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
from windows_supervisor import judge  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_selftest"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "selftest")
CH = rd.sub("children")
INJ = []

R = Results("harness_selftest")
print(f"=== harness selftest  run={rd.run_id} ===")


def inject(label, source, timeout=30, extra_env=None):
    rec = run_child(source, label, CH, BASE, timeout=timeout, extra_env=extra_env)
    INJ.append({k: rec[k] for k in ("label", "cmd", "cwd", "exit_code",
                                    "timed_out", "elapsed_ms", "stdout_log",
                                    "stderr_log")})
    return rec


def summarise(results_path):
    if not os.path.isfile(results_path):
        return None, False
    with open(results_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    s = data.get("summary", {})
    return ({k: s.get(k) for k in ("PASS", "FAIL", "BLOCK", "missing")}, True)


# ---- 1. 子套件内部 FAIL 必须导致非零退出 ----
res1 = os.path.join(CH, "inj1_results.json")
src1 = f"""
import sys
sys.path.insert(0, r"{TESTS}")
from windows_harness import Results
r = Results("injected_fail")
r.rec("I1", "故意失败项", "FAIL", "injected")
sys.exit(r.finalize(r"{res1}"))
"""
c1 = inject("inj1_recorded_fail", src1)
s1, has1 = summarise(res1)
R.check("H1", "子套件记录 FAIL 后 finalize 退出码非 0",
        c1["exit_code"] not in (0, -999) and s1 and s1["FAIL"] == 1,
        f"exit={c1['exit_code']} summary={s1}")

# ---- 2. 子进程未捕获异常必须非零 ----
src2 = """
raise RuntimeError("injected boom")
"""
c2 = inject("inj2_exception", src2)
R.check("H2", "子进程未捕获异常退出码非 0",
        c2["exit_code"] not in (0, -999) and "injected boom" in c2["stderr"],
        f"exit={c2['exit_code']}")

# ---- 3. 超时被判定为超时且不算通过 ----
src3 = """
import time
print("sleeping", flush=True)
time.sleep(8)
"""
c3 = inject("inj3_timeout", src3, timeout=2)
v3, p3 = judge(c3["exit_code"], c3["timed_out"], None, None, False)
R.check("H3", "子进程超时被标记 timed_out 且判定 FAIL",
        c3["timed_out"] is True and v3 == "FAIL",
        f"timed_out={c3['timed_out']} exit={c3['exit_code']} problems={p3}")

# ---- 4. 脚本自报 0 但结果有 FAIL（历史假成功模式）必须判 FAIL ----
res4 = os.path.join(CH, "inj4_results.json")
src4 = f"""
import sys
sys.path.insert(0, r"{TESTS}")
from windows_harness import Results
r = Results("injected_false_success")
r.rec("I4", "故意失败但退出 0", "FAIL", "injected false success")
r.finalize(r"{res4}")
print("EXIT_CODE=0")
sys.exit(0)
"""
c4 = inject("inj4_false_success", src4)
s4, has4 = summarise(res4)
v4, p4 = judge(c4["exit_code"], c4["timed_out"], 0, s4, has4)
R.check("H4", "脚本自报 0 但结果含 FAIL 被判 FAIL",
        c4["exit_code"] == 0 and v4 == "FAIL",
        f"exit={c4['exit_code']} verdict={v4} problems={p4}")

# ---- 5. 缺少 results.json 必须判 FAIL ----
src5 = """
print("EXIT_CODE=0")
"""
c5 = inject("inj5_no_results", src5)
v5, p5 = judge(c5["exit_code"], c5["timed_out"], 0, None, False)
R.check("H5", "缺少 results.json 被判 FAIL",
        v5 == "FAIL", f"verdict={v5} problems={p5}")

# ---- 6. 全 PASS 且退出 0 必须判 PASS（防止判定器永远 FAIL） ----
res6 = os.path.join(CH, "inj6_results.json")
src6 = f"""
import sys
sys.path.insert(0, r"{TESTS}")
from windows_harness import Results
r = Results("injected_all_pass")
r.rec("I6", "全部通过", "PASS")
sys.exit(r.finalize(r"{res6}"))
"""
c6 = inject("inj6_all_pass", src6)
s6, has6 = summarise(res6)
v6, p6 = judge(c6["exit_code"], c6["timed_out"], 0, s6, has6)
R.check("H6", "全 PASS 且退出 0 被判 PASS（判定器不恒定失败）",
        c6["exit_code"] == 0 and v6 == "PASS",
        f"exit={c6['exit_code']} verdict={v6}")

# ---- 7. 必测项缺失必须判 FAIL ----
res7 = os.path.join(CH, "inj7_results.json")
src7 = f"""
import sys
sys.path.insert(0, r"{TESTS}")
from windows_harness import Results
r = Results("injected_missing")
r.rec("I7", "只有一项", "PASS")
rc = r.finalize(r"{res7}", required_ids=["I7", "I7_MISSING"])
sys.exit(rc)
"""
c7 = inject("inj7_missing_required", src7)
s7, has7 = summarise(res7)
v7, p7 = judge(c7["exit_code"], c7["timed_out"], None, s7, has7)
R.check("H7", "必测项缺失被判 FAIL",
        c7["exit_code"] != 0 and s7 and s7["missing"] == 1,
        f"exit={c7['exit_code']} summary={s7} verdict={v7}")

# ---- 8. 唯一目录 + 所有权保护 ----
try:
    from windows_harness import safe_rmtree
    denied = False
    try:
        safe_rmtree(BASE, BASE)          # 固定 BASE 本身
    except RuntimeError:
        denied = True
    R.check("H8", "拒绝删除固定 BASE（所有权校验生效）", denied, f"BASE={BASE}")
except Exception as e:  # noqa: BLE001
    R.check("H8", "拒绝删除固定 BASE（所有权校验生效）", False, repr(e))

with open(os.path.join(OUT, "fault_injection.json"), "w", encoding="utf-8") as f:
    json.dump({"note": "故障注入证据：其 PASS 表示判定逻辑正确识别故障，不计入正常通过率",
               "children": INJ}, f, ensure_ascii=False, indent=2)

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"H{i}" for i in range(1, 9)]))
