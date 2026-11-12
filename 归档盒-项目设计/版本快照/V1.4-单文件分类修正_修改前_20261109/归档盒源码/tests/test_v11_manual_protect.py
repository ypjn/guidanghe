"""
归档盒 V1.1 — 手动保护目录验证（不依赖界面也可运行，M9/M10 用 offscreen Qt）

覆盖：
- 保护目录内文件不进入计划，外部文件正常参与
- 父子目录同时保护时只算一次、深层仍被跳过
- 移除手动保护后重新扫描生效
- 配置按根目录持久化、跨进程恢复、切换根目录不串用
- 默认保护（归档盒源码）不因移除手动保护而失效
- 路径校验：根目录外/不存在/符号链接/根目录自身/程序目录一律拒绝，规范化去重
- 保护原因与跳过数量在扫描结果中可区分（default / auto / manual）
- 修改保护配置后旧预览立即失效；忙碌/关闭等待期间禁止编辑

运行：
  QT_QPA_PLATFORM=offscreen \
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/test_v11_manual_protect.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scanner import scan_directory
from planner import generate_plan, MODE_BY_TYPE
from protect_config import (
    KIND_LABELS, ProtectConfigError, dedupe_manual_paths,
    load_manual_protect, manual_scan_specs, normalize_manual_path,
    save_manual_protect, config_path,
)

BASE = "/tmp/_v11_manual_protect_verify"
RESULTS: list[dict] = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status, "evidence": evidence})
    mark = {"通过": "PASS", "未通过": "FAIL"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"        {line}")


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, *parts, content=None):
    """创建文件：mk(root, "dir", "file.ext", "长内容") 或 content=...。"""
    if content is None and len(parts) >= 2:
        last = parts[-1]
        if isinstance(last, str) and len(last) >= 20 and "/" not in last:
            content, parts = last, parts[:-1]
    if content is None:
        content = "x"
    full = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return full


def plan_rel_paths(scan_result):
    plan = generate_plan(scan_result, MODE_BY_TYPE, [])
    return {i.file.rel_path for i in plan.items}


DEFAULT_SPEC = lambda p: (p, "default", "默认保护（归档盒源码）")


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)

    # ========== M1: 手动保护目录内文件不进入计划，外部文件正常参与 ==========
    root = fresh("m1")
    mk(root, "课程", "讲义.pdf", "a" * 300)
    mk(root, "课程", "作业.docx", "b" * 300)
    mk(root, "私有资料", "secret.txt", "s" * 300)
    mk(root, "私有资料", "子目录", "deep.bin", "d" * 300)
    priv = os.path.join(root, "私有资料")

    sr = scan_directory(root, protect_dirs=manual_scan_specs(root, [priv]),
                        detect_project_roots=False)
    rels = plan_rel_paths(sr)
    m1_ok = (rels == {"课程/讲义.pdf", "课程/作业.docx"}
             and sr.protected_count == 2
             and "用户手动保护" in sr.protected_roots.get(priv, "")
             and sr.protected_kinds.get(priv) == "manual")
    rec(
        "M1",
        "手动保护目录内文件被跳过，外部文件正常进入计划",
        "通过" if m1_ok else "未通过",
        f"计划文件={sorted(rels)}\n保护跳过={sr.protected_count} "
        f"原因={sr.protected_roots.get(priv)} 类别={sr.protected_kinds.get(priv)}",
    )

    # ========== M2: 父子目录同时保护 → 深层文件只跳过一次 ==========
    root = fresh("m2")
    mk(root, "父", "a.txt")
    mk(root, "父", "子", "b.txt")
    mk(root, "父", "子", "深", "c.txt")
    mk(root, "外部", "d.txt")
    parent = os.path.join(root, "父")
    child = os.path.join(parent, "子")

    sr = scan_directory(root, protect_dirs=manual_scan_specs(root, [parent, child]),
                        detect_project_roots=False)
    rels = plan_rel_paths(sr)
    m2_ok = (rels == {"外部/d.txt"}
             and sr.protected_count == 3
             and len(sr.protected_roots) == 2
             and not any(r.startswith("父/") for r in rels))
    rec(
        "M2",
        "父子目录同时保护：深层文件仅计一次且全部跳过",
        "通过" if m2_ok else "未通过",
        f"计划文件={sorted(rels)} 保护跳过={sr.protected_count}（应 3）\n"
        f"保护根={sorted(os.path.relpath(p, root) for p in sr.protected_roots)}",
    )

    # ========== M3: 移除手动保护后重新扫描生效 ==========
    root = fresh("m3")
    mk(root, "私有", "a.txt")
    mk(root, "公开", "b.txt")
    priv = os.path.join(root, "私有")
    sr_with = scan_directory(root, protect_dirs=manual_scan_specs(root, [priv]),
                             detect_project_roots=False)
    sr_without = scan_directory(root, protect_dirs=manual_scan_specs(root, []),
                                detect_project_roots=False)
    m3_ok = ("私有/a.txt" not in plan_rel_paths(sr_with)
             and "私有/a.txt" in plan_rel_paths(sr_without)
             and sr_without.protected_count == 0)
    rec(
        "M3",
        "移除手动保护后重新扫描，文件重新进入计划",
        "通过" if m3_ok else "未通过",
        f"保护时计划={sorted(plan_rel_paths(sr_with))}\n"
        f"移除后计划={sorted(plan_rel_paths(sr_without))}",
    )

    # ========== M4: 配置跨重启持久化（独立子进程重新读取） ==========
    root = fresh("m4")
    mk(root, "A", "a.txt")
    mk(root, "B", "b.txt")
    mk(root, "C", "c.txt")
    pa, pb = os.path.join(root, "A"), os.path.join(root, "B")
    save_manual_protect(root, [pa, pb, pa])   # 重复项应被去重
    reloaded = load_manual_protect(root)
    child_code = (
        "import sys, json; sys.path.insert(0, %r);"
        "from protect_config import load_manual_protect;"
        "print(json.dumps(load_manual_protect(%r)))" % (SRC, root)
    )
    proc = subprocess.run([sys.executable, "-c", child_code],
                          capture_output=True, text=True, timeout=30)
    try:
        sub_paths = json.loads(proc.stdout.strip())
    except Exception:
        sub_paths = []
    m4_ok = (sorted(reloaded) == sorted([pa, pb])
             and sorted(sub_paths) == sorted([pa, pb])
             and os.path.isfile(config_path(root)))
    rec(
        "M4",
        "手动保护配置按根目录持久化，跨进程重启后恢复",
        "通过" if m4_ok else "未通过",
        f"配置文件={config_path(root)}\n同进程读回={sorted(os.path.basename(p) for p in reloaded)}\n"
        f"新进程读回={sorted(os.path.basename(p) for p in sub_paths)}\n"
        f"子进程退出码={proc.returncode}",
    )

    # ========== M5: 默认保护不因移除手动保护而失效 ==========
    root = fresh("m5")
    src_dir = os.path.join(root, "归档盒源码")
    mk(root, "归档盒源码", "main.py")
    mk(root, "归档盒源码", "lib", "util.py")
    mk(root, "普通资料", "a.txt")
    default_specs = [DEFAULT_SPEC(src_dir)]
    sr_default = scan_directory(root, protect_dirs=default_specs,
                                detect_project_roots=False)
    sr_after_manual_removed = scan_directory(
        root, protect_dirs=default_specs + manual_scan_specs(root, []),
        detect_project_roots=False,
    )
    m5_ok = (
        "归档盒源码/main.py" not in plan_rel_paths(sr_default)
        and "归档盒源码/lib/util.py" not in plan_rel_paths(sr_default)
        and "归档盒源码/main.py" not in plan_rel_paths(sr_after_manual_removed)
        and sr_after_manual_removed.protected_kinds.get(src_dir) == "default"
        and "普通资料/a.txt" in plan_rel_paths(sr_after_manual_removed)
    )
    rec(
        "M5",
        "默认保护（归档盒源码）不因移除手动保护而解除",
        "通过" if m5_ok else "未通过",
        f"默认保护跳过={sr_default.protected_count} / "
        f"移除手动后跳过={sr_after_manual_removed.protected_count}\n"
        f"类别={sr_after_manual_removed.protected_kinds.get(src_dir)}（应 default）\n"
        f"计划={sorted(plan_rel_paths(sr_after_manual_removed))}",
    )

    # ========== M6: 路径校验 / 规范化 / 去重 / 符号链接 ==========
    root = fresh("m6")
    inner = os.path.join(root, "内层")
    os.makedirs(inner)
    os.makedirs(os.path.join(root, ".organizer"))
    outside = fresh("m6_outside")
    link_dir = os.path.join(root, "链接目录")
    try:
        os.symlink(inner, link_dir)
    except OSError:
        link_dir = None

    cases = []
    def expect_reject(label, path):
        try:
            normalize_manual_path(root, path)
            cases.append((label, False, "未拒绝"))
        except ProtectConfigError as e:
            cases.append((label, True, str(e)))

    expect_reject("根目录自身", root)
    expect_reject("根目录外", outside)
    expect_reject("不存在", os.path.join(root, "不存在"))
    expect_reject(".organizer", os.path.join(root, ".organizer"))
    if link_dir:
        expect_reject("符号链接目录", link_dir)
    # 合法：带尾随斜杠 / 相对路径 / 重复项
    norm = normalize_manual_path(root, inner + os.sep)
    deduped = dedupe_manual_paths(root, [inner, inner + os.sep, "内层"])
    m6_ok = (all(ok for _, ok, _ in cases)
             and norm == os.path.abspath(inner)
             and len(deduped) == 1)
    rec(
        "M6",
        "只接受根目录内有效目录：拒绝根/外部/不存在/程序目录/符号链接，规范化并去重",
        "通过" if m6_ok else "未通过",
        "\n".join(f"{label}: {'拒绝' if ok else '接受(错误)'} — {msg}"
                  for label, ok, msg in cases)
        + f"\n规范化={norm}\n去重后={deduped}",
    )

    # ========== M7: 三类保护原因可区分 + 跳过数量 ==========
    root = fresh("m7")
    mk(root, "项目", ".git", "config")
    mk(root, "项目", "src", "main.py")
    mk(root, "手动保护", "secret.txt")
    mk(root, "默认保护", "app.py")
    mk(root, "普通", "a.txt")
    proj = os.path.join(root, "项目")
    manual = os.path.join(root, "手动保护")
    default_dir = os.path.join(root, "默认保护")
    specs = [DEFAULT_SPEC(default_dir)] + manual_scan_specs(root, [manual])
    sr = scan_directory(root, protect_dirs=specs, detect_project_roots=True)
    kinds = {os.path.basename(p): k for p, k in sr.protected_kinds.items()}
    m7_ok = (kinds.get("手动保护") == "manual"
             and kinds.get("默认保护") == "default"
             and "auto" in kinds.get("项目", "")
             and sr.protected_count == 3
             and "普通/a.txt" in plan_rel_paths(sr))
    rec(
        "M7",
        "扫描结果区分默认/自动/手动保护，并显示跳过数量",
        "通过" if m7_ok else "未通过",
        f"保护类别={kinds}\n跳过文件数={sr.protected_count}（应 3）\n"
        + "\n".join(f"  {os.path.relpath(p, root)} → {r}"
                    for p, r in sr.protected_roots.items()),
    )

    # ========== M8: 配置按根目录隔离，切换目录不串用 ==========
    root_a = fresh("m8_a")
    root_b = fresh("m8_b")
    mk(root_a, "A", "a.txt")
    mk(root_b, "B", "b.txt")
    save_manual_protect(root_a, [os.path.join(root_a, "A")])
    m8_ok = (len(load_manual_protect(root_a)) == 1
             and load_manual_protect(root_b) == []
             and not os.path.exists(config_path(root_b)))
    rec(
        "M8",
        "按根目录隔离：A 的配置不会出现在 B",
        "通过" if m8_ok else "未通过",
        f"A 配置={load_manual_protect(root_a)}\nB 配置={load_manual_protect(root_b)}",
    )

    # ========== M9: 界面添加/移除手动保护（真实对话框控件） ==========
    from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
    import main as main_mod
    from main import MainWindow, ProtectDirDialog

    app = QApplication.instance() or QApplication([])
    uroot = fresh("m9_ui")
    mk(uroot, "私有", "x.txt")
    mk(uroot, "公开", "y.txt")
    priv = os.path.join(uroot, "私有")

    w = MainWindow()
    w.path_edit.setText(uroot)
    w.scan_result = scan_directory(uroot, detect_project_roots=False)
    w.show()
    app.processEvents()

    dialog = ProtectDirDialog(uroot, [], [], 0, w)
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **kw: priv)
    dialog._on_add()
    after_add = list(dialog.get_paths())
    added = after_add == [os.path.abspath(priv)]
    dialog.manual_list.setCurrentRow(0)
    dialog._on_remove()
    after_remove = list(dialog.get_paths())
    removed = after_remove == []
    m9a_ok = added and removed
    rec(
        "M9",
        "界面可添加/移除手动保护目录（真实对话框控件）",
        "通过" if m9a_ok else "未通过",
        f"添加后={after_add}（应包含 {priv}）\n移除后={after_remove}",
    )
    dialog.deleteLater()

    # 忙碌 / 关闭等待期间禁止编辑
    opened = {"count": 0}
    real_dialog = main_mod.ProtectDirDialog

    class _SpyDialog(real_dialog):
        def __init__(self, *a, **kw):
            opened["count"] += 1
            super().__init__(*a, **kw)

    main_mod.ProtectDirDialog = _SpyDialog
    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    w.manual_protect = []
    w._working = True
    w.on_edit_protect()
    busy_blocked = opened["count"] == 0 and w.manual_protect == []
    w._working = False
    w._close_pending = True
    w.on_edit_protect()
    close_blocked = opened["count"] == 0
    w._close_pending = False
    main_mod.ProtectDirDialog = real_dialog
    rec(
        "M10",
        "忙碌/关闭等待期间禁止修改保护配置",
        "通过" if (busy_blocked and close_blocked) else "未通过",
        f"忙碌时未打开对话框={busy_blocked}，关闭等待时未打开={close_blocked}",
    )

    # ========== M11: 保护配置变更后旧预览立即失效 ==========
    w.manual_protect = [priv]
    save_manual_protect(uroot, [priv])
    w._reset_for_new_dir(uroot)
    restored = w.manual_protect == [os.path.abspath(priv)]

    w.scan_result = scan_directory(uroot, protect_dirs=manual_scan_specs(uroot, [priv]),
                                   detect_project_roots=False)
    plan = generate_plan(w.scan_result, MODE_BY_TYPE, [])
    w.current_plan = plan
    w._plan_valid = True
    w._populate_table(plan)
    w._update_button_states()
    pre_execute = w.btn_execute.isEnabled()

    w._invalidate_for_protect_change()
    invalidated = (
        w.scan_result is None and w.current_plan is None and not w._plan_valid
        and not w.btn_execute.isEnabled() and not w.btn_regenerate.isEnabled()
        and w.invalid_label.isVisible()
        and "重新扫描" in w.invalid_label.text()
    )
    m11_ok = restored and pre_execute and invalidated
    rec(
        "M11",
        "重启后恢复手动保护；修改保护配置后旧预览立即失效并要求重新扫描",
        "通过" if m11_ok else "未通过",
        f"重启恢复={w.manual_protect}（应 [{priv}]）\n"
        f"失效前可执行={pre_execute}；失效后 scan_result/plan 清空、执行禁用、"
        f"重新预览禁用、提示={w.invalid_label.text()}",
    )

    try:
        if w._storage is not None:
            w._storage.close()
            w._storage = None
        w.close()
    except Exception:
        pass

    # ---------- 汇总 ----------
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    unv = sum(1 for r in RESULTS if r["status"] == "未验证")
    exit_code = 0 if (failed == 0 and unv == 0 and len(RESULTS) > 0) else 1

    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    results_dir = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "v11_manual_protect",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v11_manual_protect.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "base": BASE,
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": unv},
    }
    out = os.path.join(results_dir, "results_v11_manual_protect.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(results_dir, "results_v11_manual_protect.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.1 手动保护目录验证 — 结果清单\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {sys.version.split()[0]}\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未验证 {unv}\n")
        f.write(f"退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            if r["evidence"]:
                for line in str(r["evidence"]).splitlines():
                    f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    print(f"结果已保存: {txt}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
