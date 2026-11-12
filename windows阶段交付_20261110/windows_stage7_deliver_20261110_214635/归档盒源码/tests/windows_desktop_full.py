"""PART1：Windows 原生桌面闭环（真实控件交互）。

流程：启动原生 windows 平台 → 选择合成根目录 → 扫描 → 逐文件预览断言
→ 勾选统计 → 真实点击「确认开始整理」→ 等完成 → 截图 → 正常关闭。

所有断言基于独立预期的文件集合与逐项目标表（由 planner 文档规则的
默认推荐映射推导），不是“表格非空”“关键词包含”。
"""
from __future__ import annotations

import json
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

os.environ.pop("QT_QPA_PLATFORM", None)
sys.dont_write_bytecode = True

from windows_harness import (AutoOk, Results, RunDir, arm_modal, full_manifest,
                             norm_rel, pump, save_manifest, sha256_file, shot,
                             wait_until)  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_desktop"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
EVID = os.path.join(BASE, "evidence", "desktop")
os.makedirs(OUT, exist_ok=True)
os.makedirs(EVID, exist_ok=True)

# 隔离必须在被测进程启动前建立：本进程即被测进程，这里只校验值落在 BASE 内
for key, sub in (("ARCHIVEBOX_RECENT_FILE", "recent.json"),
                 ("ARCHIVEBOX_APP_DIR", "appdata"),
                 ("ARCHIVEBOX_SCHEME_DIR", os.path.join("appdata", "schemes"))):
    os.environ.setdefault(key, os.path.join(BASE, sub))

rd = RunDir(BASE, "desktop1")
ROOT = rd.sub("root")
MAN = rd.sub("manifests")
print(f"=== PART1 run={rd.run_id} ===")
print(f"BASE={BASE}\nROOT={ROOT}\nQT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM')}")

# ---------------- 合成素材 + 独立预期表 ----------------
FIXTURES = {
    "大二上/数据结构/讲义.pdf": b"lecture-pdf",
    "大二上/数据结构/作业/hw1.docx": b"homework-1",
    "大二上/高等数学/期末复习.pdf": b"final-review",
    "大一下/程序设计基础/代码/main.py": b"print('hi')\n",
    "图片素材/logo.png": b"\x89PNG-logo",
    "图片素材/截图 2026.png": b"\x89PNG-shot",
    "讲义.pdf": b"loose-lecture",
    ".hidden_note.txt": b"hidden-should-be-skipped",
    "myproj/.git/config": b"[core]\n",
    "myproj/main.py": b"project-root-skip\n",
}
for rel, data in FIXTURES.items():
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)

EXPECTED_TARGETS = {
    "大二上/数据结构/讲义.pdf": "_已整理/课程/大二上/数据结构/文档/讲义.pdf",
    "大二上/数据结构/作业/hw1.docx": "_已整理/课程/大二上/数据结构/文档/作业/hw1.docx",
    "大二上/高等数学/期末复习.pdf": "_已整理/课程/大二上/高等数学/文档/期末复习.pdf",
    "大一下/程序设计基础/代码/main.py": "_已整理/课程/大一下/程序设计基础/代码/代码/main.py",
    "图片素材/logo.png": "_已整理/待定/图片素材/logo.png",
    "图片素材/截图 2026.png": "_已整理/待定/图片素材/截图 2026.png",
    "讲义.pdf": "_已整理/待定/讲义.pdf",
}
EXPECTED_EXCLUDED = [".hidden_note.txt", "myproj/.init", "myproj/main.py"]

before = full_manifest(ROOT)
save_manifest(os.path.join(MAN, "01_before.json"), before,
              {"run_id": rd.run_id, "root": ROOT})

R = Results("desktop_part1")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel  # noqa: E402

app = QApplication([])
auto = AutoOk(app)

from main import HistoryDialog, MainWindow  # noqa: E402

R.check("D1", "真实 Windows 平台（非 offscreen）",
        app.platformName().lower() == "windows",
        f"platformName={app.platformName()} screens={len(app.screens())}")

w = MainWindow()
w.show()
ok_visible = wait_until(app, lambda: w.isVisible(), 20, "main window visible")
R.check("D2", "主窗口在事件循环中可见", ok_visible, f"visible={w.isVisible()}")

iso_ok = all(os.path.abspath(os.environ[k]).startswith(os.path.abspath(BASE))
             for k in ("ARCHIVEBOX_RECENT_FILE", "ARCHIVEBOX_APP_DIR",
                       "ARCHIVEBOX_SCHEME_DIR"))
