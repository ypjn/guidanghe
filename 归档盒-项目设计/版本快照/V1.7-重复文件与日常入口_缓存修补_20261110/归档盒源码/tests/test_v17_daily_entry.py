"""
归档盒 V1.7 — 日常整理入口 专项验证（offscreen）

E01 最近空间：保存/重启恢复、固定排序、移除记录不删目录
E02 最近空间：目录不存在只标记，不自动创建
E03 待整理清单：文件/目录/重复/父子包含
E04 待整理清单：根目录归属（无空间、空间外）
E05 范围解析：只纳入清单文件，保护目录被排除
E06 主窗口：清单范围扫描结果、清单变化使旧计划失效
E07 忙碌/关闭等待期间拒绝修改清单与入口禁用
E08 最近记录与方案数据测试隔离（不写用户真实目录）

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v17_daily_entry.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

import recent_spaces
import scheme_store
from intake_model import IntakeList
from scanner import scan_directory
from main import MainWindow

RESULTS = []
BASE = "/tmp/_v17_daily_entry"


def rec(tid, title, ok, evidence=""):
    RESULTS.append({"id": tid, "status": "通过" if ok else "未通过",
                    "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root, rel, content="x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))
    return p


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.environ[recent_spaces.ENV_OVERRIDE] = os.path.join(BASE, "recent.json")
    os.environ[scheme_store.ENV_APP_DIR] = os.path.join(BASE, "appdata")
    app = QApplication.instance() or QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # E01 最近空间保存/固定/移除
    try:
        recent_spaces.clear()
        a = fresh("e01_a")
        b = fresh("e01_b")
        recent_spaces.touch(a)
        recent_spaces.touch(b)
        a_real = os.path.realpath(a)
        b_real = os.path.realpath(b)
        listed = recent_spaces.list_spaces()
        newest_first = [r.path for r in listed] == [b_real, a_real]
        recent_spaces.set_pinned(a, True)
        pinned_first = recent_spaces.list_spaces()[0].path == a_real
        removed = recent_spaces.remove(b)
        # 模拟重启：重新从磁盘读取
        after = recent_spaces.list_spaces()
        restored = (len(after) == 1 and after[0].path == a_real
                    and after[0].pinned and os.path.isdir(a))
        ok = newest_first and pinned_first and removed and restored
        rec("E01", "最近空间保存/重启恢复、固定排序、移除记录不删目录", ok,
            f"最近在前={newest_first} 固定在前={pinned_first} "
            f"移除={removed} 重启后={[(r.path, r.pinned) for r in after]}")
    except Exception as e:
        rec("E01", "最近空间", False, f"异常：{e}")

    # E02 不存在目录不自动创建
    try:
        missing = os.path.join(BASE, "e02_missing")
        recent_spaces.touch(missing)
        rec_obj = recent_spaces.get(missing)
        ok = (rec_obj is not None and not rec_obj.exists
              and not rec_obj.usable and not os.path.exists(missing))
        rec("E02", "目录不存在时只标记，不自动创建", ok,
            f"exists={rec_obj.exists if rec_obj else '-'} "
            f"usable={rec_obj.usable if rec_obj else '-'} "
            f"目录被创建={os.path.exists(missing)}")
        recent_spaces.remove(missing)
    except Exception as e:
        rec("E02", "缺失目录", False, f"异常：{e}")

    # E03 清单去重与父子包含
    try:
        root = fresh("e03")
        mk(root, "a/x.txt")
        mk(root, "a/sub/y.txt")
        mk(root, "b/z.txt")
        lst = IntakeList()
        r1 = lst.add(os.path.join(root, "a"), root)
        r2 = lst.add(os.path.join(root, "a", "x.txt"), root)      # 被目录覆盖
        r3 = lst.add(os.path.join(root, "a", "x.txt"), root)      # 重复
        r4 = lst.add(os.path.join(root, "b", "z.txt"), root)
        r5 = lst.add(os.path.join(root, "a", "sub"), root)        # 已被 a 覆盖
        ok = (r1.status == "added" and r2.status == "covered"
              and r3.status == "covered" and r4.status == "added"
              and r5.status == "covered" and len(lst) == 2)
        rec("E03", "清单加入文件/目录，处理重复与父子包含", ok,
            f"状态={[r1.status, r2.status, r3.status, r4.status, r5.status]} "
            f"项数={len(lst)}")

        # 先加子项，再加父目录 → 合并
        lst2 = IntakeList()
        lst2.add(os.path.join(root, "b", "z.txt"), root)
        r6 = lst2.add(os.path.join(root, "b"), root)
        ok2 = (r6.status == "coalesced" and len(lst2) == 1
               and lst2.items[0].kind == "dir")
        rec("E03b", "后加入的父目录合并已有子项", ok2,
            f"状态={r6.status} 项数={len(lst2)} 类型={lst2.items[0].kind}")
    except Exception as e:
        rec("E03", "清单去重", False, f"异常：{e}")

    # E04 根目录归属
    try:
        root = fresh("e04")
        outside = fresh("e04_outside")
        mk(root, "a/x.txt")
        lst = IntakeList()
        no_root_file = lst.add(os.path.join(root, "a", "x.txt"), None)
        no_root_dir = lst.add(outside, None)
        outside_res = lst.add(outside, root)
        ok = (no_root_file.status == "needs_root"
              and not no_root_file.candidate_root
              and no_root_dir.status == "needs_root"
              and no_root_dir.candidate_root == os.path.realpath(outside)
              and outside_res.status == "outside"
              and len(lst) == 0)
        rec("E04", "未选空间/空间外来源得到明确处理，不静默设共同父目录", ok,
            f"文件无空间={no_root_file.status} 目录候选根="
            f"{no_root_dir.status} 空间外={outside_res.status}")
    except Exception as e:
        rec("E04", "根目录归属", False, f"异常：{e}")

    # E05 范围解析与保护
    try:
        root = fresh("e05")
        mk(root, "a/keep1.txt", "1")
        mk(root, "a/keep2.txt", "2")
        mk(root, "b/other.txt", "3")
        protected = os.path.join(root, "protected")
        mk(root, "protected/secret.txt", "4")
        raw = scan_directory(root, protect_dirs=[protected],
                             detect_project_roots=False)
        lst = IntakeList()
        lst.add(os.path.join(root, "a"), root)
        lst.add(os.path.join(root, "protected"), root)
        res = lst.resolve(root, raw)
        expected = {"a/keep1.txt", "a/keep2.txt"}
        ok = (res.included == expected and "b/other.txt" not in res.included
              and res.protected >= 1)
        rec("E05", "只纳入清单范围的文件，保护目录被排除", ok,
            f"纳入={sorted(res.included)} 受保护项={res.protected}\n"
            f"明细={[(os.path.basename(d.path), d.included, d.reason) for d in res.details]}")
    except Exception as e:
        rec("E05", "范围解析", False, f"异常：{e}")

    # E06 主窗口范围扫描与计划失效
    try:
        root = fresh("e06")
        mk(root, "a/x.txt")
        mk(root, "a/sub/y.txt")
        mk(root, "b/z.txt")
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        messages = w._add_intake_paths([os.path.join(root, "a")])
        raw = scan_directory(root, detect_project_roots=False)
        w._full_scan_result = raw
        w._scan_generation = 1
        w._plan_scope = "intake"
        eff = w._effective_scan_result(raw)
        scope_ok = ({f.rel_path for f in eff.files}
                    == {"a/x.txt", "a/sub/y.txt"})
        scope_note = w._plan_scope_note
        w.current_plan = None
        w._plan_valid = True
        w._add_intake_paths([os.path.join(root, "b", "z.txt")])
        invalidated = (w.current_plan is None and not w._plan_valid
                       and "已失效" in w.invalid_label.text())
        ok = scope_ok and invalidated and "纳入 2 个文件" in scope_note
        rec("E06", "清单范围扫描只纳入指定文件；清单变化使旧计划失效", ok,
            f"纳入={sorted(f.rel_path for f in eff.files)} "
            f"范围说明={scope_note} 计划失效={invalidated} 提示={messages}")
        w.close()
        pump(app)
    except Exception as e:
        rec("E06", "主窗口范围", False, f"异常：{e}")

    # E07 忙碌/关闭等待门控
    try:
        root = fresh("e07")
        mk(root, "a/x.txt")
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
        w._add_intake_paths([os.path.join(root, "a")])
        before = len(w.intake)
        w._working = True
        w._update_button_states()
        messages = w._add_intake_paths([os.path.join(root, "a", "x.txt")])
        blocked = (len(w.intake) == before
                   and any("后台任务" in m for m in messages)
                   and not w.btn_intake.isEnabled()
                   and not w.btn_recent.isEnabled()
                   and not w.btn_duplicates.isEnabled())
        w._working = False
        w._update_button_states()
        rec("E07", "忙碌期间拒绝修改清单，新增入口一致禁用", blocked,
            f"项数={before}→{len(w.intake)} 提示={messages}")
        w.close()
        pump(app)
    except Exception as e:
        rec("E07", "忙碌门控", False, f"异常：{e}")

    # E08 测试隔离
    try:
        recent_file = recent_spaces.store_path()
        scheme_dir = scheme_store.library_dir()
        isolated = (recent_file.startswith(os.path.realpath(BASE)) or
                    recent_file.startswith(BASE))
        scheme_isolated = scheme_dir.startswith(
            os.path.join(BASE, "appdata"))
        rec("E08", "最近记录与方案数据在测试中使用隔离目录", isolated and scheme_isolated,
            f"recent={recent_file}\nschemes={scheme_dir}")
    except Exception as e:
        rec("E08", "测试隔离", False, f"异常：{e}")

    # E09 最近空间 / 待整理清单窗口可构造并反映数据
    try:
        from daily_entry_dialogs import RecentSpacesDialog, IntakeDialog
        root = fresh("e09")
        mk(root, "a/x.txt")
        recent_spaces.touch(root)
        rdlg = RecentSpacesDialog(lambda p: True, None)
        rows_ok = rdlg.table.rowCount() == len(recent_spaces.list_spaces())
        rdlg.close()

        lst = IntakeList()
        lst.add(os.path.join(root, "a"), root)
        idlg = IntakeDialog(
            get_root=lambda: root,
            get_summary=lambda: "测试范围",
            get_items=lambda: lst,
            add_paths=lambda paths: [],
            remove_path=lambda p: True,
            clear_items=lambda: None,
            parent=None)
        list_ok = idlg.list_widget.count() == 1
        idlg.close()
        rec("E09", "最近空间与待整理清单窗口可构造并反映当前数据",
            rows_ok and list_ok,
            f"最近行数正确={rows_ok} 清单行数=1?{list_ok}")
    except Exception as e:
        rec("E09", "窗口构造", False, f"异常：{e}")

    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 and RESULTS else 1
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_v17_daily_entry.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"suite": "test_v17_daily_entry",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "counts": {"total": len(RESULTS), "pass": passed,
                              "fail": failed},
                   "exit_code": exit_code, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    try:
        from main import shutdown_widgets
        shutdown_widgets()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
