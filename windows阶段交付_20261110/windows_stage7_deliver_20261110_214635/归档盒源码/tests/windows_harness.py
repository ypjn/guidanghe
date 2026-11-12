"""Windows 第二轮测试公共框架。

设计要点（对应顾问指出的假成功问题）：
- Results.finalize() 由实际条目汇总退出码；任何 FAIL/BLOCK/缺失/异常 => 非零。
- 不手填 items/pass/exit_code：汇总 JSON 直接来自内存结果结构。
- 唯一运行目录 + 所有权标记；删除前校验绝对路径归属本轮。
- 子进程统一用 sys.executable，保存 cmd/cwd/env 白名单/stdout/stderr/退出码/超时。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

VALID_STATUS = {"PASS", "FAIL", "BLOCK", "UNTESTED"}

# 本进程启动过的所有子进程记录（label, rec）。
# 父套件用 Results.check_child_exits() 把它们全部纳入必测判定：
# 子进程即便输出了完整 JSON，只要最终退出码非 0（退出期崩溃）也必须判失败。
CHILD_LOG: list[tuple[str, dict]] = []


# ---------------------------------------------------------------- 运行目录
class RunDir:
    """本轮唯一目录，带所有权标记，禁止误删固定 BASE。"""

    def __init__(self, base: str, tag: str):
        self.base = os.path.abspath(base)
        self.run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.dir = os.path.join(self.base, "runs", f"{tag}_{self.run_id}")
        os.makedirs(self.dir, exist_ok=True)
        self.marker = os.path.join(self.dir, ".abx_owned_" + self.run_id)
        with open(self.marker, "w", encoding="utf-8") as f:
            f.write(self.run_id)

    def sub(self, *parts) -> str:
        p = os.path.join(self.dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p


def safe_rmtree(path: str, base: str) -> None:
    """只允许删除 base 下、且带本轮所有权标记的目录。"""
    ap = os.path.abspath(path)
    b = os.path.abspath(base)
    if os.path.commonpath([ap, b]) != b or ap == b:
        raise RuntimeError(f"拒绝删除：不在 BASE 内 {ap}")
    if not os.path.isdir(ap):
        return
    if not any(n.startswith(".abx_owned_") for n in os.listdir(ap)):
        raise RuntimeError(f"拒绝删除：缺少所有权标记 {ap}")
    import shutil
    shutil.rmtree(ap)


# ---------------------------------------------------------------- 结果
class Results:
    def __init__(self, suite: str):
        self.suite = suite
        self.items: list[dict] = []
        self.t0 = time.time()
        self.errors: list[str] = []

    def rec(self, tid: str, title: str, status: str, detail="") -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"非法状态 {status}")
        if any(i["id"] == tid for i in self.items):
            raise ValueError(f"重复用例 id {tid}")
        self.items.append({"id": tid, "title": title, "status": status,
                           "detail": str(detail)})
        tag = {"UNTESTED": "未测"}.get(status, status)
        print(f"[{tag}] {tid} {title}")
        if detail:
            for line in str(detail).splitlines()[:3]:
                print(f"         {line}")

    def check(self, tid: str, title: str, cond, detail="") -> bool:
        self.rec(tid, title, "PASS" if cond else "FAIL", detail)
        return bool(cond)

    def check_child_exits(self, tid: str, allow_labels=(), title_extra="") -> bool:
        """必测：所有非故障注入子进程的最终退出码必须为 0 且未超时。

        不采信“子进程输出了完整 JSON”这一条件——退出期崩溃（如
        Qt 清理阶段 abort）会让 returncode 非 0，必须计入父套件判定。
        allow_labels 只用于预期非零的故障注入子进程，且会被写进标题。
        """
        allow = set(allow_labels)
        bad, seen = [], []
        for label, rec in CHILD_LOG:
            if label in allow:
                continue
            seen.append(label)
            if rec.get("timed_out"):
                bad.append(f"{label}:超时(哨兵 {rec.get('exit_code')})")
            elif rec.get("exit_code") != 0:
                bad.append(f"{label}:exit={rec.get('exit_code')}")
        if not seen:
            bad.append("本套件没有采集到任何非豁免子进程记录")
        ok = not bad
        title = ("所有非故障注入子进程的最终退出码均为 0"
                 "（不只看 JSON 是否完整）")
        if allow:
            title += f"；预期非零豁免={sorted(allow)}"
        if title_extra:
            title += f"；{title_extra}"
        self.check(tid, title, ok,
                   f"子进程数={len(seen)} " + "; ".join(bad))
        return ok

    def counts(self) -> dict:
        c = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "UNTESTED": 0}
        for i in self.items:
            c[i["status"]] += 1
        return c

    def finalize(self, out_path: str, required_ids=None) -> int:
        missing = []
        if required_ids:
            have = {i["id"] for i in self.items}
            missing = [r for r in required_ids if r not in have]
            for r in missing:
                self.rec(r, f"必测项 {r} 未产生结果", "FAIL")
        c = self.counts()
        failed = [i["id"] for i in self.items if i["status"] in ("FAIL", "BLOCK")]
        c["missing"] = len(missing)
        c["errors"] = list(self.errors)
        c["elapsed_sec"] = round(time.time() - self.t0, 2)
        c["pid"] = os.getpid()
        c["python"] = sys.version.split()[0]
        c["platform"] = sys.platform
        payload = {"suite": self.suite, "summary": c, "items": self.items,
                   "required_missing": missing,
                   "finished_at_utc": datetime.now(timezone.utc).isoformat()}
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        ok = c["FAIL"] == 0 and c["BLOCK"] == 0 and not self.errors
        print(f"\n=== {self.suite} 汇总: " +
              f"PASS={c['PASS']} FAIL={c['FAIL']} BLOCK={c['BLOCK']} " +
              f"未测={c['UNTESTED']} 缺失={c['missing']} 耗时={c['elapsed_sec']}s ===")
        print(f"EXIT_CODE={0 if ok else 1}")
        return 0 if ok else 1


# ---------------------------------------------------------------- 清单
def sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_rel(p: str) -> str:
    return p.replace("\\", "/")


def full_manifest(root: str, exclude_dirs=(".organizer",)) -> dict:
    """全量逐文件清单：包含隐藏文件与 _已整理 内文件。

    只排除应用状态目录（默认 .organizer）。
    """
    excl = set(exclude_dirs)
    out = {}
    for dp, dn, fn in os.walk(root, followlinks=False):
        dn[:] = [d for d in dn if d not in excl]
        for name in fn:
            full = os.path.join(dp, name)
            rel = norm_rel(os.path.relpath(full, root))
            out[rel] = {"size": os.path.getsize(full), "sha256": sha256_file(full)}
    return out


def save_manifest(path: str, manifest: dict, meta=None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"count": len(manifest), "meta": meta or {},
                   "items": dict(sorted(manifest.items()))},
                  f, ensure_ascii=False, indent=2)


def load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["items"]


def diff_manifests(expected: dict, actual: dict) -> dict:
    return {
        "missing": sorted(set(expected) - set(actual)),
        "extra": sorted(set(actual) - set(expected)),
        "changed": sorted(k for k in set(expected) & set(actual)
                          if expected[k] != actual[k]),
    }


def files_under(manifest: dict, prefix: str) -> list[str]:
    return sorted(k for k in manifest if k.startswith(prefix))


# ---------------------------------------------------------------- 子进程
def child_env(base: str, extra=None) -> dict:
    env = os.environ.copy()
    env.pop("QT_QPA_PLATFORM", None)
    env["ARCHIVEBOX_RECENT_FILE"] = os.path.join(base, "recent.json")
    env["ARCHIVEBOX_APP_DIR"] = os.path.join(base, "appdata")
    env["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(base, "appdata", "schemes")
    if extra:
        env.update(extra)
    return env


def run_child(source: str, label: str, out_dir: str, base: str,
              timeout: int = 120, extra_env=None) -> dict:
    """写临时脚本并用 sys.executable 运行，完整记录过程。"""
    os.makedirs(out_dir, exist_ok=True)
    script = os.path.join(out_dir, f"child_{label}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(source)
    env = child_env(base, extra_env)
    cmd = [sys.executable, script]
    rec = {"label": label, "cmd": cmd, "cwd": out_dir,
           "python": sys.executable, "timeout": timeout,
           "env_isolated": {k: env.get(k) for k in
                            ("ARCHIVEBOX_RECENT_FILE", "ARCHIVEBOX_APP_DIR",
                             "ARCHIVEBOX_SCHEME_DIR")},
           "start_utc": datetime.now(timezone.utc).isoformat(),
           "timed_out": False}
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, cwd=out_dir, env=env, capture_output=True,
                            text=True, timeout=timeout,
                            creationflags=(subprocess.CREATE_NO_WINDOW
                                           if sys.platform == "win32" else 0))
        rec["exit_code"] = cp.returncode
        rec["exit_code_kind"] = "os_exit_code"
        rec["os_exit_code"] = cp.returncode
        out, err = cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as e:
        # -999 只是本框架的超时哨兵，不是操作系统给出的退出码
        rec["exit_code"] = -999
        rec["exit_code_kind"] = "timeout_sentinel"
        rec["os_exit_code"] = "未采集（进程被强制终止）"
        rec["timed_out"] = True
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    rec["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    rec["end_utc"] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(out_dir, f"{label}.stdout.log"), "w", encoding="utf-8") as f:
        f.write(out)
    with open(os.path.join(out_dir, f"{label}.stderr.log"), "w", encoding="utf-8") as f:
        f.write(err)
    rec["stdout_log"] = os.path.join(out_dir, f"{label}.stdout.log")
    rec["stderr_log"] = os.path.join(out_dir, f"{label}.stderr.log")
    with open(os.path.join(out_dir, f"{label}.process.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    CHILD_LOG.append((label, rec))
    rec["stdout"] = out
    rec["stderr"] = err
    print(f"  [child {label}] exit={rec['exit_code']} timeout={rec['timed_out']} "
          f"elapsed={rec['elapsed_ms']}ms")
    for line in out.splitlines():
        print("     ", line)
    if err.strip():
        for line in err.strip().splitlines()[-8:]:
            print("     !", line)
    return rec


def kv(text: str, key: str, default=None):
    """从子进程 stdout 解析 KEY=VALUE。"""
    import re
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else default


# ---------------------------------------------------------------- Qt 辅助
def qapp_isolate():
    """导入 Qt 前调用：清掉可能残留的 offscreen 设置。"""
    os.environ.pop("QT_QPA_PLATFORM", None)


def wait_until(app, cond, timeout=30.0, label="", interval=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(interval)
    app.processEvents()
    ok = bool(cond())
    if not ok:
        print(f"  wait TIMEOUT ({label})")
    return ok


def pump(app, seconds=0.3):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def shot(widget, path: str) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return bool(widget.grab().save(path))


def dialog_text(dlg) -> str:
    """收集对话框里所有可见文本（QLabel + QMessageBox 正文）。"""
    from PySide6.QtWidgets import QLabel, QMessageBox
    parts = []
    try:
        if isinstance(dlg, QMessageBox):
            parts.append(dlg.text())
            if dlg.informativeText():
                parts.append(dlg.informativeText())
        for lb in dlg.findChildren(QLabel):
            if lb.text():
                parts.append(lb.text())
    except RuntimeError:
        pass
    return "\n".join(parts)


class AutoOk:
    """只自动关闭“仅含 OK 的信息类 QMessageBox”，并记录其全文作为证据。

    Yes/No 等问题框不会被自动应答——必须由 arm_modal 显式处理，
    否则会一直阻塞直到测试超时判失败（不允许静默跳过弹窗）。
    """

    def __init__(self, app):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QMessageBox
        self._Qt, self._QTest = Qt, QTest
        self._QApp, self._QMB = QApplication, QMessageBox
        self.seen = []
        self._ids = set()
        self._timer = QTimer(app)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        dlg = self._QApp.activeModalWidget()
        if dlg is None or id(dlg) in self._ids:
            return
        if isinstance(dlg, self._QMB) and dlg.standardButtons() == self._QMB.Ok:
            self._ids.add(id(dlg))
            self.seen.append(dialog_text(dlg))
            self._QTest.mouseClick(dlg.button(self._QMB.Ok), self._Qt.LeftButton)

    def stop(self):
        self._timer.stop()


def arm_modal(on_modal, timeout=25.0):
    """在调用阻塞式槽之前调用；模态窗口出现后执行 on_modal(dlg)。

    on_modal 必须返回要点击的按钮（或 None 表示 reject）。
    返回 state 字典，调用返回后读取 state['captured']。
    """
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    state = {"captured": None, "error": None, "handled": False}
    t0 = time.time()

    def tick():
        if state["handled"]:
            return
        dlg = QApplication.activeModalWidget()
        if dlg is None:
            if time.time() - t0 < timeout:
                QTimer.singleShot(40, tick)
            else:
                state["error"] = "模态窗口未出现"
            return
        try:
            state["captured"] = {"class": type(dlg).__name__,
                                 "text": dialog_text(dlg)}
            btn = on_modal(dlg)
            state["handled"] = True
            if btn is None:
                dlg.reject()
            else:
                QTest.mouseClick(btn, Qt.LeftButton)
        except Exception as e:  # 记录而非吞掉
            state["error"] = repr(e)
            state["handled"] = True
            try:
                dlg.reject()
            except Exception:
                pass

    QTimer.singleShot(40, tick)
    return state
