"""
归档盒 V1.2 第二轮 — 空操作反例验证（独立进程）

目的：验证恢复验收能识别“什么都没做”的实现（消除假阳性）。
背景：旧版 H9 把 recover_batch 替换为空操作后仍能通过——
因为断言只看数据库里手写的状态和“原文件存在”（恢复前就存在）。

方法：在本独立进程中把 main.recover_batch 替换为空操作
（不核对磁盘、不改任何数据库记录），再通过真实界面入口
（历史页 → 核对恢复按钮）走完整恢复流程。H9 风格断言必须
失败（动作未被补记 moved、批次保持 executing）；若断言反而
通过，说明验收仍存在假阳性，本脚本判为未通过。

注入结果与正常运行结果分开保存：
  results_v12r2_noop_injection.json / .txt
（正常运行的 H9a-H9f 见 results_v12_history_task.*）

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/_v12r2_noop_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from executor import RecoverResult
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES
from storage import Storage
from utils import sha256_file

BASE = "/tmp/_v12r2_noop"
RESULTS: list = []


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    icon = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}[status]
    print(f"[{icon}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def pump(app, seconds=0.15):
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
    return cond()


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    t0 = time.time()
    app = QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    root = os.path.join(BASE, "root")
    os.makedirs(root)

    def mk(rel, content):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content.encode())
        return p

    mk("大二上/操作系统/实验.pdf", "OS-EXP" * 100)
    mk("大二上/操作系统/讲义.pdf", "OS-LEC" * 100)

    w = main_mod.MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w.show()
    app.processEvents()

    # 扫描 + 生成计划（真实流程）
    w.on_scan()
    assert wait_until(app, lambda: not w._working and w.scan_result is not None
                      and not w._plan_working)
    w._generate_and_show_plan()
    assert wait_until(app, lambda: not w._plan_working and w.current_plan is not None
                      and w._plan_valid)

    # 构造场景：移动已发生、状态未写回（真实移动 + 真实 SHA-256）
    st = Storage(root)
    plan = w.current_plan
    items = sorted([i for i in plan.items if i.selected],
                   key=lambda i: i.file.rel_path)
    bid = st.create_batch(plan, "中断批次")
    st.update_batch_status(bid, "executing")
    for idx, it in enumerate(items):
        st.create_action(bid, it, idx,
                         sha256_file(os.path.join(root, it.file.rel_path)))
    st.close()
    ia = items[0]
    src_a = os.path.join(root, ia.file.rel_path)
    tgt_a = os.path.join(root, ia.target_rel_path)
    os.makedirs(os.path.dirname(tgt_a), exist_ok=True)
    shutil.move(src_a, tgt_a)
    pre_moved = (not os.path.isfile(src_a) and os.path.isfile(tgt_a))

    # ==== 注入空操作：recover_batch 什么都不做 ====
    calls = []

    def noop_recover(storage, batch_id=None, progress_cb=None):
        calls.append(batch_id)
        return RecoverResult()   # 空结果：不改库、不核对磁盘

    real_recover = main_mod.recover_batch
    main_mod.recover_batch = noop_recover

    try:
        # 通过历史页真实入口发起恢复（question 自动 Yes）
        w.on_history()
        d = w._history_dialog
        pump(app)
        clicked = d.btn_recover.isEnabled()
        d.btn_recover.click()
        pump(app, 0.3)
        done = wait_until(app, lambda: not w._working
                          and w._recover_worker is not None
                          and not w._recover_worker.isRunning())
        pump(app)
    finally:
        main_mod.recover_batch = real_recover

    st = Storage(root)
    acts = {a.action_index: a for a in st.get_actions(bid)}
    batch = st.get_batch(bid)
    st.close()

    # N1：空操作被实际调用（注入生效）
    n1_ok = (clicked and done and calls == [bid])
    rec("N1", "空操作注入生效：恢复入口被调用但未做任何事",
        "通过" if n1_ok else "未通过",
        f"按钮可用={clicked}；流程完成={done}；空操作调用批次={calls}（应=[{bid}]）")

    # N2：核心——动作未被补记 moved、批次保持 executing，
    # 即 H9 风格断言（补记 moved + 批次状态变化）会失败 → 假阳性已消除
    h9_assert_would_fail = (acts[0].status == "planned"
                            and batch.status == "executing")
    rec("N2", "空操作下验收必失败：动作仍 planned、批次仍 executing（假阳性已消除）",
        "通过" if h9_assert_would_fail else "未通过",
        f"动作0={acts[0].status}（空操作后应仍 planned）；"
        f"批次状态={batch.status}（应仍 executing）；"
        f"若验收仍通过则存在假阳性={not h9_assert_would_fail}")

    # N3：文件未被搬回、未被移动（空操作连文件都不碰）
    n3_ok = (pre_moved and not os.path.isfile(src_a)
             and os.path.isfile(tgt_a))
    rec("N3", "空操作不动任何文件：源仍不在、目标仍在（与恢复前一致）",
        "通过" if n3_ok else "未通过",
        f"恢复前源不在/目标在={pre_moved}；恢复后源在={os.path.isfile(src_a)}；"
        f"目标在={os.path.isfile(tgt_a)}")

    # N4：结果文案不误报——空结果不得宣称“补记已移动”
    msg = w.statusBar().currentMessage()
    n4_ok = ("补记已移动" not in msg and "暂停待人工确认" not in msg)
    rec("N4", "空操作结果文案不误报：不显示“补记已移动/暂停待人工确认”",
        "通过" if n4_ok else "未通过",
        f"状态栏={msg}")

    if w._history_dialog:
        w._history_dialog.close()
    w.close()
    pump(app)

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if (failed == 0 and RESULTS) else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}")
    print(f"退出码: {exit_code}（0 = 验收能识别空操作）")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "v12r2_noop_injection",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python "
                   "tests/_v12r2_noop_check.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "note": "独立进程注入运行结果；正常运行结果见 results_v12_history_task.*",
        "base": BASE,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed, "fail": failed},
    }
    out = os.path.join(here, "results_v12r2_noop_injection.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_v12r2_noop_injection.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2 第二轮 — 恢复空操作注入反例（独立进程）\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}\n")
        f.write(f"退出码: {exit_code}（0 = 验收能识别空操作）\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {out}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
