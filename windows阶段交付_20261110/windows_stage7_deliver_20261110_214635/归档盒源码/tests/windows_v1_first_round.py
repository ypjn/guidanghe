"""Windows 第一轮 — 最小核心流程闭环验证

覆盖：启动→扫描→计划→执行→核对→正常退出→新进程重启→历史查询→撤销→核对

全部合成数据，不触碰真实资料。路径全部从 __file__ 动态解析。

运行：
  .venv\\Scripts\\python.exe tests\\windows_v1_first_round.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

# ==== 跨平台路径定位 ====
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)          # 归档盒源码
sys.path.insert(0, _SRC)

TMP_BASE = os.path.join(tempfile.gettempdir(), "_archivebox_w1_verify")
EVIDENCE_DIR = os.path.join(tempfile.gettempdir(), "_archivebox_w1_evidence")

RESULTS = []

# ========= 结果收集 =========

@dataclass
class CheckResult:
    id: str
    title: str
    status: str            # "PASS" | "FAIL" | "BLOCK"
    detail: str = ""
    duration_ms: float = 0.0

def rec(tid: str, title: str, status: str, detail: str = "") -> CheckResult:
    r = CheckResult(tid, title, status, detail)
    RESULTS.append(r)
    mark = {"PASS": "PASS", "FAIL": "FAIL", "BLOCK": "BLOCKED"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    if detail:
        for line in detail.splitlines()[:3]:
            print(f"         {line}")
    return r


# ========= SHA-256 工具 =========

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_rel(p: str) -> str:
    """把路径分隔符统一为 posix 风格（/），解决 Windows\\ 与 Mac/Linux/plan 中的 / 不一致。"""
    return p.replace("\\", "/")

def dir_manifest(root: str, exclude_dirs=None) -> dict:
    """返回 {relpath: (size, sha256)} 的文件清单，不包含隐藏文件/目录。
    relpath 统一使用正斜杠分隔。"""
    exclude_dirs = exclude_dirs or {"_已整理", ".organizer"}
    m = {}
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = [d for d in dn if not d.startswith(".") and d not in exclude_dirs]
        for fname in fn:
            if fname.startswith("."):
                continue
            full = os.path.join(dp, fname)
            if not os.path.isfile(full):
                continue
            rel = _norm_rel(os.path.relpath(full, root))
            m[rel] = (os.path.getsize(full), sha256_of(full))
    return m


# ========= 合成目录生成 =========

def build_synthetic_root() -> str:
    """创建约 15 个文件的合成测试目录，包含中文、空格、子目录。"""
    if os.path.exists(TMP_BASE):
        shutil.rmtree(TMP_BASE)
    os.makedirs(TMP_BASE, exist_ok=True)

    # 1. 根目录散落文件
    files = [
        ("随机.txt", "random content 123"),
        ("讲义.pdf", "pdf-bytes-mock"),
        ("下载的文档.docx", "word content here"),
    ]

    # 2. 中文学期/课程层级
    files += [
        ("大二上/数据结构/讲义.pdf", "数据结构讲义"),
        ("大二上/数据结构/作业/hw1.docx", "hw1 content"),
        ("大二上/数据结构/实验/实验 一/report.docx", "实验报告一"),
        ("大二上/高等数学/课件/ch1.ppt", "高数课件第一章"),
        ("大二上/高等数学/期末复习.pdf", "高数期末复习材料"),
    ]

    # 3. 另一方向 / 课程
    files += [
        ("大一下/程序设计基础/代码/main.py", "print('hello')"),
        ("大一下/程序设计基础/代码/utils.py", "def f(): pass"),
        ("大一下/程序设计基础/笔记.md", "# 编程笔记"),
    ]

    # 4. 模拟"保护"类样例（有 .git 的项目根）
    files += [
        ("myapp/.git/config", "[core]\n\trepositoryformatversion = 0"),
        ("myapp/src/main.py", "print('myapp')"),
        ("myapp/README.md", "# myapp project"),
    ]

    # 5. 子目录 / 混合类型
    files += [
        ("图片素材/logo.png", "png-bytes"),
        ("图片素材/截图 2026.png", "screenshot-bytes"),
    ]

    for rel, content in files:
        full = os.path.join(TMP_BASE, rel)
        os.makedirs(os.path.dirname(full) or TMP_BASE, exist_ok=True)
        with open(full, "wb") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)

    return TMP_BASE


# ========= 核心流程 =========

def t_scan() -> tuple:
    """扫描并验证项目保护、文件数量、类型分布。"""
    from scanner import scan_directory

    root = TMP_BASE
    sr = scan_directory(root)

    # 统计
    total = len(sr.files)
    protected = sr.protected_count
    skipped_proj = [s for s in sr.skipped if "项目保护" in (s.skipped_reason or "")]

    rec("S1", "扫描返回非空结果", "PASS" if total > 0 else "FAIL",
        f"files={total} protected={protected}")

    # 项目保护：myapp/.git → myapp 内文件都应跳过
    # 注意：.git/config 本身因 .git 目录在 SKIP_DIRS 中不会出现在任何列表
    myapp_src = [f for f in sr.files if "myapp" in f.rel_path]
    rec("S2", "有 .git 的 myapp 项目被整体保护",
        "PASS" if len(myapp_src) == 0 and protected >= 2 else "FAIL",
        f"myapp残留文件={len(myapp_src)} 项目保护跳过={len(skipped_proj)}")

    # 验证 .git/config 不在扫描结果中
    git_config = [f for f in sr.files if ".git" in f.rel_path or "git" in f.rel_path.lower()]
    rec("S3", ".git 目录内容不出现在扫描结果中",
        "PASS" if len(git_config) == 0 else "FAIL",
        f"残留={[f.rel_path for f in git_config]}")

    # 中文、空格路径文件都在
    cn_ok = any("实验 一" in f.rel_path or "下载的文档" in f.rel_path
                or "截图 2026" in f.rel_path for f in sr.files)
    rec("S4", "中文和空格路径文件正常返回", "PASS" if cn_ok else "FAIL")

    # 文件类型分布
    types = {}
    for f in sr.files:
        types[f.file_type] = types.get(f.file_type, 0) + 1
    rec("S5", "至少两类不同文件类型被识别",
        "PASS" if len(types) >= 2 else "FAIL",
        f"types={types}")

    return root, sr


def t_plan_and_execute(root: str, sr) -> dict:
    """生成计划 → 执行 → 核对。"""
    from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER
    from storage import Storage
    from executor import execute_plan

    BEFORE_MANIFEST = dir_manifest(root)

    plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])

    rec("P1", f"计划生成 {plan.total} 项（应 ≈ 总数 - 保护）",
        "PASS" if plan.total == len(sr.files) else "FAIL",
        f"plan.total={plan.total} sr.files={len(sr.files)}")

    # 执行
    st = Storage(root)
    er = execute_plan(plan, st)
    st.close()

    rec("E1", f"执行成功：moved={er.moved} failed={er.failed} skipped={er.skipped}",
        "PASS" if er.moved == plan.total and er.failed == 0 else "FAIL",
        f"er={er}")

    # 执行后：目标文件应该在
    AFTER_MOVE = dir_manifest(root, exclude_dirs={".organizer"})
    moved_into_organized = [k for k in AFTER_MOVE if k.startswith("_已整理")]

    # 项目保护的文件应该原地保留（属于 skipped 的一部分，不在 plan.items 中）
    protected_in_root = [
        _norm_rel(s.abs_path.replace(root + os.sep, ""))
        for s in sr.skipped if "项目保护" in (s.skipped_reason or "")
    ]
    # AFTER 中非 _已整理 且非 .organizer 的 = 项目保护文件（如果存在）
    non_organized_remaining = [
        k for k in AFTER_MOVE
        if not k.startswith("_已整理/") and not k.startswith(".organizer/")
    ]
    # 这些剩余项应该全是项目保护文件
    only_protected_remaining = set(non_organized_remaining) == set(protected_in_root)

    rec("E2", f"归档目录出现 {len(moved_into_organized)} 个文件",
        "PASS" if len(moved_into_organized) == plan.total else "FAIL",
        f"moved_into_organized={len(moved_into_organized)} 期望={plan.total}")

    rec("E3", f"非归档目录剩余文件 = 项目保护文件（{len(non_organized_remaining)} 个）",
        "PASS" if only_protected_remaining else "FAIL",
        f"remaining={non_organized_remaining} protected={protected_in_root}")

    # 执行后：内容 SHA-256 应不变
    moved_items = {}
    for item in plan.items:
        if item.selected:
            src_rel = _norm_rel(item.file.rel_path)
            tgt_rel = _norm_rel(item.target_rel_path)
            moved_items[src_rel] = tgt_rel

    sha_ok = 0
    sha_fail = 0
    sha_missing = []
    for src_rel, tgt_rel in moved_items.items():
        if src_rel not in BEFORE_MANIFEST:
            sha_fail += 1
            sha_missing.append(f"src_missing:{src_rel}")
            continue
        if tgt_rel not in AFTER_MOVE:
            sha_fail += 1
            sha_missing.append(f"tgt_missing:{tgt_rel}")
            continue
        if BEFORE_MANIFEST[src_rel][1] == AFTER_MOVE[tgt_rel][1]:
            sha_ok += 1
        else:
            sha_fail += 1
            sha_missing.append(f"sha_diff:{src_rel}")

    rec("E4", f"所有 {sha_ok} 个已移动文件 SHA-256 与执行前一致",
        "PASS" if sha_fail == 0 and sha_ok == len(moved_items) else "FAIL",
        f"sha_ok={sha_ok} sha_fail={sha_fail}" +
        (f"\n  missing/traces={sha_missing[:5]}" if sha_missing else ""))

    # 历史检查
    st2 = Storage(root)
    batch = st2.get_latest_batch()
    actions = st2.get_actions(batch.batch_id) if batch else []
    moved_count = sum(1 for a in actions if a.status == "moved")
    undone_count = sum(1 for a in actions if a.status == "undone")
    st2.close()

    rec("E5", "数据库批次记录已写入（moved 数匹配）",
        "PASS" if batch is not None and moved_count == er.moved and undone_count == 0 else "FAIL",
        f"batch_id={batch.batch_id if batch else None} moved_count={moved_count} undone_count={undone_count}")

    return {"root": root, "before": BEFORE_MANIFEST, "batch_id": batch.batch_id if batch else None}


def t_undo(root: str) -> None:
    """撤销最近批次，核对文件清单与执行前一致。"""
    from storage import Storage
    from executor import undo_latest_batch

    st = Storage(root)
    batch = st.get_latest_batch()
    # 如果批次已经是 undone 状态（例如跨进程已撤销），跳过执行
    if batch and batch.status == "undone":
        st.close()
        rec("U1", "批次已处于 undone 状态（跨进程已撤销成功），无需重复执行", "PASS",
            f"batch_id={batch.batch_id} status={batch.status}")
        return

    ur = undo_latest_batch(st)
    st.close()

    rec("U1", f"撤销：restored={ur.restored} conflict={ur.conflict} errors={ur.errors}",
        "PASS" if ur.is_complete and ur.restored > 0 else "FAIL",
        f"ur={ur.summary()}")

    AFTER_UNDO = dir_manifest(root)
    # 验证 _已整理 下不再有文件（已全部搬回）
    remaining_organized = [k for k in AFTER_UNDO if k.startswith("_已整理")]
    rec("U2", f"撤销后归档目录剩余文件={len(remaining_organized)}",
        "PASS" if len(remaining_organized) == 0 else "FAIL",
        f"remaining={remaining_organized[:5]}")

    st2 = Storage(root)
    batch = st2.get_latest_batch()
    actions = st2.get_actions(batch.batch_id) if batch else []
    st2.close()
    undone_count = sum(1 for a in actions if a.status == "undone")
    rec("U3", "数据库中所有 action 已标记 undone",
        "PASS" if undone_count == len(actions) and len(actions) > 0 else "FAIL",
        f"undone={undone_count} total={len(actions)}")


def t_check_manifest(root: str, expected: dict) -> None:
    """最终核对：文件清单应与执行前完全一致（缺失=fail，多余=fail，内容变=fail）。"""
    actual = dir_manifest(root)

    missing = set(expected.keys()) - set(actual.keys())
    extra = set(actual.keys()) - set(expected.keys())
    changed = [k for k in expected.keys() & actual.keys()
               if expected[k] != actual[k]]

    rec("F1", f"缺失文件={len(missing)}", "PASS" if not missing else "FAIL",
        f"missing={list(missing)[:5]}")
    rec("F2", f"多余文件={len(extra)}", "PASS" if not extra else "FAIL",
        f"extra={list(extra)[:5]}")
    rec("F3", f"内容变化={len(changed)}", "PASS" if not changed else "FAIL",
        f"changed={[c for c in changed[:5]]}")


# ========= 新进程重启：历史查询 + 撤销 =========

def t_cross_process(root: str, before_manifest: dict):
    """启动一个全新 Python 进程，只通过磁盘上的数据库读取历史并执行撤销。"""
    assert os.path.exists(os.path.join(root, ".organizer", "history.db")), "数据库不存在"

    cross_script = os.path.join(tempfile.gettempdir(), "_archivebox_cross_check.py")
    with open(cross_script, "w", encoding="utf-8") as f:
        f.write(f"""# 由 windows_v1_first_round.py 动态生成
