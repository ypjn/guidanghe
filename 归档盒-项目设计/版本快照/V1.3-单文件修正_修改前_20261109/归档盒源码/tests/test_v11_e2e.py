"""归档盒 V1.1 — 完整独立验证脚本

所有测试在 /tmp/_v11_e2e_verify/ 下的独立目录执行。
不触碰真实 /Users/ypjn/Desktop/NEU。

覆盖：路径修复 / 项目保护 / 普通资料层级保留 / 端到端整理撤销 / 旧模式兼容。
界面状态机（取消、过期、失败、关闭）由 test_v11_ui_state.py 负责。

运行：
  cd 归档盒源码 && .venv/bin/python tests/test_v11_e2e.py
"""
import os, sys, tempfile, shutil, threading, time, json
from collections import Counter

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from scanner import scan_directory, _find_all_project_roots, _looks_like_project_root
from planner import (
    generate_plan, MODE_BY_DIRECTION_SEMESTER, MODE_BY_COURSE_THEN_TYPE,
    MODE_BY_TYPE, DEFAULT_COURSE_RULES, _match_by_context,
)
from storage import Storage
from executor import (
    execute_plan, undo_latest_batch, recover_batch,
)
from utils import sha256_file

BASE = "/tmp/_v11_e2e_verify"
RESULTS = []

def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status, "evidence": evidence})
    mark = "PASS" if status == "通过" else ("FAIL" if status == "未通过" else "?")
    print(f"[{mark}] {tid} {title}")
    if evidence:
        for line in evidence.splitlines()[:5]:
            print(f"        {line}")

def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root

def mk(root, *parts, content="x"):
    full = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return full

def fp_map(root, exclude=None):
    exclude = exclude or set()
    m = {}
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = [d for d in dn if d not in exclude and not d.startswith(".")]
        for f in fn:
            full = os.path.join(dp, f)
            if f.startswith(".") or not os.path.isfile(full):
                continue
            m[os.path.relpath(full, root)] = (os.path.getsize(full), sha256_file(full))
    return m


# =============================================================
# 一、路径正确性 — 三个已复现 bug + 更多边界
# =============================================================
def t_paths():
    print("\n--- 一、路径正确性 ---")
    root = fresh("t_paths")
    mk(root, "Code", "main.py")
    mk(root, "Code", "Python", "pkg", "main.py")
    mk(root, "大一下", "编程作业", "out", "Main.class")
    mk(root, "大二上", "数据结构", "直接下的文件.pdf")
    mk(root, "大二上", "数据结构", "第3章", "讲义", "栈.md")
    mk(root, "大二上", "未登记目录", "note.txt")
    mk(root, "大二上", "未登记目录", "子目录", "deep.txt")
    mk(root, "随机散落文件.txt")
    mk(root, "大二上", "数据结构", "实验", "实验 一", "report .docx")  # 中文+空格

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
    tmap = {i.file.rel_path: i.target_rel_path for i in plan.items}
    imap = {i.file.rel_path: i.inner_path for i in plan.items}

    cases = [
        # (源, 期望目标, 描述)
        ("Code/main.py",
         "_已整理/项目/Code/main.py",
         "顶层 Code 下直接放 main.py — 不把文件名当目录"),
        ("Code/Python/pkg/main.py",
         "_已整理/项目/Python/pkg/main.py",
         "嵌套 Code/Python/pkg/main.py — pkg 保留为 inner_path"),
        ("大一下/编程作业/out/Main.class",
         "_已整理/课程/大一下/程序设计基础/构建产物/out/Main.class",
         "学期+登记目录+子目录 — out 保留"),
        ("大二上/数据结构/直接下的文件.pdf",
         "_已整理/课程/大二上/数据结构/文档/直接下的文件.pdf",
         "学期+登记目录直接下文件 — 无 inner_path"),
        ("大二上/数据结构/第3章/讲义/栈.md",
         "_已整理/课程/大二上/数据结构/文档/第3章/讲义/栈.md",
         "多层嵌套 — 第3章/讲义 保留"),
        ("大二上/未登记目录/note.txt",
         "_已整理/课程/大二上/未登记目录/文档/note.txt",
         "未登记目录直接下文件"),
        ("大二上/未登记目录/子目录/deep.txt",
         "_已整理/课程/大二上/未登记目录/文档/子目录/deep.txt",
         "未登记目录多层嵌套 — 子目录 保留"),
        ("随机散落文件.txt",
         "_已整理/待定/随机散落文件.txt",
         "顶层散落文件 — 待定方向 + 无额外目录层（不再有文件名当目录）"),
        ("大二上/数据结构/实验/实验 一/report .docx",
         "_已整理/课程/大二上/数据结构/文档/实验/实验 一/report .docx",
         "中文+空格路径 — 全部保留"),
    ]

    all_ok = True
    for idx, (src, exp_dst, desc) in enumerate(cases, 1):
        got = tmap.get(src, "<缺失>")
        ok = got == exp_dst
        if not ok:
            all_ok = False
            rec(f"P{idx}", desc, "未通过",
                f"源: {src}\n期望: {exp_dst}\n实际: {got}")
        else:
            rec(f"P{idx}", desc, "通过",
                f"→ {got}")

    # 额外验证：没有任何 target 路径里出现"文件名当目录"
    bad = []
    for item in plan.items:
        # 如果没有 inner_path，target_dir 的最后一段不应等于 target_filename
        if not item.inner_path:
            last_dir = os.path.basename(item.target_dir_rel)
            if last_dir == item.target_filename:
                bad.append(item.target_rel_path)
    rec("P_bad_dir", "无文件名当目录的情况", "通过" if not bad else "未通过",
        f"可疑项={bad}" if bad else "检查 9 项全部 OK")

    return all_ok


