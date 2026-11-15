"""阶段 1 验收回归：歧义面板可用性、跳过防重选、候选目标路径、正文边界。

所有样本都由本脚本在临时目录生成，不使用真实资料。默认不联网。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage1fix_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage1fix_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage1fix_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QCheckBox  # noqa: E402

from classification_config import (  # noqa: E402
    MATCH_DIR_SCOPE, INNER_REPLACE, LongTermRule, default_config,
)
from content_extract import extract_contents  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, generate_plan, resolve_ambiguity,
)
from scanner import scan_directory, FileEntry  # noqa: E402
from storage import Storage  # noqa: E402
from executor import undo_latest_batch  # noqa: E402

_APP = None
_MAIN = None


def app():
    global _APP, _MAIN
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    if _MAIN is None:
        import main as main_module
        _MAIN = main_module
    return _APP


def root_dir() -> str:
    return tempfile.mkdtemp(prefix="stage1fix_sample_")


def write(root: str, rel: str, data, binary: bool = False) -> str:
    full = os.path.join(root, rel)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(full, mode, **kwargs) as handle:
        handle.write(data)
    return full


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_root(root: str):
    return scan_directory(root, detect_project_roots=False)


def wait_for_plan(window, timeout: float = 15.0) -> bool:
    """等到 current_plan 有效且界面空闲（扫描 + 计划生成都结束）。"""
    deadline = time.time() + timeout
    qapp = QApplication.instance()
    while time.time() < deadline:
        qapp.processEvents()
        if (window.current_plan is not None and window._plan_valid
                and not window._plan_working and not window._working):
            return True
        time.sleep(0.02)
    return False


def wait_for_idle(window, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    qapp = QApplication.instance()
    while time.time() < deadline:
        qapp.processEvents()
        if not window._working and not window._plan_working:
            return True
        time.sleep(0.02)
    return False


def item_by(plan, rel):
    for item in plan.items:
        if item.file.rel_path == rel:
            return item
    raise AssertionError(f"plan 中找不到 {rel}")


def _patch_msgboxes():
    """把 QMessageBox 的弹窗改成无操作，避免测试被模态框阻塞。"""
    real_info = _MAIN.QMessageBox.information
    real_warn = _MAIN.QMessageBox.warning
    real_crit = _MAIN.QMessageBox.critical
    _MAIN.QMessageBox.information = lambda *a, **k: None
    _MAIN.QMessageBox.warning = lambda *a, **k: None
    _MAIN.QMessageBox.critical = lambda *a, **k: None
    return real_info, real_warn, real_crit


def _restore_msgboxes(real):
    _MAIN.QMessageBox.information, _MAIN.QMessageBox.warning, _MAIN.QMessageBox.critical = real


# ======================== 各项检查 ========================

def check_ambiguity_panel_enabled_after_scan():
    """修复 1：扫描真正结束后，歧义面板三个控件必须可用。"""
    app()
    root = root_dir()
    write(root, "项目/AIGC/改选.md", "高等数学 极限 导数")
    write(root, "项目/AIGC/跳过.md", "高等数学 极限 导数")

    window = _MAIN.MainWindow()
    try:
        window.path_edit.setText(root)
        window.on_scan()
        assert wait_for_plan(window), "等待计划生成超时"
        assert window.ambiguity_combo.isEnabled(), "ambiguity_combo 扫描后仍禁用"
        assert window.btn_ambiguity_apply.isEnabled(), "按所选归入 扫描后仍禁用"
        assert window.btn_ambiguity_skip.isEnabled(), "跳过保持原位 扫描后仍禁用"
    finally:
        if getattr(window, "_storage", None) is not None:
            window._storage.close()
        window.close()


def check_skipped_not_reselected_and_execute_undo():
    """修复 2：跳过的文件不能被批量/行内重新勾选；完整执行+撤销闭环。"""
    app()
    root = root_dir()
    write(root, "项目/AIGC/改选.md", "高等数学 极限 导数")
    write(root, "项目/AIGC/跳过.md", "高等数学 极限 导数")
    skip_src = os.path.join(root, "项目/AIGC/跳过.md")
    apply_src = os.path.join(root, "项目/AIGC/改选.md")
    before_skip = sha256_file(skip_src)
    before_apply = sha256_file(apply_src)

    window = _MAIN.MainWindow()
    try:
        window.path_edit.setText(root)
        window.on_scan()
        assert wait_for_plan(window)

        plan = window.current_plan
        skip_item = item_by(plan, "项目/AIGC/跳过.md")
        apply_item = item_by(plan, "项目/AIGC/改选.md")

        # 跳过其中一条
        assert window.skip_ambiguity("项目/AIGC/跳过.md")
        assert skip_item.skipped is True
        assert skip_item.selected is False

        # 改选另一条（选候选 0，即当前项目方向）
        assert window.apply_ambiguity_choice("项目/AIGC/改选.md", 0)
        assert apply_item.selected is True
        apply_target = os.path.join(root, apply_item.target_rel_path)

        # 点击「勾选看到的文件」：跳过的条目仍不能被勾上
        window._set_visible_checked(True)
        assert skip_item.selected is False, "批量勾选把已跳过条目重新纳入"
        assert skip_item.skipped is True

        # 「对调这些勾选」也不能把跳过的条目勾上
        window._set_visible_checked(None, invert=True)
        assert skip_item.selected is False, "对调勾选把已跳过条目重新纳入"
        # 改选那条本来已勾选，对调后取消；再点一次勾选看到的文件恢复
        window._set_visible_checked(True)
        assert skip_item.selected is False

        # 行内复选框手动勾跳过的条目：必须弹回未勾选
        row_skip = next(r for r, it in enumerate(plan.items)
                        if it.file.rel_path == "项目/AIGC/跳过.md")
        cell = window.table.cellWidget(row_skip, 0)
        cb = cell.findChild(QCheckBox) if cell is not None else None
        assert cb is not None
        cb.setChecked(True)
        # stateChanged 信号由 setChecked 触发；处理事件后状态应弹回
        QApplication.instance().processEvents()
        assert skip_item.selected is False, "行内勾选把已跳过条目重新纳入"
        assert cb.isChecked() is False

        # ---- 确认框取消：目录不动 ----
        class RejectConfirm:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return QDialog.Rejected

        real_confirm = _MAIN.ExecuteConfirmDialog
        _MAIN.ExecuteConfirmDialog = RejectConfirm
        try:
            window.on_execute()
        finally:
            _MAIN.ExecuteConfirmDialog = real_confirm
        assert os.path.exists(skip_src) and sha256_file(skip_src) == before_skip
        assert os.path.exists(apply_src) and sha256_file(apply_src) == before_apply

        # ---- 确认执行 ----
        class AcceptConfirm:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return QDialog.Accepted

        msg_real = _patch_msgboxes()
        _MAIN.ExecuteConfirmDialog = AcceptConfirm
        try:
            window.on_execute()
            assert wait_for_idle(window), "执行未结束"
        finally:
            _MAIN.ExecuteConfirmDialog = real_confirm
            _restore_msgboxes(msg_real)

        # 跳过的文件仍在原路径、SHA 不变
        assert os.path.exists(skip_src), "跳过文件被移走"
        assert sha256_file(skip_src) == before_skip, "跳过文件内容被改"
        # 改选的文件落到预览目标
        assert os.path.exists(apply_target), "改选文件未落到目标"
        assert sha256_file(apply_target) == before_apply
        assert not os.path.exists(apply_src), "改选文件未从源路径移走"

        # ---- 撤销 ----
        storage = window._storage
        undo = undo_latest_batch(storage)
        assert undo.restored >= 1, undo
        assert os.path.exists(skip_src) and sha256_file(skip_src) == before_skip
        assert os.path.exists(apply_src) and sha256_file(apply_src) == before_apply
        assert not os.path.exists(apply_target)
    finally:
        if getattr(window, "_storage", None) is not None:
            window._storage.close()
        window.close()


def check_resolve_ambiguity_preserves_candidate_target():
    """修复 3：选中候选后 target_dir_rel 等于候选自己的 target_dir_rel。"""
    root = root_dir()
    write(root, "大二上/数据结构/说明.md", "高等数学 极限 导数")
    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-inner-replace", name="数据结构归项目-保留实验一",
        match_type=MATCH_DIR_SCOPE, source_path="大二上/数据结构",
        include_subdirs=True, direction="项目", category="数据项目",
        inner_mode=INNER_REPLACE, inner_path="保留/实验一",
    ))
    scan = scan_root(root)
    extraction = extract_contents(scan.files, root=root)
    plan = generate_plan(
        scan, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
        content_texts=extraction.texts, content_errors=extraction.errors,
    )
    item = item_by(plan, "大二上/数据结构/说明.md")
    assert item.ambiguity is not None, item.rule_applied
    cands = item.ambiguity["candidates"]
    # 候选 0 是当前（长期规则），目标带内部路径
    cand0_target = cands[0]["target_dir_rel"]
    assert cand0_target.endswith("保留/实验一"), cand0_target

    # 选候选 0：目标必须等于候选 0 的目标
    resolve_ambiguity(plan, item.file.rel_path, 0)
    assert item.target_dir_rel == cand0_target, (
        f"选中候选 0 后目标丢了内部层级：{item.target_dir_rel} != {cand0_target}")
    assert item.target_dir_rel.endswith("保留/实验一"), item.target_dir_rel

    # 重新构造一个计划，选正文关键词候选（候选 1）：目标是该候选自己的，
    # 不能带回长期规则的内部层级。
    scan2 = scan_root(root)
    ext2 = extract_contents(scan2.files, root=root)
    plan2 = generate_plan(
        scan2, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
        content_texts=ext2.texts, content_errors=ext2.errors,
    )
    item2 = item_by(plan2, "大二上/数据结构/说明.md")
    cands2 = item2.ambiguity["candidates"]
    # 找到正文关键词「高等数学」那个候选
    body_cand = next(c for c in cands2 if "高等数学" in c.get("evidence", ""))
    body_target = body_cand["target_dir_rel"]
    body_idx = cands2.index(body_cand)
    resolve_ambiguity(plan2, item2.file.rel_path, body_idx)
    assert item2.target_dir_rel == body_target, (
        f"选中正文候选后目标不对：{item2.target_dir_rel} != {body_target}")
    assert "保留/实验一" not in item2.target_dir_rel, (
        f"正文候选错误带回了长期规则内部层级：{item2.target_dir_rel}")


def check_content_no_escape_and_no_abs_path():
    """修复 4：正文读取不得越出整理空间，失败信息不带绝对路径。"""
    root = root_dir()
    write(root, "下载/笔记.md", "所选空间内的资料")
    scan = scan_root(root)

    # 扫描后、读取正文前，把该路径换成指向空间外的符号链接
    outside = tempfile.mkdtemp(prefix="stage1fix_outside_")
    outside_file = os.path.join(outside, "外部.md")
    with open(outside_file, "w", encoding="utf-8") as h:
        h.write("独立的外部笔记：蓝鲸观察值。")
    inside_path = os.path.join(root, "下载/笔记.md")
    os.remove(inside_path)
    os.symlink(outside_file, inside_path)

    extraction = extract_contents(scan.files, root=root)
    text = extraction.texts.get("下载/笔记.md", "")
    assert "蓝鲸观察值" not in text, f"外部内容进入了正文：{text!r}"
    # 外部文件字节不变
    with open(outside_file, "r", encoding="utf-8") as h:
        assert h.read() == "独立的外部笔记：蓝鲸观察值。"

    # 读取失败（PermissionError）：错误信息不含绝对路径
    root2 = root_dir()
    blocked = write(root2, "下载/无权限.md", "打不开")
    scan2 = scan_root(root2)          # 先扫描（此时文件可读）
    entry = scan2.files[0]
    os.chmod(blocked, 0)               # 再去掉读权限，模拟读取失败
    try:
        got = extract_contents([entry], root=root2)
    finally:
        os.chmod(blocked, 0o600)
    err = got.errors.get("下载/无权限.md", "")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        assert "无法读取正文" in err, err
        # 绝对路径（root2 或 blocked 的目录）不能出现在错误里
        assert root2 not in err, f"错误信息含绝对路径：{err!r}"
        assert os.path.dirname(blocked) not in err, (
            f"错误信息含绝对路径目录：{err!r}")


CHECKS = [
    ("扫描后歧义面板可用", check_ambiguity_panel_enabled_after_scan),
    ("跳过不被重选 + 执行撤销闭环", check_skipped_not_reselected_and_execute_undo),
    ("候选目标路径完整保留", check_resolve_ambiguity_preserves_candidate_target),
    ("正文不越界且错误无绝对路径", check_content_no_escape_and_no_abs_path),
]


def main() -> int:
    failures = []
    for name, func in CHECKS:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    passed = len(CHECKS) - len(failures)
    print(f"合计 {len(CHECKS)} 项：通过 {passed} / 未通过 {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
