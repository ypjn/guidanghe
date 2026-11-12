"""
归档盒 — 独立清单工具专项测试（T1–T11）

目的：证明 tests/manifest_tool.py 的独立文件系统枚举能够正确识别
预期差异。T2–T7、T9、T11 中“通过”的含义是【正确识别了故意制造的差异】，
不是产品运行失败；T1、T8、T10 中“通过”的含义是【未产生虚假差异/排除规则正确】。

背景（本轮修复的漏洞）：旧清单 build_manifest(scan_result) 只遍历
首次扫描登记的 files+skipped 路径，新增文件、_已整理 残留文件均
不会出现在清单里，比对结果永远是“多余 0、多重集合一致”。

运行（纯标准库，无需 Qt）：
  .venv/bin/python tests/test_manifest_tool.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from manifest_tool import fs_manifest, compare_manifests, dup_stats

BASE = "/tmp/_manifest_tool_verify"
RESULTS: list = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    icon = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}[status]
    print(f"[{icon}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def wr(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return p


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    t_suite = time.time()

    # ---------- T1: 无变化 → 通过 ----------
    r1 = fresh("t1")
    wr(r1, "课程/a.pdf", "A" * 500)
    wr(r1, "课程/b.pdf", "B" * 500)
    m1a, e1a, _ = fs_manifest(r1)
    m1b, e1b, _ = fs_manifest(r1)
    c1 = compare_manifests(m1a, m1b, e1a, e1b)
    rec("T1", "文件没有变化 → 判定一致",
        "通过" if c1["ok"] else "未通过",
        f"清单 {len(m1a)} 条；ok={c1['ok']}；缺失/多余/变化="
        f"{c1['missing_n']}/{c1['extra_n']}/{c1['changed_n']}")

    # ---------- T2: 原目录新增文件 → 多余（旧工具漏检的反例） ----------
    r2 = fresh("t2")
    wr(r2, "课程/a.pdf", "A" * 500)
    m2a, e2a, _ = fs_manifest(r2)
    wr(r2, "课程/extra.txt", "新增文件")       # 清单建立后新增
    m2b, e2b, _ = fs_manifest(r2)
    c2 = compare_manifests(m2a, m2b, e2a, e2b)
    ok2 = (c2["extra_n"] == 1 and "课程/extra.txt" in c2["extra"]
           and not c2["ok"])
    rec("T2", "清单建立后原目录新增文件 → 检测为多余，整体失败",
        "通过" if ok2 else "未通过",
        f"多余 {c2['extra_n']} 个：{c2['extra']}；ok={c2['ok']}")

    # ---------- T3: _已整理 残留文件 → 多余（旧工具漏检的反例） ----------
    r3 = fresh("t3")
    wr(r3, "课程/a.pdf", "A" * 500)
    wr(r3, "_已整理/旧文件.txt", "执行前已有")   # 执行前已存在于 _已整理
    m3a, e3a, _ = fs_manifest(r3)
    wr(r3, "_已整理/leftover.txt", "撤销后意外残留")
    m3b, e3b, _ = fs_manifest(r3)
    c3 = compare_manifests(m3a, m3b, e3a, e3b)
    ok3 = (c3["extra_n"] == 1
           and "_已整理/leftover.txt" in c3["extra"] and not c3["ok"]
           and "_已整理/旧文件.txt" in m3a and "_已整理/旧文件.txt" in m3b)
    rec("T3", "_已整理 残留文件 → 检测为多余；执行前已有文件本就在清单内",
        "通过" if ok3 else "未通过",
        f"多余 {c3['extra_n']} 个：{c3['extra']}；"
        f"执行前已有 _已整理/旧文件.txt 在两侧清单={('_已整理/旧文件.txt' in m3a) and ('_已整理/旧文件.txt' in m3b)}；"
        f"ok={c3['ok']}")

    # ---------- T4: 删除原文件 → 缺失 ----------
    r4 = fresh("t4")
    wr(r4, "课程/a.pdf", "A" * 500)
    wr(r4, "课程/b.pdf", "B" * 500)
    m4a, e4a, _ = fs_manifest(r4)
    os.remove(os.path.join(r4, "课程", "a.pdf"))
    m4b, e4b, _ = fs_manifest(r4)
    c4 = compare_manifests(m4a, m4b, e4a, e4b)
    ok4 = (c4["missing_n"] == 1 and "课程/a.pdf" in c4["missing"]
           and not c4["ok"])
    rec("T4", "删除原文件 → 检测为缺失",
        "通过" if ok4 else "未通过",
        f"缺失 {c4['missing_n']} 个：{c4['missing']}；ok={c4['ok']}")

    # ---------- T5: 内容变化但大小不变 → 内容变化 ----------
    r5 = fresh("t5")
    wr(r5, "笔记.txt", "AAAA")          # 4 字节
    m5a, e5a, _ = fs_manifest(r5)
    wr(r5, "笔记.txt", "BBBB")          # 仍是 4 字节，内容已变
    m5b, e5b, _ = fs_manifest(r5)
    c5 = compare_manifests(m5a, m5b, e5a, e5b)
    ok5 = (c5["changed_n"] == 1 and "笔记.txt" in c5["changed"]
           and m5a["笔记.txt"][0] == m5b["笔记.txt"][0]     # 大小相同
           and m5a["笔记.txt"][1] != m5b["笔记.txt"][1]     # SHA 不同
           and not c5["ok"])
    rec("T5", "修改内容但保持文件大小 → 检测为内容变化",
        "通过" if ok5 else "未通过",
        f"变化 {c5['changed_n']} 个：{c5['changed']}；"
        f"大小相同、SHA 不同={m5a['笔记.txt'][0] == m5b['笔记.txt'][0]}；"
        f"ok={c5['ok']}")

    # ---------- T6: 同内容副本数量变化 → 多重集合检测 ----------
    r6 = fresh("t6")
    wr(r6, "d/副本1.txt", "SAME-CONTENT")
    wr(r6, "d/副本2.txt", "SAME-CONTENT")
    m6a, e6a, _ = fs_manifest(r6)
    g6a, f6a = dup_stats(m6a)
    os.remove(os.path.join(r6, "d", "副本2.txt"))          # 减少一份
    m6b, e6b, _ = fs_manifest(r6)
    g6b, f6b = dup_stats(m6b)
    c6_down = compare_manifests(m6a, m6b, e6a, e6b)
    wr(r6, "d/副本3.txt", "SAME-CONTENT")                    # 加回两份
    wr(r6, "d/副本4.txt", "SAME-CONTENT")
    m6c, e6c, _ = fs_manifest(r6)
    c6_up = compare_manifests(m6a, m6c, e6a, e6c)
    g6c, f6c = dup_stats(m6c)
    ok6 = (f6a == 2 and g6a == 1                            # 1 组 2 份
           and not c6_down["multiset_equal"]                 # 减少一份 → 多重集合不一致
           and not c6_up["multiset_equal"]                   # 增加副本 → 多重集合不一致
           and f6b == 0 and f6c == 3                         # 单份不成组；3 份成组
           and c6_down["multiset_equal"] is False)
    rec("T6", "同内容副本增减 → 多重集合检测到份数变化",
        "通过" if ok6 else "未通过",
        f"执行前：重复组 {g6a} 组 / {f6a} 份；减一份后 {g6b} 组 / {f6b} 份"
        f"（单份不成重复组），多重集合一致={c6_down['multiset_equal']}；"
        f"加两份后 {g6c} 组 / {f6c} 份，多重集合一致={c6_up['multiset_equal']}")

    # ---------- T7: 被保护目录内文件变化 → 仍能检测 ----------
    r7 = fresh("t7")
    os.makedirs(os.path.join(r7, "项目", ".git"))
    wr(r7, "项目/.git/HEAD", "ref: refs/heads/main")   # 使“项目”成为自动保护根
    wr(r7, "项目/main.py", "print(1)")
    m7a, e7a, _ = fs_manifest(r7)
    wr(r7, "项目/main.py", "print(2)")                  # 修改保护目录内文件
    m7b, e7b, _ = fs_manifest(r7)
    c7 = compare_manifests(m7a, m7b, e7a, e7b)
    ok7 = (c7["changed_n"] == 1 and "项目/main.py" in c7["changed"]
           and "项目/.git/HEAD" in m7a and not c7["ok"])
    rec("T7", "自动保护目录内的文件变化 → 仍能检测",
        "通过" if ok7 else "未通过",
        f"变化 {c7['changed_n']} 个：{c7['changed']}；"
        f"保护目录内文件在清单中={('项目/main.py' in m7a) and ('项目/.git/HEAD' in m7a)}；"
        f"ok={c7['ok']}")

    # ---------- T8: 符号链接不导致枚举越界 ----------
    r8 = fresh("t8")
    outside = os.path.join(BASE, "t8_outside")
    os.makedirs(outside)
    wr(outside, "外部文件.txt", "不应被枚举")
    wr(r8, "课程/a.pdf", "A" * 100)
    os.symlink(outside, os.path.join(r8, "外部目录链接"))   # 目录链接 → 根外
    os.symlink(outside + "/外部文件.txt",
               os.path.join(r8, "外部文件链接.txt"))        # 文件链接 → 根外
    inner = os.path.join(r8, "课程")
    os.symlink(inner, os.path.join(r8, "课程自引用"))       # 目录链接 → 根内
    m8, e8, _ = fs_manifest(r8)
    escaped = [k for k in m8 if k.startswith("外部目录链接/")
               or k.startswith("课程自引用/")]
    ok8 = ("外部文件链接.txt" in m8
           and m8["外部文件链接.txt"] == ["link", outside + "/外部文件.txt"]
           and "外部目录链接" in m8 and "课程自引用" in m8
           and not escaped and not e8)
    rec("T8", "符号链接不跟随、不越界：链接记录目标，目标内容不枚举",
        "通过" if ok8 else "未通过",
        f"清单 {len(m8)} 条；链接条目="
        f"{[k for k in m8 if m8[k][0] == 'link']}；"
        f"越界条目={escaped}；错误={len(e8)}")

    # ---------- T9: 不可读文件 → 显式报错，不静默判定通过 ----------
    r9 = fresh("t9")
    wr(r9, "课程/a.pdf", "A" * 100)
    wr(r9, "课程/锁.pdf", "L" * 100)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        rec("T9", "不可读文件 → 显式报错（以 root 运行，跳过 chmod 用例）",
            "未测", "root 权限下 chmod 000 仍可读取，用例不适用")
    else:
        m9a, e9a, _ = fs_manifest(r9)
        os.chmod(os.path.join(r9, "课程", "锁.pdf"), 0)
        try:
            m9b, e9b, _ = fs_manifest(r9)
            c9 = compare_manifests(m9a, m9b, e9a, e9b)
            ok9 = (len(e9b) >= 1
                   and any(p == "课程/锁.pdf" for p, _ in e9b)
                   and not c9["ok"])
            rec("T9", "不可读文件 → 显式报错并判失败，不静默通过",
                "通过" if ok9 else "未通过",
                f"枚举错误 {len(e9b)} 个：{[p for p, _ in e9b]}；"
                f"整体 ok={c9['ok']}")
        finally:
            os.chmod(os.path.join(r9, "课程", "锁.pdf"), 0o644)

    # ---------- 排除规则验证：.organizer 排除且仅此一项 ----------
    r10 = fresh("t10")
    wr(r10, "课程/a.pdf", "A" * 100)
    wr(r10, ".organizer/history.db", "db-data")
    wr(r10, "_已整理/用户文件.txt", "用户文件必须纳入")
    m10, e10, _ = fs_manifest(r10)
    ok10 = ("课程/a.pdf" in m10 and "_已整理/用户文件.txt" in m10
            and not any(k.startswith(".organizer") for k in m10)
            and len(m10) == 2)
    rec("T10", "排除规则：仅排除 .organizer，用户文件（含 _已整理）一律纳入",
        "通过" if ok10 else "未通过",
        f"清单条目 {len(m10)}：{sorted(m10)}；"
        f".organizer 被排除={not any(k.startswith('.organizer') for k in m10)}")

    # ---------- T11: 无法进入的目录 → 显式报错，不静默跳过 ----------
    # 回归：旧版 fs_manifest 未给 os.walk 传 onerror，权限拒绝的目录
    # 会被 os.walk 默认静默跳过，目录内全部文件从清单里消失且无任何错误。
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        rec("T11", "无法进入的目录 → 显式报错（以 root 运行，跳过 chmod 用例）",
            "未测", "root 权限下 chmod 000 仍可进入，用例不适用")
    else:
        r11 = fresh("t11")
        wr(r11, "课程/a.pdf", "A" * 100)
        wr(r11, "机密/内部文件.txt", "S" * 100)
        m11a, e11a, _ = fs_manifest(r11)          # 此时可正常进入
        base_ok = ("机密/内部文件.txt" in m11a and not e11a)
        os.chmod(os.path.join(r11, "机密"), 0)      # 目录本身不可进入
        try:
            m11b, e11b, _ = fs_manifest(r11)
            c11 = compare_manifests(m11a, m11b, e11a, e11b)
            ok11 = (base_ok
                    and len(e11b) >= 1
                    and any(p == "机密" for p, _ in e11b)
                    and "机密/内部文件.txt" not in m11b   # 文件确实未能登记
                    and not c11["ok"])                     # 整体核验判失败
            rec("T11", "无法进入的目录 → 显式报错并判失败，不静默跳过",
                "通过" if ok11 else "未通过",
                f"基线：目录内文件可登记={base_ok}；"
                f"chmod 000 后枚举错误 {len(e11b)} 个：{[p for p, _ in e11b]}；"
                f"目录内文件被静默漏掉={('机密/内部文件.txt' not in m11b)}；"
                f"整体 ok={c11['ok']}")
        finally:
            os.chmod(os.path.join(r11, "机密"), 0o755)

    shutil.rmtree(BASE, ignore_errors=True)

    # ---------- 汇总 ----------
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if (failed == 0 and RESULTS) else 1

    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    meta = {
        "suite": "manifest_tool",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/test_manifest_tool.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "base": BASE,
        "elapsed_s": round(time.time() - t_suite, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    out = os.path.join(HERE, "results_manifest_tool.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(HERE, "results_manifest_tool.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 — 独立清单工具专项测试结果\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in r["evidence"].splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    print(f"结果已保存: {txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
