"""
归档盒 V1.7 — Mac 集中验收：真实资料副本规模验收

在真实 NEU「大二上」资料的独立副本上执行（真实目录只读）：
扫描 → 计划 → 重复核对 → 范围整理 → 取消 → 关闭等待 → 中断恢复 → 撤销
→ 独立文件系统枚举 + SHA-256 核对路径、内容、副本数量。

副本根目录通过命令行传入，默认 /tmp/归档盒_集中验收副本_20261110。
所有实际文件操作只在该副本内进行。

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/acceptance_v17_scale.py \
      --root /tmp/归档盒_集中验收副本_20261110
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import main as main_mod
from main import MainWindow
from scanner import scan_directory
from planner import (
    build_plan, OrganizePlan, MODE_BY_DIRECTION_SEMESTER,
)
from classification_config import default_config
from storage import Storage
from executor import execute_plan, undo_latest_batch, recover_batch
from duplicate_report import assemble_report, entries_from_plan, verify_entries
from intake_model import IntakeList
from manifest_tool import fs_manifest, compare_manifests, dup_stats

RESULTS = []
OUT_DIR = "/tmp/archive_acceptance_20261110/scale"
MANIFEST_BEFORE = os.path.join(OUT_DIR, "manifest_before.json")
MANIFEST_FINAL = os.path.join(OUT_DIR, "manifest_final.json")

SCOPED_DIR = "C++程序设计"
SCOPED_FILE = os.path.join("周报", "双创委员报告样式.docx")
RECOVER_DIR = "概率论与数理统计"
CLOSE_DIR = "体育（轮滑）"


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
    mark = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}.get(status, "?")
    print(f"[{mark}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.02):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def wait_until(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(cond())


def dump(path, manifest):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)


def files_under(root, prefix):
    """副本内独立枚举某个相对目录下的文件（用于范围外保持检查）。"""
    out = {}
    base = os.path.join(root, prefix)
    for dirpath, _dirs, names in os.walk(base, followlinks=False):
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                continue
            st = os.stat(full)
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/归档盒_集中验收副本_20261110")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.isdir(root):
        print(f"副本目录不存在：{root}")
        return 2

    app = QApplication.instance() or QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    # ---------- A1 副本规模与真实资料只读 ----------
    total_files = 0
    total_bytes = 0
    for dirpath, _dirs, names in os.walk(root):
        if os.path.basename(dirpath) == ".organizer":
            continue
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                total_bytes += os.path.getsize(p)
                total_files += 1
            except OSError:
                pass
    real_src = "/Users/ypjn/Desktop/NEU/大二上"
    real_files = 0
    for dirpath, dirs, names in os.walk(real_src):
        dirs[:] = [d for d in dirs if d != "aigc2"]
        real_files += len(names)
    rec("A1", "规模验收副本与真实资料来源",
        "通过" if total_files > 1000 else "未通过",
        f"副本 {root}：{total_files} 个文件，{total_bytes / 1e9:.2f} GB\n"
        f"真实资料 {real_src}（只读，未写入）：{real_files} 个条目\n"
        f"所有实际文件操作仅在本副本内执行")

    # ---------- A2 独立执行前清单（不依赖扫描器/数据库） ----------
    print("    建立执行前独立清单（SHA-256）……")
    before, errs_before, t_m1 = fs_manifest(root)
    dump(MANIFEST_BEFORE, before)
    groups_b, files_b = dup_stats(before)
    rec("A2", "执行前独立文件系统枚举 + SHA-256",
        "通过" if (before and not errs_before) else "未通过",
        f"{len(before)} 条；枚举/指纹错误 {len(errs_before)}；耗时 {t_m1:.1f}s；"
        f"内容重复 {groups_b} 组 / {files_b} 份\n清单：{MANIFEST_BEFORE}")

    # ---------- A3 扫描 ----------
    t0 = time.time()
    sr = scan_directory(root, detect_project_roots=True)
    t_scan = time.time() - t0
    scanned_bytes = sum(f.size for f in sr.files)
    rec("A3", "真实副本扫描",
        "通过" if sr.files else "未通过",
        f"纳入 {len(sr.files)} 个文件，{scanned_bytes / 1e9:.2f} GB；"
        f"跳过 {len(sr.skipped)}（保护根 {sr.protected_count}）；"
        f"扫描错误 {len(sr.errors)}；耗时 {t_scan:.2f}s")

    # ---------- A4 计划生成（真实窗口 + QTimer 心跳） ----------
    ticks = []
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: ticks.append(time.time()))
    w = MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w._full_scan_result = sr
    w.scan_result = sr
    w._scan_generation = 1
    w.show()
    pump(app, 0.1)
    timer.start()
    t0 = time.time()
    w._generate_and_show_plan()
    plan_done = wait_until(
        app, lambda: (not w._plan_working and w._plan_valid), timeout=900)
    t_plan = time.time() - t0
    timer.stop()
    plan = w.current_plan
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    max_gap = max(gaps) * 1000 if gaps else -1
    rec("A4", "生成整理计划（后台线程，真实 QTimer 心跳）",
        "通过" if (plan_done and plan is not None and len(ticks) > 5) else "未通过",
        f"计划 {plan.total if plan else 0} 项；冲突 {plan.conflict_count if plan else 0}；"
        f"耗时 {t_plan:.1f}s\n心跳 {len(ticks)} 次（50ms），最大间隔 {max_gap:.0f} ms")

    # ---------- A5 保护边界 ----------
    protected_in_plan = []
    if plan:
        protected_roots = list(sr.protected_roots)
        protected_in_plan = [
            i.file.rel_path for i in plan.items
            if any(i.file.abs_path.startswith(p + os.sep) for p in protected_roots)]
    rec("A5", "项目保护目录不进入计划",
        "通过" if plan is not None and not protected_in_plan else "未通过",
        f"自动保护根 {sr.protected_count} 个；保护文件进入计划 "
        f"{len(protected_in_plan)} 个")

    # ---------- A6 重复核对（主窗口后台核对） ----------
    t0 = time.time()
    w.on_show_duplicates()
    dup_done = wait_until(
        app, lambda: (not w._working and w._duplicate_dialog is not None
                      and w._duplicate_dialog.report is not None), timeout=600)
    t_dup = time.time() - t0
    rep = w._duplicate_dialog.report if w._duplicate_dialog else None

    # 独立复算：只用清单里“已进入扫描”的文件，按内容指纹分组
    scanned_rels = {f.rel_path for f in sr.files}
    by_sha = {}
    for rel, val in before.items():
        if rel in scanned_rels and val and val[0] != "link":
            by_sha.setdefault(val[1], []).append(rel)
    exp_groups = {k: v for k, v in by_sha.items() if len(v) > 1}
    exp_files = sum(len(v) for v in exp_groups.values())
    dup_ok = (dup_done and rep is not None
              and rep.content_groups == len(exp_groups)
              and rep.content_files == exp_files
              and rep.verified == len(scanned_rels))
    rec("A6", "重复核对（后台）与独立 SHA-256 复算一致",
        "通过" if dup_ok else "未通过",
        f"报告：内容重复 {rep.content_groups if rep else '-'} 组 / "
        f"{rep.content_files if rep else '-'} 份；已核对 {rep.verified if rep else '-'}；"
        f"耗时 {t_dup:.1f}s\n"
        f"独立复算：内容重复 {len(exp_groups)} 组 / {exp_files} 份\n"
        f"同名不同内容 {rep.same_name_groups if rep else '-'} 组"
        if rep else f"报告未生成；耗时 {t_dup:.1f}s")
    if w._duplicate_dialog is not None:
        w._duplicate_dialog.close()
        pump(app)

    # ---------- A7 范围整理（待整理清单子目录 + 单文件） ----------
    intake = IntakeList()
    r1 = intake.add(os.path.join(root, SCOPED_DIR), root)
    r2 = intake.add(os.path.join(root, SCOPED_FILE), root)
    resolution = intake.resolve(root, sr)
    effective_files = [f for f in sr.files if f.rel_path in resolution.included]
    effective = type(sr)(root=sr.root, files=effective_files)
    scoped_plan = build_plan(effective, MODE_BY_DIRECTION_SEMESTER, [],
                             config=default_config(), compute_duplicates=True)
    scope_rels = {i.file.rel_path for i in scoped_plan.items}
    expected_scope = {f.rel_path for f in effective_files}
    outside_before = {}
    for d in ("python", "siri", "计组", "软概", "数据结构"):
        outside_before.update(files_under(root, d))

    st = Storage(root)
    t0 = time.time()
    res = execute_plan(scoped_plan, st)
    t_exec = time.time() - t0
    acts = st.get_actions(res.batch_id)
    st.close()
    scope_ok = (scope_rels == expected_scope and res.failed == 0
                and res.skipped == 0 and res.moved == len(scope_rels))
    rec("A7", "待整理清单范围整理：只移动指定范围",
        "通过" if scope_ok else "未通过",
        f"清单 {len(intake)} 项（加入 {r1.status}/{r2.status}）→ "
        f"范围文件 {len(expected_scope)} 个 → 计划 {len(scope_rels)} 项\n"
        f"执行：{res.summary()}；批次 #{res.batch_id}；动作 {len(acts)}；"
        f"耗时 {t_exec:.1f}s")

    # ---------- A8 范围外文件保持原位 ----------
    outside_ok = True
    diffs = []
    for rel, fp in outside_before.items():
        full = os.path.join(root, rel)
        try:
            stt = os.stat(full)
            now = (stt.st_size, stt.st_mtime_ns)
        except OSError:
            now = None
        if now != fp:
            outside_ok = False
            diffs.append(rel)
    rec("A8", "范围外文件（未纳入清单）保持原路径、大小与修改时间",
        "通过" if outside_ok else "未通过",
        f"抽查范围外 {len(outside_before)} 个文件；变化 {len(diffs)} 个"
        + (f"\n示例={diffs[:5]}" if diffs else ""))

    # ---------- A9 撤销范围整理 ----------
    st = Storage(root)
    t0 = time.time()
    undo = undo_latest_batch(st)
    t_undo = time.time() - t0
    st.close()
    restored_ok = undo.batch_id == res.batch_id and undo.is_complete \
        and undo.restored >= res.moved
    # 抽查恢复后的源路径确实存在
    spot = [i.file.rel_path for i in scoped_plan.items[:20]]
    spot_missing = [r for r in spot if not os.path.isfile(os.path.join(root, r))]
    rec("A9", "撤销范围整理：完整恢复",
        "通过" if (restored_ok and not spot_missing) else "未通过",
        f"撤销批次 #{undo.batch_id}；{undo.summary()}；耗时 {t_undo:.1f}s；"
        f"抽查 {len(spot)} 个源路径仍在={not spot_missing}")

    # ---------- A10 计划生成中取消 ----------
    cancel_event = threading.Event()
    state = {"n": 0}

    def cb(cur, total, name):
        state["n"] = cur
        if cur >= 500:
            cancel_event.set()

    t0 = time.time()
    partial = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                         config=default_config(), compute_duplicates=True,
                         cancel_event=cancel_event, progress_cb=cb)
    t_cancel = time.time() - t0
    cancel_ok = cancel_event.is_set() and t_cancel < t_plan
    rec("A10", "计划生成中取消：提前结束且不发布结果",
        "通过" if cancel_ok else "未通过",
        f"取消标记={cancel_event.is_set()}；处理到第 {state['n']} 个；"
        f"耗时 {t_cancel:.1f}s（完整 {t_plan:.1f}s）；"
        f"返回半成品 {partial.total} 项（调用方按取消丢弃）")

    # ---------- A11 整理中请求关闭（真实 QTimer 心跳 + 门控） ----------
    close_scan = [f for f in sr.files
                  if f.rel_path.startswith(CLOSE_DIR + os.sep)]
    close_scan_obj = type(sr)(root=sr.root, files=close_scan)
    close_plan = build_plan(close_scan_obj, MODE_BY_DIRECTION_SEMESTER, [],
                            config=default_config(), compute_duplicates=True)
    real_exec = main_mod.execute_plan
    gate = threading.Event()

    def gated_exec(*a, **kw):
        gate.wait(timeout=60)
        return real_exec(*a, **kw)

    main_mod.execute_plan = gated_exec
    ticks2 = []
    timer2 = QTimer()
    timer2.setInterval(50)
    timer2.timeout.connect(lambda: ticks2.append(time.time()))
    w2 = MainWindow()
    w2.path_edit.setText(root)
    w2._reset_for_new_dir(root)
    w2.scan_result = close_scan_obj
    w2.current_plan = close_plan
    w2._plan_valid = True
    w2._update_button_states()
    w2.show()
    pump(app, 0.1)
    timer2.start()
    w2.btn_execute.click()
    started = wait_until(
        app, lambda: (w2._exec_worker is not None
                      and w2._exec_worker.isRunning()), timeout=20)
    t_close0 = time.time()
    from PySide6.QtGui import QCloseEvent
    ev = QCloseEvent()
    w2.closeEvent(ev)
    pending = w2._close_pending
    pump(app, 1.0)          # 门控期间界面必须继续响应心跳
    gates = [b - a for a, b in zip(ticks2, ticks2[1:])]
    max_gap2 = max(gates) * 1000 if gates else -1
    gate.set()
    closed = wait_until(
        app, lambda: (not w2.isVisible() and w2._storage is None), timeout=180)
    t_close_wait = time.time() - t_close0
    timer2.stop()
    main_mod.execute_plan = real_exec
    close_ok = (started and not ev.isAccepted() and pending and closed
                and len(ticks2) >= 10 and max_gap2 < 1000)
    rec("A11", "整理中请求关闭：拒绝立即关闭、等待线程结束、界面保持响应",
        "通过" if close_ok else "未通过",
        f"执行线程已启动={started}；关闭被拒绝={not ev.isAccepted()}；"
        f"进入关闭等待={pending}；自动关闭={closed}；等待 {t_close_wait:.2f}s\n"
        f"门控期间心跳 {len(ticks2)} 次，最大间隔 {max_gap2:.0f} ms")
    # 清理该次关闭等待可能移动的文件
    st = Storage(root)
    b = st.get_pending_batch()
    cleaned = 0
    if b is not None and b.status != "undone":
        cleaned = undo_latest_batch(st).restored
    st.close()

    # ---------- A12 中断恢复（子进程被强杀） ----------
    child_code = f"""