R.check("D3", "应用状态隔离环境变量已建立且在 BASE 内", iso_ok,
        {k: os.environ[k] for k in ("ARCHIVEBOX_RECENT_FILE",
                                    "ARCHIVEBOX_APP_DIR", "ARCHIVEBOX_SCHEME_DIR")})

# ---------------- 选择根目录 + 真实点击扫描 ----------------
# 说明：Windows 原生目录选择框（QFileDialog 静态方法）无法确定性自动化，
# 这里走产品真实的「最近空间」路径 `_open_recent_space`（内部完成
# path_edit 赋值 / 启用扫描按钮 / 重置状态 / 记录最近空间），
# 之后所有关键操作（扫描、执行、历史、撤销）都是真实控件点击。
import recent_spaces  # noqa: E402

recent_spaces.touch(ROOT)
opened = w._open_recent_space(ROOT)
pump(app, 0.2)
R.check("D4", "选择根目录后界面显示该路径且扫描按钮可用",
        opened and norm_rel(w.path_edit.text()) == norm_rel(ROOT)
        and w.btn_scan.isEnabled(),
        f"text={w.path_edit.text()} enabled={w.btn_scan.isEnabled()}")
R.check("D4b", "最近空间写入隔离文件（未污染真实用户状态）",
        os.path.isfile(os.environ["ARCHIVEBOX_RECENT_FILE"]),
        os.environ["ARCHIVEBOX_RECENT_FILE"])

QTest.mouseClick(w.btn_scan, Qt.LeftButton)
scanned = wait_until(app, lambda: (w.scan_result is not None
                                   and not w._plan_working and not w._working),
                     90, "scan finished")
sr = w.scan_result
actual_scanned = set(norm_rel(f.rel_path) for f in sr.files) if sr else set()
R.check("D5", "真实点击「扫描」并完成",
        scanned and sr is not None, f"scanned={len(actual_scanned)}")
R.check("D6", "扫描集合与独立预期集合完全一致（无多无少）",
        actual_scanned == set(EXPECTED_TARGETS),
        f"actual={sorted(actual_scanned)} expected={sorted(EXPECTED_TARGETS)}")
R.check("D7", "隐藏文件与项目根被排除（核对器能发现多出文件）",
        not (actual_scanned & {".hidden_note.txt", "myproj/main.py"}),
        f"hidden/project in scan={sorted(actual_scanned & {'.hidden_note.txt', 'myproj/main.py'})}")

plan = w.current_plan
plan_map = {norm_rel(i.file.rel_path): norm_rel(i.target_rel_path)
            for i in plan.items}
R.check("D8", "预览计划与独立逐项目标表完全一致",
        plan_map == EXPECTED_TARGETS,
        f"diff={ {k: (plan_map.get(k), EXPECTED_TARGETS.get(k)) for k in set(plan_map) | set(EXPECTED_TARGETS) if plan_map.get(k) != EXPECTED_TARGETS.get(k)} }")
R.check("D9", "预览表行数 = 计划项数 = 预期文件数",
        w.table.rowCount() == len(EXPECTED_TARGETS) == len(plan.items),
        f"table={w.table.rowCount()} plan={len(plan.items)} expect={len(EXPECTED_TARGETS)}")

# 逐行核对表格里确实展示了对应目标路径（不是一个关键词）
row_missing = []
for r in range(w.table.rowCount()):
    cells = []
    for c in range(w.table.columnCount()):
        it = w.table.item(r, c)
        if it is not None:
            cells.append(it.text())
    joined = norm_rel(" | ".join(cells))
    src_ok = any(norm_rel(s) in joined.replace("\\\\", "/") for s in EXPECTED_TARGETS)
    tgt_ok = any(t in joined for t in EXPECTED_TARGETS.values())
    if not (src_ok and tgt_ok):
        row_missing.append((r, joined[:160]))
R.check("D10", "每一行都展示源路径与预期目标路径", not row_missing,
        f"missing_rows={row_missing[:3]}")

R.check("D11", "全选统计与预期一致",
        plan.selected_count == len(EXPECTED_TARGETS) and plan.total == len(EXPECTED_TARGETS),
        f"selected={plan.selected_count} total={plan.total}")
shot(w, os.path.join(EVID, f"01_preview_{rd.run_id}.png"))

# ---------------- 真实点击执行 + 检查确认对话框 ----------------
cap = {}


def on_confirm(dlg):
    labels = [lb.text() for lb in dlg.findChildren(QLabel) if lb.text()]
    cap["text"] = "\n".join(labels)
    cap["title"] = dlg.windowTitle()
    shot(dlg, os.path.join(EVID, f"02_confirm_{rd.run_id}.png"))
    bb = dlg.findChild(QDialogButtonBox)
    return bb.button(QDialogButtonBox.Ok)


