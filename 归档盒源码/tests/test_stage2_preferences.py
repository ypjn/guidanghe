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
import sys
import tempfile
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
    LongTermRule, default_config, load_config, save_config,
)
from config_dialog import ClassificationConfigDialog  # noqa: E402
from config_transfer import create_backup  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402
from main import MainWindow  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE,
    generate_plan,
)
from scanner import scan_directory  # noqa: E402
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
              inner_mode=INNER_CLEAR, inner_path="", source="下载/归档盒"):
    P.save_preference(root, P.Preference(
        preference_id=pid, name=name, match_type=MATCH_DIR_SCOPE,
        source_path=source, include_subdirs=True, direction="项目",
        category=category, inner_mode=inner_mode, inner_path=inner_path,
        enabled=True, scope=P.SCOPE_SPACE, space_root=root,
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
