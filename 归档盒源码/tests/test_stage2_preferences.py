"""阶段 2 · 个性化记忆闭环 回归测试。

覆盖：
- 从单文件修正入口保存空间偏好，字段落库
- 关闭重开后偏好继续生效（样本 B）
- 对照空间 vs 生效空间两轮目标路径
- 「仅改本次」不写偏好；取消 / 直接关闭 / AI 候选不写库
- 修改目标、停用、重启后仍停用
- 旧 JSON 一次性迁移；停用规则在 JSON 改回 enabled 也不能重新启用
- 空间偏好不跨空间；只有全局偏好跨空间
- 已保存偏好被「仅改本次」覆盖；同等级冲突生成歧义，改选后才勾选
- 旧 history.db 中 long_term: 批次不受偏好库影响，撤销仍按原路径与 SHA 恢复
- 取消执行确认不移动；确认后只移动勾选；撤销恢复

全部样本只建在临时目录；ARCHIVEBOX_APP_DIR / ARCHIVEBOX_SCHEME_DIR /
ARCHIVEBOX_RECENT_FILE 均指向临时目录；Qt 使用 offscreen。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_APP_DIR = tempfile.mkdtemp(prefix="stage2_app_")
_SCHEME_DIR = tempfile.mkdtemp(prefix="stage2_scheme_")
_RECENT_FILE = tempfile.mktemp(prefix="stage2_recent_")
os.environ["ARCHIVEBOX_APP_DIR"] = _APP_DIR
os.environ["ARCHIVEBOX_SCHEME_DIR"] = _SCHEME_DIR
os.environ["ARCHIVEBOX_RECENT_FILE"] = _RECENT_FILE

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QDialog, QMessageBox,
)

import ai_fallback  # noqa: E402
import main as main_mod  # noqa: E402
import plan_patch as pp  # noqa: E402
import preferences as P  # noqa: E402
from ai_fallback import AiSettings, apply_ai_fallback  # noqa: E402
from classification_config import (  # noqa: E402
    INNER_CLEAR, INNER_KEEP, INNER_REPLACE, MATCH_DIR_SCOPE,
    MATCH_EXACT_PATH,
    LongTermRule, config_path, default_config, load_config, save_config,
)
from config_dialog import ClassificationConfigDialog  # noqa: E402
from config_transfer import create_backup, list_backups  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from main import MainWindow  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE,
    build_plan, generate_plan,
)
from scanner import scan_directory  # noqa: E402
import scheme_store  # noqa: E402
from storage import Storage  # noqa: E402

APP = QApplication.instance() or QApplication([])
BASE = tempfile.mkdtemp(prefix="stage2_preferences_")
CHECKS = []
_ANSWER = {"question": QMessageBox.No}


def _fake_question(*args, **kwargs):
    return _ANSWER["question"]


QMessageBox.question = staticmethod(_fake_question)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)

# 让规则试运行在测试里同步完成，避免模态对话框里等后台线程
_REAL_WORKER_FACTORY = pp.rule_preview_worker_class


def _sync_worker():
    raise RuntimeError("stage2 test: use synchronous preview")


pp.rule_preview_worker_class = _sync_worker

_REAL_MO_EXEC = pp.ManualOverrideDialog.exec
_REAL_RD_EXEC = pp.RuleDraftDialog.exec


class yes_answer:
    def __enter__(self):
        _ANSWER["question"] = QMessageBox.Yes

    def __exit__(self, *exc):
        _ANSWER["question"] = QMessageBox.No


def rec(tid: str, title: str, ok: bool, evidence: str = ""):
    CHECKS.append((tid, title, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}"
          + (f" — {evidence}" if evidence else ""))


def process(seconds: float = 0.02):
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 30.0, label: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        APP.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


def root_dir(tag: str) -> str:
    path = os.path.join(BASE, tag)
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def write(root: str, rel: str, text: str = "普通说明文字") -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def open_window(root: str) -> MainWindow:
    win = MainWindow()
    win.path_edit.setText(root)
    win._reset_for_new_dir(root)
    win.show()
    process()
    wait_until(lambda: not getattr(win, "_working", True), 10, "idle")
    return win


def scan_and_preview(win: MainWindow, timeout: float = 45.0):
    win.on_scan()
    assert wait_until(
        lambda: (not win._working) and win.scan_result is not None,
        timeout, "scan"), "扫描超时"
    win._generate_and_show_plan()
    assert wait_until(
        lambda: (not win._plan_working) and win.current_plan is not None
        and win._plan_valid, timeout, "plan"), "预览生成超时"
    process()


def preview(root: str):
    win = open_window(root)
    scan_and_preview(win)
    return win


def close_window(win: MainWindow):
    try:
        win.close()
    except Exception:
        pass
    process(0.05)


def plan_for(root: str, config=None):
    scan = scan_directory(root, detect_project_roots=False)
    return generate_plan(scan, MODE_BY_DIRECTION_SEMESTER, [],
                         config=config or default_config())


def item_of(plan, rel: str):
    return next(i for i in plan.items if i.file.rel_path == rel)


def row_of(win: MainWindow, rel: str) -> int:
    for idx, item in enumerate(win.current_plan.items):
        if item.file.rel_path == rel:
            return idx
    return -1


def space_rows(root: str):
    return P.list_space_preferences(root)


def count_all(root: str) -> int:
    return len(space_rows(root)) + len(P.list_global_preferences())


def _mo_save_once(self, *, category="归档盒", direction="项目",
                  match_dir="下载/归档盒"):
    self.dir_combo.setCurrentText(direction)
    self.sem_edit.setText("")
    self.cat_edit.setText(category)
    self.inner_edit.setText("")
    self._on_save_as_rule()
    return self.dlg.result()


def install_ui_pref_save(*, name="开发文档按项目归档", category="归档盒",
                        direction="项目", match_dir="下载/归档盒",
                        scope="space", inner_clear=True, capture=None):
    """把修正入口的两个对话框替换成可编程驱动，仍走真实保存逻辑。"""

    def rd_exec(self):
        self.name_edit.setText(name)
        # 目录范围
        for i in range(self.match_combo.count()):
            if self.match_combo.itemData(i) == MATCH_DIR_SCOPE:
                self.match_combo.setCurrentIndex(i)
                break
        self.source_edit.setText(match_dir)
        self.subdir_check.setChecked(True)
        self.kw_edit.setText("")
        self.dir_combo.setCurrentText(direction)
        self.sem_edit.setText("")
        self.cat_edit.setText(category)
        # 用显示文本选择「清空内部路径」
        for i in range(self.inner_combo.count()):
            if "清空" in self.inner_combo.itemText(i):
                self.inner_combo.setCurrentIndex(i)
                break
        self.scope_combo.setCurrentIndex(0 if scope == "space" else 1)
        if capture is not None:
            capture["desc"] = self.rule_desc.text()
            capture["scope"] = self.scope_combo.currentData()
        self._on_preview()
        with yes_answer():
            self._on_save()
        result = self.dlg.result() == 1
        self._on_closed()
        return result

    pp.ManualOverrideDialog.exec = _mo_save_once
    pp.RuleDraftDialog.exec = rd_exec


def install_ui_save_cancel(*, rule_level=False):
    def mo_exec(self):
        self.dir_combo.setCurrentText("项目")
        self.cat_edit.setText("归档盒")
        if rule_level:
            self._on_save_as_rule()
        return self.dlg.result()

    def rd_exec(self):
        self._on_closed()
        return False

    pp.ManualOverrideDialog.exec = mo_exec
    pp.RuleDraftDialog.exec = rd_exec


def install_ui_one_off(*, category="高等数学", direction="课程",
                       inner_path=""):
    def mo_exec(self):
        self.dir_combo.setCurrentText(direction)
        self.sem_edit.setText("2026秋-大二上" if direction == "课程" else "")
        self.cat_edit.setText(category)
        self.inner_edit.setText(inner_path)
        self.dlg.accept()
        return self.dlg.result()

    pp.ManualOverrideDialog.exec = mo_exec


def install_ui_revert():
    def mo_exec(self):
        self._on_revert_auto()
        return self.dlg.result()

    pp.ManualOverrideDialog.exec = mo_exec


def restore_dialogs():
    pp.ManualOverrideDialog.exec = _REAL_MO_EXEC
    pp.RuleDraftDialog.exec = _REAL_RD_EXEC


def edit_preference_via_config(root: str, preference_id: str, *,
                               category=None, enabled=None) -> str:
    """直接驱动配置页：读 SQLite 偏好、修改目标/停用、保存。"""
    cfg, _ = load_config(root)
    prefs = P.list_all_preferences(root)
    dlg = ClassificationConfigDialog(
        root=root, config=cfg, preferences=prefs,
        save_callback=lambda c: save_config(root, c),
        preference_save_callback=lambda rows: P.persist_preferences(
            root, rows, sync_json=False),
    )
    table = dlg.table_longterm
    row = None
    for r in range(table.rowCount()):
        if table.item(r, 10).text() == preference_id:
            row = r
            break
    assert row is not None, f"配置页找不到偏好 {preference_id}"
    if category is not None:
        table.item(row, 8).setText(category)
    if enabled is not None:
        table.item(row, 0).setCheckState(
            Qt.Checked if enabled else Qt.Unchecked)
    dlg._on_save()
    return "ok"


def config_row(dlg, preference_id: str) -> int:
    """在配置页长期规则表里按 preference_id 找到行号。"""
    table = dlg.table_longterm
    for r in range(table.rowCount()):
        item = table.item(r, 10)
        if item is not None and item.text() == preference_id:
            return r
    raise AssertionError(f"配置页找不到偏好 {preference_id}")


def save_via_config_page(win: MainWindow, mutate=None) -> dict:
    """打开主窗口的配置页（on_edit_config），执行 mutate 后点「保存并应用」。

    回归必须走配置页的真实收集与保存（_collect_preferences → _on_save →
    save_all_callback），不能把现成的 Preference 直接交给 persist_preferences。
    """
    captured = {"errors": [], "accepted": False, "result": None}
    real_exec = ClassificationConfigDialog.exec
    real_critical = QMessageBox.critical

    def fake_critical(parent, title, text, *args, **kwargs):
        captured["errors"].append(str(text))
        return QMessageBox.Ok

    def fake_exec(self):
        if mutate is not None:
            mutate(self)
        self._on_save()
        captured["accepted"] = self.result() == 1
        captured["result"] = self.result_config
        return self.result()

    ClassificationConfigDialog.exec = fake_exec
    QMessageBox.critical = staticmethod(fake_critical)
    try:
        win.on_edit_config()
    finally:
        ClassificationConfigDialog.exec = real_exec
        QMessageBox.critical = staticmethod(real_critical)
        process()
    return captured


# ============================ 用例 ============================

def test_control_without_preference():
    root = root_dir("control")
    write(root, "下载/归档盒/开发说明.md")
    win = preview(root)
    plan = win.current_plan
    item = item_of(plan, "下载/归档盒/开发说明.md")
    ok = (item.target_dir_rel != os.path.join("_已整理", "项目", "归档盒"))
    rec("T01", "未保存偏好时样本 A 不会进入「项目/归档盒」", ok,
        f"target={item.target_rel_path}")
    close_window(win)
    return item.target_rel_path


def test_save_space_preference_from_ui():
    root = root_dir("space")
    write(root, "下载/归档盒/开发说明.md")
    win = preview(root)
    assert P.list_space_preferences(root) == []
    cap = {}
    install_ui_pref_save(capture=cap)
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/开发说明.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
        close_window(win)

    rows = P.list_space_preferences(root)
    row = rows[0] if rows else None
    ok = (
        row is not None
        and len(rows) == 1
        and row.scope == P.SCOPE_SPACE
        and row.enabled is True
        and row.match_type == MATCH_DIR_SCOPE
        and row.source_path == "下载/归档盒"
        and row.include_subdirs is True
        and row.direction == "项目"
        and row.category == "归档盒"
        and bool(row.confirmed_at)
        and row.confirmed_source_rel == "下载/归档盒/开发说明.md"
        and row.source_kind == P.SOURCE_USER
    )
    rec("T02", "从界面修正入口保存空间偏好，字段与确认信息落库", ok,
        f"rows={len(rows)} scope={getattr(row, 'scope', None)} "
        f"enabled={getattr(row, 'enabled', None)} "
        f"match={getattr(row, 'source_path', None)} "
        f"target={getattr(row, 'target_label', None)}")
    desc_ok = all(k in cap.get("desc", "") for k in
                  ("匹配条件", "适用范围", "目标"))
    rec("T03", "保存对话框同时展示匹配条件、适用范围、目标", desc_ok,
        cap.get("desc", "").replace("<br>", " | "))
    json_rules = [r.rule_id for r in load_config(root)[0].long_term_rules]
    rec("T04", "空间偏好同步写入该空间 JSON 长期规则镜像", row is not None
        and row.preference_id in json_rules, f"json_ids={json_rules}")
    return root, row.preference_id if row else ""


def test_two_round_samples(space_root: str, pref_id: str, control_target: str):
    # 关闭后重新打开，并加入样本 B
    write(space_root, "下载/归档盒/接口草稿.md")
    control = root_dir("control_b")
    write(control, "下载/归档盒/接口草稿.md")
    cwin = preview(control)
    control_b = item_of(cwin.current_plan, "下载/归档盒/接口草稿.md")
    control_target_b = control_b.target_rel_path
    close_window(cwin)

    win = preview(space_root)
    plan = win.current_plan
    a = item_of(plan, "下载/归档盒/开发说明.md")
    b = item_of(plan, "下载/归档盒/接口草稿.md")
    evidence = b.rule_applied or ""
    ok = (
        b.target_rel_path != control_target_b
        and a.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
        and b.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
        and pref_id in evidence
    )
    rec("T05", "关闭重开后样本 B 命中偏好；对照空间 / 生效空间目标不同", ok,
        f"control={control_target_b} | space={b.target_rel_path} | "
        f"依据={evidence}")
    rec("T06", "预览依据同时包含偏好名称与 preference_id", ok
        and "开发文档按项目归档" in evidence and pref_id in evidence,
        evidence)
    close_window(win)
    return control_target_b


def test_one_off_only(space_root: str):
    write(space_root, "下载/归档盒/临时稿.md")
    win = preview(space_root)
    before = len(P.list_space_preferences(space_root))
    install_ui_one_off()
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/临时稿.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    item = item_of(win.current_plan, "下载/归档盒/临时稿.md")
    after = len(P.list_space_preferences(space_root))
    expected_prefix = os.path.join(
        "_已整理", "课程", "2026秋-大二上", "高等数学")
    ok = (item.target_dir_rel.startswith(expected_prefix)
          and before == after)
    rec("T07", "「仅改本次」立即生效且不写偏好库", ok,
        f"target={item.target_rel_path} rows {before}->{after}")
    close_window(win)

    # 重新打开：临时修正消失，已保存偏好仍在
    win2 = preview(space_root)
    item2 = item_of(win2.current_plan, "下载/归档盒/临时稿.md")
    ok2 = (item2.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
           and len(P.list_space_preferences(space_root)) == before)
    rec("T08", "重新生成预览后本次临时修正消失，已保存偏好仍生效", ok2,
        f"target={item2.target_rel_path}")
    close_window(win2)


def test_one_off_control_restart():
    root = root_dir("control_once")
    write(root, "下载/归档盒/说明.md")
    win = preview(root)
    install_ui_one_off()
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/说明.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    overridden = item_of(win.current_plan, "下载/归档盒/说明.md").target_rel_path
    rows = len(P.list_space_preferences(root))
    close_window(win)
    win2 = preview(root)
    auto_again = item_of(win2.current_plan, "下载/归档盒/说明.md").target_rel_path
    close_window(win2)
    auto = item_of(plan_for(root), "下载/归档盒/说明.md").target_rel_path
    ok = (overridden != auto_again and auto_again == auto and rows == 0)
    rec("T09", "对照空间「仅改本次」重启后回到无偏好结果", ok,
        f"one_off={overridden} auto_again={auto_again} auto={auto} rows={rows}")


def test_cancel_close_and_ai_no_write(space_root: str):
    win = preview(space_root)
    before = len(P.list_space_preferences(space_root))
    win.table.selectRow(row_of(win, "下载/归档盒/临时稿.md"))
    process()

    install_ui_save_cancel(rule_level=False)
    try:
        win.on_override_selected()
    finally:
        restore_dialogs()
    after_cancel = len(P.list_space_preferences(space_root))

    install_ui_save_cancel(rule_level=True)
    try:
        win.on_override_selected()
    finally:
        restore_dialogs()
    after_close = len(P.list_space_preferences(space_root))
    close_window(win)
    ok = before == after_cancel == after_close
    rec("T10", "取消 / 直接关闭保存对话框都不增加偏好行", ok,
        f"rows={before}/{after_cancel}/{after_close}")

    # AI 候选：只在计划上给出建议，不写偏好库
    root = root_dir("ai")
    write(root, "下载/未知.md", "这是一份提到高等数学复习的说明")
    plan = plan_for(root)
    rows_before = count_all(root)
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    rel = "下载/未知.md"
    response = ('{"file":"%s","direction":"课程","category":"高等数学",'
                '"action":"move","evidence":"正文提到高等数学复习"}' % rel)
    settings = AiSettings(api_key="test-key", enabled=True)
    apply_ai_fallback(plan, settings,
                      transport=lambda _s, _m, text=response: text,
                      require_evidence=True)
    rows_after = count_all(root)
    rec("T11", "AI 回退候选不写偏好库", rows_before == rows_after == 0,
        f"rows={rows_before}->{rows_after} target="
        f"{item_of(plan, rel).target_rel_path}")


def test_modify_and_disable(space_root: str, pref_id: str):
    edit_preference_via_config(space_root, pref_id, category="课程设计")
    win = preview(space_root)
    item = item_of(win.current_plan, "下载/归档盒/接口草稿.md")
    ok = item.target_dir_rel == os.path.join("_已整理", "项目", "课程设计")
    rec("T12", "配置页修改目标后新预览使用新目标", ok,
        f"target={item.target_rel_path}")
    close_window(win)

    rows_before = len(P.list_space_preferences(space_root))
    edit_preference_via_config(space_root, pref_id, enabled=False)
    rows = P.list_space_preferences(space_root)
    win2 = preview(space_root)
    item2 = item_of(win2.current_plan, "下载/归档盒/接口草稿.md")
    control = item_of(plan_for(space_root), "下载/归档盒/接口草稿.md")
    close_window(win2)
    ok2 = (len(rows) == rows_before and rows[0].enabled is False
           and item2.target_rel_path == control.target_rel_path)
    rec("T13", "停用后新预览不再命中；偏好行保留但 enabled=false", ok2,
        f"enabled={rows[0].enabled if rows else None} "
        f"target={item2.target_rel_path}")

    # 再次关闭并打开仍停用，行数不增加
    win3 = preview(space_root)
    rows2 = P.list_space_preferences(space_root)
    close_window(win3)
    ok3 = (len(rows2) == rows_before and not rows2[0].enabled)
    rec("T14", "重启后仍停用且不新增偏好行", ok3,
        f"rows={len(rows2)} enabled={rows2[0].enabled if rows2 else None}")


def test_migrated_disabled_stays_disabled():
    root = root_dir("migrated")
    write(root, "下载/归档盒/旧稿.md")
    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-migrated", name="旧 JSON 规则", match_type=MATCH_DIR_SCOPE,
        source_path="下载/归档盒", include_subdirs=True, direction="项目",
        category="归档盒", inner_mode=INNER_CLEAR, enabled=False))
    save_config(root, cfg)

    win = preview(root)
    rows = P.list_space_preferences(root)
    marker = P.import_marker_present(root)
    item = item_of(win.current_plan, "下载/归档盒/旧稿.md")
    auto = item_of(plan_for(root, default_config()),
                   "下载/归档盒/旧稿.md")
    ok = (len(rows) == 1 and rows[0].enabled is False
          and rows[0].source_kind == P.SOURCE_MIGRATED
          and marker and item.target_rel_path == auto.target_rel_path)
    rec("T15", "旧 JSON 只迁一次；停用规则迁入后仍停用", ok,
        f"rows={len(rows)} enabled={rows[0].enabled if rows else None} "
        f"kind={rows[0].source_kind if rows else None} marker={marker}")
    close_window(win)

    # 在 JSON 里把 enabled 改回 true，再打开不得重新启用/插行
    cfg2, _ = load_config(root)
    cfg2.long_term_rules[0].enabled = True
    save_config(root, cfg2)
    win2 = preview(root)
    rows2 = P.list_space_preferences(root)
    item2 = item_of(win2.current_plan, "下载/归档盒/旧稿.md")
    close_window(win2)
    ok2 = (len(rows2) == 1 and rows2[0].enabled is False
           and item2.target_rel_path == auto.target_rel_path)
    rec("T16", "JSON 改回 enabled=true 不会重新启用或重复插入", ok2,
        f"rows={len(rows2)} enabled={rows2[0].enabled if rows2 else None} "
        f"target={item2.target_rel_path}")


def test_space_isolation_and_global():
    source = root_dir("global_src")
    write(source, "下载/其它/说明.md")
    # 第二空间先验证：相同相对路径不会命中第一空间的偏好
    space2 = root_dir("space2")
    write(space2, "下载/归档盒/接口草稿.md")
    control = root_dir("control_space2")
    write(control, "下载/归档盒/接口草稿.md")

    cwin = preview(control)
    control_target = item_of(
        cwin.current_plan, "下载/归档盒/接口草稿.md").target_rel_path
    close_window(cwin)
    win = preview(space2)
    space2_target = item_of(
        win.current_plan, "下载/归档盒/接口草稿.md").target_rel_path
    close_window(win)
    ok = space2_target == control_target
    rec("T17", "另一整理空间相同相对路径不命中空间偏好", ok,
        f"control={control_target} space2={space2_target}")

    # 从界面保存全局偏好（来源空间与匹配目录都不在第一空间）
    gwin = preview(source)
    install_ui_pref_save(
        name="全局说明归档", category="全局归档", direction="项目",
        match_dir="下载/其它", scope="global")
    try:
        gwin.table.selectRow(row_of(gwin, "下载/其它/说明.md"))
        process()
        gwin.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
        close_window(gwin)
    globals_ = P.list_global_preferences()
    gid = globals_[0].preference_id if globals_ else ""
    json_ids = [r.rule_id for r in load_config(source)[0].long_term_rules]
    rec("T18", "只有点选「全局」才写入全局库，且不写任何空间 JSON", bool(globals_)
        and gid not in json_ids,
        f"global_ids={[g.preference_id for g in globals_]} json_ids={json_ids}")

    # 第二空间加入同一全局匹配路径，应命中全局偏好
    write(space2, "下载/其它/说明.md")
    win2 = preview(space2)
    item = item_of(win2.current_plan, "下载/其它/说明.md")
    evidence = item.rule_applied or ""
    ok2 = (item.target_dir_rel == os.path.join("_已整理", "项目", "全局归档")
           and gid in evidence)
    rec("T19", "已启用全局偏好在第二空间生效，依据含 preference_id", ok2,
        f"target={item.target_rel_path} 依据={evidence}")
    close_window(win2)


def test_one_off_overrides_saved(space_root: str, pref_id: str):
    # 先恢复启用，便于观察覆盖依据
    edit_preference_via_config(space_root, pref_id, enabled=True)
    win = preview(space_root)
    rows_before = len(P.list_space_preferences(space_root))
    install_ui_one_off(category="高等数学", direction="课程")
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/接口草稿.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    item = item_of(win.current_plan, "下载/归档盒/接口草稿.md")
    evidence = item.rule_applied or ""
    expected_prefix = os.path.join(
        "_已整理", "课程", "2026秋-大二上", "高等数学")
    ok = (item.target_dir_rel.startswith(expected_prefix)
          and len(P.list_space_preferences(space_root)) == rows_before
          and pref_id in evidence and "覆盖" in evidence)
    rec("T20", "「仅改本次」覆盖已保存偏好并在依据里说明", ok,
        f"target={item.target_rel_path} 依据={evidence}")
    close_window(win)


def test_same_level_conflict_ambiguity():
    root = root_dir("conflict")
    write(root, "下载/归档盒/冲突.md")
    for pid, name, cat in (
            ("lt-a", "冲突规则 A", "归档盒"),
            ("lt-b", "冲突规则 B", "课程设计")):
        P.save_preference(root, P.Preference(
            preference_id=pid, name=name, match_type=MATCH_DIR_SCOPE,
            source_path="下载/归档盒", include_subdirs=True,
            direction="项目", category=cat, inner_mode=INNER_CLEAR,
            enabled=True, scope=P.SCOPE_SPACE, space_root=root,
            confirmed_at="2026-11-15T00:00:00",
            confirmed_source_rel="下载/归档盒/冲突.md"))
    before = len(P.list_space_preferences(root))
    win = preview(root)
    item = item_of(win.current_plan, "下载/归档盒/冲突.md")
    ambiguity = getattr(item, "ambiguity", None) or {}
    candidates = ambiguity.get("candidates") or []
    ids = {c.get("preference_id") for c in candidates if c.get("preference_id")}
    ok = (bool(ambiguity) and item.selected is False
          and {"lt-a", "lt-b"} <= ids)
    rec("T21", "同等级同匹配/不同目标生成歧义且默认不勾选", ok,
        f"selected={item.selected} candidates={len(candidates)} ids={sorted(ids)}")

    target_idx = next(i for i, c in enumerate(candidates)
                      if c.get("preference_id") == "lt-b")
    win.apply_ambiguity_choice("下载/归档盒/冲突.md", target_idx)
    process()
    item2 = item_of(win.current_plan, "下载/归档盒/冲突.md")
    rows_after = len(P.list_space_preferences(root))
    ok2 = (item2.selected is True and item2.ambiguity is None
           and item2.target_dir_rel == os.path.join("_已整理", "项目", "课程设计")
           and rows_after == before)
    rec("T22", "歧义改选后勾选且不新写偏好", ok2,
        f"selected={item2.selected} target={item2.target_rel_path} "
        f"rows {before}->{rows_after}")
    close_window(win)


def test_history_compatibility_and_execution():
    root = root_dir("history")
    src_a = write(root, "下载/归档盒/历史A.md", "历史样本 A")
    src_b = write(root, "下载/归档盒/历史B.md", "历史样本 B")
    sha_a, sha_b = sha256(src_a), sha256(src_b)
    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-history", name="历史长期规则", match_type=MATCH_DIR_SCOPE,
        source_path="下载/归档盒", include_subdirs=True, direction="项目",
        category="归档盒", inner_mode=INNER_CLEAR, enabled=True))
    plan = plan_for(root, cfg)
    item_a = item_of(plan, "下载/归档盒/历史A.md")
    item_b = item_of(plan, "下载/归档盒/历史B.md")
    # 只勾选 A，B 未勾选；B 模拟已跳过
    item_a.selected = True
    item_b.selected = False
    item_b.skipped = True
    storage = Storage(root)
    result = execute_plan(plan, storage)
    assert result.moved == 1, result.errors
    assert os.path.exists(os.path.join(root, item_a.target_rel_path)), "勾选文件未移动"
    assert os.path.exists(src_b), "未勾选文件被移动"
    batch = storage.get_undoable_batch()
    action_a = next(a for a in batch.actions
                    if a.source_rel == "下载/归档盒/历史A.md")
    ok_history_before = (
        action_a.rule_key == "long_term:lt-history"
        and action_a.classification_source == "长期规则")
    before_rows = [(a.action_index, a.rule_key, a.classification_source,
                    a.status, a.target_rel)
                   for a in storage.list_batches()[0].actions]

    # 打开偏好库 / 迁移不得改写历史行
    P.ensure_migrated(root, load_config(root)[0])
    P.list_effective_preferences(root)
    after_rows = [(a.action_index, a.rule_key, a.classification_source,
                   a.status, a.target_rel)
                  for a in storage.list_batches()[0].actions]
    rec("T23", "已有 long_term: 历史行在打开偏好库后保持不变",
        ok_history_before and before_rows == after_rows,
        f"rule_key={action_a.rule_key} source={action_a.classification_source}")

    # 撤销按原路径与 SHA 恢复
    undo_target = os.path.join(root, item_a.target_rel_path)
    undo_result = undo_latest_batch(storage)
    ok_undo = (not undo_result.conflicts and undo_result.restored >= 1
               and os.path.exists(src_a) and sha256(src_a) == sha_a
               and not os.path.exists(undo_target))
    rec("T24", "撤销 long_term 批次后路径与 SHA-256 恢复", ok_undo,
        f"restored={undo_result.restored} conflicts={len(undo_result.conflicts)} "
        f"sha_ok={sha256(src_a) == sha_a}")

    # 执行保护：取消确认框不移动
    root2 = root_dir("execute_cancel")
    src_c = write(root2, "下载/归档盒/取消.md", "取消样本")
    sha_c = sha256(src_c)
    plan2 = plan_for(root2)
    item_c = item_of(plan2, "下载/归档盒/取消.md")
    item_c.selected = True
    win = open_window(root2)
    win.current_plan = plan2
    win._plan_valid = True
    win._populate_table(plan2)

    class FakeReject:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Rejected

    old_dialog = main_mod.ExecuteConfirmDialog
    main_mod.ExecuteConfirmDialog = FakeReject
    try:
        win.on_execute()
    finally:
        main_mod.ExecuteConfirmDialog = old_dialog
    ok_cancel = (os.path.exists(src_c) and sha256(src_c) == sha_c
                 and not os.path.exists(
                     os.path.join(root2, item_c.target_rel_path)))
    rec("T25", "取消执行确认框后源文件路径与 SHA-256 不变", ok_cancel,
        f"exists={os.path.exists(src_c)} sha_ok={sha256(src_c) == sha_c}")
    close_window(win)


def save_pref(root: str, pid: str, name: str, *, category="归档盒",
              inner_mode=INNER_CLEAR, inner_path="", source="下载/归档盒",
              reason=""):
    P.save_preference(root, P.Preference(
        preference_id=pid, name=name, match_type=MATCH_DIR_SCOPE,
        source_path=source, include_subdirs=True, direction="项目",
        category=category, inner_mode=inner_mode, inner_path=inner_path,
        enabled=True, scope=P.SCOPE_SPACE, space_root=root,
        reason=reason,
        confirmed_at="2026-11-15T00:00:00",
        confirmed_source_rel=f"{source}/样本.md"))


def corrupt_global_db():
    path = P.global_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"this is not a sqlite database")


def restore_global_db():
    path = P.global_db_path()
    if os.path.exists(path):
        os.remove(path)


def test_adopted_preference_is_selected():
    root = root_dir("selected")
    write(root, "下载/归档盒/一样.md")
    save_pref(root, "lt-selected", "明确命中要勾选")
    win = preview(root)
    item = item_of(win.current_plan, "下载/归档盒/一样.md")
    ok = (
        item.rule_key == "long_term:lt-selected"
        and item.target_rel_path == os.path.join(
            "_已整理", "项目", "归档盒", "一样.md")
        and item.ambiguity is None
        and item.selected is True
    )
    rec("T26", "明确命中偏好且无歧义时预览默认勾选", ok,
        f"rule_key={item.rule_key} target={item.target_rel_path} "
        f"ambiguity={bool(item.ambiguity)} selected={item.selected}")
    close_window(win)


def test_revert_auto_returns_to_preference():
    root = root_dir("revert")
    write(root, "下载/归档盒/恢复.md")
    save_pref(root, "lt-revert", "恢复到偏好")
    win = preview(root)
    pref_target = os.path.join("_已整理", "项目", "归档盒", "恢复.md")
    item = item_of(win.current_plan, "下载/归档盒/恢复.md")
    before_ok = (item.target_rel_path == pref_target
                 and item.rule_key == "long_term:lt-revert"
                 and item.selected is True)

    install_ui_revert()
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/恢复.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    direct = item_of(win.current_plan, "下载/归档盒/恢复.md")
    ok_direct = (
        direct.target_rel_path == pref_target
        and direct.rule_key == "long_term:lt-revert"
        and "lt-revert" in (direct.rule_applied or "")
        and direct.selected is True
        and direct.is_manual_overridden is False
    )
    rec("T27", "直接点「恢复自动分类」仍回到这条偏好", before_ok and ok_direct,
        f"target={direct.target_rel_path} rule_key={direct.rule_key} "
        f"selected={direct.selected} manual={direct.is_manual_overridden}")

    rows_before = len(P.list_space_preferences(root))
    install_ui_one_off(category="高等数学", direction="课程")
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/恢复.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    manual = item_of(win.current_plan, "下载/归档盒/恢复.md")
    manual_ok = (manual.target_dir_rel.startswith(os.path.join(
        "_已整理", "课程", "2026秋-大二上", "高等数学"))
        and len(P.list_space_preferences(root)) == rows_before)
    rec("T28", "「仅改本次」改到高等数学且不新增偏好", manual_ok,
        f"target={manual.target_rel_path} rows={len(P.list_space_preferences(root))}")

    install_ui_revert()
    try:
        win.table.selectRow(row_of(win, "下载/归档盒/恢复.md"))
        process()
        win.on_override_selected()
        process(0.1)
    finally:
        restore_dialogs()
    reverted = item_of(win.current_plan, "下载/归档盒/恢复.md")
    ok_revert = (
        reverted.target_rel_path == pref_target
        and reverted.rule_key == "long_term:lt-revert"
        and "lt-revert" in (reverted.rule_applied or "")
        and reverted.selected is True
        and reverted.is_manual_overridden is False
    )
    rec("T29", "改到高等数学后再恢复自动，回到偏好目标与 long_term 依据",
        ok_revert,
        f"target={reverted.target_rel_path} rule_key={reverted.rule_key} "
        f"依据={reverted.rule_applied}")
    close_window(win)


def test_revert_keeps_preference_inner_path():
    cases = [
        ("keep", INNER_KEEP, "", os.path.join(
            "_已整理", "项目", "归档盒", "归档盒", "保留.md")),
        ("replace", INNER_REPLACE, "专题", os.path.join(
            "_已整理", "项目", "归档盒", "专题", "替换.md")),
    ]
    for tag, mode, inner, expected in cases:
        root = root_dir(f"revert_inner_{tag}")
        rel = f"下载/归档盒/{'保留' if tag == 'keep' else '替换'}.md"
        write(root, rel)
        save_pref(root, f"lt-inner-{tag}", f"内部路径-{tag}",
                  inner_mode=mode, inner_path=inner)
        win = preview(root)
        item = item_of(win.current_plan, rel)
        pref_target = item.target_rel_path
        initial_ok = pref_target == expected and item.selected is True

        install_ui_one_off(category="高等数学", direction="课程",
                           inner_path="实验")
        try:
            win.table.selectRow(row_of(win, rel))
            process()
            win.on_override_selected()
            process(0.1)
        finally:
            restore_dialogs()
        install_ui_revert()
        try:
            win.table.selectRow(row_of(win, rel))
            process()
            win.on_override_selected()
            process(0.1)
        finally:
            restore_dialogs()
        after = item_of(win.current_plan, rel)
        ok = (initial_ok and after.target_rel_path == pref_target
              and after.rule_key == f"long_term:lt-inner-{tag}")
        rec(f"T30{'.1' if tag == 'keep' else '.2'}",
            f"内部路径「{'保留' if tag == 'keep' else '替换'}」恢复后不丢失",
            ok, f"before={pref_target} after={after.target_rel_path}")
        close_window(win)


def test_selected_preference_execution_roundtrip():
    root = root_dir("selected_execute")
    src_a = write(root, "下载/归档盒/勾选.md", "勾选样本")
    src_b = write(root, "下载/归档盒/未勾.md", "未勾样本")
    sha_a, sha_b = sha256(src_a), sha256(src_b)
    save_pref(root, "lt-exec", "执行勾选回归")
    win = preview(root)
    item_a = item_of(win.current_plan, "下载/归档盒/勾选.md")
    item_b = item_of(win.current_plan, "下载/归档盒/未勾.md")
    both_selected = item_a.selected is True and item_b.selected is True

    row_b = row_of(win, "下载/归档盒/未勾.md")
    box = win.table.cellWidget(row_b, 0)
    check = box.findChild(QCheckBox) if box is not None else None
    assert check is not None, "表格行没有复选框"
    check.setChecked(False)
    process()
    uncheck_ok = item_b.selected is False and item_a.selected is True

    class FakeReject:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Rejected

    old_dialog = main_mod.ExecuteConfirmDialog
    main_mod.ExecuteConfirmDialog = FakeReject
    try:
        win.on_execute()
        process(0.1)
    finally:
        main_mod.ExecuteConfirmDialog = old_dialog
    cancel_ok = (os.path.exists(src_a) and os.path.exists(src_b)
                 and sha256(src_a) == sha_a and sha256(src_b) == sha_b)

    class FakeAccept:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted

    main_mod.ExecuteConfirmDialog = FakeAccept
    try:
        win.on_execute()
        assert wait_until(lambda: not win._working, 30, "execute"), "执行超时"
    finally:
        main_mod.ExecuteConfirmDialog = old_dialog
    moved_a = item_a.target_rel_path
    moved_ok = (os.path.exists(os.path.join(root, moved_a))
                and not os.path.exists(src_a)
                and os.path.exists(src_b) and sha256(src_b) == sha_b)
    rec("T31", "取消确认两个都不动；确认后只移动勾选文件", cancel_ok and moved_ok,
        f"both_selected={both_selected} uncheck={uncheck_ok} "
        f"moved={moved_ok} cancel={cancel_ok}")

    storage = Storage(root)
    undo_result = undo_latest_batch(storage)
    undo_ok = (os.path.exists(src_a) and sha256(src_a) == sha_a
               and os.path.exists(src_b) and sha256(src_b) == sha_b
               and not os.path.exists(os.path.join(root, moved_a)))
    rec("T32", "撤销后勾选文件路径与 SHA-256 恢复，未勾选文件始终原位",
        bool(both_selected and uncheck_ok and undo_ok),
        f"restored={undo_result.restored} "
        f"conflicts={len(undo_result.conflicts)} sha_ok={sha256(src_a) == sha_a}")
    close_window(win)


def test_global_db_corruption_keeps_space_disabled():
    root = root_dir("global_corrupt_disabled")
    write(root, "下载/归档盒/停用.md")
    save_pref(root, "lt-disabled", "已停用的空间规则")
    # SQLite 停用；JSON 镜像故意保持 enabled=true，模拟镜像过期
    P.set_preference_enabled(root, "lt-disabled", False)
    cfg, _ = load_config(root)
    cfg.long_term_rules[0].enabled = True
    save_config(root, cfg)
    corrupt_global_db()
    try:
        win = preview(root)
        item = item_of(win.current_plan, "下载/归档盒/停用.md")
        ok = (not item.target_dir_rel.startswith(
                  os.path.join("_已整理", "项目"))
              and item.selected is False
              and item.rule_key != "long_term:lt-disabled")
        rec("T33", "全局库损坏时已停用空间规则不从 JSON 镜像复活",
            ok, f"target={item.target_rel_path} selected={item.selected} "
                f"rule_key={item.rule_key}")
        close_window(win)
    finally:
        restore_global_db()


def test_failed_save_is_atomic():
    root = root_dir("save_atomic")
    write(root, "下载/归档盒/原子.md")
    save_pref(root, "lt-atomic", "原子保存", category="归档盒")
    global_pref = P.Preference(
        preference_id="lt-global-atomic", name="全局原子", match_type=MATCH_DIR_SCOPE,
        source_path="下载/其他", include_subdirs=True, direction="项目",
        category="全局", inner_mode=INNER_CLEAR, enabled=True,
        scope=P.SCOPE_GLOBAL, confirmed_at="2026-11-15T00:00:00")
    P.save_preference(root, global_pref)
    corrupt_global_db()
    new_space = P.Preference(**{**P.list_space_preferences(root)[0].__dict__,
                                "category": "新目标"})
    try:
        P.persist_preferences(root, [new_space, global_pref], sync_json=True)
        raised = False
    except Exception:
        raised = True
    space_after = P.list_space_preferences(root)[0]
    json_after = [r for r in load_config(root)[0].long_term_rules
                  if r.rule_id == "lt-atomic"][0]
    restore_global_db()
    ok = (raised and space_after.category == "归档盒"
          and json_after.category == "归档盒")
    rec("T34", "保存报错后空间库与 JSON 都保持旧目标", ok,
        f"raised={raised} db={space_after.category} json={json_after.category}")


def test_concurrent_global_preference_not_deleted():
    root = root_dir("global_concurrent")
    first = P.Preference(
        preference_id="lt-global-first", name="先开的全局", match_type=MATCH_DIR_SCOPE,
        source_path="下载/A", include_subdirs=True, direction="项目",
        category="A", inner_mode=INNER_CLEAR, enabled=True,
        scope=P.SCOPE_GLOBAL, confirmed_at="2026-11-15T00:00:00")
    P.save_preference(root, first)
    snapshot = P.list_global_preferences()
    second = P.Preference(
        preference_id="lt-global-second", name="另一窗口新加", match_type=MATCH_DIR_SCOPE,
        source_path="下载/B", include_subdirs=True, direction="项目",
        category="B", inner_mode=INNER_CLEAR, enabled=True,
        scope=P.SCOPE_GLOBAL, confirmed_at="2026-11-15T00:00:01")
    P.save_preference(root, second)
    try:
        P.persist_preferences(root, snapshot, sync_json=False)
    except Exception:
        pass
    ids = {p.preference_id for p in P.list_global_preferences()}
    ok = {"lt-global-first", "lt-global-second"} <= ids
    rec("T35", "旧窗口保存不会删掉另一窗口刚写入的全局偏好", ok,
        f"global_ids={sorted(ids)}")


def test_scheme_apply_syncs_preferences():
    root = root_dir("scheme_apply")
    write(root, "下载/归档盒/方案.md")
    save_pref(root, "lt-scheme", "方案规则", category="归档盒")
    win = open_window(root)
    cfg, _ = load_config(root)
    rule = next(r for r in cfg.long_term_rules if r.rule_id == "lt-scheme")
    rule.category = "新方案目标"
    win._apply_scheme_config(cfg)
    close_window(win)
    rows = P.list_space_preferences(root)
    win2 = preview(root)
    item = item_of(win2.current_plan, "下载/归档盒/方案.md")
    close_window(win2)
    ok = (rows and rows[0].category == "新方案目标"
          and item.target_dir_rel == os.path.join(
              "_已整理", "项目", "新方案目标"))
    rec("T36", "应用含新长期规则的方案后正式预览使用新目标", ok,
        f"db={rows[0].category if rows else None} target={item.target_rel_path}")


def test_preferences_do_not_override_plain_modes():
    root = root_dir("plain_modes")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-plain", "只应作用于推荐模式", category="归档盒")
    results = {}
    for mode in (MODE_BY_TYPE, MODE_BY_COURSE_THEN_TYPE):
        win = open_window(root)
        idx = win.mode_combo.findData(mode)
        assert idx >= 0, mode
        win.mode_combo.setCurrentIndex(idx)
        scan_and_preview(win)
        item = item_of(win.current_plan, "下载/归档盒/样本.md")
        results[mode] = item.target_dir_rel
        close_window(win)
    ok = all(not path.startswith(os.path.join("_已整理", "项目"))
             for path in results.values())
    rec("T37", "「按类型」「按课程再按类型」不被长期偏好改到项目目录",
        ok, f"by_type={results.get(MODE_BY_TYPE)} "
            f"course_then_type={results.get(MODE_BY_COURSE_THEN_TYPE)}")


def test_preference_conflict_keeps_body_candidate():
    root = root_dir("conflict_body")
    write(root, "下载/混合.md", "高等数学 AIGC")
    save_pref(root, "lt-body-a", "冲突 A", category="归档盒", source="下载")
    save_pref(root, "lt-body-b", "冲突 B", category="课程设计", source="下载")
    win = preview(root)
    item = item_of(win.current_plan, "下载/混合.md")
    ambiguity = getattr(item, "ambiguity", None) or {}
    candidates = ambiguity.get("candidates") or []
    pref_ids = {c.get("preference_id") for c in candidates
                if c.get("preference_id")}
    evidence_text = " ".join(c.get("evidence") or "" for c in candidates)
    categories_text = " ".join(c.get("category") or "" for c in candidates)
    body_kept = ("高等数学" in evidence_text or "高等数学" in categories_text) and \
        ("AIGC" in evidence_text or "AIGC" in categories_text)
    ok = (bool(ambiguity) and item.selected is False
          and body_kept and {"lt-body-a", "lt-body-b"} <= pref_ids)
    rec("T38", "同级偏好冲突仍保留 AIGC 正文去向", ok,
        f"selected={item.selected} body_kept={body_kept} ids={sorted(pref_ids)} "
        f"候选={[c.get('evidence') for c in candidates]}")
    close_window(win)


def _pref(root: str, pid: str, name: str, category: str, *,
          source="下载/归档盒", scope=P.SCOPE_SPACE):
    return P.Preference(
        preference_id=pid, name=name, match_type=MATCH_DIR_SCOPE,
        source_path=source, include_subdirs=True, direction="项目",
        category=category, inner_mode=INNER_CLEAR, enabled=True,
        scope=scope,
        space_root=(root if scope == P.SCOPE_SPACE else ""),
        confirmed_at="2026-11-15T00:00:00")


def _replace_with_garbage(path: str):
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    with open(path, "wb") as f:
        f.write(b"this is not a sqlite database")


def test_single_save_failure_is_atomic():
    root = root_dir("single_save_atomic")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-old", "旧目标", category="归档盒")
    db = P.space_db_path(root)
    _replace_with_garbage(db)
    raised = False
    try:
        P.save_preference(root, _pref(
            root, "lt-old", "旧目标", "未成功保存的目标"))
    except Exception:
        raised = True
    json_rule = next(r for r in load_config(root)[0].long_term_rules
                     if r.rule_id == "lt-old")
    json_ok = json_rule.category == "归档盒"
    # 把失败前有效的库放回，重开预览必须仍按库里的旧目标
    os.replace(db + ".bak", db)
    win = preview(root)
    item = item_of(win.current_plan, "下载/归档盒/样本.md")
    old_ok = (item.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
              and item.rule_key == "long_term:lt-old"
              and item.selected is True)
    rec("T39", "单条保存失败不写 JSON，重开仍按库里的旧目标",
        raised and json_ok and old_ok,
        f"raised={raised} json={json_rule.category} "
        f"target={item.target_rel_path} rule_key={item.rule_key} "
        f"selected={item.selected}")
    close_window(win)

    # 没有旧规则、只保存一条新偏好：失败后 JSON 也不得写入
    root2 = root_dir("single_save_atomic_new")
    write(root2, "下载/归档盒/新样本.md")
    os.makedirs(os.path.dirname(P.space_db_path(root2)), exist_ok=True)
    _replace_with_garbage(P.space_db_path(root2))
    raised2 = False
    try:
        P.save_preference(root2, _pref(
            root2, "lt-new", "新目标", "未成功保存的目标"))
    except Exception:
        raised2 = True
    json_ids = [r.rule_id for r in load_config(root2)[0].long_term_rules]
    win2 = preview(root2)
    item2 = item_of(win2.current_plan, "下载/归档盒/新样本.md")
    new_ok = (raised2 and "lt-new" not in json_ids
              and item2.target_dir_rel != os.path.join(
                  "_已整理", "项目", "未成功保存的目标")
              and item2.rule_key != "long_term:lt-new")
    rec("T39.2", "没有旧规则时，失败的保存也不得让预览采用 JSON 新规则",
        new_ok,
        f"raised={raised2} json_ids={json_ids} "
        f"target={item2.target_rel_path} rule_key={item2.rule_key}")
    close_window(win2)


def test_space_db_corrupt_uses_global_only():
    root = root_dir("space_corrupt_global")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-space", "空间规则", category="空间目标")
    # 故意让 JSON 镜像停在另一个目标，验证预览不会回退 JSON
    cfg, _ = load_config(root)
    next(r for r in cfg.long_term_rules
         if r.rule_id == "lt-space").category = "JSON目标"
    save_config(root, cfg)
    P.save_preference(root, _pref(
        root, "lt-global", "全局规则", "全局目标", scope=P.SCOPE_GLOBAL))
    _replace_with_garbage(P.space_db_path(root))
    win = preview(root)
    item = item_of(win.current_plan, "下载/归档盒/样本.md")
    ok = (item.target_dir_rel == os.path.join("_已整理", "项目", "全局目标")
          and item.rule_key == "long_term:lt-global"
          and item.selected is True
          and "本空间偏好库不可用" in (win._preference_note or ""))
    rec("T40", "空间库打不开时只用全局偏好，不回退 JSON 空间规则",
        ok, f"target={item.target_rel_path} rule_key={item.rule_key} "
            f"selected={item.selected} note={win._preference_note}")
    close_window(win)


def test_concurrent_edit_is_not_overwritten():
    root = root_dir("concurrent_edit")
    first = _pref(root, "lt-shared", "共享", "旧目标",
                  scope=P.SCOPE_GLOBAL)
    P.save_preference(root, first)
    snapshot = P.list_global_preferences()
    P.save_preference(root, _pref(
        root, "lt-shared", "共享", "另一窗口新目标", scope=P.SCOPE_GLOBAL))
    P.save_preference(root, _pref(
        root, "lt-shared-new", "新增", "新增目标", scope=P.SCOPE_GLOBAL))
    # 本窗口没有改过这条：保留库里的新内容
    unchanged_exc = None
    try:
        P.persist_preferences(
            root, snapshot, sync_json=False,
            original_ids=["lt-shared"], baseline=snapshot)
    except Exception as e:
        unchanged_exc = e
    stored = {p.preference_id: p.category
              for p in P.list_global_preferences()}
    unchanged_ok = (unchanged_exc is None
                    and stored.get("lt-shared") == "另一窗口新目标"
                    and stored.get("lt-shared-new") == "新增目标")

    # 本窗口改成第三种内容：整次保存取消，库保持保存前
    changed = _pref(root, "lt-shared", "共享", "本窗口第三目标",
                    scope=P.SCOPE_GLOBAL)
    raised = False
    try:
        P.persist_preferences(
            root, [changed], sync_json=False,
            original_ids=["lt-shared"], baseline=snapshot)
    except Exception:
        raised = True
    stored2 = {p.preference_id: p.category
               for p in P.list_global_preferences()}
    conflict_ok = (raised and stored2.get("lt-shared") == "另一窗口新目标"
                   and stored2.get("lt-shared-new") == "新增目标")

    rec("T41", "旧窗口不覆盖另一窗口对同一条偏好的修改",
        unchanged_ok and conflict_ok,
        f"unchanged={stored} conflict={stored2} raised={raised}")


def test_restore_backup_rolls_back_json_on_sync_failure():
    def setup(tag):
        root = root_dir(tag)
        write(root, "恢复专用/恢复.md")
        save_pref(root, "lt-restore", "恢复规则", category="归档盒",
                  source="恢复专用")
        create_backup(root)          # 备份 = 归档盒
        P.save_preference(root, _pref(
            root, "lt-restore", "恢复规则", "新方案目标"))
        return root

    root = setup("restore_rollback")
    win = open_window(root)
    real_sync = main_mod.sync_space_preferences_from_config
    main_mod.sync_space_preferences_from_config = (
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟同步失败")))
    raised = False
    try:
        win._restore_last_config()
    except Exception:
        raised = True
    finally:
        main_mod.sync_space_preferences_from_config = real_sync
    json_cat = load_config(root)[0].long_term_rules[0].category
    db_cat = P.list_space_preferences(root)[0].category
    status = win.statusBar().currentMessage()
    rollback_ok = (raised and json_cat == "新方案目标"
                   and db_cat == "新方案目标" and "已恢复" not in status)
    close_window(win)
    rec("T42", "恢复备份同步失败时 JSON 回到恢复前，且不提示已恢复",
        rollback_ok,
        f"raised={raised} json={json_cat} db={db_cat} status={status!r}")

    root2 = setup("restore_success")
    win2 = open_window(root2)
    message = win2._restore_last_config()
    close_window(win2)
    json_cat2 = load_config(root2)[0].long_term_rules[0].category
    db_cat2 = P.list_space_preferences(root2)[0].category
    win3 = preview(root2)
    item = item_of(win3.current_plan, "恢复专用/恢复.md")
    close_window(win3)
    success_ok = (json_cat2 == "归档盒" and db_cat2 == "归档盒"
                  and item.target_dir_rel == os.path.join(
                      "_已整理", "项目", "归档盒"))
    rec("T42.2", "恢复成功时 JSON 与库一起回到备份目标",
        success_ok,
        f"message={message!r} json={json_cat2} db={db_cat2} "
        f"target={item.target_rel_path}")


class isolated_app_dir:
    """临时换一个应用数据目录，避免前面的用例遗留的全局偏好干扰本轮预览。"""

    def __enter__(self):
        self._old = os.environ.get("ARCHIVEBOX_APP_DIR")
        self.path = tempfile.mkdtemp(prefix="stage2_iso_app_")
        os.environ["ARCHIVEBOX_APP_DIR"] = self.path
        return self.path

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("ARCHIVEBOX_APP_DIR", None)
        else:
            os.environ["ARCHIVEBOX_APP_DIR"] = self._old


def test_global_commit_failure_rolls_back_space_db():
    """问题 1：全局库提交失败时，空间库不得留下新目标。"""
    with isolated_app_dir():
        _global_commit_failure_case()


def _global_commit_failure_case():
    root = root_dir("global_commit_fail")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-space", "空间偏好", category="归档盒")
    P.save_preference(root, _pref(
        root, "lt-global", "全局偏好", "全局旧目标",
        source="下载/其他", scope=P.SCOPE_GLOBAL))
    win = open_window(root)

    lock = {}
    real_apply = P._apply_rows

    def apply_and_lock(conn, prefs, delete_ids):
        # 在全局库事务写入前，用第二个连接 BEGIN 并一直持有读事务；
        # 这样全局库 commit 会因 database is locked 失败。
        rows = conn.execute("PRAGMA database_list").fetchall()
        db_path = next((row[2] for row in rows if row[1] == "main"), "")
        if (os.path.realpath(db_path) == os.path.realpath(P.global_db_path())
                and "conn" not in lock):
            holder = sqlite3.connect(P.global_db_path(), timeout=10)
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM preferences").fetchall()
            lock["conn"] = holder
        return real_apply(conn, prefs, delete_ids)

    def mutate(dlg):
        r1 = config_row(dlg, "lt-space")
        dlg.table_longterm.item(r1, 8).setText("空间新目标")
        r2 = config_row(dlg, "lt-global")
        dlg.table_longterm.item(r2, 8).setText("全局新目标")

    P._apply_rows = apply_and_lock
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        P._apply_rows = real_apply
        holder = lock.get("conn")
        if holder is not None:
            try:
                holder.rollback()
            except Exception:
                pass
            holder.close()

    space_cat = next((p.category for p in P.list_space_preferences(root)
                      if p.preference_id == "lt-space"), None)
    global_cat = next((p.category for p in P.list_global_preferences()
                       if p.preference_id == "lt-global"), None)
    json_cats = {r.rule_id: r.category
                 for r in load_config(root)[0].long_term_rules}
    msg = " ".join(cap["errors"])
    db_ok = (space_cat == "归档盒" and global_cat == "全局旧目标"
             and json_cats.get("lt-space") == "归档盒")
    rollback_named = "已整体回滚" in msg
    close_window(win)

    win2 = preview(root)
    item = item_of(win2.current_plan, "下载/归档盒/样本.md")
    preview_ok = (
        item.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
        and item.rule_key == "long_term:lt-space"
        and item.selected is True)
    close_window(win2)
    rec("T43", "全局库提交失败后空间库/全局库/JSON 都回到保存前",
        bool(cap["errors"]) and not cap["accepted"] and db_ok
        and rollback_named and preview_ok,
        f"space={space_cat} global={global_cat} json={json_cats.get('lt-space')} "
        f"target={item.target_rel_path} rule_key={item.rule_key} "
        f"selected={item.selected} msg={msg[:80]}")


def test_config_page_keeps_reason_and_avoids_false_conflict():
    """问题 2：配置页直接保存不得清空 reason，也不得误报并发冲突。"""
    root = root_dir("reason_preserved")
    write(root, "下载/归档盒/说明.md")
    save_pref(root, "lt-reason", "手动修正规则", reason="由手动修正生成")
    win = open_window(root)
    cap1 = save_via_config_page(win)
    close_window(win)
    row1 = next((p for p in P.list_space_preferences(root)
                 if p.preference_id == "lt-reason"), None)
    json1 = next((r for r in load_config(root)[0].long_term_rules
                  if r.rule_id == "lt-reason"), None)
    unchanged_ok = (not cap1["errors"] and cap1["accepted"]
                    and row1 is not None and row1.reason == "由手动修正生成"
                    and row1.category == "归档盒"
                    and json1 is not None and json1.reason == "由手动修正生成")

    win2 = open_window(root)
    other = P.Preference(**{
        **next(p for p in P.list_space_preferences(root)
               if p.preference_id == "lt-reason").__dict__,
        "category": "另一窗口新目标"})

    def mutate(dlg):
        # 打开配置页之后，另一窗口改了同一目标
        P.save_preference(root, other)

    cap2 = save_via_config_page(win2, mutate)
    close_window(win2)
    row2 = next((p for p in P.list_space_preferences(root)
                 if p.preference_id == "lt-reason"), None)
    json2 = next((r for r in load_config(root)[0].long_term_rules
                  if r.rule_id == "lt-reason"), None)
    concurrent_ok = (not cap2["errors"] and cap2["accepted"]
                     and row2 is not None and row2.category == "另一窗口新目标"
                     and row2.reason == "由手动修正生成"
                     and json2 is not None
                     and json2.category == "另一窗口新目标"
                     and json2.reason == "由手动修正生成")
    rec("T44", "配置页一项不改保存时保留 reason 且不误报并发冲突",
        unchanged_ok and concurrent_ok,
        f"unchanged={unchanged_ok} reason1={getattr(row1, 'reason', None)} "
        f"json1={getattr(json1, 'reason', None)} concurrent={concurrent_ok} "
        f"cat2={getattr(row2, 'category', None)} "
        f"reason2={getattr(row2, 'reason', None)} errors={cap2['errors']}")


def test_scope_change_moves_preference_between_dbs():
    """问题 3：配置页可把空间偏好改成全局，或把全局改成空间。"""
    with isolated_app_dir():
        _scope_change_case()


def _scope_change_case():
    root = root_dir("scope_migrate_out")
    write(root, "下载/归档盒/迁移.md")
    save_pref(root, "lt-move", "迁移规则", category="归档盒")
    win = open_window(root)

    def to_global(dlg):
        r = config_row(dlg, "lt-move")
        dlg.table_longterm.item(r, 11).setText(
            P.SCOPE_LABELS[P.SCOPE_GLOBAL])

    cap1 = save_via_config_page(win, to_global)
    close_window(win)
    space1 = P.list_space_preferences(root)
    global1 = P.list_global_preferences()
    json_ids1 = [r.rule_id for r in load_config(root)[0].long_term_rules]
    moved_global = (
        not cap1["errors"]
        and not any(p.preference_id == "lt-move" for p in space1)
        and any(p.preference_id == "lt-move" and p.category == "归档盒"
                for p in global1)
        and "lt-move" not in json_ids1)
    win2 = preview(root)
    item1 = item_of(win2.current_plan, "下载/归档盒/迁移.md")
    preview_ok = (
        item1.target_dir_rel == os.path.join("_已整理", "项目", "归档盒")
        and item1.rule_key == "long_term:lt-move"
        and item1.selected is True)
    close_window(win2)

    root2 = root_dir("scope_migrate_in")
    write(root2, "下载/归档盒/迁移2.md")
    P.save_preference(root2, _pref(
        root2, "lt-move2", "迁移规则2", "归档盒", scope=P.SCOPE_GLOBAL))
    win3 = open_window(root2)

    def to_space(dlg):
        r = config_row(dlg, "lt-move2")
        dlg.table_longterm.item(r, 11).setText(
            P.SCOPE_LABELS[P.SCOPE_SPACE])

    cap2 = save_via_config_page(win3, to_space)
    close_window(win3)
    space2 = P.list_space_preferences(root2)
    global2 = P.list_global_preferences()
    json2 = {r.rule_id: r.category
             for r in load_config(root2)[0].long_term_rules}
    moved_space = (
        not cap2["errors"]
        and not any(p.preference_id == "lt-move2" for p in global2)
        and any(p.preference_id == "lt-move2" and p.category == "归档盒"
                for p in space2)
        and json2.get("lt-move2") == "归档盒")

    root3 = root_dir("scope_migrate_conflict")
    write(root3, "下载/归档盒/迁移3.md")
    save_pref(root3, "lt-move3", "迁移规则3", category="归档盒")
    win4 = open_window(root3)

    def conflict(dlg):
        current = next(p for p in P.list_space_preferences(root3)
                       if p.preference_id == "lt-move3")
        # 打开配置页之后，另一窗口改了原行
        P.save_preference(root3, P.Preference(
            **{**current.__dict__, "category": "别处改过"}))
        r = config_row(dlg, "lt-move3")
        dlg.table_longterm.item(r, 11).setText(
            P.SCOPE_LABELS[P.SCOPE_GLOBAL])

    cap3 = save_via_config_page(win4, conflict)
    close_window(win4)
    space3 = P.list_space_preferences(root3)
    global3 = P.list_global_preferences()
    conflict_ok = (
        bool(cap3["errors"]) and not cap3["accepted"]
        and any(p.preference_id == "lt-move3" and p.category == "别处改过"
                for p in space3)
        and not any(p.preference_id == "lt-move3" for p in global3))
    rec("T45", "改作用范围会搬迁原行；原行被别处改过时整次取消",
        moved_global and preview_ok and moved_space and conflict_ok,
        f"global={moved_global} preview={item1.target_rel_path} "
        f"space={moved_space} conflict={conflict_ok} "
        f"errors={cap3['errors']}")


def test_corrupt_json_can_be_repaired_and_rolled_back_from_config_page():
    """问题 4：配置 JSON 损坏时「保存并应用」仍能修好；失败时放回原字节。"""
    root = root_dir("corrupt_json_repair")
    write(root, "下载/归档盒/修复.md")
    path = config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bad = b'{ this is not valid json'
    with open(path, "wb") as f:
        f.write(bad)
    win = open_window(root)
    had_error = bool(win._config_error)
    with yes_answer():
        cap = save_via_config_page(win)
    close_window(win)
    cfg_ok = True
    try:
        load_config(root)
    except Exception:
        cfg_ok = False
    repaired = (had_error and not cap["errors"] and cap["accepted"]
                and cfg_ok and not win._config_error)
    rec("T46", "配置 JSON 损坏时仍可从配置页保存修好", repaired,
        f"had_error={had_error} accepted={cap['accepted']} "
        f"cfg_ok={cfg_ok} errors={cap['errors']}")

    root2 = root_dir("corrupt_json_rollback")
    write(root2, "下载/归档盒/修复2.md")
    P._open(P.global_db_path()).close()
    path2 = config_path(root2)
    os.makedirs(os.path.dirname(path2), exist_ok=True)
    bad2 = b'{ this is not valid json either'
    with open(path2, "wb") as f:
        f.write(bad2)
    win2 = open_window(root2)

    real_open = P._open

    class _FailCommit:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    def fake_open(db_path):
        conn = real_open(db_path)
        if os.path.realpath(db_path) == os.path.realpath(P.global_db_path()):
            return _FailCommit(conn)
        return conn

    P._open = fake_open
    try:
        with yes_answer():
            cap2 = save_via_config_page(win2)
    finally:
        P._open = real_open
    close_window(win2)
    with open(path2, "rb") as f:
        raw_after = f.read()
    still_corrupt = False
    try:
        load_config(root2)
    except Exception:
        still_corrupt = True
    msg2 = " ".join(cap2["errors"])
    rollback_ok = (bool(cap2["errors"]) and not cap2["accepted"]
                   and raw_after == bad2 and still_corrupt
                   and "已整体回滚" in msg2)
    rec("T46.2", "偏好库失败时把损坏的原文件字节放回，不留半份新配置",
        rollback_ok,
        f"raw_same={raw_after == bad2} still_corrupt={still_corrupt} "
        f"msg={msg2[:80]}")


def test_restore_retry_keeps_previous_target_backup():
    """问题 5：恢复失败后重试必须仍恢复原来那一份。"""
    root = root_dir("restore_retry")
    write(root, "恢复专用/重试.md")
    P.save_preference(root, _pref(
        root, "lt-retry", "重试规则", "归档盒", source="恢复专用"))
    create_backup(root)          # 更早的备份 = 归档盒（用户要恢复的那一份）
    P.save_preference(root, _pref(
        root, "lt-retry", "重试规则", "当前目标", source="恢复专用"))
    backups_before = len(list_backups(root))

    db = P.space_db_path(root)
    _replace_with_garbage(db)
    win = open_window(root)
    raised = False
    try:
        win._restore_last_config()
    except Exception:
        raised = True
    status1 = win.statusBar().currentMessage()
    json1 = load_config(root)[0].long_term_rules[0].category
    backups_after = len(list_backups(root))
    os.replace(db + ".bak", db)
    db1 = P.list_space_preferences(root)[0].category
    rollback_ok = (raised and json1 == "当前目标" and db1 == "当前目标"
                   and backups_after == backups_before
                   and "已恢复" not in status1)

    message = win._restore_last_config()
    close_window(win)
    json2 = load_config(root)[0].long_term_rules[0].category
    db2 = P.list_space_preferences(root)[0].category
    win2 = preview(root)
    item = item_of(win2.current_plan, "恢复专用/重试.md")
    close_window(win2)
    retry_ok = (json2 == "归档盒" and db2 == "归档盒"
                and item.target_dir_rel == os.path.join(
                    "_已整理", "项目", "归档盒")
                and "已恢复" in message)
    rec("T47", "恢复失败删掉本次新备份，修好库后重试恢复原目标",
        rollback_ok and retry_ok,
        f"json1={json1} db1={db1} backups={backups_before}->{backups_after} "
        f"status1={status1!r} json2={json2} db2={db2} "
        f"target={item.target_rel_path} message={message.splitlines()[0]!r}")


def test_single_preference_keeps_body_candidates():
    """问题 6：一条偏好命中时也要留下全部正文候选。"""
    root = root_dir("single_pref_body")
    write(root, "下载/混合.md", "高等数学 AIGC")
    save_pref(root, "lt-body-single", "单条项目偏好",
              category="归档盒", source="下载")
    win = preview(root)
    item = item_of(win.current_plan, "下载/混合.md")
    candidates = (getattr(item, "ambiguity", None) or {}).get(
        "candidates") or []
    text_all = " ".join(
        f"{c.get('category') or ''} {c.get('evidence') or ''}"
        for c in candidates)
    body_kept = "高等数学" in text_all and "AIGC" in text_all
    pref_first = bool(candidates) and \
        candidates[0].get("preference_id") == "lt-body-single"
    auto_untouched = (
        item.auto_rule_key != "long_term:lt-body-single"
        and item.auto_category != "归档盒"
        and item.auto_rule_key.startswith("keyword_unmatched"))
    main_ok = (bool(candidates) and item.selected is False
               and body_kept and pref_first and auto_untouched)
    math_idx = next((i for i, c in enumerate(candidates)
                     if "高等数学" in f"{c.get('category') or ''} "
                                     f"{c.get('evidence') or ''}"), None)
    select_ok = False
    if math_idx is not None:
        win.apply_ambiguity_choice("下载/混合.md", math_idx)
        process()
        chosen = item_of(win.current_plan, "下载/混合.md")
        select_ok = (chosen.selected is True
                     and "高等数学" in chosen.target_rel_path)
    rec("T48", "单条偏好命中时保留高等数学与 AIGC 正文候选且不勾选",
        main_ok and select_ok,
        f"selected={item.selected} candidates="
        f"{[(c.get('category'), c.get('preference_id')) for c in candidates]} "
        f"auto={item.auto_rule_key} selectable={select_ok}")
    close_window(win)


def test_rollback_does_not_erase_newer_save():
    """问题 1：A 失败回滚不得抹掉 B 在等待期间已保存的结果。"""
    with isolated_app_dir():
        _rollback_newer_case()


def _db_path_of(conn) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    return next((row[2] for row in rows if row[1] == "main"), "")


def _rollback_newer_case():
    root = root_dir("rollback_newer")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-space", "空间偏好", category="原空间目标")
    P.save_preference(root, _pref(
        root, "lt-global", "全局偏好", "全局旧目标",
        source="下载/其他", scope=P.SCOPE_GLOBAL))
    win = open_window(root)

    state = {}
    real_apply = P._apply_rows

    def apply_and_interleave(conn, prefs, delete_ids):
        # 全局库事务写入前：用第二个连接持有读事务，让 A 的全局提交失败；
        # 同时让 B 在 A 等待全局库时保存同一条空间偏好。
        if (os.path.realpath(_db_path_of(conn))
                == os.path.realpath(P.global_db_path())
                and "holder" not in state):
            holder = sqlite3.connect(P.global_db_path(), timeout=10)
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM preferences").fetchall()
            state["holder"] = holder

            def b_save():
                time.sleep(1.0)
                try:
                    P.save_preference(root, _pref(
                        root, "lt-space", "空间偏好", "B 已保存的空间目标"))
                    state["b_ok"] = True
                except Exception as e:                # 记录，供断言输出
                    state["b_error"] = e

            thread = threading.Thread(target=b_save)
            thread.start()
            state["thread"] = thread
        return real_apply(conn, prefs, delete_ids)

    def mutate(dlg):
        dlg.table_longterm.item(
            config_row(dlg, "lt-space"), 8).setText("A 改的空间目标")
        dlg.table_longterm.item(
            config_row(dlg, "lt-global"), 8).setText("A 改的全局目标")

    P._apply_rows = apply_and_interleave
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        P._apply_rows = real_apply
        thread = state.get("thread")
        if thread is not None:
            thread.join(20)
        holder = state.get("holder")
        if holder is not None:
            try:
                holder.rollback()
            except Exception:
                pass
            holder.close()

    space_cat = next((p.category for p in P.list_space_preferences(root)
                      if p.preference_id == "lt-space"), None)
    global_cat = next((p.category for p in P.list_global_preferences()
                       if p.preference_id == "lt-global"), None)
    json_cat = next((r.category for r in load_config(root)[0].long_term_rules
                     if r.rule_id == "lt-space"), None)
    msg = " ".join(cap["errors"])
    close_window(win)
    win2 = preview(root)
    item = item_of(win2.current_plan, "下载/归档盒/样本.md")
    close_window(win2)
    ok = (bool(cap["errors"]) and not cap["accepted"]
          and space_cat == "B 已保存的空间目标"
          and json_cat == "B 已保存的空间目标"
          and global_cat == "全局旧目标"
          and "已整体回滚" not in msg
          and "保留了其他窗口的较新内容" in msg
          and item.target_dir_rel == os.path.join(
              "_已整理", "项目", "B 已保存的空间目标")
          and item.rule_key == "long_term:lt-space"
          and item.selected is True)
    rec("T49", "失败回滚保留另一窗口已保存的空间目标与 JSON", ok,
        f"space={space_cat} json={json_cat} global={global_cat} "
        f"target={item.target_rel_path} rule_key={item.rule_key} "
        f"selected={item.selected} b_ok={state.get('b_ok')} "
        f"b_error={state.get('b_error')} msg={msg[:100]}")


def test_write_recheck_rejects_newer_save():
    """问题 2：并发检查通过后、写入前被别处保存，必须整次取消。"""
    with isolated_app_dir():
        _write_recheck_case()


def _write_recheck_case():
    root = root_dir("recheck_newer")
    write(root, "下载/归档盒/样本.md")
    P.save_preference(root, _pref(
        root, "lt-g", "全局共享", "旧目标", scope=P.SCOPE_GLOBAL))
    win = open_window(root)
    state = {}
    real_begin = getattr(P, "_begin_write", None)
    real_apply = P._apply_rows

    def is_global(conn) -> bool:
        return (os.path.realpath(_db_path_of(conn))
                == os.path.realpath(P.global_db_path()))

    def b_save():
        if "done" in state:
            return
        state["done"] = True
        P.save_preference(root, _pref(
            root, "lt-g", "全局共享", "B 已确认的新目标",
            scope=P.SCOPE_GLOBAL))
        state["b_ok"] = True

    def mutate(dlg):
        dlg.table_longterm.item(
            config_row(dlg, "lt-g"), 8).setText("A 从旧快照提交的全局目标")

    if real_begin is not None:
        # 修复后的实现：A 已读完并完成检查，还没开始写事务时让 B 保存
        def begin(conn):
            if is_global(conn):
                b_save()
            return real_begin(conn)

        P._begin_write = begin
    else:
        # 696f727 上没有 _begin_write：在全局行写入前插入 B 的保存
        def apply(conn, prefs, delete_ids):
            if is_global(conn):
                b_save()
            return real_apply(conn, prefs, delete_ids)

        P._apply_rows = apply
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        if real_begin is not None:
            P._begin_write = real_begin
        P._apply_rows = real_apply

    stored = next((p.category for p in P.list_global_preferences()
                   if p.preference_id == "lt-g"), None)
    msg = " ".join(cap["errors"])
    close_window(win)
    win2 = preview(root)
    item = item_of(win2.current_plan, "下载/归档盒/样本.md")
    close_window(win2)
    ok = (bool(cap["errors"]) and not cap["accepted"]
          and "已在其他窗口被修改" in msg
          and stored == "B 已确认的新目标"
          and item.target_dir_rel == os.path.join(
              "_已整理", "项目", "B 已确认的新目标")
          and item.selected is True)
    rec("T50", "写入前重读发现较新保存时整次取消并保留 B 的目标", ok,
        f"global={stored} b_ok={state.get('b_ok')} "
        f"target={item.target_rel_path} selected={item.selected} "
        f"msg={msg[:100]}")


def test_scope_change_fails_when_target_db_unavailable():
    """问题 3：目标库不可用时改成全局不得假装保存成功。"""
    with isolated_app_dir():
        _scope_unavailable_case()


def _scope_unavailable_case():
    root = root_dir("scope_target_unavailable")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-u", "范围规则", category="原目标")
    corrupt_global_db()
    try:
        win = open_window(root)

        def to_global(dlg):
            dlg.table_longterm.item(
                config_row(dlg, "lt-u"), 11).setText(
                    P.SCOPE_LABELS[P.SCOPE_GLOBAL])

        cap = save_via_config_page(win, to_global)
        close_window(win)
    finally:
        restore_global_db()
    space_cat = next((p.category for p in P.list_space_preferences(root)
                      if p.preference_id == "lt-u"), None)
    json_cat = next((r.category for r in load_config(root)[0].long_term_rules
                     if r.rule_id == "lt-u"), None)
    msg = " ".join(cap["errors"])
    failed_ok = (bool(cap["errors"]) and not cap["accepted"]
                 and "全局偏好库不可用" in msg
                 and space_cat == "原目标" and json_cat == "原目标")
    win2 = preview(root)
    item = item_of(win2.current_plan, "下载/归档盒/样本.md")
    close_window(win2)
    preview_ok = (
        item.target_dir_rel == os.path.join("_已整理", "项目", "原目标")
        and item.rule_key == "long_term:lt-u"
        and item.selected is True)

    # 反向：空间库不可用时把全局改回空间，也必须整次失败
    root2 = root_dir("scope_source_unavailable")
    write(root2, "下载/归档盒/样本2.md")
    P.save_preference(root2, _pref(
        root2, "lt-u2", "范围规则2", "全局目标", scope=P.SCOPE_GLOBAL))
    db2 = P.space_db_path(root2)
    P._open(db2).close()
    _replace_with_garbage(db2)
    win3 = open_window(root2)

    def to_space(dlg):
        dlg.table_longterm.item(
            config_row(dlg, "lt-u2"), 11).setText(
                P.SCOPE_LABELS[P.SCOPE_SPACE])

    cap2 = save_via_config_page(win3, to_space)
    close_window(win3)
    os.replace(db2 + ".bak", db2)
    global_cat2 = next((p.category for p in P.list_global_preferences()
                        if p.preference_id == "lt-u2"), None)
    space_ids2 = [p.preference_id for p in P.list_space_preferences(root2)]
    reverse_ok = (bool(cap2["errors"]) and not cap2["accepted"]
                  and global_cat2 == "全局目标"
                  and "lt-u2" not in space_ids2)
    rec("T51", "目标库不可用时改范围整次失败，范围与预览保持保存前",
        failed_ok and preview_ok and reverse_ok,
        f"space={space_cat} json={json_cat} target={item.target_rel_path} "
        f"selected={item.selected} msg={msg[:80]} reverse={reverse_ok}")


def test_mixed_compensation_keeps_json_and_space_db_in_sync():
    """问题：一条规则保留 B、另一条已恢复时，JSON 与空间库不得一旧一新。"""
    with isolated_app_dir():
        _mixed_compensation_case()


def _mixed_compensation_case():
    root = root_dir("mixed_compensation")
    write(root, "下载/归档盒/样本.md")
    write(root, "资料/另一份.md")
    mapping_sample = "独立归档源/普通笔记.md"
    write(root, mapping_sample)
    save_pref(root, "lt-space-1", "空间一", category="空间一旧目标")
    P.save_preference(root, P.Preference(
        preference_id="lt-space-2", name="空间二",
        match_type=MATCH_EXACT_PATH,
        source_path="资料/另一份.md", include_subdirs=False,
        direction="项目", category="空间二旧目标", inner_mode=INNER_CLEAR,
        enabled=True, scope=P.SCOPE_SPACE, space_root=root,
        confirmed_at="2026-11-15T00:00:00",
        confirmed_source_rel="资料/另一份.md"))
    P.save_preference(root, _pref(
        root, "lt-global", "全局", "全局旧目标",
        source="下载/其他", scope=P.SCOPE_GLOBAL))
    win = open_window(root)

    state = {}
    real_apply = P._apply_rows

    def apply_and_interleave(conn, prefs, delete_ids):
        if (os.path.realpath(_db_path_of(conn))
                == os.path.realpath(P.global_db_path())
                and "holder" not in state):
            holder = sqlite3.connect(P.global_db_path(), timeout=10)
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM preferences").fetchall()
            state["holder"] = holder

            def b_save():
                time.sleep(1.0)
                try:
                    P.save_preference(root, _pref(
                        root, "lt-space-1", "空间一", "B 已保存的空间目标"))
                    state["b_ok"] = True
                except Exception as e:
                    state["b_error"] = e

            thread = threading.Thread(target=b_save)
            thread.start()
            state["thread"] = thread
        return real_apply(conn, prefs, delete_ids)

    def mutate(dlg):
        mapping = dlg.table_top
        row = next(r for r in range(mapping.rowCount())
                   if mapping.item(r, 1).text() == "项目")
        mapping.item(row, 1).setText("独立归档源")
        dlg.table_longterm.item(config_row(dlg, "lt-space-1"), 8).setText(
            "A 空间一未完成的新目标")
        dlg.table_longterm.item(config_row(dlg, "lt-space-2"), 8).setText(
            "A 空间二未完成的新目标")
        dlg.table_longterm.item(config_row(dlg, "lt-global"), 8).setText(
            "A 全局未完成的新目标")

    P._apply_rows = apply_and_interleave
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        P._apply_rows = real_apply
        thread = state.get("thread")
        if thread is not None:
            thread.join(20)
        holder = state.get("holder")
        if holder is not None:
            try:
                holder.rollback()
            except Exception:
                pass
            holder.close()
    close_window(win)

    space = {p.preference_id: p.category
             for p in P.list_space_preferences(root)}
    global_cat = next((p.category for p in P.list_global_preferences()
                       if p.preference_id == "lt-global"), None)
    json_rules = {r.rule_id: r.category
                  for r in load_config(root)[0].long_term_rules}
    msg = " ".join(cap["errors"])
    stores_ok = (
        space.get("lt-space-1") == "B 已保存的空间目标"
        and space.get("lt-space-2") == "空间二旧目标"
        and json_rules.get("lt-space-1") == "B 已保存的空间目标"
        and json_rules.get("lt-space-2") == "空间二旧目标"
        and "A 空间二未完成的新目标" not in json_rules.values()
        and global_cat == "全局旧目标")
    msg_ok = (bool(cap["errors"]) and not cap["accepted"]
              and "已整体回滚" not in msg
              and "保留了其他窗口的较新内容" in msg
              and "lt-space-1" in msg and "lt-space-2" in msg)

    win2 = preview(root)
    scanner = win2.scan_result
    mapping_item = item_of(win2.current_plan, mapping_sample)
    mapping_sources = {r.source for r in load_config(root)[0].top_direction_rules}
    rec("T52.2", "全局提交失败时普通分类映射也恢复，保留 B 的空间偏好",
        stores_ok and msg_ok and "独立归档源" not in mapping_sources
        and "项目" in mapping_sources and mapping_item.rule_key == "fallback"
        and not mapping_item.selected,
        f"target={mapping_item.target_rel_path} selected={mapping_item.selected} "
        f"B={space.get('lt-space-1')}")
    item1 = item_of(win2.current_plan, "下载/归档盒/样本.md")
    item2 = item_of(win2.current_plan, "资料/另一份.md")
    official_ok = (
        item1.target_dir_rel == os.path.join(
            "_已整理", "项目", "B 已保存的空间目标")
        and item1.rule_key == "long_term:lt-space-1" and item1.selected is True
        and item2.target_dir_rel == os.path.join(
            "_已整理", "项目", "空间二旧目标")
        and item2.rule_key == "long_term:lt-space-2" and item2.selected is True)

    json_cfg = load_config(root)[0]
    json_plan = build_plan(
        scanner, MODE_BY_DIRECTION_SEMESTER, [], config=json_cfg,
        compute_duplicates=False)
    json_item = item_of(json_plan, "资料/另一份.md")
    dry_run_ok = (
        json_item.target_dir_rel == os.path.join(
            "_已整理", "项目", "空间二旧目标")
        and json_item.rule_key == "long_term:lt-space-2")

    export_path = os.path.join(tempfile.mkdtemp(prefix="stage2_export_"),
                               "scheme.json")
    scheme_store.export_current_config(export_path, json_cfg, "导出方案")
    with open(export_path, "r", encoding="utf-8") as f:
        exported = f.read()
    export_ok = ("A 空间二未完成的新目标" not in exported
                 and "空间二旧目标" in exported)

    # 真实入口：应用默认方案 → 恢复这次应用刚生成的备份
    win2._apply_scheme_config(default_config())
    restore_message = win2._restore_last_config()
    close_window(win2)
    win3 = preview(root)
    restored_item = item_of(win3.current_plan, "资料/另一份.md")
    close_window(win3)
    restore_ok = (
        "A 空间二未完成的新目标" not in restored_item.target_rel_path
        and restored_item.target_dir_rel == os.path.join(
            "_已整理", "项目", "空间二旧目标")
        and restored_item.rule_key == "long_term:lt-space-2"
        and restored_item.selected is True
        and "已恢复" in restore_message)
    rec("T52", "多规则失败补偿时 JSON 与空间库逐条一致，恢复备份后不复活被取消目标",
        stores_ok and msg_ok and official_ok and dry_run_ok and export_ok
        and restore_ok,
        f"space1={space.get('lt-space-1')} space2={space.get('lt-space-2')} "
        f"json1={json_rules.get('lt-space-1')} "
        f"json2={json_rules.get('lt-space-2')} global={global_cat} "
        f"preview2={item2.target_rel_path} dry={json_item.target_rel_path} "
        f"restored={restored_item.target_rel_path} "
        f"b_ok={state.get('b_ok')} msg={msg[:120]}")


def test_failed_scope_migration_never_writes_global_rule_into_space_json():
    """全局改当前空间失败时，配置页快照里的全局规则不得写进空间 JSON。"""
    with isolated_app_dir():
        _global_to_space_failure_case()
        _space_to_global_failure_case()
        _global_to_space_failure_without_other_window()


def _exact_pref(root: str, pid: str, name: str, rel: str, category: str,
                scope: str = P.SCOPE_SPACE) -> P.Preference:
    return P.Preference(
        preference_id=pid, name=name, match_type=MATCH_EXACT_PATH,
        source_path=rel, include_subdirs=False, direction="项目",
        category=category, inner_mode=INNER_CLEAR, enabled=True,
        scope=scope, space_root=(root if scope == P.SCOPE_SPACE else ""),
        confirmed_at="2026-11-15T00:00:00",
        confirmed_source_rel=rel)


def _interleave_b_save(root: str, pid: str, category: str):
    """返回 (包装后的 _apply_rows, 收尾函数)：全局库读锁 + B 线程保存。"""
    state = {}
    real_apply = P._apply_rows

    def apply_and_interleave(conn, prefs, delete_ids):
        if (os.path.realpath(_db_path_of(conn))
                == os.path.realpath(P.global_db_path())
                and "holder" not in state):
            holder = sqlite3.connect(P.global_db_path(), timeout=10)
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM preferences").fetchall()
            state["holder"] = holder

            def b_save():
                time.sleep(1.0)
                try:
                    current = next(p for p in P.list_space_preferences(root)
                                   if p.preference_id == pid)
                    P.save_preference(root, P.Preference(
                        **{**current.__dict__, "category": category}))
                    state["b_ok"] = True
                except Exception as e:
                    state["b_error"] = e

            thread = threading.Thread(target=b_save)
            thread.start()
            state["thread"] = thread
        return real_apply(conn, prefs, delete_ids)

    def cleanup():
        P._apply_rows = real_apply
        thread = state.get("thread")
        if thread is not None:
            thread.join(20)
        holder = state.get("holder")
        if holder is not None:
            try:
                holder.rollback()
            except Exception:
                pass
            holder.close()

    return apply_and_interleave, cleanup, state


def _global_to_space_failure_case():
    root = root_dir("global_to_space_failure")
    write(root, "下载/归档盒/样本.md")
    write(root, "下载/归档盒/另一份.md")
    P.save_preference(root, _exact_pref(
        root, "lt-space-1", "空间一", "下载/归档盒/样本.md", "空间一旧目标"))
    P.save_preference(root, _pref(
        root, "lt-global", "全局", "全局旧目标",
        source="下载/归档盒", scope=P.SCOPE_GLOBAL))
    win = open_window(root)

    def mutate(dlg):
        dlg.table_longterm.item(config_row(dlg, "lt-global"), 11).setText(
            P.SCOPE_LABELS[P.SCOPE_SPACE])

    apply_lock, cleanup, state = _interleave_b_save(
        root, "lt-space-1", "B 已保存的空间目标")
    P._apply_rows = apply_lock
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        cleanup()
    close_window(win)

    space = {p.preference_id: p for p in P.list_space_preferences(root)}
    global_rows = {p.preference_id: p for p in P.list_global_preferences()}
    json_rules = {r.rule_id: r.category
                  for r in load_config(root)[0].long_term_rules}
    msg = " ".join(cap["errors"])
    stores_ok = (
        set(space) == {"lt-space-1"}
        and space["lt-space-1"].category == "B 已保存的空间目标"
        and "lt-global" in global_rows
        and global_rows["lt-global"].scope == P.SCOPE_GLOBAL
        and global_rows["lt-global"].category == "全局旧目标"
        and "lt-global" not in json_rules
        and json_rules.get("lt-space-1") == "B 已保存的空间目标"
        and "偏好库写入失败" in msg
        and "保留了其他窗口的较新内容" in msg
        and "lt-space-1" in msg
        and "已整体回滚" not in msg)
    export_path = os.path.join(tempfile.mkdtemp(prefix="stage2_g2s_"),
                               "scheme.json")
    scheme_store.export_current_config(
        export_path, load_config(root)[0], "导出方案")
    with open(export_path, "r", encoding="utf-8") as f:
        exported = f.read()
    export_ok = "lt-global" not in exported

    win2 = preview(root)
    item1 = item_of(win2.current_plan, "下载/归档盒/样本.md")
    item2 = item_of(win2.current_plan, "下载/归档盒/另一份.md")
    preview_ok = (
        item1.target_dir_rel == os.path.join(
            "_已整理", "项目", "B 已保存的空间目标")
        and item1.selected is True
        and item2.target_dir_rel == os.path.join(
            "_已整理", "项目", "全局旧目标")
        and item2.rule_key == "long_term:lt-global"
        and item2.selected is True)

    win2._apply_scheme_config(default_config())
    win2._restore_last_config()
    close_window(win2)
    space2 = {p.preference_id for p in P.list_space_preferences(root)}
    json2 = {r.rule_id for r in load_config(root)[0].long_term_rules}
    global2 = {p.preference_id: p.scope for p in P.list_global_preferences()}
    restore_ok = ("lt-global" not in space2 and "lt-global" not in json2
                  and global2.get("lt-global") == P.SCOPE_GLOBAL)

    other = root_dir("global_to_space_other_space")
    write(other, "下载/归档盒/样本.md")
    disabled = P.set_preference_enabled(other, "lt-global", False)
    win3 = preview(root)
    item3 = item_of(win3.current_plan, "下载/归档盒/另一份.md")
    close_window(win3)
    disabled_ok = (disabled
                   and item3.rule_key != "long_term:lt-global"
                   and item3.target_dir_rel != os.path.join(
                       "_已整理", "项目", "全局旧目标"))
    rec("T53", "全局改当前空间失败后空间 JSON 不残留全局规则",
        stores_ok and export_ok and preview_ok and restore_ok and disabled_ok,
        f"space={sorted(space)} global={list(global_rows)} "
        f"json={json_rules} export_lt_global={'lt-global' in exported} "
        f"preview={item2.target_rel_path} rule={item2.rule_key} "
        f"restored_space={sorted(space2)} disabled={disabled_ok} "
        f"b_ok={state.get('b_ok')} msg={msg[:110]}")


def _space_to_global_failure_case():
    root = root_dir("space_to_global_failure")
    write(root, "资料/迁移.md")
    write(root, "下载/归档盒/样本2.md")
    P.save_preference(root, _exact_pref(
        root, "lt-move", "迁移", "资料/迁移.md", "保存前目标"))
    P.save_preference(root, _exact_pref(
        root, "lt-space-2", "空间二", "下载/归档盒/样本2.md", "空间二旧目标"))
    win = open_window(root)

    def mutate(dlg):
        dlg.table_longterm.item(config_row(dlg, "lt-move"), 11).setText(
            P.SCOPE_LABELS[P.SCOPE_GLOBAL])

    apply_lock, cleanup, state = _interleave_b_save(
        root, "lt-space-2", "B 的新目标")
    P._apply_rows = apply_lock
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        cleanup()
    close_window(win)

    space = {p.preference_id: p.category
             for p in P.list_space_preferences(root)}
    global_rows = {p.preference_id for p in P.list_global_preferences()}
    json_rules = {r.rule_id: r.category
                  for r in load_config(root)[0].long_term_rules}
    msg = " ".join(cap["errors"])
    stores_ok = (space.get("lt-move") == "保存前目标"
                 and space.get("lt-space-2") == "B 的新目标"
                 and "lt-move" not in global_rows
                 and json_rules.get("lt-move") == "保存前目标"
                 and json_rules.get("lt-space-2") == "B 的新目标"
                 and "偏好库写入失败" in msg
                 and "保留了其他窗口的较新内容" in msg
                 and "lt-space-2" in msg
                 and "已整体回滚" not in msg)

    win2 = preview(root)
    item = item_of(win2.current_plan, "资料/迁移.md")
    preview_ok = (
        item.target_dir_rel == os.path.join("_已整理", "项目", "保存前目标")
        and item.rule_key == "long_term:lt-move" and item.selected is True)
    win2._apply_scheme_config(default_config())
    win2._restore_last_config()
    close_window(win2)
    win3 = preview(root)
    item2 = item_of(win3.current_plan, "资料/迁移.md")
    global_after = {p.preference_id for p in P.list_global_preferences()}
    close_window(win3)
    restore_ok = (
        item2.target_dir_rel == os.path.join("_已整理", "项目", "保存前目标")
        and item2.rule_key == "long_term:lt-move" and item2.selected is True
        and "lt-move" not in global_after)
    rec("T53.2", "空间改全局失败后原行回到空间库与 JSON，不留在全局库",
        stores_ok and preview_ok and restore_ok,
        f"space={space} global={sorted(global_rows)} json={json_rules} "
        f"preview={item.target_rel_path} restored={item2.target_rel_path} "
        f"b_ok={state.get('b_ok')} msg={msg[:110]}")


def _global_to_space_failure_without_other_window():
    root = root_dir("global_to_space_no_other")
    write(root, "下载/归档盒/另一份.md")
    P.save_preference(root, _pref(
        root, "lt-global", "全局", "全局旧目标",
        source="下载/归档盒", scope=P.SCOPE_GLOBAL))
    win = open_window(root)

    def mutate(dlg):
        dlg.table_longterm.item(config_row(dlg, "lt-global"), 11).setText(
            P.SCOPE_LABELS[P.SCOPE_SPACE])

    real_open = P._open

    class _FailCommit:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    def fake_open(db_path):
        conn = real_open(db_path)
        if os.path.realpath(db_path) == os.path.realpath(P.global_db_path()):
            return _FailCommit(conn)
        return conn

    P._open = fake_open
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        P._open = real_open
    close_window(win)

    space = {p.preference_id for p in P.list_space_preferences(root)}
    global_rows = {p.preference_id: p.scope
                   for p in P.list_global_preferences()}
    json_rules = {r.rule_id for r in load_config(root)[0].long_term_rules}
    msg = " ".join(cap["errors"])
    ok = (bool(cap["errors"]) and not cap["accepted"]
          and "lt-global" not in space
          and "lt-global" not in json_rules
          and global_rows.get("lt-global") == P.SCOPE_GLOBAL
          and "已整体回滚" in msg)
    rec("T53.3", "无其他窗口时全局改当前空间失败仍整份回到保存前", ok,
        f"space={sorted(space)} json={sorted(json_rules)} "
        f"global={global_rows} msg={msg[:90]}")


def test_backup_during_failed_save_excludes_uncommitted_preference():
    """A 保存失败前，B 生成的备份不得收进 A 未提交成功的偏好。"""
    with isolated_app_dir():
        _backup_during_failed_save_case()


def _backup_during_failed_save_case():
    import json as _json
    from config_transfer import create_backup, list_backups
    from classification_config import default_config, save_config

    root = root_dir("backup_during_failed_save")
    write(root, "下载/归档盒/样本.md")
    save_pref(root, "lt-space-a", "空间偏好", category="空间保存前目标")
    P.save_preference(root, _pref(
        root, "lt-global-a", "全局偏好", "全局保存前目标",
        source="下载/其他", scope=P.SCOPE_GLOBAL))
    win = open_window(root)
    failure_id = "A 最终保存失败的空间目标"

    state = {}
    real_apply = P._apply_rows

    def apply_and_interleave(conn, prefs, delete_ids):
        # A 写完全局语句、还没提交时：堵住全局库，并让 B 等 A 的空间库
        # 提交可见后，走真实入口“备份当前配置 + 应用默认方案”。
        if (os.path.realpath(_db_path_of(conn))
                == os.path.realpath(P.global_db_path())
                and "holder" not in state):
            holder = sqlite3.connect(P.global_db_path(), timeout=10)
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM preferences").fetchall()
            state["holder"] = holder

            def b_apply_default():
                deadline = time.monotonic() + 8
                row = None
                while time.monotonic() < deadline:
                    row = P.get_preference(root, "lt-space-a")
                    if row is not None and row.category == failure_id:
                        break
                    time.sleep(0.01)
                else:
                    state["b_error"] = "未等到 A 的空间提交"
                    return
                try:
                    create_backup(root)
                    save_config(root, default_config())
                    P.sync_space_preferences_from_config(root, default_config())
                    state["b_ok"] = True
                except Exception as e:
                    state["b_error"] = e

            thread = threading.Thread(target=b_apply_default)
            thread.start()
            state["thread"] = thread
        return real_apply(conn, prefs, delete_ids)

    def mutate(dlg):
        mapping = dlg.table_top
        row = next(r for r in range(mapping.rowCount())
                   if mapping.item(r, 1).text() == "项目")
        mapping.item(row, 1).setText("独立归档源")
        dlg.table_longterm.item(
            config_row(dlg, "lt-space-a"), 8).setText(failure_id)
        dlg.table_longterm.item(
            config_row(dlg, "lt-global-a"), 8).setText("A 最终保存失败的全局目标")

    P._apply_rows = apply_and_interleave
    try:
        cap = save_via_config_page(win, mutate)
    finally:
        P._apply_rows = real_apply
        thread = state.get("thread")
        if thread is not None:
            thread.join(20)
        holder = state.get("holder")
        if holder is not None:
            try:
                holder.rollback()
            except Exception:
                pass
            holder.close()
    close_window(win)

    msg = " ".join(cap["errors"])
    space_ids = [p.preference_id for p in P.list_space_preferences(root)]
    global_cat = next((p.category for p in P.list_global_preferences()
                       if p.preference_id == "lt-global-a"), None)
    latest = list_backups(root)[0]
    with open(latest.path, "r", encoding="utf-8") as f:
        backed_up = {r["rule_id"]: r["category"]
                     for r in _json.load(f)["config"]["long_term_rules"]}
    failure_ok = (bool(cap["errors"]) and not cap["accepted"]
                  and "database is locked" in msg
                  and space_ids == []
                  and global_cat == "全局保存前目标"
                  and backed_up.get("lt-space-a") == "空间保存前目标"
                  and failure_id not in backed_up.values())

    win2 = preview(root)
    before = item_of(win2.current_plan, "下载/归档盒/样本.md")
    before_ok = (before.rule_key == "fallback" and before.selected is False)
    win2._restore_last_config()
    close_window(win2)

    win3 = preview(root)
    after = item_of(win3.current_plan, "下载/归档盒/样本.md")
    close_window(win3)
    restored_cat = next((p.category for p in P.list_space_preferences(root)
                         if p.preference_id == "lt-space-a"), None)
    restore_ok = (
        after.target_dir_rel == os.path.join(
            "_已整理", "项目", "空间保存前目标")
        and after.rule_key == "long_term:lt-space-a"
        and after.selected is True
        and restored_cat == "空间保存前目标")
    rec("T55", "A 失败保存期间的备份不含未提交偏好，恢复后不复活",
        failure_ok and before_ok and restore_ok,
        f"backup={backed_up} space={space_ids} global={global_cat} "
        f"before=({before.target_rel_path},{before.rule_key},{before.selected}) "
        f"after=({after.target_rel_path},{after.rule_key},{after.selected}) "
        f"restored_cat={restored_cat} b_ok={state.get('b_ok')} "
        f"b_error={state.get('b_error')} msg={msg[:90]}")


def test_two_confirmations_keep_both_space_rules():
    root = root_dir("two_confirmed_saves")
    sample = "资料乙/普通笔记.md"
    write(root, "下载/归档盒/样本.md")
    write(root, sample)
    win = open_window(root)
    rule_a = _pref(root, "lt-window-a", "窗口 A", "A 确认的目标")
    rule_b = _pref(root, "lt-window-b", "窗口 B", "B 确认的目标",
                   source="资料乙")
    real_open = P._open
    b_saved = False

    def open_with_b_save(path):
        nonlocal b_saved
        if not b_saved and os.path.realpath(path) == os.path.realpath(P.space_db_path(root)):
            b_saved = True
            P.save_preference(root, rule_b)
        return real_open(path)

    P._open = open_with_b_save
    try:
        win._save_preference(root, rule_a)
    finally:
        P._open = real_open
    space = {p.preference_id for p in P.list_space_preferences(root)}
    mirror = {r.rule_id for r in load_config(root)[0].long_term_rules}
    win._apply_scheme_config(default_config())
    win._restore_last_config()
    close_window(win)
    win = preview(root)
    restored = item_of(win.current_plan, sample)
    rec("T54", "两次成功确认都进入空间库、JSON 和恢复后的预览",
        b_saved and space == mirror == {"lt-window-a", "lt-window-b"}
        and restored.rule_key == "long_term:lt-window-b" and restored.selected,
        f"SQLite={sorted(space)} JSON={sorted(mirror)} "
        f"restored={restored.target_rel_path}")
    close_window(win)


def run():
    control_target = test_control_without_preference()
    space_root, pref_id = test_save_space_preference_from_ui()
    test_two_round_samples(space_root, pref_id, control_target)
    test_one_off_only(space_root)
    test_one_off_control_restart()
    test_cancel_close_and_ai_no_write(space_root)
    test_modify_and_disable(space_root, pref_id)
    test_one_off_overrides_saved(space_root, pref_id)
    test_migrated_disabled_stays_disabled()
    test_space_isolation_and_global()
    test_same_level_conflict_ambiguity()
    test_history_compatibility_and_execution()
    test_adopted_preference_is_selected()
    test_revert_auto_returns_to_preference()
    test_revert_keeps_preference_inner_path()
    test_selected_preference_execution_roundtrip()
    test_global_db_corruption_keeps_space_disabled()
    test_failed_save_is_atomic()
    test_concurrent_global_preference_not_deleted()
    test_scheme_apply_syncs_preferences()
    test_preferences_do_not_override_plain_modes()
    test_preference_conflict_keeps_body_candidate()
    test_single_save_failure_is_atomic()
    test_space_db_corrupt_uses_global_only()
    test_concurrent_edit_is_not_overwritten()
    test_restore_backup_rolls_back_json_on_sync_failure()
    test_global_commit_failure_rolls_back_space_db()
    test_config_page_keeps_reason_and_avoids_false_conflict()
    test_scope_change_moves_preference_between_dbs()
    test_corrupt_json_can_be_repaired_and_rolled_back_from_config_page()
    test_restore_retry_keeps_previous_target_backup()
    test_single_preference_keeps_body_candidates()
    test_rollback_does_not_erase_newer_save()
    test_write_recheck_rejects_newer_save()
    test_scope_change_fails_when_target_db_unavailable()
    test_mixed_compensation_keeps_json_and_space_db_in_sync()
    test_failed_scope_migration_never_writes_global_rule_into_space_json()
    test_backup_during_failed_save_excludes_uncommitted_preference()
    test_two_confirmations_keep_both_space_rules()


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        CHECKS.append(("EXC", "未捕获异常", False, "见上方 traceback"))
    passed = sum(1 for _, _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print()
    print(f"合计 {len(CHECKS)} 项：通过 {passed} / 未通过 {failed}")
    for tid, title, ok, evidence in CHECKS:
        if not ok:
            print(f"  未通过：{tid} {title} — {evidence}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
