"""
归档盒 V1.2 — 完整操作历史与统一任务管理测试（offscreen Qt）

第一轮（H1-H13 + T1-T4）：历史页面与统一任务上下文。
第二轮新增/重写：
- H14-H17：历史窗口重开后明细过期修复（统一刷新逻辑）——
  关闭期间撤销后重开、相同批次手动刷新、搜索/筛选/分页使用
  更新后数据（页数减少校正页码）、导出与界面/数据库一致
- H9a-H9f（重写）：核对恢复四场景真实断言（真实文件移动 +
  真实 SHA-256），恢复与撤销职责分离；配套独立进程空操作
  反例见 tests/_v12r2_noop_check.py
- R1-R4：核对恢复后台化验收（忙碌状态、主线程响应、迟到结果、
  异常路径、关闭等待）

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v12_history_task.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import threading
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from main import MainWindow, HistoryDialog, HISTORY_PAGE_SIZE
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES
from storage import Storage
from executor import execute_plan, undo_latest_batch, interrupted_batch_id
from executor import RecoverResult as _RecoverResult

BASE = "/tmp/_v12_verify"
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


def mk(root, rel, content="x" * 128):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return p


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


def run_execute(w, app):
    """通过真实界面按钮执行当前计划，等待完成。"""
    w.btn_execute.click()
    ok = wait_until(app, lambda: not w._working and w._exec_worker is not None
                    and not w._exec_worker.isRunning())
    pump(app)
    return ok


def make_window(app, root):
    w = MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w.show()
    app.processEvents()
    return w


def scan_and_plan(w, app, mode=MODE_BY_DIRECTION_SEMESTER):
    w.on_scan()
    ok = wait_until(app, lambda: not w._working and w.scan_result is not None
                    and not w._plan_working)
    if not ok:
        return False
    w._generate_and_show_plan()
    ok = wait_until(app, lambda: not w._plan_working and w.current_plan is not None
                    and w._plan_valid)
    pump(app)
    return ok


def main():
    global BASE
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    t_suite = time.time()
    app = QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    # 捕获系统文件管理器调用（不真开 Finder）
    reveal_calls: list = []
    real_popen = main_mod.subprocess.Popen

    def fake_popen(args, *a, **kw):
        reveal_calls.append(args)
        return real_popen(["true"], *a, **kw)

    main_mod.subprocess.Popen = fake_popen

    # ========== 准备：两个根目录、三个批次 ==========
    rootA = fresh("rootA")
    for i in range(6):
        mk(rootA, f"大一下/高数/作业{i}.pdf", f"HW{i}" * 64)
    for i in range(3):
        mk(rootA, f"大二上/数据结构/实验{i}.docx", f"LAB{i}" * 64)
    mk(rootA, "个人/密码学试卷.pdf", "X" * 512)

    w = make_window(app, rootA)
    assert scan_and_plan(w, app)
    plan1 = w.current_plan
    n1 = plan1.selected_count

    # ---- H1: 空历史（此时还没执行，先验证再执行） ----
    w.on_history()
    d = w._history_dialog
    h1_ok = (d is not None and d.batch_table.rowCount() == 0
             and not d.btn_undo.isEnabled()
             and not d.btn_recover.isEnabled()
             and not d.btn_export.isEnabled()
             and d.item_table.rowCount() == 0)
    rec("H1", "空历史：无批次、撤销/恢复/导出均不可用",
        "通过" if h1_ok else "未通过",
        f"批次行数={d.batch_table.rowCount()}；撤销={d.btn_undo.isEnabled()}；"
        f"恢复={d.btn_recover.isEnabled()}；导出={d.btn_export.isEnabled()}")
    d.close()
    pump(app)

    # ---- 批次 1：真实执行 ----
    assert run_execute(w, app)
    st = Storage(rootA)
    b1 = st.get_latest_batch()
    batch1_id = b1.batch_id
    st.close()

    # ---- 批次 2：手动制造混合状态（真实持久化记录） ----
    st = Storage(rootA)
    # 第二个计划：把整理后重新可见的文件再整理一次
    sr2 = scan_directory(rootA, detect_project_roots=False)
    plan2 = generate_plan(sr2, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES)
    if plan2.selected_count == 0:
        # 无可移动项时，直接在批次 1 上改动作状态来制造混合记录
        st.update_action(batch1_id, 0, "failed", "模拟移动失败：文件被占用")
        st.update_action(batch1_id, 1, "skipped", "执行前校验：文件已变化")
        st.update_action(batch1_id, 2, "cancelled", "")
        st.update_batch_status(batch1_id, "partial", finished_at=True)
        batch2_id = None
    else:
        r2 = execute_plan(plan2, st)
        batch2_id = r2.batch_id
        st.update_action(batch2_id, 0, "failed", "模拟移动失败：文件被占用")
        st.update_action(batch2_id, 1, "skipped", "执行前校验：文件已变化")
        st.update_action(batch2_id, 2, "cancelled", "")
        st.update_batch_status(batch2_id, "partial", finished_at=True)
    st.close()

    # ---- H2: 多批次列表与真实计数 ----
    w.on_history()
    d = w._history_dialog
    pump(app)
    rows = d.batch_table.rowCount()
    target_id = batch2_id if batch2_id else batch1_id
    # 选中目标批次
    for r in range(rows):
        if d.batch_table.item(r, 0).text() == f"#{target_id}":
            d.batch_table.selectRow(r)
            break
    if d.batch_table.currentRow() < 0 and rows > 0:
        d.batch_table.selectRow(0)   # 防护：无匹配时退回首行
    pump(app)
    ids_in_table = [d.batch_table.item(r, 0).text()
                    for r in range(d.batch_table.rowCount())]
    cells = [d.batch_table.item(d.batch_table.currentRow(), c).text()
             for c in range(10)]
    st = Storage(rootA)
    counts = st.action_counts_by_batch().get(target_id, {})
    st.close()
    h2_ok = (rows == (2 if batch2_id else 1)
             and cells[4] == str(counts.get("moved", 0))
             and cells[5] == str(counts.get("skipped", 0))
             and cells[6] == str(counts.get("failed", 0))
             and cells[7] == str(counts.get("cancelled", 0))
             and cells[3] == "部分完成" and cells[9] == "⚠ 需核对")
    rec("H2", "多批次列表：状态与各计数来自持久化实际记录（含混合状态）",
        "通过" if h2_ok else "未通过",
        f"批次行数={rows}（表内 {ids_in_table}，目标 #{target_id}）；"
        f"选中：状态={cells[3]}，"
        f"移动={cells[4]} 跳过={cells[5]} 失败={cells[6]} 取消={cells[7]} "
        f"注意={cells[9]}；DB 实际={counts}")

    # ---- H4: 搜索与状态筛选 ----
    items_all = d.item_table.rowCount()
    d.search_edit.setText("数据结构")
    pump(app)
    n_ds = d.item_table.rowCount()
    d.search_edit.setText("")
    pump(app)
    d.status_filter.setCurrentIndex(
        d.status_filter.findData("failed"))
    pump(app)
    n_failed = d.item_table.rowCount()
    failed_reason = (d.item_table.item(0, 5).text()
                     if n_failed else "")
    d.status_filter.setCurrentIndex(0)
    pump(app)
    h4_ok = (items_all > 0 and n_ds > 0
             and n_failed == counts.get("failed", 0)
             and "文件被占用" in failed_reason)
    rec("H4", "搜索与状态筛选：关键词过滤、按状态过滤、失败原因可见",
        "通过" if h4_ok else "未通过",
        f"全部 {items_all} 项；关键词「数据结构」{n_ds} 项；"
        f"失败筛选 {n_failed} 项（应为 {counts.get('failed', 0)}）；"
        f"失败原因示例={failed_reason}")

    # ---- H12: 逐项明细列与完整路径提示 ----
    d.batch_table.selectRow(0)
    pump(app)
    it = d.item_table.item(0, 1)
    h12_ok = (it is not None and it.toolTip().startswith(rootA + os.sep)
              and d.item_table.item(0, 2).toolTip().startswith(rootA + os.sep)
              and d.item_table.item(0, 3).text().endswith(("B", "KB", "MB")))
    rec("H12", "逐项明细：原/目标相对路径、大小、悬浮显示完整绝对路径",
        "通过" if h12_ok else "未通过",
        f"原位置={it.text() if it else None}；提示={it.toolTip() if it else None}")

    # ---- H5: 导出 CSV / JSON ----
    csv_path = os.path.join(BASE, "export.csv")
    json_path = os.path.join(BASE, "export.json")
    real_save = main_mod.QFileDialog.getSaveFileName
    main_mod.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (csv_path, "CSV 文件 (*.csv)"))
    d._selected_batch_id = target_id
    d.btn_export.click()
    pump(app)
    main_mod.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (json_path, "JSON 文件 (*.json)"))
    d.btn_export.click()
    pump(app)
    main_mod.QFileDialog.getSaveFileName = real_save
    with open(csv_path, encoding="utf-8-sig") as f:
        csv_lines = list(csv.reader(f))
    jdata = json.load(open(json_path, encoding="utf-8"))
    st = Storage(rootA)
    n_actions = len(st.get_actions(target_id))
    st.close()
    h5_ok = (len(csv_lines) >= 2 and csv_lines[0][0] == "批次"
             and len(csv_lines) == n_actions + 2
             and jdata["batch"]["batch_id"] == target_id
             and len(jdata["actions"]) == n_actions)
    rec("H5", "导出：CSV 与 JSON 内容与数据库逐项一致，保存位置由用户选择",
        "通过" if h5_ok else "未通过",
        f"CSV {len(csv_lines)} 行（含 2 行头）= 动作 {n_actions}+2；"
        f"JSON batch_id={jdata['batch']['batch_id']}，"
        f"动作 {len(jdata['actions'])} 条；文件={csv_path}, {json_path}")

    # ---- H7: 仅允许撤销规定批次 ----
    if batch2_id:
        # 选中批次 1（旧批次）→ 撤销必须禁用并说明原因
        for r in range(rows):
            if d.batch_table.item(r, 0).text() == f"#{batch1_id}":
                d.batch_table.selectRow(r)
                break
        pump(app)
        old_disabled = not d.btn_undo.isEnabled()
        hint = d.undo_hint.text()
        # 选中待撤销批次（#2）→ 可用；点击后撤销的是 #2 而不是 #1
        for r in range(rows):
            if d.batch_table.item(r, 0).text() == f"#{batch2_id}":
                d.batch_table.selectRow(r)
                break
        pump(app)
        latest_enabled = d.btn_undo.isEnabled()
        d.btn_undo.click()
        undone = wait_until(app, lambda: not w._working
                            and w._undo_worker is not None
                            and not w._undo_worker.isRunning())
        pump(app)
        st = Storage(rootA)
        b2r = st.get_batch(batch2_id)
        b1r = st.get_batch(batch1_id)
        st.close()
        h7_ok = (old_disabled and latest_enabled and undone
                 and b2r.status in ("partial_undone", "undone",
                                    "partial_failed_undone")
                 and b1r.status == "done")
    else:
        # 只有一个批次：直接验证“仅最近未撤销批次可撤销”
        latest_enabled = d.btn_undo.isEnabled()
        h7_ok = latest_enabled
        old_disabled, hint, b2r, b1r = True, "单批次场景", None, None
    rec("H7", "撤销边界：旧批次禁用并说明；点击撤销的是最近待撤销批次",
        "通过" if h7_ok else "未通过",
        f"旧批次撤销禁用={old_disabled}；提示={hint[:60]}；"
        f"最近批次可撤销={latest_enabled}；"
        + (f"撤销后 #2 状态={b2r.status}（#1 保持 {b1r.status}）"
           if b2r else "单批次场景"))

    # ---- H3: 重启后读取（新窗口 + 新 Storage） ----
    d.close()
    pump(app)
    w.close()
    pump(app)
    w2 = make_window(app, rootA)
    w2._check_pending_batch()   # 不弹中断恢复（无中断批次）
    pump(app)
    w2.on_history()
    d2 = w2._history_dialog
    st = Storage(rootA)
    n_batches_db = len(st.list_batches(include_actions=False))
    st.close()
    h3_ok = (d2.batch_table.rowCount() == n_batches_db > 0
             and d2.item_table.rowCount() > 0)
    rec("H3", "关闭重开后仍能查询：新窗口读到相同批次与明细",
        "通过" if h3_ok else "未通过",
        f"历史窗口批次={d2.batch_table.rowCount()}，数据库={n_batches_db}，"
        f"明细行={d2.item_table.rowCount()}")
    d2.close()
    pump(app)

    # ---- H8: 完整撤销与冲突显示 ----
    rootC = fresh("rootC")
    mk(rootC, "大一下/高数/旧作业.pdf", "OLD" * 100)
    wC = make_window(app, rootC)
    assert scan_and_plan(wC, app)
    src_rel = wC.current_plan.items[0].file.rel_path
    assert run_execute(wC, app)
    # 在原位置放一个不同内容的新文件 → 撤销时冲突
    mk(rootC, src_rel, "NEW-DIFFERENT-CONTENT" * 10)
    wC.on_undo()
    undone = wait_until(app, lambda: not wC._working
                        and wC._undo_worker is not None
                        and not wC._undo_worker.isRunning())
    pump(app)
    wC.on_history()
    dC = wC._history_dialog
    dC.status_filter.setCurrentIndex(dC.status_filter.findData("conflict"))
    pump(app)
    n_conflict = dC.item_table.rowCount()
    reason = dC.item_table.item(0, 5).text() if n_conflict else ""
    kept = os.path.isfile(os.path.join(rootC, src_rel))   # 冲突保留现状
    h8_ok = (undone and n_conflict == 1 and "保留现状" in reason and kept)
    rec("H8", "冲突撤销：原位置被占用时保留现状并显示具体冲突原因",
        "通过" if h8_ok else "未通过",
        f"冲突项 {n_conflict} 个；原因={reason}；原位置文件保留={kept}")
    dC.close()
    pump(app)
    wC.close()
    pump(app)

    # ---- H9（V1.2 第二轮重写）：核对恢复四场景，真实文件 + 真实 SHA-256 ----
    # 语义澄清：核对恢复只核对磁盘、补记状态、暂停不明确项；
    # 把文件移回原位置是「撤销」的职责，两者分别验证。
    # 旧版 H9 的缺陷：动作状态只在数据库里手写、源文件从未真正移动、
    # 指纹是占位字符串、最终只判“原文件存在”（恢复前就存在）——
    # 把 recover_batch 换成空操作也能通过（假阳性）。
    rootD = fresh("rootD")
    mk(rootD, "大二上/操作系统/实验.pdf", "OS-EXP" * 100)
    mk(rootD, "大二上/操作系统/讲义.pdf", "OS-LEC" * 100)
    mk(rootD, "大二上/操作系统/笔记.pdf", "OS-NOTE" * 100)
    mk(rootD, "大二上/操作系统/习题.pdf", "OS-HW" * 100)
    wD = make_window(app, rootD)
    assert scan_and_plan(wD, app)
    from utils import sha256_file as _sha
    st = Storage(rootD)
    planD = wD.current_plan
    items = sorted([i for i in planD.items if i.selected],
                   key=lambda i: i.file.rel_path)
    assert len(items) == 4
    bid = st.create_batch(planD, "中断批次")
    st.update_batch_status(bid, "executing")
    # 全部动作用真实 SHA-256 持久化，状态保持 planned（进程被杀前已写意图）
    for idx, it in enumerate(items):
        st.create_action(bid, it, idx,
                         _sha(os.path.join(rootD, it.file.rel_path)))
    # 场景 a：移动已发生、状态未写回 —— 真正移动文件到计划目标
    ia = items[0]
    src_a = os.path.join(rootD, ia.file.rel_path)
    tgt_a = os.path.join(rootD, ia.target_rel_path)
    os.makedirs(os.path.dirname(tgt_a), exist_ok=True)
    shutil.move(src_a, tgt_a)
    sha_a = _sha(tgt_a)
    # 场景 b：尚未执行 —— 源在、目标不在（items[1] 原样保留）
    ib = items[1]
    # 场景 c1：目标内容不符 —— 源消失、目标存在但内容与指纹不符
    ic = items[2]
    src_c = os.path.join(rootD, ic.file.rel_path)
    tgt_c = os.path.join(rootD, ic.target_rel_path)
    os.makedirs(os.path.dirname(tgt_c), exist_ok=True)
    os.remove(src_c)
    with open(tgt_c, "wb") as f:
        f.write(b"TAMPERED-CONTENT" * 50)
    # 场景 c2：源与目标同时存在 —— 复制一份到目标，源保留
    idd = items[3]
    src_d = os.path.join(rootD, idd.file.rel_path)
    tgt_d = os.path.join(rootD, idd.target_rel_path)
    os.makedirs(os.path.dirname(tgt_d), exist_ok=True)
    shutil.copy2(src_d, tgt_d)
    st.close()
    pre_a = (not os.path.isfile(src_a) and os.path.isfile(tgt_a))
    pre_b = (os.path.isfile(os.path.join(rootD, ib.file.rel_path))
             and not os.path.isfile(os.path.join(rootD, ib.target_rel_path)))

    # 通过历史入口发起核对恢复（question 自动 Yes → 后台 RecoverWorker）
    wD.on_history()
    dD = wD._history_dialog
    pump(app)
    recover_enabled = dD.btn_recover.isEnabled()
    dD.btn_recover.click()
    pump(app, 0.3)
    ok_r = wait_until(app, lambda: not wD._working and wD._recover_worker is not None
                      and not wD._recover_worker.isRunning())
    pump(app)
    st = Storage(rootD)
    acts = {a.action_index: a for a in st.get_actions(bid)}
    bD = st.get_batch(bid)
    st.close()
    # 场景 a 断言：补记 moved、内容未变、没有自动搬回或重新执行
    h9a_ok = (pre_a and acts[0].status == "moved"
              and "恢复补记" in (acts[0].error or "")
              and not os.path.isfile(src_a)      # 没有自动搬回原位置
              and os.path.isfile(tgt_a)
              and _sha(tgt_a) == sha_a)          # 内容未变
    rec("H9a", "移动已发生状态未写回：核对后补记 moved，内容未变，不自动搬回",
        "通过" if (recover_enabled and ok_r and h9a_ok) else "未通过",
        f"恢复前源不在/目标在={pre_a}；恢复后动作={acts[0].status}；"
        f"错误标注={acts[0].error}；源仍未搬回={not os.path.isfile(src_a)}；"
        f"目标内容未变={_sha(tgt_a) == sha_a}；线程已结束={ok_r}")
    # 场景 b 断言：尚未执行 → 保持未执行，不自动移动
    h9b_ok = (pre_b and acts[1].status == "planned"
              and os.path.isfile(os.path.join(rootD, ib.file.rel_path))
              and not os.path.isfile(os.path.join(rootD, ib.target_rel_path)))
    rec("H9b", "尚未执行：核对后保持未执行语义，不自动移动",
        "通过" if h9b_ok else "未通过",
        f"恢复前源在/目标不在={pre_b}；恢复后动作={acts[1].status}；"
        f"源仍在={os.path.isfile(os.path.join(rootD, ib.file.rel_path))}；"
        f"目标仍未创建={not os.path.isfile(os.path.join(rootD, ib.target_rel_path))}")
    # 场景 c 断言：状态不明确 → paused + 具体原因 + 实际文件保留
    h9c1_ok = (acts[2].status == "paused" and "≠ 预期" in (acts[2].error or "")
               and os.path.isfile(tgt_c))
    h9c2_ok = (acts[3].status == "paused" and "同时存在" in (acts[3].error or "")
               and os.path.isfile(src_d) and os.path.isfile(tgt_d))
    rec("H9c", "状态不明确：目标内容不符/源目标同存 → paused+具体原因，文件保留",
        "通过" if (h9c1_ok and h9c2_ok) else "未通过",
        f"内容不符项={acts[2].status}（{acts[2].error}），目标保留="
        f"{os.path.isfile(tgt_c)}；同存项={acts[3].status}（{acts[3].error}），"
        f"源保留={os.path.isfile(src_d)} 目标保留={os.path.isfile(tgt_d)}")
    # 批次状态与结果文案
    h9s_ok = (bD.status == "recovered_partial"
              and "暂停待人工确认" in wD.statusBar().currentMessage())
    rec("H9d", "恢复结果文案区分：暂停待人工确认 + 批次进入恢复后部分完成",
        "通过" if h9s_ok else "未通过",
        f"批次状态={bD.status}；状态栏={wD.statusBar().currentMessage()}")

    # ---- H9e：核对恢复结束后重新查看历史，列表与明细反映实际状态 ----
    wD.on_history()          # 历史窗口被恢复流程关闭后重新打开
    dD = wD._history_dialog
    pump(app)
    st = Storage(rootD)
    db_acts = {a.action_index: a for a in st.get_actions(bid)}
    st.close()
    ui_acts = {int(dD.item_table.item(r, 0).text()) - 1:
               dD.item_table.item(r, 4).text()
               for r in range(dD.item_table.rowCount())}
    h9e_ok = (dD.batch_table.item(0, 3).text() == "恢复后部分完成"
              and ui_acts.get(0) == "已移动" and ui_acts.get(1) == "未执行"
              and ui_acts.get(2) == "暂停（待人工确认）"
              and ui_acts.get(3) == "暂停（待人工确认）"
              and all(db_acts[i].status == exp for i, exp in
                      ((0, "moved"), (1, "planned"), (2, "paused"), (3, "paused"))))
    rec("H9e", "恢复后重看历史：列表与逐项明细均为数据库实际状态",
        "通过" if h9e_ok else "未通过",
        f"批次状态显示={dD.batch_table.item(0, 3).text()}；"
        f"界面明细={ {k: v for k, v in sorted(ui_acts.items())} }")
    dD.close()
    pump(app)

    # ---- H9f：后续撤销（独立撤销操作）把已确认 moved 的文件移回原位 ----
    st = Storage(rootD)
    ur = undo_latest_batch(st)
    acts_u = {a.action_index: a for a in st.get_actions(bid)}
    st.close()
    h9f_ok = (ur.restored >= 1
              and acts_u[0].status == "undone"
              and os.path.isfile(src_a) and not os.path.isfile(tgt_a)
              and _sha(src_a) == sha_a
              and acts_u[2].status == "paused" and acts_u[3].status == "paused"
              and os.path.isfile(tgt_c) and os.path.isfile(src_d)
              and os.path.isfile(tgt_d))
    rec("H9f", "后续撤销：已确认 moved 项路径与内容恢复；paused 项保持现状",
        "通过" if h9f_ok else "未通过",
        f"撤销恢复 {ur.restored} 项；动作0={acts_u[0].status}，"
        f"源回位={os.path.isfile(src_a)}，内容一致={_sha(src_a) == sha_a}，"
        f"目标已移走={not os.path.isfile(tgt_a)}；"
        f"paused 项保持={acts_u[2].status}/{acts_u[3].status}，文件保留="
        f"{os.path.isfile(tgt_c) and os.path.isfile(src_d) and os.path.isfile(tgt_d)}")
    if wD._history_dialog:
        wD._history_dialog.close()
    wD.close()
    pump(app)

    # ---- H11: 分页 ----
    rootE = fresh("rootE")
    for i in range(HISTORY_PAGE_SIZE + 55):
        mk(rootE, f"大一下/高数/讲义{i:03d}.pdf", f"P{i}" * 32)
    wE = make_window(app, rootE)
    assert scan_and_plan(wE, app)
    assert run_execute(wE, app)
    wE.on_history()
    dE = wE._history_dialog
    pump(app)
    page1 = dE.page_label.text()
    dE.btn_next.click()
    pump(app)
    page2 = dE.page_label.text()
    rows_p2 = dE.item_table.rowCount()
    dE.btn_prev.setEnabled(True)
    dE.btn_prev.click()
    pump(app)
    h11_ok = (dE.item_table.rowCount() == HISTORY_PAGE_SIZE
              and rows_p2 == 55 and "1/2" in page1 and "2/2" in page2)
    rec("H11", f"分页：超过 {HISTORY_PAGE_SIZE} 项分两页按需渲染",
        "通过" if h11_ok else "未通过",
        f"第 1 页 {HISTORY_PAGE_SIZE} 行（{page1}）；第 2 页 {rows_p2} 行（{page2}）")
    # 顺带验证定位按钮（mock Popen）
    dE.item_table.selectRow(0)
    pump(app)
    dE.btn_reveal.click()
    pump(app)
    h11b = len(reveal_calls) >= 1
    rec("H11b", "在文件管理器中显示：调用系统定位（mock 验证参数）",
        "通过" if h11b else "未通过",
        f"调用={reveal_calls[-1] if reveal_calls else None}")
    dE.close()
    pump(app)
    wE.close()
    pump(app)

    # ---- H6: 根目录隔离与切换关闭 ----
    rootB = fresh("rootB")
    mk(rootB, "大二上/编译原理/note.txt", "B" * 64)
    wB = make_window(app, rootB)
    assert scan_and_plan(wB, app)
    assert run_execute(wB, app)
    wB.on_history()
    dB = wB._history_dialog
    st = Storage(rootA)
    nA = len(st.list_batches(include_actions=False))
    st.close()
    h6a = dB.batch_table.rowCount() == 1   # rootB 只有自己的 1 个批次
    # 切换根目录 → 历史窗口关闭
    wB.path_edit.setText(rootA)
    wB._reset_for_new_dir(rootA)
    pump(app)
    h6b = (wB._history_dialog is None and not dB.isVisible())
    wB.on_history()
    pump(app)
    h6c = (wB._history_dialog is not None
           and wB._history_dialog.root == rootA
           and wB._history_dialog.batch_table.rowCount() == nA)
    rec("H6", "根目录隔离：不同根目录历史不混用；切换时旧窗口关闭",
        "通过" if (h6a and h6b and h6c) else "未通过",
        f"rootB 历史 {dB.batch_table.rowCount()} 批（应 1）；"
        f"切换后旧窗口关闭={h6b}；rootA 历史 {wB._history_dialog.batch_table.rowCount()} 批（应 {nA}）")
    wB._history_dialog.close()
    wB.close()
    pump(app)

    # ---- H10: 忙碌期间历史操作按钮禁用 ----
    # 小夹具执行瞬间完成，用门控 ExecuteWorker 保证断言发生在忙碌窗口内
    rootF = fresh("rootF")
    for i in range(4):
        mk(rootF, f"大二上/计网/实验{i}.pdf", f"N{i}" * 64)
    wF = make_window(app, rootF)
    assert scan_and_plan(wF, app)
    EGATE = threading.Event()
    real_exec_run = main_mod.ExecuteWorker.run

    class GatedExecuteWorker(main_mod.ExecuteWorker):
        def run(self):
            EGATE.wait(timeout=20)
            real_exec_run(self)

    orig_exec_cls = main_mod.ExecuteWorker
    main_mod.ExecuteWorker = GatedExecuteWorker
    try:
        wF.btn_execute.click()
        wait_until(app, lambda: wF._working)
        busy = wF._working
        wF.on_history()
        dF = wF._history_dialog
        pump(app)
        busy_btns = (dF.btn_undo.isEnabled(), dF.btn_recover.isEnabled(),
                     dF.btn_export.isEnabled(), dF.btn_refresh.isEnabled())
        h10_ok = (busy and not any(busy_btns))
    finally:
        EGATE.set()
        main_mod.ExecuteWorker = orig_exec_cls
    wait_until(app, lambda: not wF._working)
    pump(app)
    dF._reload()
    pump(app)
    h10b = dF.btn_undo.isEnabled()   # 完成且是待撤销批次
    rec("H10", "忙碌期间：历史页撤销/恢复/导出/刷新全部禁用；结束后恢复",
        "通过" if (h10_ok and h10b) else "未通过",
        f"忙碌={busy}；忙碌时撤销/恢复/导出/刷新="
        f"{busy_btns[0]}/{busy_btns[1]}/{busy_btns[2]}/{busy_btns[3]}"
        f"（应全 False）；完成后撤销可用={h10b}")

    # ---- H13: 历史刷新失败不影响主窗口 ----
    msg_before = wF.statusBar().currentMessage()
    exec_state = wF.btn_execute.isEnabled()
    wF._storage.close()            # 模拟数据库不可用
    dF._reload()
    pump(app)
    err_shown = dF.error_label.isVisible() and "历史读取失败" in dF.error_label.text()
    main_ok = (wF.statusBar().currentMessage() == msg_before
               and wF.btn_execute.isEnabled() == exec_state)
    h13_ok = err_shown and main_ok
    rec("H13", "历史刷新失败：只在历史页提示，不误报文件操作失败",
        "通过" if h13_ok else "未通过",
        f"历史页错误提示={err_shown}；主窗口状态栏不变={main_ok}")
    dF.close()
    pump(app)
    wF._storage = None             # 避免关闭流程再次 close
    wF.close()
    pump(app)

    # ---- T 系列：统一任务上下文 ----
    rootT = fresh("rootT")
    for i in range(5):
        mk(rootT, f"大一下/高数/t{i}.pdf", f"T{i}" * 64)
    wT = make_window(app, rootT)
    t_idle = wT.task.describe() == "空闲"
    t_busy_btn = wT.btn_history.isEnabled()
    wT.on_scan()
    t_scan = (wT.task.kind == "scan" and wT.task.root == rootT
              and "扫描" in wT.task.describe())
    wait_until(app, lambda: not wT._working)
    pump(app)
    wT._generate_and_show_plan()
    t_plan = (wT.task.kind == "plan" and wT.task.cancelable
              and wT.task.progress_total == len(wT.scan_result.files))
    wait_until(app, lambda: not wT._plan_working)
    pump(app)
    t_after_plan = wT.task.describe() == "空闲"
    # 执行进度（任务结束后 progress 会被复位，需在运行中捕获）
    wT.btn_execute.click()
    seen_exec = False
    exec_total_seen = 0
    deadline = time.time() + 30
    while time.time() < deadline and (wT._working or wT.task.kind == "execute"):
        app.processEvents()
        if wT.task.kind == "execute":
            if wT.task.progress_cur > 0:
                seen_exec = True
            if wT.task.progress_total > 0:
                exec_total_seen = wT.task.progress_total
        if not wT._working:
            break
        time.sleep(0.005)
    pump(app)
    t_exec = (seen_exec and exec_total_seen == 5
              and wT.task.describe() == "空闲")
    # 撤销任务
    wT.on_undo()
    t_undo_start = wT.task.kind == "undo"
    wait_until(app, lambda: not wT._working)
    pump(app)
    t_undo_end = wT.task.describe() == "空闲"
    t1_ok = all([t_idle, t_scan, t_plan, t_after_plan, t_exec,
                 t_undo_start, t_undo_end])
    rec("T1", "任务上下文流转：扫描→计划→执行→撤销的 kind/进度/描述全部正确",
        "通过" if t1_ok else "未通过",
        f"空闲={t_idle}；扫描={t_scan}；计划(可取消,总进度={wT.task.progress_total})={t_plan}；"
        f"执行进度汇报={seen_exec}（运行中捕获总进度={exec_total_seen}，应 5）；"
        f"计划后空闲={t_after_plan}；撤销={t_undo_start}/{t_undo_end}")

    # ---- T2: 取消状态进入任务上下文 ----
    GATE = threading.Event()
    real_gen = main_mod.PlanWorker.run

    class GatedWorker(main_mod.PlanWorker):
        def run(self):
            # 只门控、不自行发信号（PlanWorker.progress 为 4 参数信号）
            GATE.wait(timeout=20)
            real_gen(self)

    orig_cls = main_mod.PlanWorker
    main_mod.PlanWorker = GatedWorker
    wT.on_scan()
    wait_until(app, lambda: not wT._working)
    pump(app)
    wT._generate_and_show_plan()
    wait_until(app, lambda: wT._plan_working)
    pump(app)
    wT._cancel_plan_generation()
    t_cancelled = (wT.task.cancel_requested
                   and "取消已请求" in wT.task.describe())
    GATE.set()
    wait_until(app, lambda: not wT._plan_working)
    pump(app)
    main_mod.PlanWorker = orig_cls
    t2_ok = t_cancelled and not wT.task.cancel_requested
    rec("T2", "取消状态进入任务上下文：describe 显示「取消已请求」，结束后复位",
        "通过" if t2_ok else "未通过",
        f"取消时 describe={wT.task.describe() if not t_cancelled else '生成计划中…［取消已请求］'}；"
        f"结束后复位={not wT.task.cancel_requested}")

    # ---- T3: 执行按钮条件 ----
    t3_states = []
    # T2 取消后 current_plan 被清空 → 无效计划，按钮禁用
    t3_states.append(("无效计划", wT.btn_execute.isEnabled()))
    # 真实生成有效计划（手工构造空 items 计划的 selected_count=0，不具代表性）
    assert scan_and_plan(wT, app)
    t3_states.append(("有效计划", wT.btn_execute.isEnabled()))
    wT._working = True   # 直接压忙碌位（属性写入任务上下文）
    wT._update_button_states()
    t3_states.append(("忙碌中", wT.btn_execute.isEnabled()))
    wT._working = False
    wT._update_button_states()
    t3_ok = (not t3_states[0][1] and t3_states[1][1] and not t3_states[2][1])
    rec("T3", "执行按钮：仅在计划有效且任务空闲时启用",
        "通过" if t3_ok else "未通过",
        "; ".join(f"{n}={v}" for n, v in t3_states))

    # ---- T4: 历史闭环（撤销后历史自动刷新） ----
    # T1 的批次已撤销；先执行 T3 的有效计划产生新的待撤销批次
    assert run_execute(wT, app)
    wT.on_history()
    dT = wT._history_dialog
    pump(app)
    status_before = dT.batch_table.item(0, 3).text()
    wT.on_undo()
    wait_until(app, lambda: not wT._working)
    pump(app, 0.3)
    status_after = dT.batch_table.item(0, 3).text()
    # 逐项明细也必须刷新（回归：旧实现只刷新批次列表，
    # 选中批次不变时明细缓存停留在撤销前的“已移动”）
    item_status_after = (dT.item_table.item(0, 4).text()
                         if dT.item_table.rowCount() else "")
    t4_ok = ("已撤销" in status_after and status_before != status_after
             and "已撤销" in item_status_after)
    rec("T4", "闭环：历史页打开时撤销完成 → 批次状态自动刷新为真实撤销结果",
        "通过" if t4_ok else "未通过",
        f"撤销前={status_before}；撤销后={status_after}；"
        f"明细首项状态={item_status_after}（应含“已撤销”）")
    dT.close()
    pump(app)
    wT.close()
    pump(app)

    # ---- H14（第二轮）：历史关闭期间撤销，重开后列表与明细均为最新 ----
    # 复现路径：整理 → 开历史（明细=已移动）→ 关历史 → 主窗口撤销 →
    # 重开历史。旧缺陷：明细仍显示“已移动”，点“刷新”也不更新。
    rootG = fresh("rootG")
    for i in range(4):
        mk(rootG, f"大二上/计网/g{i}.pdf", f"G{i}" * 64)
    wG = make_window(app, rootG)
    assert scan_and_plan(wG, app)
    assert run_execute(wG, app)
    wG.on_history()
    dG = wG._history_dialog
    pump(app)
    before_batch = dG.batch_table.item(0, 3).text()
    before_item = dG.item_table.item(0, 4).text()
    dG.close()                      # 关闭历史（窗口对象保留，走复用路径）
    pump(app)
    wG.on_undo()                    # 历史不可见时从主窗口撤销
    wait_until(app, lambda: not wG._working)
    pump(app, 0.3)
    wG.on_history()                 # 重新打开 → 复用窗口走统一刷新
    dG = wG._history_dialog
    pump(app)
    after_batch = dG.batch_table.item(0, 3).text()
    after_item = dG.item_table.item(0, 4).text()
    st = Storage(rootG)
    bG = st.get_batch(dG._selected_batch_id)
    db_item0 = st.get_actions(dG._selected_batch_id)[0].status
    st.close()
    h14_ok = ("已移动" in before_item and "已完成" in before_batch
              and "已撤销" in after_item and "已撤销" in after_batch
              and db_item0 == "undone"
              and bG.status in ("undone", "partial_undone",
                                "partial_failed_undone"))
    rec("H14", "历史关闭期间撤销：重开后批次列表与逐项明细均为最新实际状态",
        "通过" if h14_ok else "未通过",
        f"关闭前：批次={before_batch}，明细={before_item}；"
        f"重开后：批次={after_batch}，明细={after_item}；"
        f"数据库首项={db_item0}，批次状态={bG.status}")

    # ---- H15（第二轮）：相同批次被选中时，手动刷新也能更新明细 ----
    # 通过独立连接写入真实状态变更（模拟外部复核结果落库），
    # 选中批次不变，点击“刷新”必须重读逐项动作。
    st = Storage(rootG)
    st.update_action(dG._selected_batch_id, 0, "failed",
                     "外部复核：目标位置被占用")
    st.close()
    dG.btn_refresh.click()
    pump(app)
    item_after = dG.item_table.item(0, 4).text()
    reason_after = dG.item_table.item(0, 5).text()
    h15_ok = (item_after == "失败" and "目标位置被占用" in reason_after)
    rec("H15", "相同批次被选中时手动刷新：明细按数据库实际状态更新",
        "通过" if h15_ok else "未通过",
        f"刷新后明细首项={item_after}（原为已撤销）；原因={reason_after}")

    # ---- H17（第二轮）：导出的状态与界面、数据库一致 ----
    json_path = os.path.join(BASE, "h17_export.json")
    real_save = main_mod.QFileDialog.getSaveFileName
    main_mod.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (json_path, "JSON 文件 (*.json)"))
    dG.btn_export.click()
    pump(app)
    main_mod.QFileDialog.getSaveFileName = real_save
    jdata17 = json.load(open(json_path, encoding="utf-8"))
    st = Storage(rootG)
    dba17 = st.get_actions(dG._selected_batch_id)
    st.close()
    ui_statuses = [dG.item_table.item(r, 4).text()
                   for r in range(dG.item_table.rowCount())]
    lbl17 = main_mod.ACTION_STATUS_LABELS
    h17_ok = ([a["status"] for a in jdata17["actions"]]
              == [a.status for a in dba17]
              and ui_statuses == [lbl17.get(a.status) for a in dba17]
              and "失败" in ui_statuses)
    rec("H17", "导出一致性：JSON 状态与界面明细、数据库逐项一致",
        "通过" if h17_ok else "未通过",
        f"导出={[a['status'] for a in jdata17['actions']]}；"
        f"数据库={[a.status for a in dba17]}；界面={ui_statuses}")

    # ---- H16（第二轮）：搜索/筛选/分页使用更新后的数据，页数减少校正页码 ----
    dG.search_edit.setText("g0")
    pump(app)
    n_search = dG.item_table.rowCount()
    dG.search_edit.setText("")
    pump(app)
    dG.status_filter.setCurrentIndex(dG.status_filter.findData("failed"))
    pump(app)
    n_failed = dG.item_table.rowCount()
    dG.status_filter.setCurrentIndex(0)
    pump(app)
    # 分页与页码校正：临时调小页大小构造多页，再让数据减少
    real_page_size = main_mod.HISTORY_PAGE_SIZE
    main_mod.HISTORY_PAGE_SIZE = 2
    try:
        dG.refresh_all()
        pump(app)
        p_two = dG.page_label.text()
        dG.btn_next.click()
        pump(app)
        p_page2 = dG.page_label.text()
        # 独立连接删除 3 条动作（真实数据减少）→ 刷新后页数减少
        st = Storage(rootG)
        st.conn.execute(
            "DELETE FROM actions WHERE batch_id = ? AND action_index > 0",
            (dG._selected_batch_id,))
        st.conn.commit()
        st.close()
        dG.btn_refresh.click()
        pump(app)
        p_corrected = dG.page_label.text()
        rows_left = dG.item_table.rowCount()
    finally:
        main_mod.HISTORY_PAGE_SIZE = real_page_size
    h16_ok = (n_search == 1 and n_failed == 1
              and "1/2" in p_two and "2/2" in p_page2
              and "1/1" in p_corrected and rows_left == 1)
    rec("H16", "搜索/筛选/分页使用更新后数据；页数减少时自动校正页码",
        "通过" if h16_ok else "未通过",
        f"搜索 g0={n_search} 项；失败筛选={n_failed} 项；"
        f"页大小 2 时：{p_two} → {p_page2}；数据减少后刷新：{p_corrected}"
        f"（剩 {rows_left} 项）")
    dG.close()
    pump(app)
    wG.close()
    pump(app)

    # ---- R 系列（第二轮）：核对恢复后台化验收 ----
    def make_executing_batch(root, label):
        """构造真实的 executing 中断批次（planned 动作 + 真实 SHA）。"""
        sr = scan_directory(root, detect_project_roots=False)
        pl = generate_plan(sr, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES)
        s_ = Storage(root)
        b_ = s_.create_batch(pl, label)
        s_.update_batch_status(b_, "executing")
        for i_, it_ in enumerate([x for x in pl.items if x.selected]):
            s_.create_action(b_, it_, i_,
                             _sha(os.path.join(root, it_.file.rel_path)))
        s_.close()
        return b_

    # R1/R2：忙碌状态、主线程响应、无重叠、迟到结果、独立连接
    rootR1 = fresh("rootR1")
    mk(rootR1, "大二上/软工/需求文档.pdf", "SE" * 100)
    wR1 = make_window(app, rootR1)
    bidR1 = make_executing_batch(rootR1, "R1 中断")
    RGATE = threading.Event()
    real_recover_run = main_mod.RecoverWorker.run

    class GatedRecoverWorker(main_mod.RecoverWorker):
        def run(self):
            RGATE.wait(timeout=20)
            real_recover_run(self)

    orig_recover_cls = main_mod.RecoverWorker
    main_mod.RecoverWorker = GatedRecoverWorker
    try:
        wR1._do_recover(bidR1)
        wait_until(app, lambda: wR1._working)
        # 核对期间主线程仍能处理定时器事件
        timer_fired = []
        QTimer.singleShot(60, lambda: timer_fired.append(True))
        pump(app, 0.3)
        r1_busy = (wR1.task.kind == "recover" and wR1.task.root == rootR1
                   and not wR1.task.cancelable
                   and not wR1.btn_cancel.isVisible()
                   and not wR1.btn_scan.isEnabled())
        r1_responsive = bool(timer_fired)
        # 忙碌期间不产生重叠任务
        second = wR1._do_recover(bidR1)
        r1_no_overlap = second is None
        # 迟到的旧任务结果（seq=0）不得污染当前状态
        fake = _RecoverResult()
        fake.batch_id = bidR1
        fake.recovered_moved = 99
        wR1._on_recover_done(0, rootR1, fake)
        r2_late_ignored = wR1._recover_result is None
        RGATE.set()
        ok_r1 = wait_until(app, lambda: not wR1._working
                           and wR1._recover_worker is not None
                           and not wR1._recover_worker.isRunning())
        pump(app)
    finally:
        main_mod.RecoverWorker = orig_recover_cls
        RGATE.set()
    st = Storage(rootR1)
    bR1 = st.get_batch(bidR1)
    st.close()
    r1_thread_exit = ok_r1 and wR1.task.describe() == "空闲"
    r2_status = bR1.status == "cancelled_clean"    # 全部仍未执行
    main_conn_ok = len(wR1._storage.list_batches(include_actions=False)) >= 1
    rec("R1", "后台核对恢复：忙碌状态正确、主线程可处理定时器事件、无重叠任务",
        "通过" if (r1_busy and r1_responsive and r1_no_overlap
                  and r1_thread_exit) else "未通过",
        f"任务={wR1.task.describe()}（运行中应为核对恢复中）；"
        f"忙碌位={r1_busy}；定时器触发={r1_responsive}；"
        f"重叠启动被拒={r1_no_overlap}；线程退出后空闲={r1_thread_exit}")
    rec("R2", "迟到结果被忽略；恢复真实结果落库；主线程数据库全程可用（独立连接）",
        "通过" if (r2_late_ignored and r2_status and main_conn_ok) else "未通过",
        f"旧 seq 结果被忽略={r2_late_ignored}；批次最终状态={bR1.status}；"
        f"主线程连接可用={main_conn_ok}")
    wR1.close()
    pump(app)

    # R3：异常路径——不误报成功，保留可复核记录
    rootR3 = fresh("rootR3")
    mk(rootR3, "大二上/软工/测试用例.pdf", "TC" * 100)
    wR3 = make_window(app, rootR3)
    bidR3 = make_executing_batch(rootR3, "R3 中断")
    real_rb = main_mod.recover_batch

    def _boom(*a, **k):
        raise RuntimeError("注入的核对故障")

    main_mod.recover_batch = _boom
    try:
        wR3._do_recover(bidR3)
        ok_r3 = wait_until(app, lambda: not wR3._working
                           and wR3._recover_worker is not None
                           and not wR3._recover_worker.isRunning())
        pump(app)
    finally:
        main_mod.recover_batch = real_rb
    st = Storage(rootR3)
    bR3 = st.get_batch(bidR3)
    acts3 = st.get_actions(bidR3)
    st.close()
    r3_ok = (ok_r3 and "核对恢复失败" in wR3.statusBar().currentMessage()
             and bR3.status == "executing"
             and all(a.status == "planned" for a in acts3))
    rec("R3", "恢复异常：明确失败提示，批次与动作保持原状（可复核），不误报成功",
        "通过" if r3_ok else "未通过",
        f"状态栏={wR3.statusBar().currentMessage()}；批次状态={bR3.status}；"
        f"动作保持 planned={all(a.status == 'planned' for a in acts3)}")
    wR3.close()
    pump(app)

    # R4：恢复运行中关闭——窗口与数据库保持到线程结束，随后自动关闭
    rootR4 = fresh("rootR4")
    mk(rootR4, "大二上/软工/部署手册.pdf", "DP" * 100)
    wR4 = make_window(app, rootR4)
    bidR4 = make_executing_batch(rootR4, "R4 中断")
    RGATE2 = threading.Event()

    class GatedRecoverWorker2(main_mod.RecoverWorker):
        def run(self):
            RGATE2.wait(timeout=20)
            real_recover_run(self)

    info_calls = []
    real_info = QMessageBox.information

    def _counting_info(*a, **k):
        info_calls.append(a)
        return QMessageBox.Ok

    main_mod.RecoverWorker = GatedRecoverWorker2
    QMessageBox.information = staticmethod(_counting_info)
    try:
        wR4._do_recover(bidR4)
        wait_until(app, lambda: wR4._working)
        wR4.close()                     # 恢复运行期间请求关闭
        pump(app)
        r4_close_wait = (wR4._close_pending and not wR4._closing
                         and wR4.isVisible())
        r4_block_new = wR4._do_recover(bidR4) is None   # 关闭等待期禁新任务
        RGATE2.set()
        ok_r4 = wait_until(app, lambda: wR4._closing or not wR4.isVisible(),
                           timeout=15)
        pump(app, 0.6)
    finally:
        main_mod.RecoverWorker = orig_recover_cls
        RGATE2.set()
        QMessageBox.information = real_info
    r4_ok = (r4_close_wait and r4_block_new and ok_r4
             and wR4._storage is None)
    rec("R4", "恢复运行中关闭：等待线程自然结束，随后自动关闭；期间禁新任务、无阻塞弹窗",
        "通过" if (r4_ok and not info_calls) else "未通过",
        f"关闭等待状态={r4_close_wait}；禁新任务={r4_block_new}；"
        f"自动关闭={ok_r4}；主线程数据库最后关闭={wR4._storage is None}；"
        f"关闭等待期间业务弹窗数={len(info_calls)}")

    # 清理 mock
    main_mod.subprocess.Popen = real_popen

    # ---- 汇总 ----
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if (failed == 0 and RESULTS) else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "v12_history_task",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": "QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v12_history_task.py",
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
    out = os.path.join(here, "results_v12_history_task.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_v12_history_task.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2（含第二轮：历史刷新修复/H9 重写/恢复后台化）— 历史与任务测试结果\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}\n")
        f.write(f"退出码: {exit_code}\n\n")
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