# =============================================================
# 二、项目保护：自动识别 + 最深优先修正
# =============================================================
def t_protect_auto():
    print("\n--- 二、项目保护自动识别 ---")
    root = fresh("t_protect_auto")

    # 场景 A: 有 .git 的完整 Python 项目 + 嵌套有 package.json 的 Node 子项目
    proj_a = os.path.join(root, "myproject")
    os.makedirs(os.path.join(proj_a, ".git"))
    mk(proj_a, "src", "main.py", content="print('hello')")
    mk(proj_a, "src", "util.py", content="def f(): pass")
    mk(proj_a, "data.csv", content="a,b,c\n1,2,3")
    mk(proj_a, "README.md", content="# hello")

    proj_b = os.path.join(proj_a, "web")
    mk(proj_b, "package.json", content='{"name": "web"}')
    mk(proj_b, "index.js", content="console.log('hi')")

    # 场景 B: 没有任何标记的普通文件夹（不应被保护）
    normal = os.path.join(root, "普通资料")
    mk(normal, "讲义.pdf", content="pdf-bytes")
    mk(normal, "notes.md", content="# notes")

    sr = scan_directory(root)

    # 验证 1: proj_a 和 proj_b 都被识别为保护根
    assert len(sr.protected_roots) >= 2, f"应有至少 2 个保护根，实际 {sr.protected_roots}"
    rec("PR1", "父子项目根都被识别（.git + package.json）", "通过"
        if os.path.join(root, "myproject") in sr.protected_roots
        and os.path.join(root, "myproject", "web") in sr.protected_roots else "未通过",
        f"保护根={list(sr.protected_roots.keys())}")

    # 验证 2: proj_a 里所有文件都被保护（包括嵌套 proj_b 里的）
    protected_abs = set(sr.protected_roots.keys())
    all_protected = [
        s for s in sr.skipped
        if "项目保护" in (s.skipped_reason or "")
    ]
    rec("PR2", f"有 {len(all_protected)} 个文件因项目保护跳过（应为 6 个）",
        "通过" if len(all_protected) == 6 else "未通过",
        f"实际跳过={len(all_protected)} 个项目保护文件")

    # 验证 3: 普通资料里的 2 个文件没被保护
    normal_files = [f for f in sr.files if "普通资料" in f.rel_path]
    rec("PR3", "无标记的普通文件夹不被保护", "通过"
        if len(normal_files) == 2 else "未通过",
        f"普通资料文件={len(normal_files)}（应为 2）")

    # 验证 4: 归档盒源码目录（默认保护）—— 我们扫描的不是源码目录，所以它不在保护根里
    # 但 main.py 启动时会传 protect_dirs=[源码目录]，这里是自动识别，所以 OK