import os, sys, signal
sys.path.insert(0, {SRC!r})
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from scanner import scan_directory
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from classification_config import default_config
from storage import Storage
from executor import execute_plan
root = {root!r}
prefix = {RECOVER_DIR!r}
sr = scan_directory(root, detect_project_roots=True)
files = [f for f in sr.files if f.rel_path.startswith(prefix + os.sep)]
sr2 = type(sr)(root=sr.root, files=files)
plan = build_plan(sr2, MODE_BY_DIRECTION_SEMESTER, [],
                  config=default_config(), compute_duplicates=True)
st = Storage(root)
def cb(cur, total, name):
    if cur == 12:
        os.kill(os.getpid(), signal.SIGSTOP)
execute_plan(plan, st, progress_cb=cb)
st.close()
print('child finished unexpectedly')
"""
    child = subprocess.Popen([sys.executable, "-c", child_code])
    stopped = False
    deadline = time.time() + 180
    while time.time() < deadline:
        p = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)],
                           capture_output=True, text=True)
        if "T" in p.stdout:
            stopped = True
            break
        if child.poll() is not None:
            break
        time.sleep(0.05)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=30)
    st = Storage(root)
    rec_batch = st.get_latest_batch()
    batch_id = rec_batch.batch_id if rec_batch else 0
    st.close()
    st = Storage(root)
    t0 = time.time()
    recovered = recover_batch(st, batch_id)
    t_recover = time.time() - t0
    acts_after = st.get_actions(batch_id) if batch_id else []
    moved_after = sum(1 for a in acts_after if a.status == "moved")
    st.close()
    recover_ok = (stopped and batch_id > 0
                  and recovered.batch_id == batch_id
                  and recovered.still_planned > 0
                  and not recovered.paused
                  and recovered.batch_status_after == "recovered_partial")
    rec("A12", "中断恢复：强杀后核对磁盘状态、保留已完成记录",
        "通过" if recover_ok else "未通过",
        f"子进程已暂停(可观测)={stopped}；批次 #{batch_id}；"
        f"{recovered.summary()}；核对后 moved 动作 {moved_after}；"
        f"耗时 {t_recover:.1f}s")
    st = Storage(root)
    undo3 = undo_latest_batch(st)
    st.close()
    undo3_ok = undo3.batch_id == batch_id and undo3.is_complete \
        and undo3.restored >= moved_after
    rec("A12b", "中断恢复后可撤销：按实际记录恢复已移动文件",
        "通过" if undo3_ok else "未通过",
        f"撤销批次 #{undo3.batch_id}；{undo3.summary()}；"
        f"应恢复 {moved_after} 项")

    # ---------- A13 最终独立清单核对 ----------
    print("    建立最终独立清单（SHA-256）……")
    final, errs_final, t_m2 = fs_manifest(root)
    dump(MANIFEST_FINAL, final)
    cmp = compare_manifests(before, final, errs_before, errs_final)
    rec("A13", "最终文件系统核对：路径、内容、副本数量全部一致",
        "通过" if cmp["ok"] else "未通过",
        f"缺失 {cmp['missing_n']}、多余 {cmp['extra_n']}、内容变化 {cmp['changed_n']}；"
        f"内容指纹多重集合一致={cmp['multiset_equal']}；"
        f"枚举错误 {cmp['errors_n']}\n"
        f"重复组（前/后）={cmp['dup_groups_before']}/{cmp['dup_groups_after']}；"
        f"组内文件（前/后）={cmp['dup_files_before']}/{cmp['dup_files_after']}；"
        f"清单 {len(final)} 条，耗时 {t_m2:.1f}s\n文件：{MANIFEST_FINAL}"
        + (f"\n缺失={cmp['missing']}" if cmp["missing_n"] else "")
        + (f"\n多余={cmp['extra']}" if cmp["extra_n"] else "")
        + (f"\n变化={cmp['changed']}" if cmp["changed_n"] else "")
        + (f"\n错误={cmp['errors']}" if cmp["errors_n"] else ""))

    # ---------- A14 元数据与其他未验证边界（如实标注） ----------
    rec("A14", "元数据与其他未验证边界",
        "未测",
        "已核对：相对路径、文件大小、SHA-256、内容重复副本数量、符号链接文本。\n"
        "未核对：修改时间/访问时间恢复（撤销后 mtime 由系统当前时间决定）、"
        "权限位、扩展属性、ACL、硬链接身份、空目录残留、文件所有者。\n"
        "清单不记录目录；撤销允许残留空父目录。真实资料原目录未被写入。")

    # ---------- 汇总 ----------
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 else 1
    meta = {
        "suite": "acceptance_v17_scale",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": root, "files": total_files, "bytes": total_bytes,
        "scanned_files": len(sr.files), "scanned_bytes": scanned_bytes,
        "plan_items": plan.total if plan else 0,
        "durations": {
            "manifest_before_s": round(t_m1, 1),
            "scan_s": round(t_scan, 2),
            "plan_s": round(t_plan, 1),
            "duplicates_s": round(t_dup, 1),
            "scoped_execute_s": round(t_exec, 1),
            "undo_s": round(t_undo, 1),
            "cancel_plan_s": round(t_cancel, 1),
            "close_wait_s": round(t_close_wait, 1),
            "recover_s": round(t_recover, 1),
            "manifest_final_s": round(t_m2, 1),
        },
        "cleanup_undo_after_close_wait": cleaned,
        "recover_undo": {"restored": undo3.restored,
                         "complete": undo3.is_complete},
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "untested": untested},
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_acceptance_v17_scale.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码 {exit_code}")
    print("=" * 70)
    print(f"结果已保存: {out}")
    try:
        from main import shutdown_widgets
        shutdown_widgets()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
