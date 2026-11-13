"""S3-01 行为验证：执行确认框按回车不得触发「确认开始整理」。

在验证包的 app/ 源码上跑，不修改源码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

APP = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtTest import QTest                                 # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

import main as m                                                 # noqa: E402
from scanner import scan_directory                               # noqa: E402
from planner import generate_plan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES  # noqa: E402

app = QApplication.instance() or QApplication([])

work = tempfile.mkdtemp(prefix="s301_enter_")
with open(os.path.join(work, "笔记.txt"), "w", encoding="utf-8") as f:
    f.write("内容")
plan = generate_plan(scan_directory(work, detect_project_roots=False),
                     MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)

results = {}

dlg = m.ExecuteConfirmDialog(work, plan)
dlg.show()
app.processEvents()
results["focus_is_cancel"] = dlg.focusWidget().text() if dlg.focusWidget() else None
box = dlg.findChild(QDialogButtonBox)
ok_btn = box.button(QDialogButtonBox.Ok)
cancel_btn = box.button(QDialogButtonBox.Cancel)
results["ok_autoDefault"] = ok_btn.autoDefault()
results["ok_isDefault"] = ok_btn.isDefault()
results["cancel_autoDefault"] = cancel_btn.autoDefault()
QTest.keyClick(dlg, Qt.Key_Return)
app.processEvents()
results["enter_result"] = int(dlg.result())
results["enter_rejected"] = (dlg.result() == QDialog.Rejected)
dlg.close()

# 对照：点击「确认开始整理」必须仍然可以接受
dlg2 = m.ExecuteConfirmDialog(work, plan)
dlg2.show()
app.processEvents()
dlg2.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
app.processEvents()
results["ok_click_accepts"] = (dlg2.result() == QDialog.Accepted)
dlg2.close()

print(json.dumps(results, ensure_ascii=False, indent=2))
ok = (results["enter_rejected"] and results["ok_click_accepts"]
      and not results["ok_autoDefault"] and not results["ok_isDefault"])
print("RESULT:", "PASS" if ok else "FAIL")
sys.stdout.flush()
os._exit(0 if ok else 1)