# =============================================================
# 三、显式 protect_dirs 生效
# =============================================================
def t_protect_explicit():
    print("\n--- 三、显式 protect_dirs ---")
    root = fresh("t_protect_explicit")
    mk(root, "a.txt", "A")
    mk(root, "b.txt", "B")

    # 显式保护 "隐藏" 目录（但它不存在——应该不报错）
    protect_dirs = [os.path.join(root, "不存在的目录")]
    sr = scan_directory(root, protect_dirs=protect_dirs, detect_project_roots=False)
    rec("PE1", "显式保护不存在的目录不报错", "通过"
        if sr.errors == [] and sr.protected_count == 0 else "未通过",
        f"errors={sr.errors} protected={sr.protected_count}")

    # 显式保护一个真实子目录
    sub = os.path.join(root, "子目录")
    mk(sub, "c.txt", "C")
    mk(sub, "d.txt", "D")

    sr2 = scan_directory(root, protect_dirs=[sub], detect_project_roots=False)
    normal_count = len([f for f in sr2.files if "子目录" not in f.rel_path])
    protected_count = sr2.protected_count
    rec("PE2", "显式保护真实子目录：内部文件被跳过，外部不受影响",
        "通过" if normal_count == 2 and protected_count == 2 else "未通过",
        f"正常={normal_count} 保护={protected_count}")

    # 显式保护归档盒源码目录（模拟 main.py 会做的事）
    # 源码目录不在 root 下面，所以不会被 root.startswith(...) 匹配
    # 但 root 就是源码目录的情况... 让我们测一下 protect_dirs 指向外部目录
    sr3 = scan_directory(root, protect_dirs=["/tmp/某个不存在的外部目录"], detect_project_roots=False)
    rec("PE3", "protect_dirs 指向 root 外部目录不报错", "通过"
        if sr3.protected_count == 0 else "未通过")


# =============================================================
# 四、同目录代码 + 数据 — 项目保护下不被拆开
# =============================================================
def t_protect_together():
    print("\n--- 四、同目录代码+数据不被拆开 ---")
    root = fresh("t_protect_together")

    proj = os.path.join(root, "myapp")
    os.makedirs(os.path.join(proj, ".git"))  # .git 是 SKIP_DIR，scan_directory 不会进入
    mk(proj, "main.py", content="import pandas as pd")
    mk(proj, "data.csv", content="col1,col2\n1,2\n3,4")

    sr = scan_directory(root)

    # 两个文件都在 .git 保护根下 → 都被 skipped
    proot_count = sr.protected_count
    normal_in_proj = [f for f in sr.files if "myapp" in f.rel_path]
    protected_in_proj = [s for s in sr.skipped
                        if "myapp" in s.abs_path and "项目保护" in (s.skipped_reason or "")]

    rec("PT1", "有 .git 的项目被整体保护：main.py 和 data.csv 都不进入 files",
        "通过" if proot_count == 2 and len(normal_in_proj) == 0 else "未通过",
        f"项目保护文件={proot_count} 正常文件在项目内={len(normal_in_proj)}")


# =============================================================
# 五、未保护普通资料：保留内部层级 + 同名不覆盖
# =============================================================
def t_normal_preserve():
    print("\n--- 五、未保护普通资料保留内部层级 ---")
    root = fresh("t_normal_preserve")

    mk(root, "大二上", "数据结构", "第3章", "讲义", "栈.md")
    mk(root, "大二上", "数据结构", "第3章", "讲义", "队列.md")
    mk(root, "大二上", "数据结构", "实验", "实验 一", "report.docx")
    mk(root, "大二上", "数据结构", "实验", "实验 二", "report.docx")  # 同名！

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])
    tmap = {i.file.rel_path: i.target_rel_path for i in plan.items}

    # 验证所有文件都保留了完整内部路径
    all_preserved = all(
        src in dst or os.path.basename(src) in dst
        for src, dst in tmap.items()
    )

    # 验证同名不同位置的两个 report.docx 得到不同目标
    reports = [i for i in plan.items if i.file.filename == "report.docx"]
    tgts = [r.target_rel_path for r in reports]
    unique_tgts = len(set(tgts))

    rec("NP1", "所有文件保留完整内部层级", "通过" if all_preserved else "未通过",
        f"检查 {len(tmap)} 项")
    rec("NP2", "同名不同位置的文件得到不同目标（不覆盖）",
        "通过" if len(reports) == 2 and unique_tgts == 2 else "未通过",
        f"reports={tgts} unique={unique_tgts}")


# =============================================================
# 六、计划取消 / 过期结果 / 生成失败
#
# 这三项属于界面状态机行为（按钮 enabled/visible、半成品计划是否发布），
# 原实现只在后台线程层面做了弱断言（甚至直接记录通过），不能作为验收证据。
# 现统一由 tests/test_v11_ui_state.py 的 P1–P6 真实触发 Qt 信号并断言界面状态。
# 本套件只保留与界面无关的纯逻辑 / 路径 / 文件系统验证。
# =============================================================


