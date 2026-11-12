"""S7-5 重复核对（真实 Qt 控件，只读）。

覆盖：内容重复 / 同名不同内容 / 待核对与失败分组；打开、定位、复制路径、
CSV/JSON 导出；外部修改文件后重开窗口重新核对变化项；副本数量与内容不变，
不删除、不合并、不自动执行。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil

from s7_common import (make_app, ok, pump, wait, shot, Dispatcher, ack_message,
                       finish, mkfile, open_root, close_app, RESULT, OUT, BASE,
                       FakeFileDialog)

import windows_harness as H
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import duplicate_dialog as dd

_TAG = re.compile(r"<[^>]+>")
DUP_BYTES = b"DUP-CONTENT-0123456789"


def _strip(text):
    return _TAG.sub("", str(text or "")).replace("\xa0", " ")


app = make_app()
ROOT = os.path.join(BASE, "dupes_root")
if os.path.exists(ROOT):
    shutil.rmtree(ROOT)
mkfile(ROOT, "a/讲义.pdf", DUP_BYTES)
mkfile(ROOT, "b/副本-讲义.pdf", DUP_BYTES)
mkfile(ROOT, "c/报告.txt", b"AAA")
mkfile(ROOT, "d/报告.txt", b"BBBB")
mkfile(ROOT, "e/将删除.pdf", b"gone")

# 拦截外部程序唤起（避免测试弹出真实阅读器/资源管理器），只记录调用参数
OPENED = []
REVEALED = []


class _StubServices:
    @staticmethod
    def openUrl(url):
        OPENED.append(url.toLocalFile())
        return True


dd.QDesktopServices = _StubServices
dd.QFileDialog = FakeFileDialog
dd._reveal_in_file_manager = lambda p: (REVEALED.append(p), None)[1]

DSP = Dispatcher(app)
DSP.on("*", ack_message)

from main import MainWindow  # noqa: E402

w = MainWindow()
w.show()
pump(app)
ok("D0-OPEN", open_root(app, w, ROOT), w.path_edit.text())
w.on_scan()
ok("D0-PLAN", wait(app, lambda: w._plan_valid and w.current_plan is not None, 45),
   f"total={w.current_plan.total if w.current_plan else None}")
os.remove(os.path.join(ROOT, "e", "将删除.pdf"))     # 制造“核对失败”条目
base_manifest = os.path.join(OUT, "dupes_manifest_base.json")
H.save_manifest(base_manifest, H.full_manifest(ROOT))
RESULT["artifacts"]["manifest_base"] = base_manifest


def group_of(tree, kind):
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        if it.data(3, Qt.UserRole) == kind:
            return it
    return None


def group_count(tree, kind):
    return sum(1 for i in range(tree.topLevelItemCount())
               if tree.topLevelItem(i).data(3, Qt.UserRole) == kind)


def child_paths(group):
    return [group.child(j).text(1).replace("\\", "/") for j in range(group.childCount())]


def open_dupes():
    w.btn_duplicates.click()
    pump(app, 0.3)
    dlg = getattr(w, "_duplicate_dialog", None)
    ready = wait(app, lambda: dlg is not None and dlg.report is not None, 90)
    return dlg, ready


# ---------------- D1 报告统计与分组 ----------------
dlg, ready = open_dupes()
ok("D1-REPORT-READY", ready, f"dialog={type(dlg).__name__ if dlg else None}")
rep = dlg.report
info = _strip(dlg.info.text())
ok("D1-COUNTS", (rep.total_files, rep.verified, rep.failed) == (5, 4, 1),
   f"total={rep.total_files} verified={rep.verified} failed={rep.failed}")
ok("D1-STATS-TEXT", "内容重复 1 组 / 2 份" in info
   and "同名不同内容 1 组 / 2 份" in info and "核对失败 1" in info, info[:200])
ok("D1-READONLY-NOTE", "不会删除、合并或移动文件" in info, info[-80:])
shot(dlg, "40_duplicates.png")

g_content = group_of(dlg.tree, "content")
g_same = group_of(dlg.tree, "same_name_diff")
g_unver = group_of(dlg.tree, "unverified")
ok("D2-CONTENT-GROUP",
   g_content is not None and child_paths(g_content) == ["a/讲义.pdf", "b/副本-讲义.pdf"]
   and g_content.child(0).text(4) == g_content.child(1).text(4),
   f"paths={child_paths(g_content) if g_content is not None else None}")
ok("D3-SAME-NAME-GROUP",
   g_same is not None and child_paths(g_same) == ["c/报告.txt", "d/报告.txt"]
   and "2 种不同内容" in g_same.text(5)
   and g_same.child(0).text(4) != g_same.child(1).text(4),
   f"paths={child_paths(g_same) if g_same is not None else None} note="
   f"{g_same.text(5) if g_same is not None else None}")
ok("D4-FAILED-GROUP", g_unver is not None and g_unver.childCount() == 1
   and child_paths(g_unver) == ["e/将删除.pdf"]
   and "失败" in g_unver.child(0).text(3),
   f"paths={child_paths(g_unver) if g_unver is not None else None} "
   f"status={g_unver.child(0).text(3) if g_unver is not None else None}")
ok("D4-GROUP-TOTAL", dlg.tree.topLevelItemCount() == rep.groups.__len__(),
   f"tree={dlg.tree.topLevelItemCount()} report={len(rep.groups)}")

# ---------------- D5 复制路径 / 打开 / 定位 ----------------
target = g_content.child(0)
dlg.tree.setCurrentItem(target)
pump(app, 0.1)
wanted = os.path.join(ROOT, "a", "讲义.pdf")
dlg.btn_copy.click()
pump(app, 0.1)
ok("D5-COPY-PATH", QApplication.clipboard().text() == wanted
   and wanted in dlg.progress_label.text(),
   f"clip={QApplication.clipboard().text()}")
dlg.btn_open.click()
pump(app, 0.2)
ok("D5-OPEN-CALLED", OPENED and os.path.normcase(OPENED[-1]) == os.path.normcase(wanted),
   f"opened={OPENED}（外部程序唤起被拦截，仅记录调用参数）")
dlg.btn_reveal.click()
pump(app, 0.2)
ok("D5-REVEAL-CALLED", REVEALED
   and os.path.normcase(REVEALED[-1]) == os.path.normcase(wanted),
   f"revealed={REVEALED}（资源管理器唤起被拦截，仅记录调用参数）")

# ---------------- D6 CSV / JSON 导出 ----------------
csv_path = os.path.join(OUT, "duplicates.csv")
json_path = os.path.join(OUT, "duplicates.json")
FakeFileDialog._save = (csv_path, "")
dlg.btn_csv.click()
pump(app, 0.4)
ok("D6-CSV-FILE", os.path.isfile(csv_path), csv_path)
with open(csv_path, "r", encoding="utf-8", newline="") as f:
    rows = list(csv.reader(f))
data_rows = [r for r in rows[1:] if len(r) == 12]
rel_col = {r[4].replace("\\", "/") for r in data_rows}
ok("D6-CSV-CONTENT",
   rows[0][:5] == ["组类型", "组标识", "组内份数", "文件", "源相对路径"]
   and len(data_rows) == 5
   and rel_col == {"a/讲义.pdf", "b/副本-讲义.pdf", "c/报告.txt", "d/报告.txt",
                   "e/将删除.pdf"}
   and sum(1 for r in data_rows if r[8] == "核对失败") == 1,
   f"head={rows[0][:5]} data_rows={len(data_rows)} rel={sorted(rel_col)}")
FakeFileDialog._save = (json_path, "")
dlg.btn_json.click()
pump(app, 0.4)
ok("D6-JSON-FILE", os.path.isfile(json_path), json_path)
with open(json_path, "r", encoding="utf-8") as f:
    payload = json.load(f)
kinds = [g["kind"] for g in payload["groups"]]
ok("D6-JSON-CONTENT",
   payload["format"] == "archive-box-duplicate-report"
   and payload["counts"]["total_files"] == 5
   and payload["counts"]["content_groups"] == 1
   and payload["counts"]["same_name_groups"] == 1
   and kinds == ["content", "same_name_diff", "unverified"]
   and payload["groups"][2]["entries"][0]["status"] == "failed",
   f"counts={payload['counts']} kinds={kinds}")
RESULT["artifacts"]["duplicates_csv"] = csv_path
RESULT["artifacts"]["duplicates_json"] = json_path

# ---------------- D7 外部修改后重开窗口重新核对 ----------------
with open(os.path.join(ROOT, "a", "讲义.pdf"), "ab") as f:
    f.write(b"-CHANGED")
dlg.close()
pump(app, 0.4)
ok("D7-DIALOG-CLOSED", getattr(w, "_duplicate_dialog", None) is None,
   f"dialog={getattr(w, '_duplicate_dialog', None)}")
dlg2, ready2 = open_dupes()
ok("D7-RECHECK-READY", ready2 and dlg2.report is not None,
   f"ready={ready2}")
rep2 = dlg2.report
checked = getattr(w, "_duplicate_cache_entries", None) or []
a_entry = next((e for e in checked
                if e.rel_path.replace("\\", "/") == "a/讲义.pdf"), None)
b_entry = next((e for e in checked
                if e.rel_path.replace("\\", "/") == "b/副本-讲义.pdf"), None)
ok("D7-CHANGED-FILE-RECHECKED",
   a_entry is not None and a_entry.rechecked
   and "指纹已重新核对" in a_entry.reason
   and a_entry.status == "verified"
   and rep2.content_groups == 0
   and "内容重复 0 组" in _strip(dlg2.info.text()),
   f"rechecked={getattr(a_entry, 'rechecked', None)} "
   f"reason={getattr(a_entry, 'reason', None)} content_groups={rep2.content_groups}")
ok("D7-UNCHANGED-COPY-KEPT",
   b_entry is not None and b_entry.sha256 == H.sha256_file(
       os.path.join(ROOT, "b", "副本-讲义.pdf"))
   and open(os.path.join(ROOT, "b", "副本-讲义.pdf"), "rb").read() == DUP_BYTES,
   f"sha={getattr(b_entry, 'sha256', None)[:12]}")
shot(dlg2, "41_duplicates_after_change.png")
dlg2.close()
pump(app, 0.3)

# ---------------- D8 不删除 / 不合并 / 不自动执行 ----------------
files_now = H.full_manifest(ROOT)
expected = {k: v for k, v in H.load_manifest(base_manifest).items()
            if "a/讲义.pdf" not in k.replace("\\", "/")}
actual = {k: v for k, v in files_now.items() if "a/讲义.pdf" not in k.replace("\\", "/")}
ok("D8-NO-FILE-LOST", actual == expected,
   f"before={len(expected)} after={len(actual)} "
   f"diff={json.dumps(H.diff_manifests(expected, actual), ensure_ascii=False)}")
ok("D8-NO-MOVE-NO-BATCH",
   not os.path.exists(os.path.join(ROOT, "_已整理"))
   and (w._storage is None or not w._storage.list_batches())
   and len(H.full_manifest(ROOT)) == 4,
   f"files={len(files_now)} "
   f"batches={len(w._storage.list_batches()) if w._storage else 0}")

close_app(app, w)
DSP.stop()
finish(app)
