"""S7-W2-3：超过 260 字符的路径（扫描 / 计划 / 移动 / 撤销全链路）。

约束与口径：
- 全部走产品代码路径：scanner.scan_directory / planner.generate_plan /
  executor.execute_plan / executor.undo_latest_batch（与真实界面同一套逻辑）。
- 长路径启用状态从注册表读取并记录（只记录，不修改系统设置）。
- 夹具创建（目录/文件）若普通 API 失败则改用扩展长度前缀 "\\\\?\\"（仅建
  夹具用，产品链路一律用普通路径，验证的是产品在长路径下的真实行为）。
- 撤销后用全量清单（路径+大小+SHA-256）与移动前逐项比对。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, run_child, full_manifest,  # noqa: E402
                             save_manifest, sha256_file)

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", os.getcwd()),
                                   "_abx_w2_longpath"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "w2_longpath")
CH = rd.sub("children")
R = Results("w2_longpath")
print(f"=== w2_longpath run={rd.run_id} ===")

import planner  # noqa: E402
from scanner import scan_directory  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from storage import Storage  # noqa: E402

# ---------------------------------------------------------------- 环境记录
import winreg  # noqa: E402

try:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
        lp, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
except OSError:
    lp = None
R.rec("LP0_1", f"Windows 长路径注册表 LongPathsEnabled = {lp}"
      "（1=已启用；0=未启用；仅记录，不修改）", "PASS",
      f"LongPathsEnabled={lp}；Python {sys.version.split()[0]}（python.exe "
      "清单声明 longPathAware）")

# ---------------------------------------------------------------- 夹具
root = rd.sub("lp_root")

# 构造 >260 字符的源路径：深层中文目录 + 长文件名
DEEP = "课程资料存档目录层级甲乙丙丁戊己庚辛壬癸" * 6   # 每层 20 字符（UTF-16 40 码元）
deep_rel = os.sep.join([DEEP[i * 20:(i + 1) * 20] for i in range(6)])
LONG_NAME = "超长路径原始讲义文件名_用于超过两百六十字符路径上限验证_VeryLongFileName.pdf"
long_rel = os.path.join(deep_rel, LONG_NAME)

short_rel = os.path.join("大二上", "数据结构", "对照短路径讲义.pdf")

DATA_LONG = b"LONG-PATH-CONTENT-W2"
DATA_SHORT = b"SHORT-PATH-CONTENT-W2"


def mkfile_force(rootp: str, rel: str, data: bytes):
    """普通 API 建夹具；失败时用扩展长度前缀 "\\\\?\\"（仅夹具，不进产品链路）。"""
    p = os.path.join(rootp, rel)
    parent = os.path.dirname(p)
    used = "normal"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    except OSError:
        used = "\\\\?\\ prefix"
        pp = "\\\\?\\" + os.path.abspath(p)
        os.makedirs(os.path.dirname(pp), exist_ok=True)
        with open(pp, "wb") as f:
            f.write(data)
    return p, used


long_abs, fixture_mode = mkfile_force(root, long_rel, DATA_LONG)
short_abs, _ = mkfile_force(root, short_rel, DATA_SHORT)

len_long = len(os.path.abspath(long_abs))
len_short = len(os.path.abspath(short_abs))
R.check("LP1_1", "长路径夹具已建立：源文件完整路径 > 260 字符，对照文件 < 260",
        len_long > 260 and len_short < 260,
        f"len(long)={len_long} len(short)={len_short} "
        f"fixture_mode={fixture_mode} long_rel_chars={len(long_rel)}")

want_sha_long = hashlib.sha256(DATA_LONG).hexdigest()
want_sha_short = hashlib.sha256(DATA_SHORT).hexdigest()

before = full_manifest(root)
save_manifest(os.path.join(OUT, "lp_manifest_before.json"), before,
              {"phase": "before_move", "long_abs": long_abs,
               "len_long": len_long})
R.check("LP1_2", "移动前全量清单含长路径文件与对照文件（路径键为相对路径）",
        len(before) == 2,
        f"files={sorted(before)}")

# ---------------------------------------------------------------- 扫描
sr = scan_directory(root)
found = {f.rel_path.replace("\\", "/") for f in sr.files}
long_rel_norm = long_rel.replace("\\", "/")
short_rel_norm = short_rel.replace("\\", "/")
R.check("LP2_1", "扫描：长路径文件被扫描器发现（>260 也可枚举）",
        long_rel_norm in found,
        f"scanned={sorted(found)} expect_long={long_rel_norm}")

# ---------------------------------------------------------------- 计划
plan = planner.generate_plan(sr, planner.MODE_BY_COURSE_THEN_TYPE,
                             planner.DEFAULT_COURSE_RULES)
srcs = [i.file.rel_path.replace("\\", "/") for i in plan.items]
R.check("LP3_1", "计划：包含长路径与对照共 2 项", len(plan.items) == 2,
        f"sources={srcs}")
long_item = next((i for i in plan.items
                  if i.file.rel_path.replace("\\", "/") == long_rel_norm), None)
tgt_long = (long_item.target_rel_path.replace("\\", "/")
            if long_item else "")
len_tgt = (len(os.path.abspath(os.path.join(root,
                                            long_item.target_rel_path)))
           if long_item else 0)
R.check("LP3_2", "计划：长路径文件获得 _已整理 下目标（记录目标路径长度）",
        long_item is not None and tgt_long.startswith("_已整理/"),
        f"target={tgt_long} len(target_abs)={len_tgt} "
        f"conflict={long_item.conflict_with_existing if long_item else None}")

# ---------------------------------------------------------------- 移动
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
by_src = {a.source_rel.replace("\\", "/"): (a.status, a.error) for a in acts}
R.check("LP4_1", "移动：两项动作记录与磁盘结果一致（长路径项实际成功或失败均如实记录）",
        res.moved + res.failed + res.skipped == 2
        and (os.path.isfile(long_abs) ^ bool(long_item and os.path.isfile(
            os.path.join(root, tgt_long.replace("/", os.sep)))))
        and (os.path.isfile(short_abs) ^ os.path.isfile(os.path.join(
            root, next(i.target_rel_path for i in plan.items
                       if i.file.rel_path.replace("\\", "/") == short_rel_norm)
            .replace("/", os.sep)))),
        f"summary={res.summary()} actions={by_src}")
R.check("LP4_2", "移动：长路径项已到达目标位置且 SHA-256 与移动前一致"
        "（若失败则本项 FAIL，错误串原样保留）",
        long_item is not None
        and os.path.isfile(os.path.join(root, tgt_long.replace("/", os.sep)))
        and sha256_file(os.path.join(root, tgt_long.replace("/", os.sep)))
        == want_sha_long
        and not os.path.exists(long_abs),
        f"target={tgt_long} status={by_src.get(long_rel_norm)} "
        f"src_still_exists={os.path.exists(long_abs)}")
tgt_short = next(i.target_rel_path for i in plan.items
                 if i.file.rel_path.replace("\\", "/") == short_rel_norm)
R.check("LP4_3", "移动：对照短路径项同样成功（区分列出）",
        os.path.isfile(os.path.join(root, tgt_short.replace("/", os.sep)))
        and sha256_file(os.path.join(root, tgt_short.replace("/", os.sep)))
        == want_sha_short,
        f"target={tgt_short} status={by_src.get(short_rel_norm)}")
st.close()

# ---------------------------------------------------------------- 撤销
st = Storage(root)
undo = undo_latest_batch(st)
after = full_manifest(root)
d = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
     if before.get(k) != after.get(k)}
R.check("LP5_1", "撤销：长路径 + 对照全部恢复，撤销后全量清单（路径+大小+SHA-256）"
        "与移动前完全一致",
        undo.restored == 2 and undo.conflict == 0 and undo.errors == 0
        and not d and os.path.isfile(long_abs)
        and sha256_file(long_abs) == want_sha_long
        and sha256_file(short_abs) == want_sha_short,
        f"undo={undo.summary()} diff={d}")
save_manifest(os.path.join(OUT, "lp_manifest_after_undo.json"), after,
              {"phase": "after_undo"})
st.close()

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["LP0_1", "LP1_1", "LP1_2", "LP2_1",
                                  "LP3_1", "LP3_2", "LP4_1", "LP4_2",
                                  "LP4_3", "LP5_1"]))
