"""
归档盒 V1.0 最终回归 — 统一验收测试（独立场景根目录）

每个验收场景使用自己的全新根目录，互不污染。
所有执行 / 撤销 / 恢复均通过生产代码公开函数驱动。

运行：
  cd /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码
  .venv/bin/python /tmp/v10_harness/test_v10.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from scanner import scan_directory                        # noqa: E402
from planner import (                                     # noqa: E402
    generate_plan, MODE_BY_TYPE, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES,
)
from storage import Storage                               # noqa: E402
from executor import (                                    # noqa: E402
    execute_plan, undo_latest_batch, recover_batch, has_unrecovered_batch,
)
from utils import sha256_file                             # noqa: E402

BASE = "/tmp/归档盒V10回归"
OUT = "/tmp/归档盒V10回归_out"
HARNESS = "/tmp/v10_harness"
TS = datetime.now().strftime("%Y%m%d_%H%M%S")

RESULTS: list[dict] = []


def rec(tid: str, title: str, status: str, evidence: str = "", steps: str = ""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence, "steps": steps})
    icon = {"通过": "PASS", "未通过": "FAIL", "未验证": "UNVERIFIED"}[status]
    print(f"[{icon}] {tid} {title}")
    if evidence:
        for line in evidence.splitlines():
            print(f"        {line}")


# ---------------- 文件与指纹工具 ----------------

def mk(root: str, rel: str, content: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


def fresh(scenario: str) -> str:
    root = os.path.join(BASE, scenario)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


USER_EXCLUDE = {".organizer", "_已整理", ".git"}


def fp_multiset(root: str) -> Counter:
    """用户文件 (size, sha256) 多重集合：不含归档目录/日志/隐藏/符号链接。"""
    c: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in USER_EXCLUDE and not d.startswith(".")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn.startswith(".") or os.path.islink(full) or not os.path.isfile(full):
                continue
            size = os.path.getsize(full)
            sha = sha256_file(full)
            if sha:
                c[(size, sha)] += 1
    return c


def fp_map(root: str) -> dict:
    """相对路径 → (size, sha256)。"""
    m = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in USER_EXCLUDE and not d.startswith(".")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn.startswith(".") or os.path.islink(full) or not os.path.isfile(full):
                continue
            m[os.path.relpath(full, root)] = (os.path.getsize(full), sha256_file(full))
    return m


FPDIR = os.path.join(BASE, "_fingerprints")


def write_before(root: str, name: str = "") -> str:
    """指纹清单写在**被整理目录之外**（设计文档要求），避免污染扫描结果。"""
    os.makedirs(FPDIR, exist_ok=True)
    name = name or os.path.basename(root)
    path = os.path.join(FPDIR, f"{name}_before.txt")
    m = fp_map(root)
    with open(path, "w", encoding="utf-8") as f:
        for rel in sorted(m):
            size, sha = m[rel]
            f.write(f"{rel}\t{size}\t{sha}\n")
    return path


def diff_summary(a: Counter, b: Counter) -> str:
    only_a = a - b
    only_b = b - a
    return (f"仅整理前有 {sum(only_a.values())} 份 / 仅整理后有 {sum(only_b.values())} 份"
            f"; 缺失示例={[k[1][:10] for k in list(only_a)[:3]]}"
            f"; 多余示例={[k[1][:10] for k in list(only_b)[:3]]}")


# ---------------- 场景 1：扫描基础 A01 / A02 / A16 ----------------

def s01_scan():
    root = fresh("s01_scan")
    # 空目录
    mk(root, "空目录/placeholder.txt", "x")
    os.remove(os.path.join(root, "空目录/placeholder.txt"))  # 留一个真正的空目录
    # 中文 / 空格 / 无扩展名 / 大小写扩展名
    mk(root, "下载/高等数学_第一章.pdf", "高数内容")
    mk(root, "下载/课程资料/作业 第一版.docx", "含空格文件")
    mk(root, "下载/无扩展名文件", "无扩展名")
    mk(root, "下载/大写扩展.PDF", "大写扩展")
    mk(root, "下载/未知扩展名.zzz", "不在类型表内的扩展")
    # 多层目录
    mk(root, "课程群/2026秋季/高数/第一章/笔记.md", "深层笔记")
    mk(root, "课程群/2026秋季/高数/第二章/习题.docx", "深层习题")
    # 跳过项
    mk(root, "下载/.hidden_file", "隐藏")
    mk(root, ".git/config", "git")
    mk(root, "_已整理/残留/旧输出.txt", "不应被扫描")
    os.symlink(os.path.join(root, "下载/高等数学_第一章.pdf"),
               os.path.join(root, "下载/链接.pdf"))
    os.symlink(os.path.join(root, "下载"), os.path.join(root, "链接目录"))

    write_before(root)

    steps = ("cd 归档盒源码 && .venv/bin/python -c \"from scanner import scan_directory;"
             f"r=scan_directory('{root}');print(r.summary())\"")

    # A01a 空目录
    empty = os.path.join(root, "空目录")
    sr_empty = scan_directory(empty, detect_project_roots=False)
    rec("A01a", "空目录扫描不崩溃", "通过" if sr_empty.total_files == 0 and not sr_empty.errors else "未通过",
        f"files={sr_empty.total_files}, errors={sr_empty.errors}", steps)

    sr = scan_directory(root, detect_project_roots=False)
    # A01b 中文/空格
    cn = [f.rel_path for f in sr.files if " " in f.filename or any(ord(c) > 127 for c in f.filename)]
    rec("A01b", "中文/空格文件名正常进入扫描结果", "通过" if len(cn) >= 3 else "未通过",
        f"命中 {len(cn)} 项：{cn[:4]}", steps)

    # A01c 无扩展名
    noext = [f for f in sr.files if f.filename == "无扩展名文件"]
    ok = bool(noext) and noext[0].file_type == "其他"
    rec("A01c", "无扩展名归入「其他」", "通过" if ok else "未通过",
        f"{[(f.filename, f.file_type) for f in noext]}", steps)

    # A01d 大小写扩展名
    upper = [f for f in sr.files if f.filename == "大写扩展.PDF"]
    ok = bool(upper) and upper[0].file_type == "文档"
    rec("A01d", "大小写不敏感：.PDF → 文档", "通过" if ok else "未通过",
        f"{[(f.filename, f.file_type) for f in upper]}", steps)

    # A01e 未知扩展名 → 其他
    unk = [f for f in sr.files if f.filename == "未知扩展名.zzz"]
    rec("A01e", "未登记扩展名归入「其他」", "通过" if unk and unk[0].file_type == "其他" else "未通过",
        f"{[(f.filename, f.file_type) for f in unk]}", steps)

    # A02 多层目录
    deep = [f.rel_path for f in sr.files if f.rel_path.count(os.sep) >= 4]
    expected = {"课程群/2026秋季/高数/第一章/笔记.md", "课程群/2026秋季/高数/第二章/习题.docx"}
    got = {p.replace(os.sep, "/") for p in deep}
    rec("A02", "多层子目录普通文件全部进入预览且源路径准确",
        "通过" if expected <= got else "未通过",
        f"扫描总数={sr.total_files}，深层={sorted(got)}", steps)

    # A16 跳过规则
    in_result = {p.replace(os.sep, "/") for p in (f.rel_path for f in sr.files)}
    bad = [p for p in in_result if p.startswith("_已整理") or p.startswith(".git")
           or "/.hidden" in p or p.endswith("链接.pdf")]
    rec("A16", "符号链接/隐藏文件/.git/_已整理 均不进入扫描结果",
        "通过" if not bad else "未通过",
        f"hidden={sr.hidden_count} symlink={sr.symlink_count} git={sr.git_count} "
        f"skipdir={sr.skipdir_count} 越界项={bad}", steps)


# ---------------- 场景 2：计划正确性 A03–A07 ----------------

def s02_plan():
    root = fresh("s02_plan")
    mk(root, "下载/高数_AIGC_期中复习.pdf", "同时含两个关键词")
    mk(root, "下载/实验报告.docx", "下载目录实验报告")
    mk(root, "实验/化学/实验报告.docx", "化学实验报告内容")
    mk(root, "实验/物理/实验报告.docx", "物理实验报告内容")
    same = "完全相同的重复副本内容，用于重复检测。"
    mk(root, "下载/副本A_报告.pdf", same)
    mk(root, "下载/副本B_同为副本.pdf", same)
    c1 = "A" * 40 + "projectA_unique_tail"
    c2 = "B" * 40 + "projectB_unique_tail"   # 等长不同内容
    assert len(c1) == len(c2)
    mk(root, "项目A/实验报告.docx", c1)
    mk(root, "项目B/实验报告.docx", c2)
    # 目标已存在同名
    mk(root, "_已整理/未分类课程/文档/预存文件.pdf", "磁盘已存在")
    mk(root, "下载/预存文件.pdf", "源文件同名")
    write_before(root)

    steps = ("cd 归档盒源码 && .venv/bin/python - <<'PY'\n"
             "from scanner import scan_directory\nfrom planner import generate_plan,"
             " MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES\n"
             f"sr=scan_directory('{root}')\n"
             "p=generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)\n"
             "for i in p.items: print(i.file.rel_path,'->',i.target_rel_path,i.course,i.dup_group_id)\nPY")

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)

    # A03 规则顺序
    t = [i for i in plan.items if "高数_AIGC" in i.file.filename]
    ok = bool(t) and t[0].course == "高等数学" and "高数" in t[0].rule_applied
    rec("A03", "同时命中两条规则时第一条（高数 order=1）生效并显示依据",
        "通过" if ok else "未通过",
        f"course={t[0].course}, rule={t[0].rule_applied}" if t else "未找到目标文件", steps)

    # A04 同名不同内容各得唯一目标
    reports = [i for i in plan.items if i.file.filename == "实验报告.docx"]
    tgts = [i.target_rel_path for i in reports]
    ok = len(reports) == 5 and len(set(tgts)) == 5
    rec("A04", "5 份同名「实验报告.docx」全部保留，预览目标路径互不相同",
        "通过" if ok else "未通过",
        f"{[(i.file.rel_path, i.target_filename) for i in reports]}", steps)

    # A05 不同名同内容 → 同一重复组
    copies = [i for i in plan.items if "副本" in i.file.filename]
    ok = len(copies) == 2 and len({i.dup_group_id for i in copies}) == 1 and copies[0].dup_count == 2
    rec("A05", "不同名同内容进入同一重复组且 dup_count=2",
        "通过" if ok else "未通过",
        f"gid={[i.dup_group_id for i in copies]} dup_count={[i.dup_count for i in copies]}", steps)

    # A06 同名同大小不同内容 → 不进同组
    proj = [i for i in plan.items if i.file.rel_path.startswith(("项目A", "项目B"))]
    ok = len(proj) == 2 and all(i.dup_group_id is None for i in proj)
    rec("A06", "同名同大小但不同内容不误判为重复",
        "通过" if ok else "未通过",
        f"gid={[i.dup_group_id for i in proj]} size={[i.file.size for i in proj]}", steps)

    # A07 目标已存在 → 唯一新名称
    c = [i for i in plan.items if i.file.rel_path == os.path.join("下载", "预存文件.pdf")]
    ok = bool(c) and c[0].conflict_with_existing and c[0].target_filename != "预存文件.pdf"
    rec("A07", "整理前目标已存在同名文件 → 保留既有文件并分配唯一新名称",
        "通过" if ok else "未通过",
        f"target={c[0].target_filename} conflict={c[0].conflict_with_existing}" if c else "未找到", steps)

    # 计划零副作用
    before = fp_multiset(root)
    sr2 = scan_directory(root, detect_project_roots=False)
    generate_plan(sr2, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
    rec("A02b", "生成计划零副作用：不创建目录/不移动文件",
        "通过" if fp_multiset(root) == before and not os.path.exists(os.path.join(root, ".organizer"))
        else "未通过", "计划生成前后指纹一致，无 .organizer 目录", steps)


# ---------------- 场景 3：预览后变化 A08 ----------------

def s03_preview_change():
    steps = "见 test_v10.py s03_preview_change()：计划后修改/删除源文件或占用目标，再 execute_plan"

    # A08a 预览后修改源文件
    root = fresh("s03a_source_modified")
    mk(root, "a.txt", "A 原始内容")
    mk(root, "b.txt", "B 原始内容")
    write_before(root)
    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    mk(root, "b.txt", "B 内容被用户改成了别的")
    st = Storage(root)
    r = execute_plan(plan, st)
    acts = {a.source_rel: a for a in st.get_actions(r.batch_id)}
    st.close()
    a = acts["b.txt"]
    rec("A08a", "预览后源文件内容变化 → 该项跳过且原因明确",
        "通过" if a.status == "skipped" and "内容已变化" in a.error else "未通过",
        f"status={a.status} error={a.error} 同批 a.txt={acts['a.txt'].status}", steps)

    # A08b 预览后删除源文件
    root = fresh("s03b_source_deleted")
    mk(root, "a.txt", "A")
    mk(root, "c.txt", "C")
    write_before(root)
    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    os.remove(os.path.join(root, "c.txt"))
    st = Storage(root)
    r = execute_plan(plan, st)
    acts = {a.source_rel: a for a in st.get_actions(r.batch_id)}
    st.close()
    c = acts["c.txt"]
    rec("A08b", "预览后源文件消失 → 该项跳过且原因明确",
        "通过" if c.status == "skipped" and "已不存在" in c.error else "未通过",
        f"status={c.status} error={c.error}", steps)

    # A08c 预览后目标被占用（不得悄悄改名）
    root = fresh("s03c_target_occupied")
    mk(root, "d.pdf", "D 内容")
    write_before(root)
    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    planned_target = plan.items[0].target_rel_path
    os.makedirs(os.path.dirname(os.path.join(root, planned_target)), exist_ok=True)
    mk(root, planned_target, "别人占用了这个目标路径")
    st = Storage(root)
    r = execute_plan(plan, st)
    a = st.get_actions(r.batch_id)[0]
    st.close()
    target_unchanged = a.target_rel == planned_target
    occupied_ok = os.path.isfile(os.path.join(root, planned_target))
    rec("A08c", "预览后目标出现新占用 → 跳过、不改名、不覆盖",
        "通过" if (a.status == "skipped" and "已被占用" in a.error and target_unchanged and occupied_ok)
        else "未通过",
        f"status={a.status} error={a.error} 目标名未变={target_unchanged} 占用文件仍在={occupied_ok}", steps)


# ---------------- 场景 4：端到端 + 完整撤销 A09 / A18 ----------------

def _build_e2e(root: str):
    mk(root, "下载/高数_第一章.pdf", "高数第一章")
    mk(root, "下载/实验报告.docx", "实验报告正文")
    mk(root, "课程群/AIGC_作业.py", "print('aigc')")
    mk(root, "课程群/截图.png", "png-bytes")
    mk(root, "课程群/线代_课件.pptx", "线代")
    mk(root, "课程群/英语_essay.docx", "essay")
    mk(root, "杂项/未知.xyz", "未知类型")


def s04_e2e():
    root = fresh("s04_e2e")
    _build_e2e(root)
    write_before(root)
    before_set = fp_multiset(root)
    before_map = fp_map(root)

    steps = ("见 test_v10.py s04_e2e()：两轮 scan→plan→execute→(关闭)重开 Storage→undo，"
             "每轮比对 (size,sha256) 多重集合与「路径→内容」映射")

    round_details = []
    all_ok = True
    for rnd in (1, 2):
        sr = scan_directory(root, detect_project_roots=False)
        plan = generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
        st = Storage(root)
        er = execute_plan(plan, st)
        batch_id = er.batch_id
        # 模拟关闭程序
        st.close()
        st2 = Storage(root)
        ur = undo_latest_batch(st2)
        acts = st2.get_actions(batch_id)
        status = st2.get_batch(batch_id).status
        st2.close()

        after_set = fp_multiset(root)
        after_map = fp_map(root)
        set_ok = after_set == before_set
        map_ok = after_map == before_map
        leftover = [p for p in after_map if p.startswith("_已整理")]
        no_dup = all(a.status in ("undone",) for a in acts if a.status == "moved") is True
        all_ok = all_ok and set_ok and map_ok and not leftover and ur.is_complete and status == "undone"
        round_details.append(
            f"第{rnd}轮：moved={er.moved} 撤销恢复={ur.restored} 冲突={ur.conflict} "
            f"批次状态={status} 多重集合一致={set_ok} 路径映射一致={map_ok} "
            f"归档目录残留文件={len(leftover)} 撤销No-op={ur.is_complete}")

    rec("A09", "整理 → 关闭程序 → 重新打开 → 撤销：路径与内容完全恢复",
        "通过" if all_ok else "未通过", "\n".join(round_details), steps)

    # 重复撤销无副作用
    before_repeat = fp_multiset(root)
    st = Storage(root)
    ur = undo_latest_batch(st)
    st.close()
    rec("A09b", "对已完整撤销的批次再次撤销：无第二次移动/删除",
        "通过" if fp_multiset(root) == before_repeat and ur.restored == 0 else "未通过",
        f"restored={ur.restored} errors={ur.errors_list}", steps)

    rec("A18", "重复两轮「完整整理→撤销」结果稳定，历史状态真实（非统一虚假成功）",
        "通过" if all_ok else "未通过", "\n".join(round_details), steps)


# ---------------- 场景 5：撤销冲突 A10 / A11 / A12 ----------------

def s05_undo_conflict():
    root = fresh("s05_undo_conflict")
    names = ["f1.txt", "f2.txt", "f3.txt", "f4.txt", "f5.txt", "f6.txt"]
    for i, n in enumerate(names):
        mk(root, n, f"文件{i}原始内容")
    write_before(root)
    before_map = fp_map(root)

    steps = ("见 test_v10.py s05_undo_conflict()：整理全部 6 项后，制造"
             "①归档目录新建文件 ②修改 1 个已移动目标 ③占用 1 个原路径 ④移走 1 个目标，再撤销")

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    st = Storage(root)
    er = execute_plan(plan, st)
    moved = [a for a in st.get_actions(er.batch_id) if a.status == "moved"]
    assert len(moved) == 6, f"预期移动 6 项，实际 {len(moved)}"
    st.close()

    # ① 归档目录新建文件
    archive_dir = os.path.dirname(os.path.join(root, moved[0].target_rel))
    new_arch = os.path.join(archive_dir, "用户新建_后期文件.txt")
    with open(new_arch, "w", encoding="utf-8") as f:
        f.write("后来新建，撤销不得删除")

    # ② 修改一个已移动目标
    mod_target = os.path.join(root, moved[1].target_rel)
    with open(mod_target, "w", encoding="utf-8") as f:
        f.write("用户修改后的内容，必须保留")
    mod_sha = sha256_file(mod_target)

    # ③ 原路径被新文件占用
    occ_source = os.path.join(root, moved[2].source_rel)
    with open(occ_source, "w", encoding="utf-8") as f:
        f.write("占位的新文件，不得被覆盖")
    occ_sha = sha256_file(occ_source)

    # ④ 目标被移走
    gone_target = os.path.join(root, moved[3].target_rel)
    os.remove(gone_target)

    st = Storage(root)
    ur = undo_latest_batch(st)
    acts = {a.action_index: a for a in st.get_actions(er.batch_id)}
    batch_status = st.get_batch(er.batch_id).status
    st.close()

    # A10 归档目录新文件保留
    a10 = os.path.isfile(new_arch) and sha256_file(new_arch) == sha256_file(new_arch)
    rec("A10", "整理后在归档目录新建的文件及其所在目录被保留",
        "通过" if os.path.isfile(new_arch) else "未通过",
        f"新文件存在={os.path.isfile(new_arch)}；归档目录仍存在={os.path.isdir(archive_dir)}", steps)

    # A11 修改内容保留 + 冲突
    idx_mod = moved[1].action_index
    a11_status = acts[idx_mod].status
    a11_keep = os.path.isfile(mod_target) and sha256_file(mod_target) == mod_sha
    rec("A11", "整理后被修改的目标：撤销保留修改内容并标记冲突",
        "通过" if a11_status == "conflict" and a11_keep else "未通过",
        f"status={a11_status} error={acts[idx_mod].error} 修改内容保留={a11_keep}", steps)

    # A12a 原路径被占
    idx_occ = moved[2].action_index
    a12a = acts[idx_occ].status == "conflict" and os.path.isfile(occ_source) \
        and sha256_file(occ_source) == occ_sha
    rec("A12a", "整理后原路径被新文件占用：不覆盖，标记冲突",
        "通过" if a12a else "未通过",
        f"status={acts[idx_occ].status} error={acts[idx_occ].error} 占用文件未变={os.path.isfile(occ_source)}", steps)

    # A12b 目标被移走
    idx_gone = moved[3].action_index
    a12b = acts[idx_gone].status == "conflict" and "消失" in acts[idx_gone].error
    rec("A12b", "整理后目标文件被移走：不猜测找回，标记冲突",
        "通过" if a12b else "未通过",
        f"status={acts[idx_gone].status} error={acts[idx_gone].error}", steps)

    # 未冲突项恢复 + 指纹
    restored_idx = [moved[0].action_index, moved[4].action_index, moved[5].action_index]
    restored_ok = all(acts[i].status == "undone" for i in restored_idx)
    content_ok = all(
        os.path.isfile(os.path.join(root, moved[k].source_rel))
        and sha256_file(os.path.join(root, moved[k].source_rel)) == moved[k].source_sha256
        for k in (0, 4, 5)
    )
    rec("A12c", "冲突场景下未受影响项仍按原路径/原内容恢复；批次显示「部分撤销」",
        "通过" if restored_ok and content_ok and batch_status == "partial_failed_undone"
        else "未通过",
        f"恢复项状态={[acts[i].status for i in restored_idx]} 内容一致={content_ok} "
        f"批次状态={batch_status} 撤销汇总={ur.summary()}", steps)

    # 多重集合：排除用户新增/修改的 3 项后应一致
    before_multi = Counter(before_map.values())
    after_map = fp_map(root)
    # 还原场景中保留的冲突项属于「用户后来变化」，不计入回滚要求
    excluded_sources = {moved[1].source_rel, moved[2].source_rel, moved[3].source_rel}
    after_cmp = Counter(v for k, v in after_map.items() if k not in excluded_sources)
    expected_cmp = Counter(v for k, v in before_map.items() if k not in excluded_sources)
    rec("A12d", "冲突场景指纹核验：排除用户后改/新增项后，其余文件多重集合一致",
        "通过" if after_cmp == expected_cmp else "未通过",
        f"待比对项 前={sum(expected_cmp.values())} 后={sum(after_cmp.values())} "
        f"一致={after_cmp == expected_cmp}", steps)

    # A10b 只清理本批次创建的空目录，不删含新文件的目录
    rec("A10b", "撤销只清理空目录，不递归删除含新文件的目录",
        "通过" if os.path.isdir(archive_dir) and os.path.isfile(new_arch) else "未通过",
        f"归档目录存在={os.path.isdir(archive_dir)}，新文件存在={os.path.isfile(new_arch)}", steps)


# ---------------- 场景 6：取消 A13 ----------------

def s06_cancel():
    root = fresh("s06_cancel")
    for i in range(10):
        mk(root, f"file_{i:02d}.txt", f"取消测试内容 {i}")
    write_before(root)

    steps = ("见 test_v10.py s06_cancel()：progress_cb 在第 3 项时 set cancel_event，"
             "execute_plan 应完成当前项后停止，剩余标记 cancelled")

    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    ev = threading.Event()

    def cb(cur, total, path):
        if cur == 3:
            ev.set()

    st = Storage(root)
    er = execute_plan(plan, st, progress_cb=cb, cancel_event=ev)
    acts = st.get_actions(er.batch_id)
    st.close()

    counts = Counter(a.status for a in acts)
    rec("A13", "取消后：完成数与实际磁盘一致，剩余项标记 cancelled",
        "通过" if er.was_cancelled and er.moved == 3 and counts.get("cancelled", 0) == 7
        and counts.get("moved", 0) == 3 else "未通过",
        f"moved={er.moved} cancelled={er.cancelled} 状态分布={dict(counts)} 批次={er.was_cancelled}", steps)

    # 可撤销已完成部分
    st = Storage(root)
    ur = undo_latest_batch(st)
    st.close()
    restored = sum(1 for a in ur.batch_id and [] or [] for _ in [])
    rec("A13b", "取消后仍可撤销已完成部分",
        "通过" if ur.restored == 3 and ur.is_complete else "未通过",
        f"restored={ur.restored} conflict={ur.conflict} errors={ur.errors}", steps)

    # 未执行项仍在原位
    on_disk = sorted(p for p in fp_map(root) if p.startswith("file_"))
    rec("A13c", "取消后未执行项仍留在原位置",
        "通过" if len(on_disk) == 10 else "未通过",
        f"原目录剩余用户文件={len(on_disk)}（应为 10）", steps)


# ---------------- 场景 7：进程中断恢复 A14 / A15 ----------------

CHILD = os.path.join(HARNESS, "_child_interrupt.py")


def _write_child():
    with open(CHILD, "w", encoding="utf-8") as f:
        f.write(f'''import os, sys
sys.path.insert(0, {SRC!r})
from scanner import scan_directory
from planner import generate_plan, MODE_BY_TYPE, DEFAULT_COURSE_RULES
from storage import Storage
from utils import sha256_file

root = sys.argv[1]
n = int(sys.argv[2])
sr = scan_directory(root, detect_project_roots=False)
plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
st = Storage(root)
bid = st.create_batch(plan, "进程中断复现")
for idx, item in enumerate(plan.items):
    src = os.path.join(root, item.file.rel_path)
    st.create_action(bid, item, idx, item.source_sha256 or sha256_file(src) or "")
st.update_batch_status(bid, "executing")
for i in range(min(n, len(plan.items))):
    item = plan.items[i]
    src = os.path.join(root, item.file.rel_path)
    tgt = os.path.join(root, item.target_rel_path)
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    os.rename(src, tgt)
# 模拟进程在 os.rename 之后、update_action 之前被强杀
os._exit(9)
''')


def s07_recovery():
    _write_child()
    root = fresh("s07_recover")
    for i in range(5):
        mk(root, f"hw_{i}.py", f"print('hw {i}')")
    write_before(root)

    steps = ("见 test_v10.py s07_recovery()：子进程 _child_interrupt.py 移动前 2 项后 os._exit(9)，"
             "父进程重新打开 Storage 调 recover_batch()")

    proc = subprocess.run([sys.executable, CHILD, root, "2"],
                          capture_output=True, text=True)
    interrupted = proc.returncode == 9
    print(f"        子进程退出码={proc.returncode}（9=模拟强杀）")

    st = Storage(root)
    batch_id = st.list_batches()[0].batch_id
    rec_pre = recover_batch(st, batch_id)
    acts = st.get_actions(batch_id)
    batch_status = st.get_batch(batch_id).status
    st.close()

    recovered = sum(1 for a in acts if a.status == "moved")
    planned = sum(1 for a in acts if a.status == "planned")
    rec("A14", "进程被强杀后重启：按磁盘实际状态核对未完成批次，不重复移动/不丢失",
        "通过" if interrupted and recovered == 2 and planned == 3
        and rec_pre.recovered_moved == 2 else "未通过",
        f"子进程被中断={interrupted}；恢复报告：{rec_pre.summary()}\n"
        + "\n".join(rec_pre.report), steps)

    # A15：日志状态为 planned 但磁盘已移动 → 必须识别为 moved，不能当没移动
    b_case = [a for a in acts if a.status == "moved" and a.action_index < 2]
    rec("A15", "「已移动但结果未记录」的不确定状态被识别为 moved（未仅凭日志判为未移动）",
        "通过" if len(b_case) == 2 and "恢复补记" in b_case[0].error else "未通过",
        f"补记项={[(a.source_rel, a.status, a.error) for a in b_case]}", steps)

    # 无重复副本：移动过的文件不应同时存在于源与目标
    dup = [a for a in acts if a.status == "moved"
           and os.path.isfile(os.path.join(root, a.source_rel))
           and os.path.isfile(os.path.join(root, a.target_rel))]
    rec("A14b", "恢复后不存在「源与目标同时存在」的重复副本",
        "通过" if not dup else "未通过",
        f"重复项={[a.source_rel for a in dup]}", steps)


def s07b_cases():
    """恢复分类：C(目标内容不符) / D(两者都不存在) / E(源与目标同时存在且不符) / F(两者同时存在且相符) → 全部 paused。"""
    root = fresh("s07b_recover_cases")
    for i in range(4):
        mk(root, f"g_{i}.txt", f"case 文件 {i}")
    write_before(root)
    _write_child()
    steps = ("见 test_v10.py s07b_cases()：子进程中断后，用磁盘改造构造 C/D/E/F 四类异常状态，"
             "recover_batch() 应全部标为 paused 并给出原因")

    subprocess.run([sys.executable, CHILD, root, "0"], capture_output=True, text=True)
    st = Storage(root)
    batch_id = st.list_batches()[0].batch_id
    acts = st.get_actions(batch_id)
    st.close()

    a0, a1, a2, a3 = acts[0], acts[1], acts[2], acts[3]
    # E：源与目标同时存在，目标内容不符
    tgt0 = os.path.join(root, a0.target_rel)
    os.makedirs(os.path.dirname(tgt0), exist_ok=True)
    with open(tgt0, "w", encoding="utf-8") as f:
        f.write("目标内容与预期不符")
    # D：两者都不存在
    os.remove(os.path.join(root, a1.source_rel))
    # C：源消失、目标存在但内容不符
    tgt2 = os.path.join(root, a2.target_rel)
    os.makedirs(os.path.dirname(tgt2), exist_ok=True)
    with open(tgt2, "w", encoding="utf-8") as f:
        f.write("另一个不符内容")
    os.remove(os.path.join(root, a2.source_rel))
    # F：源与目标同时存在且内容相同（可能跨盘 copy 未删源）→ 保守 paused
    src3 = os.path.join(root, a3.source_rel)
    tgt3 = os.path.join(root, a3.target_rel)
    os.makedirs(os.path.dirname(tgt3), exist_ok=True)
    shutil.copyfile(src3, tgt3)

    st = Storage(root)
    rr = recover_batch(st, batch_id)
    acts2 = {a.action_index: a for a in st.get_actions(batch_id)}
    bs = st.get_batch(batch_id).status
    st.close()

    ok = all(acts2[i].status == "paused" for i in (0, 1, 2, 3)) and rr.paused == 4
    rec("A14c", "磁盘状态不明确的 C/D/E/F 四类均暂停人工处理（不盲目重放）",
        "通过" if ok else "未通过",
        f"状态={[acts2[i].status for i in (0,1,2,3)]} 报告={rr.summary()} 批次状态={bs}", steps)


# ---------------- 场景 8：重叠批次 A17 ----------------

def s08_multi_batch():
    root = fresh("s08_multi")
    for i in range(4):
        mk(root, f"m_{i}.txt", f"多批次文件 {i}")
    write_before(root)
    _write_child()
    steps = ("见 test_v10.py s08_multi_batch()：用子进程留下一个 executing 未恢复批次，"
             "检查 has_unrecovered_batch，并尝试再启动一次整理")

    subprocess.run([sys.executable, CHILD, root, "1"], capture_output=True, text=True)
    st = Storage(root)
    pending = st.get_pending_batch()
    unrecovered = has_unrecovered_batch(st)
    st.close()

    rec("A17a", "存在未恢复批次时能被检测到（提示应先处理当前任务）",
        "通过" if pending is not None and unrecovered else "未通过",
        f"pending=#{pending.batch_id if pending else None} status="
        f"{pending.status if pending else None} has_unrecovered={unrecovered}", steps)

    # 再启动一次整理，观察是否产生重叠批次
    sr = scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    st = Storage(root)
    er = execute_plan(plan, st)
    batches = st.list_batches()
    st.close()
    overlap = len(batches) > 1 and any(b.status in ("executing", "planning") for b in batches)
    # 若第一次被正确拦截，只会存在 1 个批次（第一个 interrupted 的）
    rec("A17b", "未恢复批次存在时再次开始整理：不产生重叠批次",
        "通过" if not overlap and len(batches) == 1 else "未通过",
        f"批次数={len(batches)} 状态={[b.status for b in batches]} "
        f"第二次执行={er.summary()}", steps)

    # A17c 连续点击开始：不产生两个执行中的批次
    root2 = fresh("s08c_double_click")
    for i in range(3):
        mk(root2, f"d_{i}.txt", f"连点 {i}")
    sr = scan_directory(root2, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    st = Storage(root2)
    e1 = execute_plan(plan, st)
    e2 = execute_plan(plan, st)
    sts = [b.status for b in st.list_batches()]
    st.close()
    rec("A17c", "对同一计划连续执行两次：不产生重叠的「执行中」批次",
        "通过" if sts.count("executing") == 0 else "未通过",
        f"两次执行={e1.summary()} / {e2.summary()} 批次状态={sts}", steps)


# ---------------- 汇总 ----------------

def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(HARNESS, exist_ok=True)
    print("=" * 78)
    print("归档盒 V1.0 最终回归")
    print(f"测试根目录 base = {BASE}")
    print(f"解释器 = {sys.executable}")
    print(f"Python = {sys.version.split()[0]}")
    print("=" * 78)

    scenarios = [
        ("扫描基础 A01/A02/A16", s01_scan),
        ("计划正确性 A03–A07", s02_plan),
        ("预览后变化 A08", s03_preview_change),
        ("端到端与完整撤销 A09/A18", s04_e2e),
        ("撤销冲突 A10/A11/A12", s05_undo_conflict),
        ("取消 A13", s06_cancel),
        ("进程中断恢复 A14/A15", s07_recovery),
        ("恢复分类 C/D/E", s07b_cases),
        ("重叠批次 A17", s08_multi_batch),
    ]

    for title, fn in scenarios:
        print(f"\n--- {title} ---")
        try:
            fn()
        except Exception as e:
            rec("SCENARIO", f"{title} 执行异常", "未通过",
                f"{e}\n{traceback.format_exc()[-1200:]}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    unv = sum(1 for r in RESULTS if r["status"] == "未验证")

    print("\n" + "=" * 78)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}")
    print("=" * 78)
    for r in RESULTS:
        print(f"  {r['status']:>5}  {r['id']:<8} {r['title']}")

    txt = os.path.join(OUT, f"v10_results_{TS}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.0 最终回归 — 结果清单\n")
        f.write(f"时间: {TS}\n")
        f.write(f"解释器: {sys.executable}\n")
        f.write(f"Python: {sys.version.split()[0]}  PySide6: 6.12.0\n")
        f.write(f"测试根目录: {BASE}\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            if r["evidence"]:
                for line in r["evidence"].splitlines():
                    f.write(f"    证据: {line}\n")
            if r["steps"]:
                f.write(f"    复现: {r['steps']}\n")
            f.write("\n")
    print(f"\n结果文件: {txt}")

    with open(os.path.join(OUT, f"v10_results_{TS}.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": TS, "results": RESULTS,
                   "summary": {"pass": passed, "fail": failed, "unverified": unv}},
                  f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
