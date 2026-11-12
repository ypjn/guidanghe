"""PART2：全新进程从历史界面找到 PART1 的同一批次并撤销。

硬约束：
- 必须找到 PART1 写下的同一 batch_id；找不到或已撤销 => FAIL，禁止重新扫描补造。
- 撤销通过历史窗口的真实控件（批次行点击 + 「撤销该批次」按钮 + 确认框 Yes）。
- 核对每条动作与完整文件清单回到 PART1 之前的哈希。
- 核对器反例用独立副本验证能检出：归档目录多出文件、隐藏文件变化、文件缺失。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

os.environ.pop("QT_QPA_PLATFORM", None)
sys.dont_write_bytecode = True

from windows_harness import (AutoOk, Results, arm_modal, diff_manifests,
                             full_manifest, load_manifest, norm_rel, pump,
                             save_manifest, shot, wait_until)  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_desktop"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

for key, sub in (("ARCHIVEBOX_RECENT_FILE", "recent.json"),
                 ("ARCHIVEBOX_APP_DIR", "appdata"),
                 ("ARCHIVEBOX_SCHEME_DIR", os.path.join("appdata", "schemes"))):
    os.environ.setdefault(key, os.path.join(BASE, sub))

R = Results("desktop_part2")
STATE_PATH = os.path.join(BASE, "desktop_state.json")
print(f"=== PART2  (state={STATE_PATH}) ===")

if not os.path.isfile(STATE_PATH):
    R.rec("P0", "读取 PART1 交接状态", "FAIL", "缺少 desktop_state.json，PART1 未成功")
    sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                        required_ids=["P0"] + [f"Q{i}" for i in range(1, 12)]))

with open(STATE_PATH, "r", encoding="utf-8") as f:
    st = json.load(f)
RUN_ID = st["run_id"]
ROOT = st["root"]
BATCH_ID = st["batch_id"]
EXPECTED = st["expected_targets"]
EVID = st.get("evidence_dir") or os.path.join(BASE, "evidence", "desktop")
BEFORE = load_manifest(st["manifest_before"])
print(f"run_id={RUN_ID} root={ROOT} batch_id={BATCH_ID} expected={len(EXPECTED)}")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication([])
auto = AutoOk(app)
from main import MainWindow  # noqa: E402
from executor import undo_is_available  # noqa: E402

R.check("Q1", "PART1 交接的批次号与根目录有效",
        isinstance(BATCH_ID, int) and os.path.isdir(ROOT),
        f"batch_id={BATCH_ID} root_exists={os.path.isdir(ROOT)}")

w = MainWindow()
w.show()
wait_until(app, lambda: w.isVisible(), 20, "main visible")
print(f"[trace] after MainWindow: storage={w._storage is not None}")

import recent_spaces  # noqa: E402

recent_spaces.touch(ROOT)

# 产品行为：打开根目录时若存在未完整撤销的批次，会弹「是否现在尝试撤销？」。
# 这里真实应答该对话框（选择“否”），走历史界面的撤销入口，而不是在这里就地撤销。
pending_cap = {}


def on_pending_question(dlg):
    from windows_harness import dialog_text
    pending_cap["text"] = dialog_text(dlg)
    shot(dlg, os.path.join(EVID, f"03b_pending_prompt_{RUN_ID}.png"))
    return dlg.button(QMessageBox.No)


state_pending = arm_modal(on_pending_question)
opened_root = w._open_recent_space(ROOT)
pump(app, 0.3)
print(f"[trace] pending_prompt={pending_cap.get('text', '未出现')[:120]!r} "
      f"handled={state_pending['handled']}")
_b0 = w._storage.get_batch(BATCH_ID) if w._storage else None
print(f"[trace] after _open_recent_space: batch_status="
      f"{getattr(_b0, 'status', None)}")
R.check("Q2", "新进程打开同一根目录并载入持久化历史库",
        opened_root and norm_rel(w.path_edit.text()) == norm_rel(ROOT)
        and w._storage is not None,
        f"storage={getattr(getattr(w, '_storage', None), 'root', None)}")

R.check("Q2b", "启动提示确实指向同一批次（真实对话框被应答为“否”）",
        state_pending["handled"] and state_pending["error"] is None
        and str(BATCH_ID) in pending_cap.get("text", ""),
        f"text={pending_cap.get('text', '')[:200]} error={state_pending['error']}")

db_batch = w._storage.get_batch(BATCH_ID) if w._storage else None
R.check("Q3", "数据库中该批次存在且处于可撤销状态",
        db_batch is not None and str(db_batch.status) in ("done", "partial", "cancelled")
        and undo_is_available(w._storage),
        f"status={getattr(db_batch, 'status', None)}")

# ---------------- 历史窗口：真实点击批次行 ----------------
QTest.mouseClick(w.btn_history, Qt.LeftButton)
opened = wait_until(app, lambda: w._history_dialog is not None
                    and w._history_dialog.isVisible(), 20, "history visible")
hist = w._history_dialog
R.check("Q4", "真实点击「操作历史」打开历史窗口", opened and hist is not None,
        f"visible={getattr(hist, 'isVisible', lambda: None)()}")

row = -1
if hist is not None:
    for r in range(hist.batch_table.rowCount()):
        it = hist.batch_table.item(r, 0)
        if it is not None and str(BATCH_ID) in it.text():
            row = r
            break
R.check("Q5", "历史列表中能找到 PART1 的同一批次", row >= 0,
        f"rows={0 if hist is None else hist.batch_table.rowCount()} batch={BATCH_ID} row={row}")

if row >= 0:
    item = hist.batch_table.item(row, 0)
    rect = hist.batch_table.visualItemRect(item)
    QTest.mouseClick(hist.batch_table.viewport(), Qt.LeftButton,
                     pos=rect.center())
    pump(app, 0.3)
n_items = hist.item_table.rowCount() if hist is not None else -1
R.check("Q6", "选中批次后明细显示逐项动作且数量一致",
        n_items == len(EXPECTED), f"item_rows={n_items} expected={len(EXPECTED)}")
if hist is not None:
    shot(hist, os.path.join(EVID, f"04_history_before_undo_{RUN_ID}.png"))

# ---------------- 真实点击撤销 + 确认 Yes ----------------
R.check("Q7", "历史窗口撤销按钮可用", hist is not None and hist.btn_undo.isEnabled(),
        f"enabled={None if hist is None else hist.btn_undo.isEnabled()}")


def on_question(dlg):
    shot(dlg, os.path.join(EVID, f"04b_undo_confirm_{RUN_ID}.png"))
    return dlg.button(QMessageBox.Yes)


state = arm_modal(on_question)
QTest.mouseClick(hist.btn_undo, Qt.LeftButton)
pump(app, 0.2)
R.check("Q8", "撤销确认对话框由真实点击 Yes 应答",
        state["handled"] and state["error"] is None,
        f"error={state['error']} captured={str(state['captured'])[:120]}")

done = wait_until(app, lambda: not w._working and not w._plan_working, 120,
                  "undo finished")
pump(app, 0.5)
R.check("Q9", "撤销任务完成", done, f"working={w._working}")

# ---------------- 磁盘 + 数据库核对 ----------------
after = full_manifest(ROOT)
save_manifest(os.path.join(EVID, f"05_after_undo_manifest_{RUN_ID}.json"), after,
              {"run_id": RUN_ID, "root": ROOT})
d = diff_manifests(BEFORE, after)
R.check("Q10", "撤销后逐文件清单与整理前完全一致（路径/大小/SHA-256）",
        not (d["missing"] or d["extra"] or d["changed"]),
        f"manifest_diff={d}")

b2 = w._storage.get_batch(BATCH_ID)
acts = w._storage.get_actions(BATCH_ID)
not_undone = [a.action_index for a in acts if a.status != "undone"]
R.check("Q11", "批次与每条动作在数据库中均为已撤销",
        str(getattr(b2, "status", "")) == "undone" and not not_undone,
        f"batch_status={getattr(b2, 'status', None)} not_undone={not_undone} "
        f"total={len(acts)}")
shot(hist, os.path.join(EVID, f"06_after_undo_{RUN_ID}.png"))

R.check("Q12", "撤销后不再有可撤销批次（防重复撤销）",
        not undo_is_available(w._storage), f"available={undo_is_available(w._storage)}")

# ---------------- 核对器反例（独立副本，不污染正式数据） ----------------
PROBE = os.path.join(BASE, "counterprobe")
if os.path.exists(PROBE):
    shutil.rmtree(PROBE)
os.makedirs(PROBE)

copy1 = os.path.join(PROBE, "case_extra")
shutil.copytree(ROOT, copy1)
open(os.path.join(copy1, "多余文件.txt"), "w", encoding="utf-8").write("extra")
m1 = full_manifest(copy1)
d1 = diff_manifests(after, m1)
R.check("R1", "反例：归档/根目录多出文件能被检出", d1["extra"] == ["多余文件.txt"], f"{d1}")

copy2 = os.path.join(PROBE, "case_changed")
shutil.copytree(ROOT, copy2)
with open(os.path.join(copy2, ".hidden_note.txt"), "a", encoding="utf-8") as f:
    f.write("tampered")
m2 = full_manifest(copy2)
d2 = diff_manifests(after, m2)
R.check("R2", "反例：隐藏文件内容变化能被检出",
        d2["changed"] == [".hidden_note.txt"], f"{d2}")

copy3 = os.path.join(PROBE, "case_missing")
shutil.copytree(ROOT, copy3)
os.remove(os.path.join(copy3, "大二上", "数据结构", "讲义.pdf"))
m3 = full_manifest(copy3)
d3 = diff_manifests(after, m3)
R.check("R3", "反例：文件缺失能被检出",
        d3["missing"] == ["大二上/数据结构/讲义.pdf"], f"{d3}")

R.check("R4", "反例未改动正式测试根目录",
        full_manifest(ROOT) == after,
        f"root_manifest_unchanged={full_manifest(ROOT) == after}")

# ---------------- 正常关闭 ----------------
auto.stop()
w.close()
closed = wait_until(app, lambda: not w.isVisible(), 30, "closed")
R.check("Q13", "新进程正常关闭退出", closed and getattr(w, "_closing", False),
        f"visible={w.isVisible()} closing={getattr(w, '_closing', None)}")

sys.exit(R.finalize(os.path.join(OUT, "results.json"),
                    required_ids=[f"Q{i}" for i in range(1, 14)]
                    + ["Q2b", "R1", "R2", "R3", "R4"]))
