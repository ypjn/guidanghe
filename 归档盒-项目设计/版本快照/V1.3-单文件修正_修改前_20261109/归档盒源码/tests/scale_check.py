"""
归档盒 V1.1 — 第一阶段真实规模验收（在真实资料的独立副本上执行）

范围：大二上、大一下、校园事务、Code、个人项目灵感、大创答辩
副本：/tmp/归档盒_阶段1副本_20261106（已用 rsync 从真实 NEU 复制，真实目录只读）
不触碰真实 NEU 原目录。

流程：
  扫描 → 生成计划 → 分类/保护检查 → 执行前清单 → 整理 → 关闭重开 → 撤销
  → 撤销后清单比对（相对路径→大小/SHA-256 映射 + 内容指纹多重集合）
  另测：计划生成中取消、整理中取消、扫描中关闭、计划生成中关闭、整理中关闭
  记录：文件数、数据量、扫描/计划/执行/撤销耗时、关闭等待时长、界面响应性

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/scale_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from main import MainWindow, ExecuteConfirmDialog
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES
from storage import Storage
from executor import execute_plan, undo_latest_batch
from utils import sha256_file

COPY_ROOT = "/tmp/归档盒_阶段1副本_20261106"
OUT_DIR = "/tmp/归档盒_阶段1验收"
MANIFEST_BEFORE = os.path.join(OUT_DIR, "manifest_before.json")
MANIFEST_AFTER_UNDO = os.path.join(OUT_DIR, "manifest_after_undo.json")
MANIFEST_FINAL = os.path.join(OUT_DIR, "manifest_final.json")
RESULTS = []

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manifest_tool import fs_manifest, compare_manifests, dup_stats as manifest_stats


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
    mark = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"        {line}")


def dump(path, manifest):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    app = QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    # 测试环境自动接受「开始整理」确认对话框（真实对话框逻辑不在本脚本验证范围）
    class _AutoAcceptExecuteDialog(ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    # ---------- 1. 扫描 ----------
    t0 = time.time()
    sr = scan_directory(COPY_ROOT, detect_project_roots=True)
    t_scan = time.time() - t0
    total_bytes = sum(e.size for e in sr.files)
    rec("S1", "真实副本扫描",
        "通过" if sr.files else "未通过",
        f"可见文件 {len(sr.files)} 个，{total_bytes / 1e9:.2f} GB；"
        f"跳过 {len(sr.skipped)}（保护 {sr.protected_count}）；"
        f"扫描耗时 {t_scan:.2f}s；错误 {len(sr.errors)}")

    # ---------- 2. 执行前清单（独立文件系统枚举） ----------
    print("    建立执行前清单（独立文件系统枚举，不依赖扫描结果）……")
    before, errs_before, t_manifest = fs_manifest(COPY_ROOT)
    dump(MANIFEST_BEFORE, before)
    g_b, f_b = manifest_stats(before)
    rec("S2", "执行前文件清单（独立枚举，副本之外保存）",
        "通过" if (before and not errs_before) else "未通过",
        f"{len(before)} 条（含被保护目录、_已整理、符号链接）；"
        f"枚举/指纹错误 {len(errs_before)} 个；耗时 {t_manifest:.1f}s；"
        f"重复组 {g_b} 组 / 组内文件 {f_b} 份；文件 {MANIFEST_BEFORE}")

    # ---------- 3. 生成计划（真实窗口，测界面响应性） ----------
    from PySide6.QtCore import QTimer
    ticks = []
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: ticks.append(time.time()))
    w = MainWindow()
    w.path_edit.setText(COPY_ROOT)
    w.scan_result = sr
    w._storage = Storage(COPY_ROOT)
    w.show()
    app.processEvents()
    timer.start()
    t0 = time.time()
    w._generate_and_show_plan()
    plan_done = False
    deadline = t0 + 600
    while time.time() < deadline:
        app.processEvents()
        if not w._plan_working and w._plan_valid:
            plan_done = True
            break
        time.sleep(0.01)
    timer.stop()
    t_plan = time.time() - t0
    plan = w.current_plan
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    max_gap = max(gaps) * 1000 if gaps else -1

    rec("S3", "生成整理计划（后台线程）",
        "通过" if (plan_done and plan is not None) else "未通过",
        f"计划 {plan.total if plan else 0} 个文件；冲突 {plan.conflict_count if plan else 0}；"
        f"耗时 {t_plan:.1f}s\n"
        f"界面响应：{len(ticks)} 次 50ms 心跳，最大间隔 {max_gap:.0f} ms")

    # 关闭计划窗口（释放其数据库连接，整理阶段使用独立 Storage）
    w.close()
    app.processEvents()

    # ---------- 4. 分类与保护检查 ----------
    if plan:
        top_counts = Counter(i.target_rel_path.split("/")[1]
                             for i in plan.items
                             if "/" in i.target_rel_path)
        protected = [(p.replace(COPY_ROOT + "/", ""), r)
                     for p, r in sr.protected_roots.items()]
        protected_in_plan = [
            i.file.rel_path for i in plan.items
            if any(i.file.abs_path.startswith(p + os.sep) for p in sr.protected_roots)
        ]
        rec("S4", "分类与项目保护边界检查",
            "通过" if (not protected_in_plan) else "未通过",
            f"计划目标顶层分布={dict(top_counts.most_common(8))}\n"
            f"自动保护根目录 {len(protected)} 个；保护文件进入计划={len(protected_in_plan)}\n"
            + "\n".join(f"  {p} → {r}" for p, r in protected[:6]))

    # ---------- 5. 整理（真实执行） ----------
    st = Storage(COPY_ROOT)
    t0 = time.time()
    result = execute_plan(plan, st)
    t_exec = time.time() - t0
    batch_id = result.batch_id
    rec("S5", "实际移动文件",
        "通过" if (batch_id > 0 and result.moved == plan.selected_count) else "未通过",
        f"批次 #{batch_id}；{result.summary()}；计划已选 {plan.selected_count}；"
        f"耗时 {t_exec:.1f}s")

    # ---------- 6. 关闭重开（独立进程读取批次） ----------
    st.close()
    child = (
        "import sys, json; sys.path.insert(0, %r);"
        "from storage import Storage; s=Storage(%r); b=s.get_latest_batch();"
        "print(json.dumps({'batch': b.batch_id if b else 0,"
        "'actions': len(b.actions) if b else 0})); s.close()"
        % (SRC, COPY_ROOT)
    )
    p = subprocess.run([sys.executable, "-c", child], capture_output=True,
                       text=True, timeout=60)
    try:
        info = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        info = {"batch": 0, "actions": 0}
    reopen_ok = info["batch"] == batch_id and info["actions"] == result.moved
    rec("S6", "关闭重开后仍能读取整理批次",
        "通过" if reopen_ok else "未通过",
        f"独立进程读到批次 #{info['batch']}，动作 {info['actions']} 条"
        f"（应 #{batch_id} / {result.moved}）")

    # ---------- 7. 撤销 ----------
    st = Storage(COPY_ROOT)
    t0 = time.time()
    undo = undo_latest_batch(st)
    t_undo = time.time() - t0
    st.close()
    rec("S7", "撤销最近批次",
        "通过" if undo.batch_id == batch_id and undo.restored >= result.moved else "未通过",
        f"撤销批次 #{undo.batch_id}；{undo.summary()}；耗时 {t_undo:.1f}s")

    # ---------- 8. 撤销后清单比对（独立文件系统枚举） ----------
    print("    建立撤销后清单（独立文件系统枚举）……")
    after, errs_after, t_manifest2 = fs_manifest(COPY_ROOT)
    dump(MANIFEST_AFTER_UNDO, after)
    cmp = compare_manifests(before, after, errs_before, errs_after)
    ok8 = cmp["ok"]
    rec("S8", "撤销后相对路径→大小/SHA-256 映射与内容指纹多重集合一致（独立枚举）",
        "通过" if ok8 else "未通过",
        f"缺失 {cmp['missing_n']}、多余 {cmp['extra_n']}、内容变化 {cmp['changed_n']}；"
        f"多重集合一致={cmp['multiset_equal']}；"
        f"枚举/指纹错误 {cmp['errors_n']} 个\n"
        f"重复组（撤销前/后）={cmp['dup_groups_before']}/{cmp['dup_groups_after']} 组；"
        f"组内文件份数（撤销前/后）={cmp['dup_files_before']}/{cmp['dup_files_after']} 份\n"
        f"清单 {len(after)} 条，耗时 {t_manifest2:.1f}s\n"
        f"文件 {MANIFEST_AFTER_UNDO}"
        + (f"\n缺失示例={cmp['missing']}" if cmp["missing_n"] else "")
        + (f"\n多余示例={cmp['extra']}" if cmp["extra_n"] else "")
        + (f"\n变化示例={cmp['changed']}" if cmp["changed_n"] else "")
        + (f"\n错误示例={cmp['errors']}" if cmp["errors_n"] else ""))

    # ---------- 9. 计划生成中取消 ----------
    cancel_event = threading.Event()
    state = {"n": 0}

    def cb(cur, total, name):
        state["n"] = cur
        if cur >= 300:
            cancel_event.set()

    t0 = time.time()
    partial = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES,
                            cancel_event=cancel_event, progress_cb=cb)
    t_cancel_plan = time.time() - t0
    ok9 = (cancel_event.is_set() and t_cancel_plan < t_plan * 0.8)
    rec("S9", "计划生成中取消",
        "通过" if ok9 else "未通过",
        f"取消标记={cancel_event.is_set()}，处理到第 {state['n']} 个文件；"
        f"耗时 {t_cancel_plan:.1f}s（完整计划 {t_plan:.1f}s）；"
        f"返回半成品 {partial.total} 项（界面按取消丢弃）")

    # ---------- 10. 整理中取消并撤销 ----------
    small = main_mod.OrganizePlan(root=plan.root, mode=plan.mode,
                                  rules=plan.rules, items=plan.items[:120])
    st = Storage(COPY_ROOT)
    cancel2 = threading.Event()

    def ecb(cur, total, name):
        if cur >= 30:
            cancel2.set()

    t0 = time.time()
    res2 = execute_plan(small, st, progress_cb=ecb, cancel_event=cancel2)
    t_cancel_exec = time.time() - t0
    undo2 = undo_latest_batch(st) if res2.batch_id else None
    st.close()
    restored2 = undo2.restored if undo2 else 0
    ok10 = (cancel2.is_set() and res2.was_cancelled and res2.moved > 0
            and res2.batch_id > 0 and restored2 >= res2.moved)
    rec("S10", "整理中取消：已完成文件保留记录，撤销后恢复",
        "通过" if ok10 else "未通过",
        f"已移动 {res2.moved} / 计划 {small.total}；取消剩余 {res2.cancelled}；"
        f"批次 #{res2.batch_id}；撤销恢复 {restored2}；耗时 {t_cancel_exec:.1f}s")

    # ---------- 11. 扫描中关闭 ----------
    scan_gate = threading.Event()
    real_scan = main_mod.scan_directory

    def gated_scan(*a, **kw):
        scan_gate.wait(timeout=60)
        return real_scan(*a, **kw)

    main_mod.scan_directory = gated_scan
    w2 = MainWindow()
    w2.path_edit.setText(COPY_ROOT)
    w2.scan_result = None
    w2._storage = Storage(COPY_ROOT)
    w2.btn_scan.setEnabled(True)
    w2.show()
    app.processEvents()
    w2.btn_scan.click()
    started = False
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        if w2._scan_worker is not None and w2._scan_worker.isRunning():
            started = True
            break
        time.sleep(0.01)
    seq_before = w2._plan_seq_counter
    from PySide6.QtGui import QCloseEvent
    ev = QCloseEvent()
    w2.closeEvent(ev)
    pending = w2._close_pending
    t_close0 = time.time()
    scan_gate.set()
    closed = False
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if not w2.isVisible() and w2._storage is None:
            closed = True
            break
        time.sleep(0.01)
    t_close_wait_scan = time.time() - t_close0
    ok11 = (started and not ev.isAccepted() and pending and closed
            and w2._plan_worker is None and w2._plan_seq_counter == seq_before)
    main_mod.scan_directory = real_scan
    rec("S11", "扫描中请求关闭：不启动计划任务，线程结束后自动关闭",
        "通过" if ok11 else "未通过",
        f"扫描线程已启动={started}，关闭被拒绝={not ev.isAccepted()}，"
        f"关闭等待={pending}；等待时长 {t_close_wait_scan:.2f}s；"
        f"未启动 PlanWorker={w2._plan_worker is None}；自动关闭={closed}")

    # ---------- 12. 计划生成中关闭 ----------
    gen_gate = threading.Event()
    real_gen = main_mod.generate_plan

    def gated_gen(*a, **kw):
        gen_gate.wait(timeout=60)
        return real_gen(*a, **kw)

    main_mod.generate_plan = gated_gen
    w3 = MainWindow()
    w3.path_edit.setText(COPY_ROOT)
    w3.scan_result = sr
    w3._storage = Storage(COPY_ROOT)
    w3.show()
    app.processEvents()
    w3._generate_and_show_plan()
    started3 = False
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        if w3._plan_worker is not None and w3._plan_worker.isRunning():
            started3 = True
            break
        time.sleep(0.01)
    ev3 = QCloseEvent()
    w3.closeEvent(ev3)
    pending3 = w3._close_pending
    t3 = time.time()
    gen_gate.set()
    closed3 = False
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if not w3.isVisible() and w3._storage is None:
            closed3 = True
            break
        time.sleep(0.01)
    t_close_wait_plan = time.time() - t3
    ok12 = started3 and (not ev3.isAccepted()) and pending3 and closed3
    main_mod.generate_plan = real_gen
    rec("S12", "计划生成中请求关闭：取消生成并在结束后自动关闭",
        "通过" if ok12 else "未通过",
        f"计划线程已启动={started3}，进入关闭等待={pending3}；"
        f"等待时长 {t_close_wait_plan:.2f}s；自动关闭={closed3}")

    # ---------- 13. 整理中关闭 ----------
    from planner import OrganizePlan
    st = Storage(COPY_ROOT)
    full_plan = OrganizePlan(root=plan.root, mode=plan.mode, rules=plan.rules,
                             items=plan.items)
    w4 = MainWindow()
    w4.path_edit.setText(COPY_ROOT)
    w4.scan_result = sr
    w4.current_plan = full_plan
    w4._plan_valid = True
    w4._storage = st
    w4._update_button_states()
    w4.show()
    app.processEvents()
    t4 = time.time()
    w4.btn_execute.click()
    running = False
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        if w4._exec_worker is not None and w4._exec_worker.isRunning():
            running = True
            break
        time.sleep(0.01)

    # 等待执行线程真正开始移动文件（大数据量下前置 SHA 校验耗时较长），
    # 确认后再请求关闭，才能验证“整理中关闭”的协作式取消。
    first_moved_at = None
    deadline = time.time() + 240
    poll_st = Storage(COPY_ROOT)
    while time.time() < deadline:
        app.processEvents()
        b = poll_st.get_latest_batch()
        if b is not None and b.moved_count > 0:
            first_moved_at = time.time()
            break
        time.sleep(0.2)
    poll_st.close()
    app.processEvents()
    ev4 = QCloseEvent()
    t_close_request = time.time()
    w4.closeEvent(ev4)
    pending4 = w4._close_pending
    closed4 = False
    deadline = time.time() + 180
    while time.time() < deadline:
        app.processEvents()
        if not w4.isVisible() and w4._storage is None:
            closed4 = True
            break
        time.sleep(0.01)
    t_close_wait_exec = time.time() - t_close_request
    st2 = Storage(COPY_ROOT)
    b = st2.get_pending_batch() or st2.get_latest_batch()
    moved4 = b.moved_count if b else 0
    undone4 = 0
    if b and b.status != "undone":
        u = undo_latest_batch(st2)
        undone4 = u.restored
    st2.close()
    ok13 = (running and first_moved_at is not None and (not ev4.isAccepted())
            and pending4 and closed4 and moved4 > 0)
    rec("S13", "整理中请求关闭：协作式取消、记录保留、可撤销",
        "通过" if ok13 else "未通过",
        f"执行线程已启动={running}；已开始移动文件={first_moved_at is not None}；"
        f"进入关闭等待={pending4}；关闭等待时长 {t_close_wait_exec:.2f}s；"
        f"已移动 {moved4} 个；撤销恢复 {undone4} 个；自动关闭={closed4}")

    # ---------- 14. 最终清单复核（独立文件系统枚举，所有取消/关闭测试之后） ----------
    print("    建立最终清单（独立文件系统枚举）……")
    final, errs_final, t_manifest3 = fs_manifest(COPY_ROOT)
    dump(MANIFEST_FINAL, final)
    cmp2 = compare_manifests(before, final, errs_before, errs_final)
    ok14 = cmp2["ok"]
    rec("S14", "全部故障测试后副本内容仍与执行前一致（独立枚举）",
        "通过" if ok14 else "未通过",
        f"缺失 {cmp2['missing_n']}、多余 {cmp2['extra_n']}、变化 {cmp2['changed_n']}；"
        f"多重集合一致={cmp2['multiset_equal']}；枚举/指纹错误 {cmp2['errors_n']} 个\n"
        f"重复组 {cmp2['dup_groups_after']} 组、组内文件 {cmp2['dup_files_after']} 份\n"
        f"清单 {len(final)} 条，耗时 {t_manifest3:.1f}s；文件 {MANIFEST_FINAL}"
        + (f"\n缺失示例={cmp2['missing']}" if cmp2["missing_n"] else "")
        + (f"\n多余示例={cmp2['extra']}" if cmp2["extra_n"] else "")
        + (f"\n变化示例={cmp2['changed']}" if cmp2["changed_n"] else "")
        + (f"\n错误示例={cmp2['errors']}" if cmp2["errors_n"] else ""))

    # ---------- 汇总 ----------
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if (failed == 0 and len(RESULTS) > 0) else 1

    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    results_dir = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "scale_check_stage1",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/scale_check.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "copy_root": COPY_ROOT,
        "manifest_before": MANIFEST_BEFORE,
        "manifest_after_undo": MANIFEST_AFTER_UNDO,
        "manifest_final": MANIFEST_FINAL,
        "files": len(sr.files),
        "data_bytes": total_bytes,
        "manifest_entries": len(before),
        "manifest_errors": len(errs_before) + len(errs_after) + len(errs_final),
        "dup": {
            "groups_before": cmp["dup_groups_before"],
            "files_before": cmp["dup_files_before"],
            "groups_after_undo": cmp["dup_groups_after"],
            "files_after_undo": cmp["dup_files_after"],
            "groups_final": cmp2["dup_groups_after"],
            "files_final": cmp2["dup_files_after"],
        },
        "durations": {
            "scan_s": round(t_scan, 2),
            "manifest_before_s": round(t_manifest, 1),
            "plan_s": round(t_plan, 1),
            "execute_s": round(t_exec, 1),
            "undo_s": round(t_undo, 1),
            "manifest_after_undo_s": round(t_manifest2, 1),
            "manifest_final_s": round(t_manifest3, 1),
            "close_wait_scan_s": round(t_close_wait_scan, 2),
            "close_wait_plan_s": round(t_close_wait_plan, 2),
            "close_wait_exec_s": round(t_close_wait_exec, 2),
        },
        "batch_id": batch_id,
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    out = os.path.join(results_dir, "results_scale_stage1.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    txt = os.path.join(results_dir, "results_scale_stage1.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("归档盒 V1.1 第一阶段真实规模验收 — 结果清单\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {sys.version.split()[0]}\n")
        f.write(f"副本: {COPY_ROOT}\n")
        f.write(f"文件 {meta['files']} 个，{meta['data_bytes'] / 1e9:.2f} GB\n")
        f.write(f"耗时: {json.dumps(meta['durations'], ensure_ascii=False)}\n")
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