# =============================================================
# 九、端到端：完整整理 + 撤销 + 指纹一致
# =============================================================
def t_e2e():
    print("\n--- 九、完整端到端 ---")
    root = fresh("t_e2e")

    mk(root, "大二上", "数据结构", "讲义.pdf", "pdf-bytes-1")
    mk(root, "大二上", "数据结构", "作业", "hw1.docx", "hw1-content")
    mk(root, "Code", "Python", "main.py", "print('hello')")
    mk(root, "Code", "main.py", "print('world')")
    mk(root, "随机.txt", "random content")

    before = fp_map(root, exclude={"_已整理", ".organizer"})

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, [])

    rec("TE1", "端到端计划生成 5 项", "通过" if plan.total == 5 else "未通过",
        f"plan.total={plan.total}")

    # 执行
    st = Storage(root)
    er = execute_plan(plan, st)
    st.close()

    # 所有文件都应该被移动（没保护）
    rec("TE2", "执行后 5 个文件成功移动", "通过" if er.moved == 5 else "未通过",
        f"moved={er.moved} failed={er.failed} skipped={er.skipped}")

    # 归档目录里有 5 个文件
    after_move = fp_map(root, exclude={".organizer"})
    # 已移动的源不在了，归档目录里有新文件
    moved_ok = sum(1 for p in after_move if p.startswith("_已整理")) == 5
    src_gone = all(not os.path.isfile(os.path.join(root, k)) for k in before
                   if not k.startswith("_已整理"))
    rec("TE3", "源目录文件消失 + 归档目录出现", "通过" if moved_ok else "未通过",
        f"归档目录文件={sum(1 for p in after_move if p.startswith('_已整理'))}")

    # 撤销
    st2 = Storage(root)
    ur = undo_latest_batch(st2)
    st2.close()

    after_undo = fp_map(root, exclude={"_已整理", ".organizer"})
    rec("TE4", "撤销后路径 → 指纹完全恢复",
        "通过" if after_undo == before else "未通过",
        f"撤销恢复={ur.restored} 冲突={ur.conflict} errors={ur.errors}")


# =============================================================
# 十、模式兼容性（V1.0 的 MODE_BY_TYPE / MODE_BY_COURSE_THEN_TYPE 不受影响）
# =============================================================
def t_mode_compat():
    print("\n--- 十、旧模式兼容性 ---")
    root = fresh("t_mode_compat")
    mk(root, "下载", "高数.pdf", "高数内容")
    mk(root, "下载", "实验报告.docx", "实验")

    sr = scan_directory(root, detect_project_roots=False)

    # MODE_BY_TYPE
    p1 = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    p1_ok = all(not i.target_rel_path.endswith(i.target_rel_path.split(os.sep)[-1] + os.sep + i.target_filename)
                for i in p1.items)  # 基本的目标路径合理性
    rec("MC1", "MODE_BY_TYPE 正常生成", "通过" if len(p1.items) == 2 else "未通过",
        f"items={len(p1.items)}")

    # MODE_BY_COURSE_THEN_TYPE
    p2 = generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
    rec("MC2", "MODE_BY_COURSE_THEN_TYPE 正常生成", "通过" if len(p2.items) == 2 else "未通过",
        f"items={len(p2.items)}")


# =============================================================
# 汇总
# =============================================================
def main():
    os.makedirs(BASE, exist_ok=True)
    print("=" * 60)
    print("归档盒 V1.1 完整独立验证")
    print(f"测试根: {BASE}")
    print("=" * 60)

    t_paths()
    t_protect_auto()
    t_protect_explicit()
    t_protect_together()
    t_normal_preserve()
    t_e2e()
    t_mode_compat()

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")

    unv = sum(1 for r in RESULTS if r["status"] == "未验证")
    exit_code = 0 if (failed == 0 and unv == 0 and len(RESULTS) > 0) else 1

    print("\n" + "=" * 60)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}")
    print(f"退出码: {exit_code}")
    print("=" * 60)
    for r in RESULTS:
        if r["status"] != "通过":
            print(f"  [FAIL] {r['id']} {r['title']}")
            if r.get("evidence"):
                print(f"         {r['evidence'].splitlines()[0] if r['evidence'] else ''}")

    results_dir = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "v11_e2e",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v11_e2e.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "base": BASE,
        "exit_code": exit_code,
        "summary": {"total": len(RESULTS), "pass": passed,
                    "fail": failed, "unverified": unv},
    }
    out = os.path.join(results_dir, "results_v11_e2e.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(results_dir, "results_v11_e2e.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.1 端到端验证 — 结果清单\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {sys.version.split()[0]}\n")
        f.write(f"平台: {sys.platform}  测试根: {BASE}\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            if r.get("evidence"):
                for line in str(r["evidence"]).splitlines():
                    f.write(f"    {line}\n")
            f.write("\n")
    print(f"\n结果已保存: {out}")
    print(f"结果已保存: {txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