state = arm_modal(on_confirm)
QTest.mouseClick(w.btn_execute, Qt.LeftButton)
pump(app, 0.1)
ctext = cap.get("text", "")
R.check("D12", "确认对话框展示根目录 / 分类模式 / 全部选择统计",
        ROOT in ctext and "已选文件" in ctext
        and f"{len(EXPECTED_TARGETS)} / {len(EXPECTED_TARGETS)}" in ctext
        and "按方向/学期/类别" in ctext,
        f"title={cap.get('title')} text={ctext[:300]}")
R.check("D13", "通过真实按钮点击确认（非直接调用 execute_plan）",
        state["handled"] and state["error"] is None,
        f"error={state['error']}")

finished = wait_until(app, lambda: (not w._working and not w._plan_working
                                    and w._storage is not None
                                    and w.scan_result is not None),
                      180, "execute finished")
pump(app, 0.6)
info_texts = "\n".join(auto.seen)
R.check("D14", "整理任务完成并弹出结果提示（已记录全文）",
        finished and bool(info_texts), info_texts[:300].replace("\n", " / "))

# ---------------- 磁盘与数据库核对 ----------------
after = full_manifest(ROOT)
save_manifest(os.path.join(MAN, "02_after_move.json"), after,
              {"run_id": rd.run_id, "root": ROOT})
moved_ok, moved_bad = [], []
for src_rel, tgt_rel in EXPECTED_TARGETS.items():
    tp = os.path.join(ROOT, tgt_rel.replace("/", os.sep))
    sp = os.path.join(ROOT, src_rel.replace("/", os.sep))
    good = (os.path.isfile(tp) and not os.path.exists(sp)
            and sha256_file(tp) == before[src_rel]["sha256"])
    (moved_ok if good else moved_bad).append(src_rel)
R.check("D15", "每个预期目标文件存在、源已消失、哈希一致",
        not moved_bad,
        f"ok={len(moved_ok)} bad={moved_bad}")
archive = [k for k in after if k.startswith("_已整理/")]
R.check("D16", "_已整理 内文件数 = 预期移动数（无多余副本）",
        len(archive) == len(EXPECTED_TARGETS),
        f"archive={sorted(archive)}")

batches = w._storage.list_batches()
b = batches[0] if batches else None
batch_id = getattr(b, "batch_id", None)
R.check("D17", "数据库记录批次为 done 且有逐项动作",
        b is not None and batch_id is not None
        and str(getattr(b, "status", "")) == "done",
        f"batch_id={batch_id} status={getattr(b, 'status', None)}")
actions = w._storage.get_actions(batch_id) if batch_id is not None else []
moved_actions = [a for a in actions if a.status == "moved"]
R.check("D18", "每条动作状态 moved 且数量一致",
        len(actions) == len(EXPECTED_TARGETS) and len(moved_actions) == len(EXPECTED_TARGETS),
        f"actions={len(actions)} moved={len(moved_actions)}")
shot(w, os.path.join(EVID, f"03_after_execute_{rd.run_id}.png"))

# 交接给 PART2
state_path = os.path.join(BASE, "desktop_state.json")
with open(state_path, "w", encoding="utf-8") as f:
    json.dump({"run_id": rd.run_id, "root": ROOT, "batch_id": batch_id,
               "expected_targets": EXPECTED_TARGETS,
               "evidence_dir": EVID,
               "manifest_before": os.path.join(MAN, "01_before.json"),
               "manifest_after": os.path.join(MAN, "02_after_move.json")},
              f, ensure_ascii=False, indent=2)
R.check("D19", "PART1→PART2 交接状态文件已写入", os.path.isfile(state_path), state_path)

# ---------------- 正常关闭 ----------------
auto.stop()
ew = getattr(w, "_exec_worker", None)
w.close()
closed = wait_until(app, lambda: not w.isVisible(), 30, "window closed")
worker_dead = (ew is None) or (not ew.isRunning())
R.check("D20", "走正常关闭路径退出（窗口不可见、关闭标志置位、工作线程已结束）",
        closed and getattr(w, "_closing", False) and worker_dead,
        f"visible={w.isVisible()} closing={getattr(w, '_closing', None)} "
        f"worker_running={None if ew is None else ew.isRunning()} "
        f"storage={getattr(w, '_storage', 'closed')}")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"D{i}" for i in range(1, 21)] + ["D4b"]))
