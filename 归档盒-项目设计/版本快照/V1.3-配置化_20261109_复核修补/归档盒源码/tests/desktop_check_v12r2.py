"""
归档盒 V1.2 第二轮 — 真实桌面交互检查

在系统默认图形后端（macOS cocoa）启动真实窗口，覆盖本轮三项修复：
- D1: 历史重开刷新（关闭历史 → 撤销 → 重开，明细必须为最新）
- D2: 后台核对恢复进度与结果文案（真实大文件使核对可观察）
- D3: 恢复运行中关闭 → 关闭等待 → 自动关闭
- D4: 人工观察（逐张查看渲染截图）

交互方式：D1–D3 的按钮/输入全部为【程序化点击】真实 cocoa 控件；
D4 为【人工观察】。若只能使用 offscreen，则标记“真实桌面交互未测”。

运行：
  /Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python \
  tests/desktop_check_v12r2.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog

import main as main_mod
from scanner import scan_directory
from planner import generate_plan, MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES
from storage import Storage
from utils import sha256_file

BASE = "/tmp/_v12r2_desktop_verify"
OUT = ("/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒-项目设计/"
       "验收截图_20261107_V12R2")
RESULTS = []


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


def mk(root, rel, size=256, pattern=b"x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        chunk = pattern * 65536
        written = 0
        while written < size:
            n = min(len(chunk), size - written)
            f.write(chunk[:n])
            written += n
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
    ok = wait_until(app, lambda: not w._plan_working and w.current_plan is not None
                    and w._plan_valid)
    pump(app)
    return ok


def run_execute(w, app):
    w.btn_execute.click()
    ok = wait_until(app, lambda: not w._working and w._exec_worker is not None
                    and not w._exec_worker.isRunning())
    pump(app)
    return ok


def make_interrupted(root, plan, n_moved, label):
    """构造真实 executing 中断批次：前 n_moved 项真实移动、全部 planned。"""
    st = Storage(root)
    bid = st.create_batch(plan, label)
    st.update_batch_status(bid, "executing")
    items = sorted([i for i in plan.items if i.selected],
                   key=lambda i: i.file.rel_path)
    for idx, it in enumerate(items):
        st.create_action(bid, it, idx,
                         sha256_file(os.path.join(root, it.file.rel_path)))
    moved = []
    for it in items[:n_moved]:
        src = os.path.join(root, it.file.rel_path)
        tgt = os.path.join(root, it.target_rel_path)
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        shutil.move(src, tgt)
        moved.append((src, tgt))
    st.close()
    return bid, moved


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    app = QApplication([])
    platform = app.platformName()
    desktop = platform == "cocoa"

    info_calls = []
    real_info = QMessageBox.information

    def _info(*a, **k):
        info_calls.append(a)
        return QMessageBox.Ok

    QMessageBox.information = staticmethod(_info)
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

    class _AutoAcceptExecuteDialog(main_mod.ExecuteConfirmDialog):
        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = _AutoAcceptExecuteDialog

    if not desktop:
        rec("D0", "真实桌面环境", "未测",
            f"当前平台后端={platform}（非 cocoa），真实桌面交互未测，"
            "不伪造通过")

    # ================= D1: 历史重开刷新（真实桌面） =================
    if desktop:
        try:
            root1 = fresh("d1_root")
            for i in range(5):
                mk(root1, f"大二上/计网/桌面检查{i}.pdf", 4096)
            w1 = make_window(app, root1)
            assert scan_and_plan(w1, app)
            assert run_execute(w1, app)
            w1.on_history()
            d1 = w1._history_dialog
            pump(app)
            before_item = d1.item_table.item(0, 4).text()
            s1 = shot(d1, "d1_历史_撤销前明细已移动.png")
            d1.close()
            pump(app)
            w1.on_undo()                      # 历史关闭期间撤销
            wait_until(app, lambda: not w1._working)
            pump(app, 0.3)
            w1.on_history()                   # 重新打开
            d1 = w1._history_dialog
            pump(app)
            after_batch = d1.batch_table.item(0, 3).text()
            after_item = d1.item_table.item(0, 4).text()
            s2 = shot(d1, "d1_历史_重开后明细已撤销.png")
            st = Storage(root1)
            db0 = st.get_actions(d1._selected_batch_id)[0].status
            st.close()
            ok = ("已移动" in before_item and "已撤销" in after_item
                  and "已撤销" in after_batch and db0 == "undone")
            rec("D1", "真实桌面：历史关闭期间撤销，重开后明细为最新（已撤销）",
                "通过" if ok else "未通过",
                f"关闭前明细={before_item}；重开后批次={after_batch}，"
                f"明细={after_item}；数据库={db0}；"
                f"截图={os.path.basename(s1)}, {os.path.basename(s2)}")
            d1.close()
            pump(app)
            w1.close()
            pump(app)
        except Exception as e:
            rec("D1", "真实桌面：历史重开刷新", "未通过", f"异常：{e}")

    # ============ D2: 后台核对恢复进度与结果（真实大文件） ============
    if desktop:
        try:
            root2 = fresh("d2_root")
            # 5 个 60MB 文件：已移动项核对需重算 SHA，进度可观察
            for i in range(5):
                mk(root2, f"大二上/软工/规格说明{i}.pdf",
                   60 * 1024 * 1024, pattern=bytes([65 + i]))
            w2 = make_window(app, root2)
            assert scan_and_plan(w2, app)
            bid2, moved2 = make_interrupted(root2, w2.current_plan, 3,
                                            "D2 中断批次")
            w2.on_history()
            d2 = w2._history_dialog
            pump(app)
            d2.btn_recover.click()            # 程序化点击真实按钮
            # 轮询捕获恢复运行中的界面（进度/状态栏）
            progress_seen = ""
            s3 = ""
            deadline = time.time() + 10
            while time.time() < deadline:
                app.processEvents()
                msg = w2.statusBar().currentMessage()
                if w2._working and ("核对" in msg or "恢复" in msg):
                    progress_seen = msg
                    if not s3:
                        s3 = shot(w2, "d2_恢复运行中_界面响应.png")
                    if "正在核对 [" in msg:
                        break
                if not w2._working and w2._recover_worker is not None \
                        and not w2._recover_worker.isRunning():
                    break
                time.sleep(0.005)
            wait_until(app, lambda: not w2._working
                       and w2._recover_worker is not None
                       and not w2._recover_worker.isRunning())
            pump(app)
            result_msg = w2.statusBar().currentMessage()
            st = Storage(root2)
            acts2 = {a.action_index: a for a in st.get_actions(bid2)}
            b2 = st.get_batch(bid2)
            st.close()
            w2.on_history()                   # 恢复后重看历史
            d2 = w2._history_dialog
            pump(app)
            s4 = shot(d2, "d2_恢复后历史明细.png")
            ui2 = [d2.item_table.item(r, 4).text()
                   for r in range(d2.item_table.rowCount())]
            n_moved_ui = sum(1 for s in ui2 if s == "已移动")
            n_planned_ui = sum(1 for s in ui2 if s == "未执行")
            ok = (b2.status == "recovered_partial"
                  and acts2[0].status == "moved" and acts2[3].status == "planned"
                  and n_moved_ui == 3 and n_planned_ui == 2
                  and "补记已移动" in result_msg and "仍未执行" in result_msg
                  and not os.path.isfile(moved2[0][0])
                  and os.path.isfile(moved2[0][1]))
            rec("D2", "真实桌面：后台核对恢复——运行中界面响应，补记/未执行如实记录",
                "通过" if ok else "未通过",
                f"运行中状态栏={progress_seen or '（太快未捕获，线程已后台运行）'}；"
                f"结果={result_msg}；批次={b2.status}；"
                f"明细：已移动 {n_moved_ui} / 未执行 {n_planned_ui}；"
                f"截图={os.path.basename(s3) if s3 else '无'}, "
                f"{os.path.basename(s4)}")
            d2.close()
            pump(app)
            w2.close()
            pump(app)
        except Exception as e:
            rec("D2", "真实桌面：后台核对恢复", "未通过", f"异常：{e}")

    # ============ D3: 恢复运行中关闭 → 关闭等待 → 自动关闭 ============
    if desktop:
        try:
            root3 = fresh("d3_root")
            for i in range(5):
                mk(root3, f"大二上/软工/验收报告{i}.pdf",
                   60 * 1024 * 1024, pattern=bytes([97 + i]))
            w3 = make_window(app, root3)
            assert scan_and_plan(w3, app)
            bid3, moved3 = make_interrupted(root3, w3.current_plan, 3,
                                            "D3 中断批次")
            w3.on_history()
            d3 = w3._history_dialog
            pump(app)
            d3.btn_recover.click()            # 发起后台恢复
            wait_until(app, lambda: w3._working)
            n_info_before = len(info_calls)
            w3.close()                        # 恢复运行期间请求关闭
            pump(app, 0.1)
            close_wait_state = (w3._close_pending and not w3._closing
                                and w3.isVisible())
            s5 = shot(w3, "d3_恢复中关闭_等待后台结束.png")
            closed = wait_until(app, lambda: w3._closing
                                or not w3.isVisible(), timeout=20)
            pump(app, 0.6)
            st = Storage(root3)
            b3 = st.get_batch(bid3)
            st.close()
            n_info_during = len(info_calls) - n_info_before
            ok = (close_wait_state and closed and w3._storage is None
                  and b3.status == "recovered_partial" and n_info_during == 0)
            rec("D3", "真实桌面：恢复运行中关闭——关闭等待、无阻塞弹窗、随后自动关闭",
                "通过" if ok else "未通过",
                f"关闭等待={close_wait_state}（窗口保持可见等待线程结束）；"
                f"自动关闭={closed}；数据库最后关闭={w3._storage is None}；"
                f"恢复仍真实完成={b3.status == 'recovered_partial'}；"
                f"关闭等待期间业务弹窗={n_info_during}（应 0）；"
                f"截图={os.path.basename(s5)}")
            if w3._history_dialog:
                w3._history_dialog.close()
        except Exception as e:
            rec("D3", "真实桌面：恢复中关闭", "未通过", f"异常：{e}")

    # ================= D4: 人工观察 =================
    if desktop:
        shots = sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []
        rec("D4", "人工观察：逐张查看截图，确认布局与中文显示正常",
            "未测",
            "请人工查看以下截图（本轮桌面检查产物）：\n"
            + "\n".join(f"    {s}" for s in shots)
            + "\n观察点：撤销前明细=已移动、重开后=已撤销；恢复运行中主窗口"
              "进度与状态栏；关闭等待提示文案；历史明细中文状态。")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    untested = sum(1 for r in RESULTS if r["status"] == "未测")
    exit_code = 0 if failed == 0 else 1
    print("\n" + "=" * 70)
    print(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed} / 未测 {untested}")
    print(f"退出码: {exit_code}")
    print("=" * 70)

    here = os.path.dirname(os.path.abspath(__file__))
    meta = {
        "suite": "desktop_check_v12r2",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": ".venv/bin/python tests/desktop_check_v12r2.py",
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "qt_platform": platform,
        "shots_dir": OUT,
        "elapsed_s": round(time.time() - t0, 2),
        "exit_code": exit_code,
        "counts": {"total": len(RESULTS), "pass": passed,
                   "fail": failed, "unverified": untested},
    }
    out = os.path.join(here, "results_desktop_check_v12r2.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({**meta, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(here, "results_desktop_check_v12r2.txt"), "w",
              encoding="utf-8") as f:
        f.write("归档盒 V1.2 第二轮 — 真实桌面交互检查\n")
        f.write(f"时间: {meta['ts']}\n命令: {meta['command']}\n")
        f.write(f"解释器: {sys.executable}  Python {meta['python']}\n")
        f.write(f"Qt 后端: {platform}（真实桌面={'是' if desktop else '否'}）\n")
        f.write(f"耗时: {meta['elapsed_s']}s\n")
        f.write(f"截图目录: {OUT}\n")
        f.write(f"合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}"
                f" / 未测 {untested}\n")
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
