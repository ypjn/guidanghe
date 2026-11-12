"""Stage7 子进程公共驱动：真实 Qt 控件交互 + 模态对话框调度。

每个子脚本在自己的进程内启动原生 Qt（windows 平台插件），通过真实控件
（按钮 click、表格选择、输入框 setText）完成操作；模态对话框由 Dispatcher
在模态事件循环内按窗口标题分派处理，不调用被测对象的私有业务方法绕过界面。

环境变量（由父套件注入）：
  ARCHIVEBOX_SRC      源码根
  ARCHIVEBOX_TEST_BASE 本轮运行根（隔离状态）
  ARCHIVEBOX_RESULTS_DIR 本子脚本日志/截图/清单输出目录
  S7_OUT              结果 JSON 路径
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

SRC = os.environ["ARCHIVEBOX_SRC"]
TESTS = os.path.join(SRC, "tests")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

from PySide6.QtCore import QTimer, QPoint  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialogButtonBox, QFormLayout, QInputDialog, QMessageBox,
    QLineEdit, QComboBox, QLabel,
)

BASE = os.environ["ARCHIVEBOX_TEST_BASE"]
OUT = os.environ["ARCHIVEBOX_RESULTS_DIR"]
os.makedirs(OUT, exist_ok=True)
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)

RESULT = {"checks": [], "artifacts": {}, "dialogs": [], "notes": [], "errors": []}


def ok(cid: str, cond, detail="") -> bool:
    RESULT["checks"].append({"id": cid, "ok": bool(cond), "detail": str(detail)})
    print(("PASS " if cond else "FAIL ") + cid + (" | " + str(detail) if detail else ""),
          flush=True)
    return bool(cond)


def note(text: str) -> None:
    RESULT["notes"].append(str(text))
    print("NOTE " + str(text))


def pump(app, sec=0.25):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(app, cond, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(cond())


def shot(widget, name: str) -> str:
    path = os.path.join(SHOTS, name)
    try:
        widget.grab().save(path)
    except Exception as e:  # 截图失败不改变功能判定
        RESULT["errors"].append(f"screenshot {name}: {e!r}")
        return ""
    print(f"shot -> {path}")
    return path


class Dispatcher:
    """在模态事件循环里按窗口标题分派；每个处理器可被重复调用（步骤机）。

    "*" 为兜底处理器（标题不匹配任何显式登记时使用）。
    未处理超过 25 秒的模态窗口会被记录并强制取消，避免测试挂死。
    """

    def __init__(self, app: QApplication):
        self.app = app
        self.handlers = {}
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)
        self.errors = []
        self._seen = {}

    def on(self, title_contains: str, fn):
        self.handlers[title_contains] = fn
        return self

    def _tick(self):
        w = self.app.activeModalWidget()
        if w is None:
            self._seen.clear()
            return
        try:
            title = w.windowTitle()
        except RuntimeError:
            return
        fn = None
        for key, h in list(self.handlers.items()):
            if key and key != "*" and key in title:
                fn = h
                break
        if fn is None:
            fn = self.handlers.get("*")
        if fn is None:
            return
        key = id(w)
        self._seen.setdefault(key, time.time())
        if time.time() - self._seen[key] > 25:
            RESULT["errors"].append(f"模态窗口未处理超时：{title!r}")
            self._seen[key] = time.time()
            try:
                w.reject()
            except Exception:
                w.close()
            return
        try:
            fn(w)
        except RuntimeError:
            return
        except Exception as e:
            self.errors.append(f"{title}: {e!r}")
            RESULT["errors"].append(f"dispatcher {title!r}: {e!r}")
            traceback.print_exc()
            try:
                w.reject()
            except Exception:
                pass

    def stop(self):
        self._timer.stop()


def ack_message(w):
    """通用消息框：记录文本并按 Yes / OK 关闭。"""
    if isinstance(w, QMessageBox):
        RESULT["dialogs"].append({"title": w.windowTitle(), "text": w.text()})
        _click_standard(w)
        return
    bb = w.findChild(QDialogButtonBox)
    if bb is not None:
        b = bb.button(QDialogButtonBox.Yes) or bb.button(QDialogButtonBox.Ok)
        if b is not None:
            defer_click(b)
            return
    w.accept()


def ack_input(w, value="s7"):
    if isinstance(w, QInputDialog):
        w.setTextValue(value)
        w.accept()
    else:
        ack_message(w)


def form_field(dlg, label_part: str):
    """按 QFormLayout 标签文本取字段控件（真实控件，不依赖内部属性名）。"""
    form = dlg.findChild(QFormLayout)
    if form is None:
        return None
    for i in range(form.rowCount()):
        lab = form.itemAt(i, QFormLayout.LabelRole)
        fld = form.itemAt(i, QFormLayout.FieldRole)
        if lab is None or fld is None or lab.widget() is None or fld.widget() is None:
            continue
        if label_part in lab.widget().text():
            return fld.widget()
    return None


def all_texts(dlg):
    out = []
    for lab in dlg.findChildren(QLabel):
        try:
            out.append(lab.text())
        except RuntimeError:
            pass
    return out


def button_of(dlg, std):
    bb = dlg.findChild(QDialogButtonBox)
    if bb is None:
        return None
    return bb.button(std)


def btn_by_text(widget, text: str):
    from PySide6.QtWidgets import QPushButton
    for b in widget.findChildren(QPushButton):
        try:
            if text in b.text():
                return b
        except RuntimeError:
            continue
    return None


def _safe_click(btn) -> None:
    try:
        btn.click()
    except RuntimeError:
        pass    # 对话框已被 Qt 关闭销毁，延后点击自然失效


def defer_click(btn) -> None:
    """在定时器回调里点击“会再开一个模态对话框”的按钮必须延后到事件循环，
    否则内层 exec() 会阻塞当前定时器回调，调度器无法继续处理内层窗口。"""
    QTimer.singleShot(0, lambda: _safe_click(btn))


def _click_standard(w):
    """延后到事件循环再关闭消息框：按钮包装对象必须在使用的那一刻重新获取
    （提前拿到的 QPushButton 包装会在回调返回后被回收，延后点击时报已删除）。"""
    def run():
        try:
            from PySide6.QtWidgets import QPushButton
            btns = [b for b in w.findChildren(QPushButton) if b.isVisible()]
            pick = None
            for b in btns:
                if b.text().replace("&", "") in ("OK", "确定", "是", "Yes"):
                    pick = b
                    break
            if pick is None and btns:
                pick = btns[0]
            if pick is not None:
                pick.click()
            else:
                w.done(QMessageBox.Ok)
        except RuntimeError:
            pass
    QTimer.singleShot(0, run)


class Universal:
    """按控件结构与类型分派模态窗口（不依赖窗口标题），供多对话框流程使用。

    plan 支持：
      inputs: [str, ...]        依次填入 QInputDialog 的文本
      choice: "replace"|"merge" ConfigChoiceDialog 单选
    records: 记录每个消息框 / 差异预览 / 冲突表，供断言。
    """

    def __init__(self, app: QApplication):
        self.app = app
        self.plan = {"inputs": [], "choice": "replace"}
        self.records = {"messages": [], "choice": [], "preview": [], "unknown": []}
        self._acted = set()
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def _tick(self):
        w = self.app.activeModalWidget()
        if w is None:
            self._acted.clear()
            return
        if os.environ.get("S7_DEBUG"):
            try:
                print(f"tick modal={type(w).__name__} title={w.windowTitle()!r}",
                      flush=True)
            except Exception:
                pass
        try:
            if isinstance(w, QMessageBox):
                self.records["messages"].append(
                    {"title": w.windowTitle(), "text": w.text()})
                _click_standard(w)
                return
            if isinstance(w, QInputDialog):
                if w.property("s7_acted"):
                    return
                w.setProperty("s7_acted", True)
                vals = self.plan.get("inputs") or []
                val = vals.pop(0) if vals else "s7"
                self.records.setdefault("inputs", []).append(val)
                w.setTextValue(val)
                QTimer.singleShot(0, w.accept)
                return
            if hasattr(w, "rb_replace") and hasattr(w, "rb_merge"):
                if w.property("s7_acted"):
                    return
                mode = self.plan.get("choice") or "replace"
                (w.rb_merge if mode == "merge" else w.rb_replace).setChecked(True)
                self.records["choice"].append(
                    {"mode": mode, "diff": w.diff_label.text(),
                     "conflicts": w.conflict_table.rowCount(),
                     "ok": w.bb.button(QDialogButtonBox.Ok).isEnabled()})
                w.setProperty("s7_acted", True)
                defer_click(w.bb.button(QDialogButtonBox.Ok))
                return
            if hasattr(w, "impact_view") and hasattr(w, "bb"):
                ok_btn = w.bb.button(QDialogButtonBox.Ok)
                if ok_btn is not None and ok_btn.isEnabled():
                    if w.property("s7_acted"):
                        return
                    w.setProperty("s7_acted", True)
                    self.records["preview"].append(
                        {"title": w.windowTitle(), "status": w.status.text(),
                         "impact": w.impact_view.toPlainText()})
                    defer_click(ok_btn)
                return
            if w.property("s7_seen_at") is None:
                w.setProperty("s7_seen_at", time.time())
            elif time.time() - w.property("s7_seen_at") > 20:
                self.records["unknown"].append(
                    {"title": w.windowTitle(), "texts": all_texts(w)[:12],
                     "timeout": True})
                self.app.processEvents()
                w.reject()
        except RuntimeError:
            return

    def stop(self):
        self._timer.stop()


def finish(app, code=None):
    RESULT["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    bad = [c for c in RESULT["checks"] if not c["ok"]]
    if code is None:
        code = 0 if not bad and not RESULT["errors"] else 1
    out = os.environ.get("S7_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(RESULT, f, ensure_ascii=False, indent=2)
    print("S7_RESULT=" + json.dumps(RESULT, ensure_ascii=False))
    print(f"CHILD_EXIT_CODE={code}")
    app.processEvents()
    sys.exit(code)


def make_app():
    app = QApplication.instance() or QApplication(sys.argv)
    print(f"platform = {app.platformName()}  screens = {len(app.screens())}")
    if app.platformName() != "windows":
        RESULT["errors"].append(f"不是 Windows 原生平台：{app.platformName()}")
    return app


def mkfile(root, rel, data):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def row_by_source(window, rel: str) -> int:
    t = window.table
    want = rel.replace("\\", "/")
    for r in range(t.rowCount()):
        it = t.item(r, 1)
        if it is not None and it.text().replace("✎ ", "").replace("\\", "/") == want:
            return r
    return -1


def visible_rows(window):
    t = window.table
    return [r for r in range(t.rowCount()) if not t.isRowHidden(r)]


class FakeFileDialog:
    """替换被测模块名空间里的 QFileDialog，返回预设路径（真实按钮流程不变）。"""

    _open = ""
    _open_many = []
    _save = ("", "")
    calls = []
    ShowDirsOnly = 0
    DontResolveSymlinks = 0

    @classmethod
    def getExistingDirectory(cls, *a, **k):
        cls.calls.append("getExistingDirectory")
        return cls._open

    @classmethod
    def getOpenFileName(cls, *a, **k):
        cls.calls.append("getOpenFileName")
        return cls._open, "归档盒方案 (*.json)"

    @classmethod
    def getOpenFileNames(cls, *a, **k):
        cls.calls.append("getOpenFileNames")
        return list(cls._open_many), "所有文件 (*)"

    @classmethod
    def getSaveFileName(cls, *a, **k):
        cls.calls.append("getSaveFileName")
        return cls._save


def open_root(app, window, root):
    """通过「浏览…」按钮 + 真实 on_browse 流程打开根目录。"""
    import main as mainmod
    FakeFileDialog._open = root
    mainmod.QFileDialog = FakeFileDialog
    window.btn_browse.click()
    pump(app, 0.4)
    return window.path_edit.text().strip() == root


def close_app(app, window=None):
    if window is not None:
        try:
            window.close()
        except Exception:
            pass
    pump(app, 0.3)
