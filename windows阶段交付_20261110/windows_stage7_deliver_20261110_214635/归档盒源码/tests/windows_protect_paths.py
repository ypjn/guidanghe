"""protect_config.normalize_manual_path 路径矩阵（修复前/后对照）。

覆盖：cwd≠root 的相对路径、绝对路径、~/路径、根目录自身、../越界、
不存在目录、重复项、大小写边界。

隔离：子进程把 USERPROFILE/HOME 指向本轮独立目录，不触碰真实用户配置；
基线模块只读导入（不改冻结基线）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import Results, RunDir, child_env  # noqa: E402

BASELINE_SRC = os.environ.get(
    "ARCHIVEBOX_BASELINE_SRC",
    r"C:\Users\Lenovo\Documents\Trae-CN\temp_20261110_01"
    r"\archivebox_v17_baseline_20261110"
    r"\ArchiveBox_V1.7_Windows_Handoff_20261110\归档盒源码")

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_protect"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "protect")
ROOT = rd.sub("root")
HOME_IN = os.path.join(ROOT, "userhome")          # ~ 展开落在 root 内
HOME_OUT = rd.sub("home_outside")                 # ~ 展开落在 root 外
CWD = rd.sub("elsewhere")                         # 故意 ≠ ROOT 的工作目录
CH = rd.sub("children")

for rel in ("大二上/高等数学", "大二上/数据结构", "keep", "照片 2026", ".organizer"):
    os.makedirs(os.path.join(ROOT, rel), exist_ok=True)
os.makedirs(os.path.join(HOME_IN, "project", "keep"), exist_ok=True)
os.makedirs(os.path.join(HOME_OUT, "project", "keep"), exist_ok=True)
os.makedirs(os.path.join(BASE, "outside"), exist_ok=True)

R = Results("protect_paths")
print(f"=== protect 路径矩阵 run={rd.run_id} ===")
print(f"ROOT={ROOT}\nHOME_IN={HOME_IN}\nHOME_OUT={HOME_OUT}\nCWD={CWD}")
print(f"BASELINE={BASELINE_SRC}")

CHILD = r'''
import importlib.util, json, os, sys
mod_path, ROOT, BASE = sys.argv[1], sys.argv[2], sys.argv[3]
cases = json.loads(sys.argv[4])
spec = importlib.util.spec_from_file_location("pc_under_test", mod_path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
out = {}
for name, values in cases.items():
    try:
        out[name] = {"ok": True, "paths": [m.normalize_manual_path(ROOT, v) for v in values]}
    except m.ProtectConfigError as e:
        out[name] = {"ok": False, "error": str(e)}
out["dedupe_same_two_forms"] = {
    "ok": True,
    "paths": m.dedupe_manual_paths(ROOT, ["大二上/高等数学", "./大二上/高等数学"]),
}
out["dedupe_repeat"] = {
    "ok": True,
    "paths": m.dedupe_manual_paths(ROOT, ["大二上/数据结构", "大二上/数据结构/."]),
}
print("RESULT_JSON=" + json.dumps(out, ensure_ascii=False))
'''


def exec_matrix(module_path: str, label: str, home: str) -> dict:
    script = os.path.join(CH, f"matrix_{label}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(CHILD)
    cases = {
        "relative_plain": ["keep"],
        "relative_chinese": ["大二上/高等数学"],
        "relative_space": ["照片 2026"],
        "absolute": [os.path.join(ROOT, "keep")],
        "home_expand": ["~/project/keep"],
        "root_itself": [ROOT],
        "escape_parent": ["../outside"],
        "not_exist": ["nope_dir"],
        "program_dir": [".organizer"],
    }
    env = child_env(BASE, {"USERPROFILE": home, "HOME": home,
                           "PYTHONIOENCODING": "utf-8"})
    cp = subprocess.run(
        [sys.executable, script, module_path, ROOT, BASE,
         json.dumps(cases, ensure_ascii=False)],
        cwd=CWD, env=env, capture_output=True, text=True, timeout=120,
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
    log = os.path.join(CH, f"matrix_{label}.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"cmd={[sys.executable, script]}\ncwd={CWD}\nHOME={home}\n"
                f"EXIT={cp.returncode}\n--- stdout ---\n{cp.stdout}\n"
                f"--- stderr ---\n{cp.stderr}")
    data = {}
    for line in cp.stdout.splitlines():
        if line.startswith("RESULT_JSON="):
            data = json.loads(line[len("RESULT_JSON="):])
    return {"exit": cp.returncode, "data": data, "log": log,
            "stderr": cp.stderr[-800:]}


fixed = exec_matrix(os.path.join(SRC, "protect_config.py"), "fixed", HOME_IN)
base = exec_matrix(os.path.join(BASELINE_SRC, "protect_config.py"), "baseline", HOME_IN)
fixed_out = exec_matrix(os.path.join(SRC, "protect_config.py"), "fixed_home_out", HOME_OUT)
fd, bd, fo = fixed["data"], base["data"], fixed_out["data"]

R.check("P1", "被测（修复后）模块导入并跑完矩阵",
        fixed["exit"] == 0 and bool(fd), f"exit={fixed['exit']} {fixed['stderr']}")
R.check("P2", "基线模块只读导入并跑完矩阵（冻结基线未改）",
        base["exit"] == 0 and bool(bd), f"exit={base['exit']} {base['stderr']}")
R.check("P2b", "第三个矩阵子进程（HOME 在 root 外）最终退出码为 0 且产出结果",
        fixed_out["exit"] == 0 and bool(fo),
        f"exit={fixed_out['exit']} keys={sorted(fo)} {fixed_out['stderr']}")

R.check("P3", "~/project/keep 在修复后仍被接受（expanduser 回归已恢复）",
        fd.get("home_expand", {}).get("ok") is True,
        f"fixed={fd.get('home_expand')} baseline={bd.get('home_expand')}")
if fd.get("home_expand", {}).get("ok"):
    got = fd["home_expand"]["paths"][0]
    expect = os.path.join(HOME_IN, "project", "keep")
    R.check("P4", "~/path 展开到 HOME 下真实目录",
            os.path.normcase(got) == os.path.normcase(expect),
            f"got={got} expect={expect}")

R.check("P5", "cwd≠root 时相对路径解析到整理 root 内",
        fd.get("relative_plain", {}).get("ok") is True
        and os.path.normcase(fd["relative_plain"]["paths"][0]) ==
        os.path.normcase(os.path.join(ROOT, "keep")),
        f"fixed={fd.get('relative_plain')} cwd={CWD}")
R.check("P6", "基线在 cwd≠root 时拒绝相对路径（原缺陷证据保留）",
        bd.get("relative_plain", {}).get("ok") is False,
        f"baseline={bd.get('relative_plain')}")
R.check("P7", "中文相对路径被接受且指向 root 内目录",
        fd.get("relative_chinese", {}).get("ok") is True
        and os.path.normcase(fd["relative_chinese"]["paths"][0]) ==
        os.path.normcase(os.path.join(ROOT, "大二上", "高等数学")),
        f"fixed={fd.get('relative_chinese')}")
R.check("P8", "含空格相对路径被接受",
        fd.get("relative_space", {}).get("ok") is True,
        f"fixed={fd.get('relative_space')}")
R.check("P9", "绝对路径被接受",
        fd.get("absolute", {}).get("ok") is True
        and os.path.normcase(fd["absolute"]["paths"][0]) ==
        os.path.normcase(os.path.join(ROOT, "keep")),
        f"fixed={fd.get('absolute')}")
R.check("P10", "根目录自身被拒绝",
        fd.get("root_itself", {}).get("ok") is False, f"{fd.get('root_itself')}")
R.check("P11", "../ 越界被拒绝",
        fd.get("escape_parent", {}).get("ok") is False, f"{fd.get('escape_parent')}")
R.check("P12", "不存在的目录被拒绝",
        fd.get("not_exist", {}).get("ok") is False, f"{fd.get('not_exist')}")
R.check("P13", "程序产物目录 .organizer 被拒绝",
        fd.get("program_dir", {}).get("ok") is False, f"{fd.get('program_dir')}")
R.check("P14", "同目录两种写法去重为 1",
        len(fd.get("dedupe_same_two_forms", {}).get("paths", [])) == 1,
        f"{fd.get('dedupe_same_two_forms')}")
R.check("P15", "重复路径（带 ./）去重为 1",
        len(fd.get("dedupe_repeat", {}).get("paths", [])) == 1,
        f"{fd.get('dedupe_repeat')}")
R.check("P16", "~ 展开到 root 外时被拒绝（root 边界未放宽）",
        fo.get("home_expand", {}).get("ok") is False,
        f"home_outside={HOME_OUT} result={fo.get('home_expand')}")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"P{i}" for i in range(1, 17)] + ["P2b"]))