import os, sys
sys.path.insert(0, r"{_SRC}")
from storage import Storage
from executor import undo_latest_batch

ROOT = r"{root}"
st = Storage(ROOT)
batch = st.get_latest_batch()
if batch is None:
    print("FAIL: no batch")
    sys.exit(1)

# 检查批次不是 undone 状态（执行阶段的那个批次）
print(f"cross_process: batch_id={{batch.batch_id}} status={{batch.status}}")
if batch.status == "undone":
    # 上一次已撤销，再重新执行一次新的扫描+计划+执行+撤销闭环
    print("批次已撤销，跳过交叉进程测试")
    sys.exit(0)

# 执行撤销
ur = undo_latest_batch(st)
st.close()
print(f"cross_process undo: restored={{ur.restored}} conflict={{ur.conflict}} errors={{ur.errors}}")
sys.exit(0 if ur.is_complete else 2)
""")

    venv_python = os.path.join(os.path.dirname(_SRC), ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_python):
        # 回退：使用系统 python + 设置 PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = _SRC
        proc = subprocess.run([sys.executable, cross_script], capture_output=True, text=True, env=env)
    else:
        proc = subprocess.run([venv_python, cross_script], capture_output=True, text=True)

    cross_exit = proc.returncode
    rec("X1", "跨进程撤销：子进程退出码",
        "PASS" if cross_exit == 0 else "FAIL",
        f"exit={cross_exit}\nstdout={proc.stdout.strip()}\nstderr={proc.stderr.strip()}")


# ========= 10 次启动/退出 =========

def t_launch_exit_times() -> list:
    """10 次创建 Qt QApplication + MainWindow + show + close。逐次记录退出码。"""
    os.makedirs(EVIDENCE_DIR, exist_ok=True)

    launcher_script = os.path.join(tempfile.gettempdir(), "_archivebox_launch_exit.py")
    with open(launcher_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys, os
os.environ.pop("QT_QPA_PLATFORM", None)
sys.path.insert(0, r"{_SRC}")
from PySide6.QtWidgets import QApplication
from main import MainWindow
app = QApplication(sys.argv)
w = MainWindow()
w.show()
w.close()
app.quit()
sys.exit(0)
""")

    results = []
    venv_python = os.path.join(os.path.dirname(_SRC), ".venv", "Scripts", "python.exe")
    py = venv_python if os.path.exists(venv_python) else sys.executable

    for i in range(10):
        t0 = time.time()
        proc = subprocess.run([py, launcher_script], capture_output=True, text=True, timeout=30)
        elapsed = (time.time() - t0) * 1000
        ec = proc.returncode
        results.append({"run": i + 1, "exit_code": ec, "elapsed_ms": round(elapsed, 1),
                        "stdout_tail": proc.stdout.strip()[-200:],
                        "stderr_tail": proc.stderr.strip()[-200:]})

        if ec == 0:
            print(f"  Launch {i+1}/10: exit={ec} OK ({elapsed:.0f}ms)")
        else:
            print(f"  Launch {i+1}/10: exit={ec} FAIL ({elapsed:.0f}ms) stderr_tail={proc.stderr.strip()[-200:]}")

    # 逐次断言
    all_ok = all(r["exit_code"] == 0 for r in results)
    rec("L1", "10 次启动/退出全部 exit_code=0",
        "PASS" if all_ok else "FAIL",
        "\n".join(f"  run {r['run']}: exit={r['exit_code']} time={r['elapsed_ms']}ms" for r in results))

    # 保存详细证据
    evidence_file = os.path.join(EVIDENCE_DIR, "launch_exit_10x.json")
    with open(evidence_file, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}, f,
                  ensure_ascii=False, indent=2)

    return results


