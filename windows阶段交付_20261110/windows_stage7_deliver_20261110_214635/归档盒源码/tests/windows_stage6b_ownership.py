"""S6b：异常移动的安全边界（rename 失败即如实失败，不复制、不删除）。

本轮针对顾问 stage6 的两个发布后缺陷：
  P1 发布后目标被替换（同内容）→ 旧实现 _remove_owned(target, tmp_sha) 删除外部文件；
  P2 复制完成后源被改写 → 旧实现直接删源、返回成功，新修改丢失。

修复前反例用冻结副本 tests/frozen_executor_stage6.py（stage6 交付的 executor.py 原样）
复现；修复后行为由本套件 B1/B2/A1/A2/A3/A7/A8/A9/P3 断言。
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import (Results, RunDir, run_child, full_manifest,  # noqa: E402
                             save_manifest, load_manifest, diff_manifests,
                             sha256_file)

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_s6b"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

rd = RunDir(BASE, "s6b")
CH = rd.sub("children")
R = Results("s6b_ownership")
print(f"=== S6b run={rd.run_id} ===")

import planner  # noqa: E402
from scanner import scan_directory  # noqa: E402
from planner import MODE_BY_COURSE_THEN_TYPE  # noqa: E402
import executor as ex  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from storage import Storage  # noqa: E402


# ------------------------------------------------------------------ 工具
def mkfile(root, rel, data):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def sha(path):
    return sha256_file(path) if os.path.isfile(path) else None


def read(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def plan_of(root):
    sr = scan_directory(root)
    return planner.generate_plan(sr, MODE_BY_COURSE_THEN_TYPE,
                                 planner.DEFAULT_COURSE_RULES)


def leftovers(root):
    """任何以 .abx-copy- 开头的残留（旧实现会留，新实现不应产生）。"""
    out = []
    for dp, _dn, fn in os.walk(root):
        for name in fn:
            if name.startswith(".abx-copy-"):
                out.append(os.path.join(dp, name))
    return out


class OsProxy:
    def __init__(self, real, rename=None, remove=None):
        self._r = real
        self._rename = rename
        self._remove = remove

    def rename(self, a, b):
        if self._rename:
            return self._rename(a, b)
        return self._r.rename(a, b)

    def remove(self, p):
        if self._remove:
            return self._remove(p)
        return self._r.remove(p)

    def __getattr__(self, name):
        return getattr(self._r, name)


class ShutilProxy:
    def __init__(self, real, copyfileobj=None, move=None):
        self._r = real
        self._cb = copyfileobj
        self._mv = move

    def copyfileobj(self, fsrc, fdst, length=0):
        if self._cb:
            return self._cb(fsrc, fdst, length)
        return self._r.copyfileobj(fsrc, fdst, length)

    def move(self, a, b, *x, **k):
        if self._mv:
            return self._mv(a, b, *x, **k)
        return self._r.move(a, b, *x, **k)

    def __getattr__(self, name):
        return getattr(self._r, name)


@contextlib.contextmanager
def faults(mod, rename=None, remove=None, copyfileobj=None, move=None):
    real_os = mod.os
    had_shutil = hasattr(mod, "shutil")     # 新实现已不再 import shutil
    real_sh = getattr(mod, "shutil", None)
    mod.os = OsProxy(real_os, rename, remove)
    if had_shutil:
        mod.shutil = ShutilProxy(real_sh, copyfileobj, move)
    try:
        yield
    finally:
        mod.os = real_os
        if had_shutil:
            mod.shutil = real_sh


def load_frozen(filename, name):
    p = os.path.join(TESTS, filename)
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ================================================== 修复前反例（冻结 stage6 副本）
frozen = load_frozen("frozen_executor_stage6.py", "frozen_executor_stage6")

# ---- P1：发布成功后目标被替换成同内容新文件，源删除失败 → 外部文件被删
root = rd.sub("p1")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"original")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)
parked = src_abs + ".parked-by-external"


def p1_rename(a, b):
    if same(a, src_abs):
        raise OSError("injected rename failure")
    if same(b, tgt_abs) and os.path.basename(a).startswith(".abx-copy-"):
        os.rename(a, b)                                   # 本次发布成功
        os.replace(b, parked)                             # 外部把发布结果挪走
        with open(b, "wb") as f:                          # 外部在同路径写同内容新文件
            f.write(b"original")
        return
    return os.rename(a, b)


def p1_remove(p):
    if same(p, src_abs):
        raise OSError("source sharing violation")
    return os.remove(p)


st = Storage(root)
with faults(frozen, rename=p1_rename, remove=p1_remove):
    res = frozen.execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
p1 = {"moved": res.moved, "failed": res.failed,
      "target_exists": os.path.isfile(tgt_abs),
      "parked_exists": os.path.isfile(parked),
      "src_bytes": read(src_abs),
      "err": acts[0].error if acts else ""}
R.check("P1", "修复前反例：发布后目标被替换为同内容新文件，旧实现删除该外部文件",
        p1["failed"] == 1 and p1["target_exists"] is False
        and p1["parked_exists"] is True and p1["src_bytes"] == b"original"
        and "已撤回本次发布的目标副本" in p1["err"],
        json.dumps({k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                    for k, v in p1.items()}, ensure_ascii=False))
st.close()

# ---- P2：发布成功后、删源前源被改写 → 旧实现删掉新内容并返回成功
root = rd.sub("p2")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"original")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)


def p2_rename(a, b):
    if same(a, src_abs):
        raise OSError("injected rename failure")
    return os.rename(a, b)


def p2_remove(p):
    if same(p, src_abs):
        with open(p, "wb") as f:      # 复制完成、删源之前，源产生了新修改
            f.write(b"new-user-edit")
        os.remove(p)                  # 旧实现直接删除，新修改随之丢失
        return
    return os.remove(p)


st = Storage(root)
with faults(frozen, rename=p2_rename, remove=p2_remove):
    res = frozen.execute_plan(plan, st)
p2 = {"moved": res.moved, "failed": res.failed,
      "src_exists": os.path.isfile(src_abs),
      "tgt_bytes": read(tgt_abs)}
R.check("P2", "修复前反例：复制完成后源被改写，旧实现删掉新修改且仍记成功",
        p2["moved"] == 1 and p2["failed"] == 0 and p2["src_exists"] is False
        and p2["tgt_bytes"] == b"original",
        json.dumps({k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                    for k, v in p2.items()}, ensure_ascii=False))
st.close()


# ================================================== 修复后：失败即保留
# ---- B1：检查后外部写入同内容目标，rename 失败 → 外部文件保留、不删、不覆盖
root = rd.sub("b1")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"original")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)


def b1_rename(a, b):
    if same(a, src_abs):
        with open(b, "wb") as f:          # 外部在失败瞬间写入同内容目标
            f.write(b"original")
        raise OSError("injected rename failure")
    return os.rename(a, b)


st = Storage(root)
with faults(ex, rename=b1_rename):
    res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
b1 = {"moved": res.moved, "failed": res.failed,
      "src_bytes": read(src_abs), "tgt_bytes": read(tgt_abs),
      "leftovers": leftovers(root), "err": acts[0].error if acts else ""}
R.check("B1", "修复后：发布窗口不再存在——rename 失败即停，外部同内容目标保留、源保留、无残留",
        b1["moved"] == 0 and b1["failed"] == 1
        and b1["src_bytes"] == b"original" and b1["tgt_bytes"] == b"original"
        and b1["leftovers"] == []
        and "源文件保留在" in b1["err"]
        and "未由本次操作创建或改动" in b1["err"]
        and "本次未复制、未删除任何文件" in b1["err"],
        json.dumps({k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                    for k, v in b1.items()}, ensure_ascii=False))
st.close()

# ---- B2：失败之后源产生新修改 → 不被删除、不被误记成功
root = rd.sub("b2")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"original")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)


def b2_rename(a, b):
    if same(a, src_abs):
        with open(a, "ab") as f:          # 失败同时，外部改写了源
            f.write(b"|new-user-edit")
        raise OSError("injected rename failure")
    return os.rename(a, b)


st = Storage(root)
with faults(ex, rename=b2_rename):
    res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
b2 = {"moved": res.moved, "failed": res.failed, "src_bytes": read(src_abs),
      "tgt_exists": os.path.isfile(tgt_abs), "leftovers": leftovers(root),
      "err": acts[0].error if acts else ""}
R.check("B2", "修复后：源在失败期间被改写，内容完整保留且历史不记成功",
        b2["moved"] == 0 and b2["failed"] == 1
        and b2["src_bytes"] == b"original|new-user-edit"
        and b2["tgt_exists"] is False and b2["leftovers"] == []
        and "源文件保留在" in b2["err"],
        json.dumps({k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                    for k, v in b2.items()}, ensure_ascii=False))
st.close()


# ================================================== A1 真实 Windows 共享占用
HOLDER = r'''
import ctypes, os, sys, time
from ctypes import wintypes
path = os.environ["ABX_HOLD_PATH"]
ready = os.environ["ABX_HOLD_READY"]
release = os.environ["ABX_HOLD_RELEASE"]
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                          wintypes.HANDLE]
k.CreateFileW.restype = wintypes.HANDLE
h = k.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                  None, OPEN_EXISTING, 0, None)
if h == wintypes.HANDLE(-1).value or not h:
    print("HANDLE_FAIL", ctypes.get_last_error())
    sys.exit(2)
with open(ready, "w", encoding="utf-8") as f:
    f.write("ready")
t0 = time.time()
while not os.path.exists(release) and time.time() - t0 < 60:
    time.sleep(0.05)
k.CloseHandle(wintypes.HANDLE(h))
print("RELEASED")
sys.exit(0)
'''

root = rd.sub("a1")
held_abs = mkfile(root, "大二上/数据结构/乙-被占用.pdf", b"HELD-CONTENT")
held_rel = os.path.relpath(held_abs, root)
mkfile(root, "大二上/数据结构/甲-未占用.pdf", b"FREE-CONTENT")
plan = plan_of(root)
ready = os.path.join(rd.dir, "a1_ready.flag")
release = os.path.join(rd.dir, "a1_release.flag")
for p in (ready, release):
    if os.path.exists(p):
        os.remove(p)

th = threading.Thread(target=lambda: run_child(
    HOLDER, "a1_holder", CH, rd.dir, timeout=120,
    extra_env={"ABX_HOLD_PATH": held_abs, "ABX_HOLD_READY": ready,
               "ABX_HOLD_RELEASE": release}), daemon=True)
th.start()
t0 = time.time()
while not os.path.exists(ready) and time.time() - t0 < 20:
    time.sleep(0.05)
holder_started = os.path.exists(ready)
probe = ""
try:
    os.rename(held_abs, held_abs + ".probe")   # 占用成立时这里必须失败
    os.rename(held_abs + ".probe", held_abs)
except OSError as e:
    probe = f"winerror={getattr(e, 'winerror', None)} {e}"
R.check("A1_1", "占用反例前置：独立进程共享模式持有（无 FILE_SHARE_DELETE），改名探测被拒",
        holder_started and "winerror=32" in probe,
        f"holder_started={holder_started} probe={probe or '改名居然成功'}")

st = Storage(root)
held_sha = sha(held_abs)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
held_act = [a for a in acts if a.source_rel.replace("\\", "/") == held_rel.replace("\\", "/")]
held_err = held_act[0].error if held_act else ""
held_tgt = os.path.join(root, held_act[0].target_rel.replace("/", os.sep)) if held_act else ""
R.check("A1_2", "占用项：如实 failed，未记成功；源内容与哈希不变",
        len(held_act) == 1 and held_act[0].status == "failed"
        and sha(held_abs) == held_sha
        and "移动失败" in held_err,
        f"summary={res.summary()} err={held_err}")
R.check("A1_3", "占用项：目标未创建、无任何临时副本残留，历史写明源保留且未复制未删除",
        held_tgt and not os.path.exists(held_tgt) and leftovers(root) == []
        and "源文件保留在" in held_err
        and "本次未复制、未删除任何文件" in held_err,
        f"target_exists={bool(held_tgt) and os.path.exists(held_tgt)} "
        f"leftovers={leftovers(root)}")

with open(release, "w", encoding="utf-8") as f:
    f.write("go")
th.join(60)
R.check("A1_4", "占用持有进程收到释放信号后正常退出（exit=0）",
        not th.is_alive(), f"alive={th.is_alive()}")
st.close()


# ================================================== A2/A3 外部目标保留
for tag, ext_bytes, label in (("a2", b"original", "同内容"),
                              ("a3", b"EXTERNAL-DIFFERENT", "不同内容")):
    root = rd.sub(tag)
    src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"original")
    plan = plan_of(root)
    tgt_abs = os.path.join(root, plan.items[0].target_rel_path)

    def make_hook(src_abs=src_abs, ext_bytes=ext_bytes):
        def hook(a, b):
            if same(a, src_abs):
                with open(b, "wb") as f:
                    f.write(ext_bytes)
                raise OSError("injected rename failure")
            return os.rename(a, b)
        return hook

    st = Storage(root)
    with faults(ex, rename=make_hook()):
        res = execute_plan(plan, st)
    acts = st.get_actions(res.batch_id)
    err = acts[0].error if acts else ""
    ok = (res.failed == 1 and res.moved == 0
          and read(tgt_abs) == ext_bytes and read(src_abs) == b"original"
          and leftovers(root) == [] and "未由本次操作创建或改动" in err)
    R.check(tag.upper(), f"检查后外部创建{label}目标：不覆盖、不删除，源保留",
            ok,
            f"failed={res.failed} tgt_bytes={read(tgt_abs)!r} "
            f"src_bytes={read(src_abs)!r} err={err}")
    st.close()


# ================================================== A7 失败后新进程诊断
root = rd.sub("a7")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"NEWPROC-SRC")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)
before = full_manifest(root)
save_manifest(os.path.join(OUT, "a7_manifest_before.json"), before,
              meta={"root": root, "case": "a7"})

CHILD_EXEC = f'''
import json, os, sys
sys.path.insert(0, r"{SRC}")
sys.path.insert(0, r"{TESTS}")
import executor as ex
from storage import Storage
root = r"{root}"
src = r"{src_abs}"

class Proxy:
    def __init__(self, real):
        self._real = real
    def rename(self, a, b):
        if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(src)):
            raise OSError("injected rename failure (new process)")
        return self._real.rename(a, b)
    def __getattr__(self, name):
        return getattr(self._real, name)

ex.os = Proxy(os)
st = Storage(root)
import planner
from planner import MODE_BY_COURSE_THEN_TYPE
from scanner import scan_directory
p = planner.generate_plan(scan_directory(root), MODE_BY_COURSE_THEN_TYPE,
                          planner.DEFAULT_COURSE_RULES)
r = ex.execute_plan(p, st)
acts = st.get_actions(r.batch_id)
print(json.dumps({{"moved": r.moved, "failed": r.failed,
                  "status": acts[0].status, "err": acts[0].error}},
                 ensure_ascii=False))
st.close()
'''
CHILD_INSPECT = f'''
import json, os, sys
sys.path.insert(0, r"{SRC}")
from storage import Storage
root = r"{root}"
st = Storage(root)
bs = st.list_batches(include_actions=True)
acts = st.get_actions(bs[0].batch_id)
left = []
for dp, dn, fn in os.walk(root):
    for n in fn:
        if n.startswith(".abx-copy-"):
            left.append(os.path.join(dp, n))
print(json.dumps({{"batches": len(bs), "statuses": [a.status for a in acts],
                  "err": acts[0].error, "leftovers": left,
                  "src_exists": os.path.isfile(r"{src_abs}"),
                  "tgt_exists": os.path.isfile(r"{tgt_abs}")}},
                 ensure_ascii=False))
st.close()
'''
CHILD_UNDO = f'''
import json, sys
sys.path.insert(0, r"{SRC}")
from storage import Storage
import executor as ex
root = r"{root}"
st = Storage(root)
u = ex.undo_latest_batch(st)
print(json.dumps({{"restored": u.restored, "conflict": u.conflict,
                  "errors": u.errors,
                  "src_exists": __import__("os").path.isfile(r"{src_abs}"),
                  "tgt_files": [n for n in __import__("os").listdir(r"{root}")
                                if n.startswith(".abx-copy-")]}},
                 ensure_ascii=False))
st.close()
'''

rec = run_child(CHILD_EXEC, "a7_exec", CH, rd.dir, timeout=120)
exec_out = rec.get("stdout", "")
rec2 = run_child(CHILD_INSPECT, "a7_inspect", CH, rd.dir, timeout=120)
inspect = json.loads(rec2.get("stdout", "{}").strip().splitlines()[-1]) if rec2.get("stdout") else {}
rec3 = run_child(CHILD_UNDO, "a7_undo", CH, rd.dir, timeout=120)
undo_out = json.loads(rec3.get("stdout", "{}").strip().splitlines()[-1]) if rec3.get("stdout") else {}

R.check("A7_1", "新进程执行：失败如实落库，退出码 0，错误串含源保留说明",
        rec.get("exit_code") == 0 and '"failed": 1' in exec_out
        and "源文件保留在" in exec_out,
        exec_out.strip().splitlines()[-1] if exec_out else "no stdout")
R.check("A7_2", "另一新进程读取历史：可读到 failed 与完整错误依据，磁盘无临时残留",
        inspect.get("statuses") == ["failed"] and inspect.get("leftovers") == []
        and inspect.get("src_exists") is True and inspect.get("tgt_exists") is False
        and "移动失败" in (inspect.get("err") or ""),
        json.dumps(inspect, ensure_ascii=False)[:400])
R.check("A7_3", "新进程撤销：无可撤销项，源与目标状态不变，不静默删除任何文件",
        undo_out.get("restored") == 0 and undo_out.get("src_exists") is True
        and undo_out.get("tgt_files") == [] and rec3.get("exit_code") == 0,
        json.dumps(undo_out, ensure_ascii=False))


# ================================================== A8 正常整理与撤销
root = rd.sub("a8")
mkfile(root, "大二上/数据结构/讲义.pdf", b"NORMAL-1")
mkfile(root, "大二上/操作系统/实验.pdf", b"NORMAL-2")
plan = plan_of(root)
m0 = full_manifest(root)
st = Storage(root)
res = execute_plan(plan, st)
m1 = full_manifest(root)
d1 = diff_manifests(m0, m1)
R.check("A8_1", "正常整理：2 项 moved、无 failed、无临时文件，内容按清单落到新路径",
        res.moved == 2 and res.failed == 0 and leftovers(root) == []
        and len(d1["extra"]) == 2 and len(d1["missing"]) == 2
        and not d1["changed"],
        f"summary={res.summary()} diff={d1}")
ures = undo_latest_batch(st)
m2 = full_manifest(root)
d2 = diff_manifests(m0, m2)
R.check("A8_2", "撤销：2 项 restored，撤销后逐文件清单（含哈希）与初始完全一致",
        ures.restored == 2 and ures.conflict == 0 and ures.errors == 0
        and d2 == {"missing": [], "extra": [], "changed": []}
        and leftovers(root) == [],
        f"undo={ures.summary()} diff={d2}")
st.close()


# ================================================== A9 撤销 rename 失败
root = rd.sub("a9")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"UNDO-SRC")
plan = plan_of(root)
st = Storage(root)
res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
tgt_abs = os.path.join(root, acts[0].target_rel.replace("/", os.sep))
tgt_sha = sha(tgt_abs)


def a9_rename(a, b):
    if same(a, tgt_abs):
        raise OSError("injected undo rename failure")
    return os.rename(a, b)


with faults(ex, rename=a9_rename):
    ures = undo_latest_batch(st)
acts2 = st.get_actions(res.batch_id)
a9 = {"errors": ures.errors, "conflict": ures.conflict,
      "src_exists": os.path.isfile(src_abs),
      "tgt_exists": os.path.isfile(tgt_abs), "tgt_sha_same": sha(tgt_abs) == tgt_sha,
      "leftovers": leftovers(root), "status": acts2[0].status,
      "err": acts2[0].error}
R.check("A9", "撤销 rename 失败：目标与哈希不变、原始路径无半成品、conflict 且写明实际状态",
        a9["errors"] == 1 and a9["conflict"] == 0 and a9["src_exists"] is False
        and a9["tgt_exists"] and a9["tgt_sha_same"] and a9["leftovers"] == []
        and a9["status"] == "conflict" and "目标文件保留在" in a9["err"]
        and "原始路径未写入任何内容" in a9["err"],
        json.dumps(a9, ensure_ascii=False))
st.close()


# ================================================== P3 平台边界（无分支实现）
with open(os.path.join(SRC, "executor.py"), "r", encoding="utf-8") as f:
    code = f.read()
import re  # noqa: E402
bad = []
for pat in (r"(?m)^import shutil", r"shutil\.move\(", r"shutil\.copyfileobj\(",
            r"\.abx-copy-", r"TMP_PREFIX", r"os\.link\(", r"sys\.platform",
            r"os\.name\s*==", r"platform\.system"):
    if re.search(pat, code):
        bad.append(pat)
R.check("P3", "实现无平台分支：异常路径只用 rename 失败结果，不含 shutil 复制/临时文件/平台判断",
        bad == [],
        f"命中={bad}" if bad else "executor.py 中未出现 shutil 调用、临时副本前缀、os.link 或平台判断")

# 同一条异常路径用 PermissionError（更像 POSIX 的失败形态）再走一次
root = rd.sub("p3b")
src_abs = mkfile(root, "大二上/数据结构/讲义.pdf", b"POSIX-LIKE")
plan = plan_of(root)
tgt_abs = os.path.join(root, plan.items[0].target_rel_path)


def p3_rename(a, b):
    if same(a, src_abs):
        raise PermissionError(13, "Permission denied", b)
    return os.rename(a, b)


st = Storage(root)
with faults(ex, rename=p3_rename):
    res = execute_plan(plan, st)
acts = st.get_actions(res.batch_id)
R.check("P3b", "平台分支行为（模拟 POSIX 式失败，非原生 Mac）：同样失败即停、源保留、目标不创建",
        res.failed == 1 and read(src_abs) == b"POSIX-LIKE"
        and not os.path.isfile(tgt_abs) and leftovers(root) == []
        and "源文件保留在" in (acts[0].error or ""),
        f"summary={res.summary()} err={acts[0].error}")
st.close()


# ================================================== 已移除路径的记录
R.rec("R1", "stage6 的临时副本兜底用例（部分复制/清理失败/复制期源变化）已随实现删除，不再被覆盖",
      "UNTESTED",
      "旧实现与旧反例保留在 windows_stage6_deliver_20261110_195710"
      "（prefix_logs/results.json、tests/frozen_executor_stage6.py）；"
      "现实现不复制、不创建临时文件，这三条路径不存在，不伪造为已通过")

R.check_child_exits("S6B_EXIT",
                    title_extra="占用持有进程 a1_holder 与 a7 三个子进程必须 exit=0")

R.finalize(os.path.join(OUT, "results.json"),
           required_ids=["P1", "P2", "B1", "B2", "A1_1", "A1_2", "A1_3", "A1_4",
                         "A2", "A3", "A7_1", "A7_2", "A7_3",
                         "A8_1", "A8_2", "A9", "P3", "P3b", "R1", "S6B_EXIT"])
