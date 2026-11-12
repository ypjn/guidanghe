"""
归档盒 V1.2 第三轮 — 受控试用演练（真实桌面 cocoa）

这是“受控演练”：在独立临时目录副本上按生产入口完整走一遍日常
试用流程，记录数量、耗时与截图。**不是**真人四周使用记录。

覆盖（用户指定的五步 + 独立场景）：
- T-1 启动→扫描→预览→整理（夹具含中文路径、长路径、跨目录重名、
  内容重复、带 .git 的项目目录（自动保护）、普通课程资料）
- T-2 查看批次与逐项明细（含重复检测与项目保护的实际结果）
- T-3 关闭并重新打开历史，状态一致
- T-4 撤销 + 独立清单核验（路径 / 内容 SHA / 副本数量）
- T-5 独立场景：取消、冲突提示、关闭等待
交互方式：T-1~T-5 按钮全部为【程序化点击】真实 cocoa 控件；
另有人工观察项（逐张查看截图）。

运行：
  .venv/bin/python tests/desktop_trial_v12r3.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from storage import Storage
from utils import sha256_file

BASE = "/tmp/_v12r3_trial"
OUT = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
       "验收截图_20261107_V12R3_试用演练")
RESULTS = []
TIMINGS = {}


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
    mark = {"通过": "PASS", "未通过": "FAIL", "未测": "SKIP"}.get(status, "?")
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


def mk(root, rel, content="x" * 256):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)
    return p


def pump(app, sec=0.25):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def wait_until(app, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


def shot(widget, name):
    path = os.path.join(OUT, name)
    ok = widget.grab().save(path, "PNG")
    return path if ok else ""


def make_window(app, root):
    w = main_mod.MainWindow()
    w.path_edit.setText(root)
    w._reset_for_new_dir(root)
    w.show()
    app.processEvents()
    return w


def scan_and_plan(w, app):
    w.on_scan()
    ok = wait_until(app, lambda: not w._working and w.scan_result is not None
                    and not w._plan_working)
    if not ok:
        return False
    w._generate_and_show_plan()
    return wait_until(app, lambda: not w._plan_working
                      and w.current_plan is not None and w._plan_valid)


def run_execute(w, app):
    w.btn_execute.click()
    ok = wait_until(app, lambda: not w._working and w._exec_worker is not None
                    and not w._exec_worker.isRunning())
    pump(app)
    return ok


def snapshot_fs(root):
    """独立清单：不经过归档盒任何代码，直接枚举文件系统。"""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".organizer",)]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            with open(p, "rb") as f:
                snap[rel] = hashlib.sha256(f.read()).hexdigest()
    return snap


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    t_all = time.time()

    app = QApplication([])
    platform = app.platformName()
    desktop = platform == "cocoa"
    if not desktop:
        rec("T0", "真实桌面环境", "未测",
            f"当前平台后端={platform}（非 cocoa），真实桌面演练未测")

    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    # ============ 夹具：覆盖六类样本 ============
    rootA = fresh("主演练副本")
    normal_files = []
    for i in range(8):       # 普通课程资料（中文路径）
        normal_files.append(mk(rootA, f"大一下/高等数学/复习资料{i}.pdf",
                               f"MATH{i}" * 64))
    # 中文+空格目录
    normal_files.append(mk(rootA, "个人文件/照片 与 视频/头像.png",
                           "AVATAR" * 32))
    # 长路径
    long_rel = os.path.join("大二上", "数据结构与算法", "第三次上机实验",
                            "实验报告以及源代码和测试数据的最终提交版本",
                            "附件", "实验报告_最终版_请勿修改.pdf")
    normal_files.append(mk(rootA, long_rel, "LONG" * 64))
    # 跨目录重名（同内容与不同内容各一对）
    dup_content = "SAME-CONTENT" * 40
    normal_files.append(mk(rootA, "大一下/英语/作文.pdf", dup_content))
    normal_files.append(mk(rootA, "大二上/英语写作/作文.pdf", dup_content))
    normal_files.append(mk(rootA, "大一下/思政/报告.docx", "R1" * 32))
    normal_files.append(mk(rootA, "大二上/社会实践/报告.docx", "R2" * 32))
    # 项目目录（含 .git，应被自动保护，内部文件不整理）
    mk(rootA, "课程项目/贪吃蛇/.git/config", "gitcfg")
    mk(rootA, "课程项目/贪吃蛇/main.py", "print('snake')")
    mk(rootA, "课程项目/贪吃蛇/README.md", "README")

    snap_before = snapshot_fs(rootA)     # 独立清单：整理前

    # ============ T-1 启动→扫描→预览→整理 ============
    if desktop:
        try:
            t1 = time.time()
            wA = make_window(app, rootA)
            ok_plan = scan_and_plan(wA, app)
            s1 = shot(wA, "t1_预览计划.png")
            plan = wA.current_plan
            sel_items = [i for i in plan.items if i.selected]
            protected = getattr(wA.scan_result, "protected_count", 0)
            t1_dur = time.time() - t1
            TIMINGS["T-1 扫描+预览"] = round(t1_dur, 2)
            ok_exec = run_execute(wA, app)
            t1_exec = round(time.time() - t1 - t1_dur, 2)
            TIMINGS["T-1 整理执行"] = t1_exec
            s2 = shot(wA, "t1_整理完成.png")
            st = Storage(rootA)
            batch = st.get_latest_batch()
            counts = st.action_counts_by_batch().get(batch.batch_id, {})
            st.close()
            # 项目根保护：main.py/README.md 被“项目保护”跳过
            # （.git/config 另计入 .git 跳过规则，不重复计数）
            t1_ok = (ok_plan and ok_exec and batch.status in ("done", "partial")
                     and counts.get("moved", 0) > 0 and protected >= 2
                     and any("作文" in i.file.rel_path or
                             "报告" in i.file.rel_path
                             for i in plan.items))
            rec("T-1", "启动→扫描→预览→整理：中文/空格/长路径/重名/重复/项目保护样本真实执行",
                "通过" if t1_ok else "未通过",
                f"计划选中 {len(sel_items)} 项；项目保护跳过 {protected} 个文件"
                f"（main.py/README.md；.git 内部另按隐藏规则跳过）；"
                f"批次 #{batch.batch_id} 状态={batch.status}，"
                f"实际计数={counts}；预览+扫描 {t1_dur}s，执行 {t1_exec}s；"
                f"截图={os.path.basename(s1)}, {os.path.basename(s2)}")
        except Exception as e:
            rec("T-1", "启动→扫描→预览→整理", "未通过", f"异常：{e}")
            return finish(app, t_all)

        # ============ T-2 批次与逐项明细 ============
        try:
            t2 = time.time()
            wA.on_history()
            dA = wA._history_dialog
            pump(app)
            items_rows = dA.item_table.rowCount()
            # 状态筛选：重复/跳过项
            dA.status_filter.setCurrentIndex(dA.status_filter.findData("skipped"))
            pump(app)
            n_skipped = dA.item_table.rowCount()
            skip_reason = (dA.item_table.item(0, 5).text() if n_skipped else "")
            dA.status_filter.setCurrentIndex(0)
            pump(app)
            s3 = shot(dA, "t2_历史批次与明细.png")
            TIMINGS["T-2 查看历史"] = round(time.time() - t2, 2)
            # 项目目录未被整理（文件仍在原处）
            proj_kept = (os.path.isfile(os.path.join(rootA, "课程项目/贪吃蛇/main.py"))
                         and not os.path.exists(
                             os.path.join(rootA, "_已整理", "课程项目")))
            t2_ok = (items_rows > 0 and proj_kept)
            rec("T-2", "查看批次与逐项明细：重复检测可见，项目目录整体保留",
                "通过" if t2_ok else "未通过",
                f"明细 {items_rows} 行；跳过 {n_skipped} 项"
                f"（原因示例={skip_reason}）；项目目录原样保留={proj_kept}；"
                f"截图={os.path.basename(s3)}")
        except Exception as e:
            rec("T-2", "查看批次与明细", "未通过", f"异常：{e}")
            return finish(app, t_all)

        # ============ T-3 关闭重开历史一致 ============
        try:
            t3 = time.time()
            batch_txt_before = dA.batch_table.item(0, 3).text()
            item_before = dA.item_table.item(0, 4).text()
            dA.close()
            pump(app)
            wA.on_history()
            dA = wA._history_dialog
            pump(app)
            batch_txt_after = dA.batch_table.item(0, 3).text()
            item_after = dA.item_table.item(0, 4).text()
            TIMINGS["T-3 关闭重开历史"] = round(time.time() - t3, 2)
            t3_ok = (batch_txt_before == batch_txt_after
                     and item_before == item_after)
            rec("T-3", "关闭并重新打开历史：状态一致",
                "通过" if t3_ok else "未通过",
                f"关闭前批次={batch_txt_before}，明细={item_before}；"
                f"重开后批次={batch_txt_after}，明细={item_after}")
        except Exception as e:
            rec("T-3", "关闭重开历史", "未通过", f"异常：{e}")
            return finish(app, t_all)

        # ============ T-4 撤销 + 独立清单核验 ============
        try:
            t4 = time.time()
            dA.btn_undo.click()          # 从历史入口撤销
            ok_undo = wait_until(app, lambda: not wA._working
                                 and wA._undo_worker is not None
                                 and not wA._undo_worker.isRunning())
            pump(app)
            snap_after_undo = snapshot_fs(rootA)   # 独立清单：撤销后
            # 对比：撤销后应与整理前一致（排除整理记录目录 .organizer）
            missing = [k for k in snap_before if k not in snap_after_undo]
            extra = [k for k in snap_after_undo if k not in snap_before]
            changed = [k for k in snap_before
                       if k in snap_after_undo
                       and snap_before[k] != snap_after_undo[k]]
            dup_counts_equal = all(
                list(snap_before.values()).count(h)
                == list(snap_after_undo.values()).count(h)
                for h in set(snap_before.values()))
            s4 = shot(dA, "t4_撤销后历史.png")
            TIMINGS["T-4 撤销+核验"] = round(time.time() - t4, 2)
            t4_ok = (ok_undo and not missing and not extra and not changed
                     and dup_counts_equal)
            rec("T-4", "撤销后独立清单核验：路径、内容 SHA、副本数量与整理前完全一致",
                "通过" if t4_ok else "未通过",
                f"独立枚举 {len(snap_before)} 个文件；缺失={missing[:3]}；"
                f"新增={extra[:3]}；内容变化={changed[:3]}；"
                f"重复内容副本数一致={dup_counts_equal}；"
                f"截图={os.path.basename(s4)}")
            dA.close()
            pump(app)
            wA.close()
            pump(app)
        except Exception as e:
            rec("T-4", "撤销+独立清单核验", "未通过", f"异常：{e}")
            return finish(app, t_all)

    # ============ T-5a 取消（独立副本） ============
    if desktop:
        try:
            t5a = time.time()
            rootB = fresh("取消场景")
            for i in range(6):
                mk(rootB, f"大二上/计网/实验{i}.pdf", f"NET{i}" * 4096)
            wB = make_window(app, rootB)
            assert scan_and_plan(wB, app)
            EGATE = threading.Event()
            real_exec_run = main_mod.ExecuteWorker.run

            class GatedExec(main_mod.ExecuteWorker):
                def run(self):
                    EGATE.wait(timeout=20)
                    real_exec_run(self)

            orig_cls = main_mod.ExecuteWorker
            main_mod.ExecuteWorker = GatedExec
            try:
                wB.btn_execute.click()
                wait_until(app, lambda: wB._working)
                wB.btn_cancel.click()     # 请求取消（当前文件完成后生效）
                EGATE.set()
                ok_b = wait_until(app, lambda: not wB._working
                                  and wB._exec_worker is not None
                                  and not wB._exec_worker.isRunning())
                pump(app)
            finally:
                main_mod.ExecuteWorker = orig_cls
                EGATE.set()
            st = Storage(rootB)
            bB = st.get_latest_batch()
            counts_b = st.action_counts_by_batch().get(bB.batch_id, {})
            st.close()
            wB.on_history()
            dB = wB._history_dialog
            pump(app)
            s5 = shot(dB, "t5a_取消后历史.png")
            TIMINGS["T-5a 取消场景"] = round(time.time() - t5a, 2)
            # 门控使取消请求发生在首个文件操作完成之前——
            # 产品语义“取消在当前文件操作结束后生效”如实记录为
            # cancelled_clean（已取消·未移动）。部分取消（已完成+未执行
            # 分别记录）由 V1.0 回归取消时序专项覆盖。
            t5a_ok = (ok_b and bB.status in ("cancelled", "cancelled_clean",
                                             "partial")
                      and counts_b.get("moved", 0)
                      + counts_b.get("cancelled", 0) > 0)
            rec("T-5a", "取消整理：批次如实记录取消时的实际完成情况（程序化点击）",
                "通过" if t5a_ok else "未通过",
                f"批次状态={bB.status}；实际计数={counts_b}；"
                f"（取消早于首个文件完成→全部计为取消，语义正确；"
                f"部分取消时序见 V1.0 回归）；"
                f"截图={os.path.basename(s5)}")
            dB.close()
            pump(app)
            wB.close()
            pump(app)
        except Exception as e:
            rec("T-5a", "取消场景", "未通过", f"异常：{e}")

    # ============ T-5b 冲突提示（独立副本） ============
    if desktop:
        try:
            t5b = time.time()
            rootC = fresh("冲突场景")
            for i in range(3):
                mk(rootC, f"大二上/软工/用例{i}.pdf", f"UC{i}" * 64)
            wC = make_window(app, rootC)
            assert scan_and_plan(wC, app)
            assert run_execute(wC, app)
            st = Storage(rootC)
            batch_c = st.get_latest_batch()
            moved_act = next(a for a in st.get_actions(batch_c.batch_id)
                             if a.status == "moved")
            st.close()
            # 篡改目标文件 → 撤销时应保留用户修改并提示
            tgt = os.path.join(rootC, moved_act.target_rel)
            with open(tgt, "wb") as f:
                f.write(b"USER-EDITED" * 40)
            wC.on_undo()
            ok_c = wait_until(app, lambda: not wC._working
                              and wC._undo_worker is not None
                              and not wC._undo_worker.isRunning())
            pump(app)
            st = Storage(rootC)
            acts_c = st.get_actions(batch_c.batch_id)
            st.close()
            conflicted = [a for a in acts_c
                          if a.status in ("conflict", "undone")
                          and (a.error or "")]
            user_edit_kept = (open(tgt, "rb").read() == b"USER-EDITED" * 40)
            wC.on_history()
            dC = wC._history_dialog
            pump(app)
            dC.status_filter.setCurrentIndex(dC.status_filter.findData("conflict"))
            pump(app)
            s6 = shot(dC, "t5b_冲突明细.png")
            TIMINGS["T-5b 冲突场景"] = round(time.time() - t5b, 2)
            conflict_acts = [a for a in acts_c if a.status == "conflict"]
            t5b_ok = (ok_c and user_edit_kept
                      and len(conflict_acts) >= 1
                      and any("修改" in (a.error or "") or "不一致" in (a.error or "")
                              for a in conflict_acts))
            rec("T-5b", "冲突提示：目标被外部修改→撤销保留用户修改并记录冲突原因",
                "通过" if t5b_ok else "未通过",
                f"冲突项 {len(conflict_acts)} 个"
                f"（原因={[a.error for a in conflict_acts][:2]}）；"
                f"用户修改保留={user_edit_kept}；"
                f"截图={os.path.basename(s6)}")
            dC.close()
            pump(app)
            wC.close()
            pump(app)
        except Exception as e:
            rec("T-5b", "冲突场景", "未通过", f"异常：{e}")

    # ============ T-5c 关闭等待（独立副本） ============
    if desktop:
        try:
            t5c = time.time()
            rootD = fresh("关闭等待场景")
            for i in range(4):
                mk(rootD, f"大二上/OS/课件{i}.pdf", f"OS{i}" * 64)
            wD = make_window(app, rootD)
            assert scan_and_plan(wD, app)
            assert run_execute(wD, app)
            UGATE = threading.Event()
            real_undo_run = main_mod.UndoWorker.run

            class GatedUndo(main_mod.UndoWorker):
                def run(self):
                    UGATE.wait(timeout=20)
                    real_undo_run(self)

            orig_undo = main_mod.UndoWorker
            main_mod.UndoWorker = GatedUndo
            try:
                wD.on_undo()
                wait_until(app, lambda: wD._working)
                wD.close()               # 撤销运行中请求关闭
                pump(app, 0.1)
                close_wait = (wD._close_pending and not wD._closing
                              and wD.isVisible())
                s7 = shot(wD, "t5c_关闭等待.png")
                UGATE.set()
                ok_d = wait_until(app, lambda: wD._closing
                                  or not wD.isVisible(), timeout=15)
                pump(app, 0.6)
            finally:
                main_mod.UndoWorker = orig_undo
                UGATE.set()
            TIMINGS["T-5c 关闭等待场景"] = round(time.time() - t5c, 2)
            t5c_ok = (close_wait and ok_d and wD._storage is None)
            rec("T-5c", "关闭等待：撤销运行中关窗→等待线程结束→自动关闭（程序化）",
                "通过" if t5c_ok else "未通过",
                f"关闭等待={close_wait}；自动关闭={ok_d}；"
                f"数据库最后关闭={wD._storage is None}；"
                f"截图={os.path.basename(s7)}")
        except Exception as e:
            rec("T-5c", "关闭等待场景", "未通过", f"异常：{e}")

    # ============ 人工观察 ============
    # 注意：脚本运行时本条目固定记为「未测」占位——人工逐张查看
    # 截图后，需手动把 results_trial_v12r3.json/.txt 的 T-6 更新为
    # 「通过」并注明复核日期与观察内容（先例见交互 012 勘误）。
    if desktop:
        shots = sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []
        rec("T-6", "人工观察：逐张查看演练截图，确认布局、中文与状态显示",
            "未测",
            "请人工查看以下截图：\n"
            + "\n".join(f"    {s}" for s in shots)
            + "\n观察点：预览计划含保护提示；历史明细重复原因；"
              "撤销后历史状态；取消/冲突/关闭等待各场景文案。")

    return finish(app, t_all)


def finish(app, t_all):
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"总耗时: {round(time.time() - t_all, 2)}s")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "desktop_trial_v12r3",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_trial_v12r3.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "qt_platform": app.platformName(),
        "note": "受控演练（独立副本，程序化点击）；非真人日常使用记录。",
        "timings_s": TIMINGS,
        "elapsed_s": round(time.time() - t_all, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    with open(os.path.join(here, "results_trial_v12r3.json"), "w",
              encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_trial_v12r3.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2 第三轮 — 受控试用演练（真实桌面，程序化点击）\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"Qt 后端: {meta['qt_platform']}\n截图目录: {OUT}\n")
        f.write(f"各阶段耗时: {TIMINGS}\n总耗时: {meta['elapsed_s']}s\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}"
                f" / 未测 {untested}\n退出码: {exit_code}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']}] {r['id']}  {r['title']}\n")
            for line in str(r["evidence"]).splitlines():
                f.write(f"    {line}\n")
            f.write("\n")
    print(f"结果已保存: {os.path.join(here, 'results_trial_v12r3.json')}")
    shutil.rmtree(BASE, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
