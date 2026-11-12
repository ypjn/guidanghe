"""S5-A/B：Windows 名称与路径边界 + 文件占用（小样本，独立合成目录）。

约束：
- 只用独立合成目录，不触碰真实资料，不展开全量规模。
- 名称部分先用产品现有规则（classification_config.validate_config /
  planner._sanitize_component / executor）跑一遍，再区分：
    · 输入验证层（配置校验拒绝）
    · 真实磁盘行为（Windows 实际能否创建）
- 占用部分由测试专属子进程用显式 Windows 共享模式
  （dwShareMode = FILE_SHARE_READ|FILE_SHARE_WRITE，不含 FILE_SHARE_DELETE）
  持有文件，父进程先用改名探测证明占用已建立，再执行整理。
- 只读属性单独记录，不当作权限测试。
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
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_s5ab"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "s5ab")
CH = rd.sub("children")
R = Results("s5_names_occupancy")
print(f"=== S5-A/B run={rd.run_id} ===")

import planner  # noqa: E402
from scanner import scan_directory  # noqa: E402
from planner import MODE_BY_COURSE_THEN_TYPE, CourseRule  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from storage import Storage  # noqa: E402
import classification_config as cc  # noqa: E402


# ------------------------------------------------------------------ 工具
def mkfile(root, rel, data):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def plan_of(root, rules, cfg=None):
    sr = scan_directory(root)
    rules = rules or planner.DEFAULT_COURSE_RULES
    return sr, planner.generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, rules,
                                     config=cfg)


def targets(plan):
    return [i.target_rel_path.replace("\\", "/") for i in plan.items]


def sources(plan):
    return [i.file.rel_path.replace("\\", "/") for i in plan.items]


# ================================================================ A. 名称边界
# ---- N1 中文 + 空格
root = rd.sub("n1")
mkfile(root, "大二上/数据结构/我的 讲义.pdf", "CN-SPACE")
_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
tgt = targets(plan)
R.check("N1_1", "中文+空格文件名：计划目标保留原名、位于 _已整理 下且不写出根目录",
        len(plan.items) == 1
        and tgt[0].startswith("_已整理/")
        and tgt[0].endswith("/文档/我的 讲义.pdf"),
        f"sources={sources(plan)} targets={tgt}")
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
abs_t = os.path.join(root, tgt[0].replace("/", os.sep)) if tgt else ""
src_abs = os.path.join(root, "大二上", "数据结构", "我的 讲义.pdf")
want_sha = hashlib.sha256(b"CN-SPACE").hexdigest()
R.check("N1_2", "中文+空格：执行后磁盘命中目标、历史记为 moved、无多余成功项",
        res.moved == 1 and res.failed == 0 and res.skipped == 0
        and bool(tgt) and os.path.isfile(abs_t)
        and sha256_file(abs_t) == want_sha
        and [a.status for a in acts] == ["moved"],
        f"moved={res.moved} failed={res.failed} skipped={res.skipped} "
        f"target_exists={bool(tgt) and os.path.isfile(abs_t)} "
        f"sha_ok={bool(tgt) and os.path.isfile(abs_t) and sha256_file(abs_t) == want_sha}")
R.check("N1_3", "中文+空格：源已不存在（同一份内容只在一个位置）",
        not os.path.exists(src_abs) and os.path.isfile(abs_t),
        f"src_exists={os.path.exists(src_abs)} target_exists={os.path.isfile(abs_t)}")
st.close()

# ---- N2 计划内同名目标碰撞
root = rd.sub("n2")
mkfile(root, "大二上/数据结构/甲/讲义.pdf", "CONTENT-A")
mkfile(root, "大二上/数据结构/乙/讲义.pdf", "CONTENT-B")
_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
tgt = targets(plan)
R.check("N2_1", "计划内同名目标碰撞：分配出两个互不相同的目标名",
        len(plan.items) == 2 and len(set(tgt)) == 2
        and os.path.basename(tgt[0]).startswith("讲义")
        and "(1)" in os.path.basename(tgt[1]),
        f"targets={tgt}")
st = Storage(root)
res = execute_plan(plan, st)
abs_targets = [os.path.join(root, t.replace("/", os.sep)) for t in tgt]
sha = sorted(sha256_file(p) for p in abs_targets if os.path.isfile(p))
want = sorted([__import__("hashlib").sha256(b"CONTENT-A").hexdigest(),
               __import__("hashlib").sha256(b"CONTENT-B").hexdigest()])
R.check("N2_2", "执行后两份不同内容都在，未互相覆盖",
        res.moved == 2 and sha == want,
        f"moved={res.moved} targets={abs_targets} sha_ok={sha == want}")
st.close()

# ---- N3 磁盘已存在同名目标
root = rd.sub("n3")
mkfile(root, "大二上/数据结构/讲义.pdf", "NEW")
_, plan_probe = plan_of(root, planner.DEFAULT_COURSE_RULES)
first_target = targets(plan_probe)[0]
pre = mkfile(root, first_target, "OLD-KEEP")
pre_sha = sha256_file(pre)
_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
tgt = targets(plan)
R.check("N3_1", "磁盘已有同名目标：计划改用 (1) 并标记 conflict_with_existing",
        len(plan.items) == 1 and "(1)" in os.path.basename(tgt[0])
        and plan.items[0].conflict_with_existing is True,
        f"probe_target={first_target} targets={tgt} "
        f"conflict={[i.conflict_with_existing for i in plan.items]}")
st = Storage(root)
res = execute_plan(plan, st)
R.check("N3_2", "执行后既有文件哈希未变，新文件落在 (1) 名下",
        res.moved == 1 and sha256_file(pre) == pre_sha
        and sha256_file(os.path.join(root, tgt[0].replace("/", os.sep))) ==
        __import__("hashlib").sha256(b"NEW").hexdigest(),
        f"moved={res.moved} pre_sha_same={sha256_file(pre) == pre_sha}")
st.close()

# ---- N4 大小写差异（Windows 文件系统大小写不敏感）
root = rd.sub("n4")
mkfile(root, "大二上/数据结构/甲/RePort.pdf", "CASE-UPPER")
mkfile(root, "大二上/数据结构/乙/report.pdf", "CASE-LOWER")
_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
tgt = targets(plan)
lower = [t.lower() for t in tgt]
R.check("N4_1", "大小写差异：计划给出两个仅大小写不同的目标名（记录实测，不做推断）",
        len(plan.items) == 2 and len(set(tgt)) == 2 and len(set(lower)) == 1,
        f"targets={tgt}")
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
one_dir = os.path.join(root, os.path.dirname(tgt[0]).replace("/", os.sep))
existing = [n for n in os.listdir(one_dir)] if os.path.isdir(one_dir) else []
upper_sha = __import__("hashlib").sha256(b"CASE-UPPER").hexdigest()
lower_sha = __import__("hashlib").sha256(b"CASE-LOWER").hexdigest()
present = []
for n in existing:
    present.append(sha256_file(os.path.join(one_dir, n)))
src_u = os.path.join(root, "大二上", "数据结构", "甲", "RePort.pdf")
src_l = os.path.join(root, "大二上", "数据结构", "乙", "report.pdf")
kept = []
for p, s in ((src_u, upper_sha), (src_l, lower_sha)):
    if os.path.isfile(p):
        kept.append(sha256_file(p))
R.check("N4_2", "大小写差异：未覆盖丢内容——目标仅一份，另一份内容仍在源位置",
        sorted(present + kept) == sorted([upper_sha, lower_sha])
        and len(existing) == 1,
        f"target_dir_files={existing} present={len(present)} kept={len(kept)} "
        f"moved={res.moved} skipped={res.skipped} failed={res.failed}")
skipped = [a for a in acts if a.status == "skipped"]
R.check("N4_3", "大小写差异：被跳过项在历史中如实记录（未记成功）且错误提示为目标被占用",
        res.moved == 1 and res.skipped == 1 and res.failed == 0
        and len(skipped) == 1 and "目标路径已被占用" in (skipped[0].error or ""),
        f"summary={res.summary()} skipped_error="
        f"{[a.error for a in skipped]}")
st.close()

# ---- N5 Windows 保留名称
# Microsoft 文档（Naming Files, Paths, and Namespaces）把 CON/PRN/AUX/NUL/COM1…
# 列为保留设备名；但“是否真能创建”必须在目标机器上实测，不能照抄文档。
# 下面把「输入验证」「计划层」「真实磁盘」三段分开记录。
root = rd.sub("n5")
mkfile(root, "大二上/数据结构/讲义.pdf", "RESERVED")
bad = os.path.join(root, "CON.txt")
disk_err = ""
created = False
try:
    with open(bad, "w", encoding="utf-8") as f:
        f.write("x")
    created = os.path.exists(bad)
    disk_err = "创建成功"
except OSError as e:
    disk_err = f"{type(e).__name__}: winerror={getattr(e, 'winerror', None)} {e}"
R.rec("N5_1",
      f"真实磁盘行为：CON.txt 在本机 {('可以' if created else '不能')}创建"
      f"（实测，非推断）", "PASS",
      f"created={created} detail={disk_err}；Microsoft 文档仍将其列为保留设备名，"
      f"故不同 Windows 版本可能不同，本机结论只对本机成立")
if created:
    os.remove(bad)   # 只保留“讲义.pdf”一个文件，便于下面按单条动作断言

rules_con = [CourseRule(keywords=["讲义"], course_name="CON", order=1)]
_, plan = plan_of(root, rules_con)
tgt = targets(plan)
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
src_ok = os.path.isfile(os.path.join(root, "大二上", "数据结构", "讲义.pdf"))
tgt_ok = bool(tgt) and os.path.isfile(os.path.join(root, tgt[0].replace("/", os.sep)))
honest_fail = (res.moved == 0 and res.failed == 1
               and len(acts) == 1 and acts[0].status == "failed"
               and src_ok and not tgt_ok)
honest_move = (res.moved == 1 and res.failed == 0 and tgt_ok and not src_ok)
R.check("N5_2", "保留名课程 CON：历史与磁盘一致——要么如实 failed 且源在目标不在，"
              "要么如实 moved 且目标在源不在；不出现“失败却记成功”或源/目标双丢",
        len(plan.items) == 1 and len(acts) == 1
        and (honest_fail or honest_move),
        f"observed={'failed' if honest_fail else 'moved'} targets={tgt} "
        f"summary={res.summary()} err={[a.error for a in acts]} "
        f"src_ok={src_ok} tgt_ok={tgt_ok}")
R.check("N5_3", "保留名场景未在整理根目录外写出任何文件",
        not any(n.upper().startswith("CON") for n in os.listdir(root)),
        f"root_entries={sorted(os.listdir(root))}")
st.close()

# ---- N6 非法字符 / 尾随点 / 尾随空白
root = rd.sub("n6")
mkfile(root, "大二上/数据结构/讲义.pdf", "ILLEGAL")
rules_il = [CourseRule(keywords=["讲义"], course_name='高数:实验?*"x|',
                       order=1)]
_, plan = plan_of(root, rules_il)
tgt = targets(plan)
R.check("N6_1", "课程名含非法字符：目标目录中的非法字符被替换为 _（计划层面）",
        len(tgt) == 1 and not any(c in tgt[0] for c in '<>:"|?*')
        and tgt[0].startswith("_已整理/高数")
        and "/文档/" in tgt[0],
        f"targets={tgt}")
st = Storage(root)
res = execute_plan(plan, st)
R.check("N6_1b", "非法字符课程名：真实磁盘创建目录并移动成功",
        res.moved == 1 and os.path.isfile(
            os.path.join(root, tgt[0].replace("/", os.sep))),
        f"moved={res.moved} failed={res.failed} targets={tgt}")
st.close()

root = rd.sub("n6b")
mkfile(root, "大二上/数据结构/讲义.pdf", "TRAILING-DOT")
rules_dot = [CourseRule(keywords=["讲义"], course_name="高数.", order=1)]
_, plan = plan_of(root, rules_dot)
tgt = targets(plan)
st = Storage(root)
res = execute_plan(plan, st)
R.check("N6_2", "课程名带尾随点：目标目录去掉尾随点并可真实创建",
        len(tgt) == 1 and "_已整理/高数/文档/" in tgt[0]
        and res.moved == 1
        and os.path.isfile(os.path.join(root, tgt[0].replace("/", os.sep))),
        f"targets={tgt} moved={res.moved} failed={res.failed}")
st.close()

cfg_space = cc.default_config()
cfg_space.semester_rules.append(cc.SemesterRule(source="测试来源Z", semester="测试来源Z "))
errs_space = cc.validate_config(cfg_space)
cfg_dot = cc.default_config()
cfg_dot.semester_rules.append(
    cc.SemesterRule(source="测试来源Z", semester="测试来源Z."))
errs_dot = cc.validate_config(cfg_dot)
cfg_reserved = cc.default_config()
cfg_reserved.semester_rules.append(
    cc.SemesterRule(source="测试来源Z", semester="CON"))
errs_reserved = cc.validate_config(cfg_reserved)
R.check("N6_3", "输入验证层：配置组件尾随空白被拒绝；尾随点与 Windows 保留名未被拒绝"
              "（后者只能在计划/执行层清理，已记录为已知限制）",
        any("首尾不能有空白" in e for e in errs_space)
        and not errs_dot and not errs_reserved,
        f"space_errors={errs_space} dot_errors={errs_dot} "
        f"reserved_errors={errs_reserved}")

root = rd.sub("n6c")
mkfile(root, "讲义.pdf", "ESCAPE")
rules_esc = [CourseRule(keywords=["讲义"], course_name="..", order=1)]
_, plan = plan_of(root, rules_esc)
tgt = targets(plan)
abs_ok = [os.path.abspath(os.path.join(root, t.replace("/", os.sep)))
          for t in tgt]
R.check("N6_4", "路径逃逸：课程名 .. 不会把目标写出整理根目录",
        len(tgt) == 1
        and all(os.path.commonpath([p, os.path.abspath(root)]) ==
                os.path.abspath(root) for p in abs_ok)
        and tgt[0].startswith("_已整理/"),
        f"targets={tgt}")

# ================================================================ B. 文件占用
root = rd.sub("occ")
mkfile(root, "大二上/数据结构/甲-未占用.pdf", "FREE")
held_rel = "大二上/数据结构/乙-被占用.pdf"
held_abs = mkfile(root, held_rel, "HELD")
before = full_manifest(root)
save_manifest(os.path.join(OUT, "occ_manifest_before.json"), before,
              {"phase": "before_hold"})

ready = os.path.join(CH, "occ_ready.json")
release = os.path.join(CH, "occ_release.flag")
HOLDER = r'''
import ctypes, json, os, sys, time
from ctypes import wintypes
path = r"__PATH__"
ready = r"__READY__"
release = r"__RELEASE__"
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p]
k32.CreateFileW.restype = ctypes.c_void_p
INVALID = ctypes.c_void_p(-1).value
h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None, OPEN_EXISTING, 0, None)
ok = h not in (None, INVALID)
info = {"handle_ok": bool(ok), "handle": hex(h) if ok else None,
        "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE (无 FILE_SHARE_DELETE)",
        "desired_access": "GENERIC_READ",
        "get_last_error": ctypes.get_last_error(),
        "pid": os.getpid()}
with open(ready, "w", encoding="utf-8") as f:
    json.dump(info, f, ensure_ascii=False)
if not ok:
    sys.exit(3)
deadline = time.time() + 90
while not os.path.exists(release) and time.time() < deadline:
    time.sleep(0.05)
k32.CloseHandle(ctypes.c_void_p(h))
sys.exit(0)
'''.replace("__PATH__", held_abs).replace("__READY__", ready).replace(
    "__RELEASE__", release)

import threading  # noqa: E402
rec_holder = {}


def _hold():
    rec_holder["rec"] = run_child(HOLDER, "occ_holder", CH, BASE, timeout=120)


th = threading.Thread(target=_hold, daemon=True)
th.start()

import time  # noqa: E402
t0 = time.time()
while time.time() - t0 < 60 and not os.path.exists(ready):
    time.sleep(0.05)
info = {}
if os.path.exists(ready):
    with open(ready, "r", encoding="utf-8") as f:
        info = json.load(f)
R.check("O1_1", "占用子进程已用显式共享模式（无 FILE_SHARE_DELETE）打开文件",
        bool(info.get("handle_ok")) and info.get("desired_access") == "GENERIC_READ",
        json.dumps(info, ensure_ascii=False))

probe = held_abs + ".__probe"
probe_err = ""
try:
    os.rename(held_abs, probe)
    probe_err = "改名成功（占用未建立）"
    os.rename(probe, held_abs)
except OSError as e:
    probe_err = f"{type(e).__name__}: winerror={getattr(e, 'winerror', None)} {e}"
R.check("O1_2", "父进程改名探测失败 → 占用已实际建立（可删/可改名被拒绝）",
        "占用未建立" not in probe_err and not os.path.exists(probe), probe_err)

_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
R.check("O2_1", "占用中执行：未占用项成功 1、占用项失败 1，失败未被记成成功",
        res.moved == 1 and res.failed == 1 and res.skipped == 0
        and sorted(a.status for a in acts) == ["failed", "moved"],
        f"summary={res.summary()} actions="
        f"{[(a.source_rel, a.status, a.error) for a in acts]}")
failed = [a for a in acts if a.status == "failed"]
R.check("O2_2", "占用项的历史错误串给出移动失败原因（非空）",
        len(failed) == 1 and "移动失败" in (failed[0].error or ""),
        f"error={[a.error for a in failed]}")
fail_tgt = (os.path.join(root, failed[0].target_rel.replace("/", os.sep))
            if failed else "")
occ_manifest = full_manifest(root)
save_manifest(os.path.join(OUT, "occ_manifest_after_fail.json"), occ_manifest,
              {"phase": "while_held", "batch": res.batch_id})
R.check("O2_3", "占用失败项：源仍在原位、目标路径不存在（失败不得留下副本/半成品）",
        len(failed) == 1 and os.path.isfile(held_abs)
        and bool(fail_tgt) and not os.path.exists(fail_tgt),
        f"held_exists={os.path.isfile(held_abs)} "
        f"target={failed[0].target_rel if failed else '?'} "
        f"target_exists={bool(fail_tgt) and os.path.exists(fail_tgt)}")
st.close()

with open(release, "w", encoding="utf-8") as f:
    f.write("release")
th.join(timeout=90)
rec_h = rec_holder.get("rec", {})
R.check("O3_1", "占用释放：子进程正常退出（exit=0）",
        rec_h.get("exit_code") == 0 and not rec_h.get("timed_out"),
        f"exit={rec_h.get('exit_code')} timeout={rec_h.get('timed_out')}")

st = Storage(root)
undo1 = undo_latest_batch(st)
after_undo1 = full_manifest(root)
d1 = {k: (before.get(k), after_undo1.get(k)) for k in set(before) | set(after_undo1)
      if before.get(k) != after_undo1.get(k)}
R.check("O4_1", "释放后按产品流程撤销该批次：恢复 1 项，清单与初始一致",
        undo1.restored == 1 and undo1.conflict == 0 and undo1.errors == 0
        and not d1,
        f"undo={undo1.summary()} diff={d1}")
st.close()

_, plan2 = plan_of(root, planner.DEFAULT_COURSE_RULES)
st = Storage(root)
res2 = execute_plan(plan2, st)
R.check("O5_1", "释放后完整流程：两项都移动成功",
        res2.moved == 2 and res2.failed == 0,
        f"summary={res2.summary()}")
undo2 = undo_latest_batch(st)
final = full_manifest(root)
d2 = {k: (before.get(k), final.get(k)) for k in set(before) | set(final)
      if before.get(k) != final.get(k)}
R.check("O5_2", "撤销后完整文件清单（含大小与 SHA-256）与初始完全一致",
        undo2.restored == 2 and undo2.conflict == 0 and undo2.errors == 0 and not d2,
        f"undo={undo2.summary()} diff={d2}")
save_manifest(os.path.join(OUT, "occ_manifest_final.json"), final,
              {"phase": "after_undo2"})
st.close()

# ---- O6 只读属性（单独记录，不作为权限测试）
root = rd.sub("ro")
p = mkfile(root, "大二上/数据结构/只读.pdf", "READONLY")
os.chmod(p, 0o444)
_, plan = plan_of(root, planner.DEFAULT_COURSE_RULES)
st = Storage(root)
res = execute_plan(plan, st)
moved = res.moved
R.rec("O6_1",
      "只读属性观察（单独记录，不等同于权限/ACL 测试）：实测 moved="
      f"{moved}, failed={res.failed}", "PASS" if moved + res.failed == 1 else "FAIL",
      f"summary={res.summary()}（Windows 同卷 os.rename 不受只读属性限制，"
      f"因此不能把只读属性当写权限测试）")
st.close()
try:
    os.chmod(os.path.join(root, "大二上", "数据结构"), 0o777)
except OSError:
    pass

R.check_child_exits("O_EXIT", title_extra="占用持有进程 occ_holder 必须 exit=0")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=["N1_1", "N1_2", "N1_3", "N2_1", "N2_2",
                                  "N3_1", "N3_2", "N4_1", "N4_2", "N4_3",
                                  "N5_1", "N5_2", "N5_3",
                                  "N6_1", "N6_1b", "N6_2", "N6_3", "N6_4",
                                  "O1_1", "O1_2", "O2_1", "O2_2", "O2_3",
                                  "O3_1", "O4_1", "O5_1", "O5_2", "O6_1",
                                  "O_EXIT"]))