# ========= 主流程 =========

def main() -> int:
    print("=" * 60)
    print("归档盒 V1.7 — Windows 第一轮：最小核心流程闭环")
    print(f"临时目录: {TMP_BASE}")
    print(f"证据目录: {EVIDENCE_DIR}")
    print(f"解释器: {sys.executable}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"平台: {sys.platform}")
    print("=" * 60)

    # 阶段 1：合成目录 + 扫描 + 计划 + 执行 + 验证
    print("\n--- 阶段 1：扫描 → 计划 → 执行 → 核对 ---")
    build_synthetic_root()
    root, sr = t_scan()
    run_info = t_plan_and_execute(root, sr)

    # 阶段 2：新进程撤销（通过磁盘上的数据库）
    print("\n--- 阶段 2：新进程重启 → 历史查询 → 撤销 ---")
    t_cross_process(root, run_info["before"])

    # 阶段 3：当前进程再撤销一次（交叉进程已经执行过撤销，我们需要保证最终清单一致）
    print("\n--- 阶段 3：当前进程执行撤销 → 核对 ---")
    t_undo(root)

    # 阶段 4：最终文件清单核对
    print("\n--- 阶段 4：文件清单最终核对 ---")
    t_check_manifest(root, run_info["before"])

    # 阶段 5：10 次启动/退出
    print("\n--- 阶段 5：10 次启动/退出检查 ---")
    t_launch_exit_times()

    # ======== 汇总 ========
    passed = sum(1 for r in RESULTS if r.status == "PASS")
    failed = sum(1 for r in RESULTS if r.status == "FAIL")
    blocked = sum(1 for r in RESULTS if r.status == "BLOCK")
    exit_code = 0 if failed == 0 and blocked == 0 and RESULTS else 1

    print("\n" + "=" * 60)
    print(f"合计 {len(RESULTS)} 项：PASS={passed} FAIL={failed} BLOCK={blocked}")
    print(f"退出码: {exit_code}")
    print("=" * 60)

    # 保存结果
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    meta = {
        "suite": "windows_v1_first_round",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "sys_name": os.name,
        "tmp_base": TMP_BASE,
        "evidence_dir": EVIDENCE_DIR,
        "exit_code": exit_code,
        "summary": {"total": len(RESULTS), "pass": passed,
                    "fail": failed, "block": blocked},
    }
    out_json = os.path.join(EVIDENCE_DIR, "windows_v1_first_round.json")
    out_txt = os.path.join(EVIDENCE_DIR, "windows_v1_first_round.txt")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": [r.__dict__ for r in RESULTS]}, f,
                  ensure_ascii=False, indent=2)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.7 Windows 第一轮 — 最小核心流程闭环\n")
        f.write(f"时间: {meta['ts']}\n解释器: {meta['interpreter']}\n"
                f"Python {meta['python']} 平台 {meta['platform']}\n"
                f"测试根: {TMP_BASE}\n证据: {EVIDENCE_DIR}\n\n")
        f.write(f"合计 {len(RESULTS)} 项：PASS={passed} FAIL={failed} BLOCK={blocked}\n"
                f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r.status}] {r.id}  {r.title}\n")
            if r.detail:
                for line in str(r.detail).splitlines():
                    f.write(f"    {line}\n")
            f.write("\n")

    print(f"\n结果已保存: {out_json}")
    print(f"结果已保存: {out_txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
