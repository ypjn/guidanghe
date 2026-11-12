"""
归档盒 V1.4 — 单文件分类修正（仅当前计划生效）

设计：
- 修正只改变当前有效 PlanItem 的分类字段、目标路径与依据，
  不写回配置、不创建规则、不移动文件、不写执行历史。
- 不维护第二套目标路径构造：直接复用 planner 的
  _build_target_dir_v2 / _sanitize_component /
  _assign_unique_names，避免两套算法漂移。
- 本轮只支持「按方向/学期/类别（推荐）」模式。其他模式的自动分类
  字段本身不记录方向/学期/类别，无法做"恢复自动"，因此入口整体限缩
  到推荐模式，避免出现"能改但不能恢复"的半成品状态。
- item 身份不用表格行号或 filename，统一用 source_rel（= file.rel_path）
  在 PlanItem.file.rel_path 上匹配，避免同名/排序变化/过期回调改错对象。
- 单次修正后，整批计划必须重新计算目标路径冲突——因为分配唯一名称是
  批次范围的；连续修正多个文件，最后一次冲突重算就足以覆盖全部。
  但为让预览实时正确，每次修正后都重算一次批次内的唯一名称分配。
- PlanItem.auto_* 字段在 plan 生成时已被填入，恢复自动分类时直接
  把 auto_* 复制回当前分类字段，再重算目标路径，不读当前配置重跑规则。
  这保证了"恢复自动"与"当前重新生成"可能不同——因为配置可能已被改过；
  但用户要的是"还原到第一次自动生成的结果"，不是"按当前配置重跑"。

测试友好：本模块无 UI 依赖，所有输入输出都是纯数据结构。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from planner import (
    PlanItem, OrganizePlan, TARGET_ROOT,
    _build_target_dir_v2,
    _sanitize_component, _assign_unique_names,
    MODE_BY_DIRECTION_SEMESTER,
)

# ========== ManualOverrideDialog（PySide6 UI） ==========
# 放这里而不是独立文件，避免 UI 模块依赖 planner/storage/main 的环形。
# 导入 PySide6 用延迟方式，纯逻辑函数在无 Qt 环境下也可被单测调用。

_MANUAL_OVERRIDE_IMPORT_FAIL = None


def _ensure_qt():
    """延迟导入 PySide6，失败时在 ManualOverrideDialog 使用处才报错。"""
    global _MANUAL_OVERRIDE_IMPORT_FAIL
    if _MANUAL_OVERRIDE_IMPORT_FAIL is not None:
        return None, _MANUAL_OVERRIDE_IMPORT_FAIL
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
            QLineEdit, QComboBox, QLabel, QDialogButtonBox, QPushButton,
            QMessageBox,
        )
        return (Qt, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                QLineEdit, QComboBox, QLabel, QDialogButtonBox, QPushButton,
                QMessageBox), None
    except Exception as e:
        _MANUAL_OVERRIDE_IMPORT_FAIL = e
        return None, e


class ManualOverrideDialog:
    """按方向展示字段 + 实时预览目标路径的纯 UI 组件。

    本类**不**持有 PlanItem 引用，也不直接调用 apply/revert；
    它只负责让用户把字段改好、校验、实时显示目标路径。
    main.py 在用户点击「应用到本次计划」后，从 self.params 取值
    调用 apply_override_to_plan，刷新表格。

    这样 ManualOverrideDialog 可以独立被单测构造、检查 fields
    与预览——不需要真的接 Qt 事件循环。
    """

    DIRECTIONS = ["课程", "竞赛", "项目", "校园事务", "待定"]

    def __init__(self, plan: OrganizePlan, source_rel: str, mode: str = None):
        if mode is None:
            mode = plan.mode
        self.plan = plan
        self.source_rel = source_rel
        self.mode = mode
        if mode != MODE_BY_DIRECTION_SEMESTER:
            raise OverrideValidationError(
                "本轮只支持在「按方向/学期/类别（推荐）」模式下修正单个文件；"
                "其他模式请使用对应的规则配置入口。")
        item = None
        for i in plan.items:
            if i.file.rel_path == source_rel:
                item = i
                break
        if item is None:
            raise OverrideValidationError(f"计划中不存在源路径：{source_rel}")
        self.item = item

        # 用户当前在对话框里编辑的字段值
        self.direction = item.direction or ""
        self.semester = item.semester or ""
        self.category = item.category or ""
        self.inner_path = item.inner_path or ""
        self.course = item.course or ""
        self.file_type = item.file_type or ""
        self.override_note = item.override_note or ""
        # 记录"这次应用前自动快照里的分类"，供恢复自动用
        self.auto_direction = item.auto_direction
        self.auto_semester = item.auto_semester
        self.auto_category = item.auto_category
        self.auto_inner_path = item.auto_inner_path
        self.auto_rule_applied = item.auto_rule_applied

        # ---- 以下是 PySide6 UI（_ensure_qt 延迟 import）----
        self._qt_ok, self._err = _ensure_qt()
        if self._qt_ok is None:
            return

        (Qt, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
         QLineEdit, QComboBox, QLabel, QDialogButtonBox, QPushButton,
         QMessageBox) = self._qt_ok
        self.Qt = Qt
        self.QDialogButtonBox = QDialogButtonBox

        self.dlg = QDialog()
        self.dlg.setWindowTitle("修正分类 — 仅本次计划生效")
        self.dlg.setMinimumWidth(560)
        self.dlg.resize(640, 520)
        lay = QVBoxLayout(self.dlg)

        head = QLabel(
            f"源路径：{self.source_rel}\n"
            f"文件：{self.item.file.filename}（{self.item.file.size_display}）\n"
            f"原自动分类：{self.item.auto_rule_applied or self.item.rule_applied}"
        )
        head.setStyleSheet("padding:6px;background:#f5f5f5;border-radius:4px;")
        lay.addWidget(head)

        self.form = QFormLayout()
        self.dir_combo = QComboBox()
        self.dir_combo.addItems(self.DIRECTIONS)
        if self.direction:
            idx = self.dir_combo.findText(self.direction)
            if idx >= 0:
                self.dir_combo.setCurrentIndex(idx)
        self.dir_combo.currentTextChanged.connect(self._on_field_change)
        self.form.addRow("方向", self.dir_combo)

        self.sem_edit = QLineEdit(self.semester)
        self.sem_edit.textChanged.connect(self._on_field_change)
        self.sem_label = QLabel("学期（课程方向必填，其他方向留空）")
        self.form.addRow(self.sem_label, self.sem_edit)

        self.cat_edit = QLineEdit(self.category)
        self.cat_edit.textChanged.connect(self._on_field_change)
        self.cat_label = QLabel("类别（课程=课程名/竞赛=竞赛名/项目=项目名/校园事务=类别）")
        self.form.addRow(self.cat_label, self.cat_edit)

        self.inner_edit = QLineEdit(self.inner_path)
        self.inner_edit.setPlaceholderText("（可选）内部路径段，如 实验一/分组报告")
        self.inner_edit.textChanged.connect(self._on_field_change)
        self.inner_label = QLabel("内部路径")
        self.form.addRow(self.inner_label, self.inner_edit)

        self.note_edit = QLineEdit(self.override_note)
        self.note_edit.setPlaceholderText("（可选）说明，如：原自动分类漏了关键词")
        self.form.addRow("修正说明", self.note_edit)

        lay.addLayout(self.form)

        self.preview_label = QLabel("目标路径预览")
        self.preview_label.setStyleSheet(
            "padding:6px;background:#E3F2FD;border-radius:4px;"
            "font-family: Menlo, Consolas, monospace;")
        self.preview_label.setWordWrap(True)
        lay.addWidget(self.preview_label)

        self.override_hint = QLabel("⚠ 本次修正只作用于当前计划；重新扫描/重新预览/切换根目录后失效。")
        self.override_hint.setStyleSheet("color:#E65100;")
        self.override_hint.setWordWrap(True)
        lay.addWidget(self.override_hint)

        btn_row = QHBoxLayout()
        self.btn_revert = QPushButton("恢复自动分类")
        self.btn_revert.clicked.connect(self._on_revert_auto)
        btn_row.addWidget(self.btn_revert)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.bb = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("应用到本次计划")
        self.bb.button(QDialogButtonBox.Cancel).setText("取消")
        self.bb.accepted.connect(self.dlg.accept)
        self.bb.rejected.connect(self.dlg.reject)
        lay.addWidget(self.bb)

        self._revert_on_ok = False
        self._on_field_change()  # 初始化预览

    # ---- PySide6 内部槽 ----

    def _on_field_change(self, *_):
        if self._qt_ok is None:
            return
        try:
            params = self._current_params()
            target_dir = _compute_target_dir(
                self.mode, params.direction or None,
                params.semester, params.category,
                self.item.file_type, inner_path=params.inner_path,
            )
            target_path = os.path.join(target_dir, self.item.file.filename)
            self.preview_label.setText(
                f"<b>目标路径：</b>{target_path}<br>"
                f"<b>目标目录：</b>{target_dir}<br>"
                f"<b>非法检查：</b>通过")
            self.preview_label.setStyleSheet(
                "padding:6px;background:#E8F5E9;border-radius:4px;"
                "font-family: Menlo, Consolas, monospace;color:#2E7D32;")
            self.bb.button(self.QDialogButtonBox.Ok).setEnabled(True)
        except OverrideValidationError as e:
            self.preview_label.setText(
                f"<b>目标路径：</b>无法计算<br>"
                f"<b>非法检查：</b>{e}")
            self.preview_label.setStyleSheet(
                "padding:6px;background:#FFEBEE;border-radius:4px;"
                "font-family: Menlo, Consolas, monospace;color:#C62828;")
            self.bb.button(self.QDialogButtonBox.Ok).setEnabled(False)

    def _on_revert_auto(self):
        # 把当前字段重置为 auto_* 快照（如果有）
        if self._qt_ok is None:
            return
        if not self.auto_direction and not self.auto_category:
            QMessageBox.information(
                self.dlg, "归档盒",
                "此文件没有可用的自动分类快照，无法恢复。")
            return
        if self.dir_combo.findText(self.auto_direction or "") >= 0:
            self.dir_combo.setCurrentText(self.auto_direction or "待定")
        self.sem_edit.setText(self.auto_semester or "")
        self.cat_edit.setText(self.auto_category or "")
        self.inner_edit.setText(self.auto_inner_path or "")
        self.note_edit.setText("")
        self._revert_on_ok = True
        # 应用后对话框自行关闭，main.py 拿到 revert_on_ok 标志调用 revert_auto
        self.dlg.accept()

    # ---- 供 main.py 调用的接口 ----

    def _current_params(self) -> OverrideParams:
        return OverrideParams(
            direction=self.dir_combo.currentText(),
            semester=self.sem_edit.text().strip(),
            category=self.cat_edit.text().strip(),
            inner_path=self.inner_edit.text().strip(),
            file_type=self.item.file_type,
            override_note=self.note_edit.text().strip(),
        )

    @property
    def params(self) -> OverrideParams:
        return self._current_params()

    @property
    def revert_on_ok(self) -> bool:
        """用户点击了「恢复自动分类」并确定；main.py 应用后应调用 revert_auto。"""
        return self._revert_on_ok

    def exec(self) -> int:
        if self._qt_ok is None:
            raise RuntimeError(f"PySide6 不可用：{self._err}")
        return self.dlg.exec()


# ======================== 字段校验 ========================


class OverrideValidationError(ValueError):
    """单文件修正的字段校验失败，错误文本直接给用户看。"""


# ======================== 字段校验 ========================

def _validate_component(value: str, field_label: str) -> None:
    if value is None or str(value).strip() == "":
        raise OverrideValidationError(f"{field_label}不能为空")
    v = str(value)
    if os.sep in v or (os.altsep and os.altsep in v):
        raise OverrideValidationError(
            f"{field_label}：不能包含路径分隔符（当前：'{v}'）")
    if v in (".", ".."):
        raise OverrideValidationError(f"{field_label}：不能是 '.' 或 '..'（路径逃逸）")
    if v.startswith("/") or v.startswith("\\"):
        raise OverrideValidationError(f"{field_label}：不能以分隔符开头（绝对路径）")
    # _sanitize_component 里还有非法字符替换（macOS 不允许 : / 等），
    # 这里只做结构性校验；sanitize 会替换非法字符，所以这里只在
    # 转换后发现空字符串时再报错。


def _normalize_inner_path(value: str) -> str:
    """把用户填写的内部路径拆成若干段逐段校验，返回规范化后的相对路径。

    允许 "实验一"（一段）和 "实验一/分组报告"（多段）；
    拒绝空段、"." / ".."、绝对路径。原实现把整串当成单个目录名校验，
    导致多段输入因含分隔符被直接拒绝。
    """
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    segs = re.split(r"[/\\]", raw)
    normalized: List[str] = []
    for seg in segs:
        seg = seg.strip()
        if not seg:
            raise OverrideValidationError(
                "内部路径：不能包含空目录段（如开头的 /、连续的 // 或结尾的 /）")
        _validate_component(seg, "内部路径段")
        normalized.append(_sanitize_component(seg))
    return os.sep.join(normalized)


def _compute_target_dir(
    mode: str,
    direction: Optional[str],
    semester: Optional[str],
    category: Optional[str],
    file_type: str,
    inner_path: str = "",
) -> str:
    """复用 planner 的目标路径构造；不维护第二套算法。

    注意：planner._build_target_dir_v2 只构造到「类型目录」为止，
    inner_path 由调用方在之后拼接（classify_entries 也是这么做的）。
    因此这里必须显式 join，否则用户填写的内部路径会被静默丢弃。
    """
    if mode != MODE_BY_DIRECTION_SEMESTER:
        raise OverrideValidationError(
            "本轮只支持在「按方向/学期/类别（推荐）」模式下修正单个文件；"
            f"当前模式为：{mode}")
    base = _build_target_dir_v2(
        direction or "", semester or "", category or "", file_type)
    return os.path.join(base, inner_path) if inner_path else base


# ======================== 批次级唯一名称重算 ========================

def _recompute_unique_names(plan: OrganizePlan) -> None:
    """按 planner 的 _assign_unique_names 逻辑，重新分配批次内唯一名称。

    因为 _assign_unique_names 是"遍历 items_so_far + 磁盘检查"的增量
    算法，而批次间目标存在交叉冲突（改一个文件的目标可能让另一个
    原来不冲突的条目变成冲突），这里对**每个**条目都按当前
    target_dir_rel + 原名重新跑一遍。

    不改变条目顺序；只更新 target_filename / target_rel_path /
    conflict_with_existing。已选中/手动修正/自动快照字段都不动。
    """
    seen_in_batch: set[str] = set()
    root = plan.root
    for item in plan.items:
        base_target_dir = item.target_dir_rel
        base_name = item.file.filename
        target_rel = os.path.join(base_target_dir, base_name)

        candidate_name = base_name
        n = 1
        while target_rel in seen_in_batch:
            stem, ext = os.path.splitext(base_name)
            candidate_name = f"{stem} ({n}){ext}"
            target_rel = os.path.join(base_target_dir, candidate_name)
            n += 1

        disk_path = os.path.join(root, target_rel)
        if os.path.exists(disk_path) and os.path.abspath(disk_path) not in {
            os.path.abspath(os.path.join(root, i.file.rel_path))
            for i in plan.items
        }:
            # 磁盘上非计划来源的文件已占用；但如果就是本轮文件自己的位置
            # （它原来的目标），就不算冲突
            base_existing = os.path.join(root, base_target_dir, base_name)
            if os.path.abspath(base_existing) != os.path.abspath(
                os.path.join(root, item.file.rel_path)):
                candidate_name = base_name
                n = 1
                seen_this_pass = set(seen_in_batch)
                while (os.path.exists(os.path.join(root, base_target_dir, candidate_name))
                       or os.path.join(base_target_dir, candidate_name) in seen_this_pass):
                    stem, ext = os.path.splitext(base_name)
                    candidate_name = f"{stem} ({n}){ext}"
                    n += 1
                target_rel = os.path.join(base_target_dir, candidate_name)
                item.conflict_with_existing = True
            else:
                item.conflict_with_existing = False

        seen_in_batch.add(target_rel)
        item.target_filename = candidate_name if candidate_name else base_name
        item.target_rel_path = target_rel


# ======================== 单文件修正主入口 ========================

@dataclass
class OverrideParams:
    direction: str           # 推荐模式必填
    semester: str = ""       # 推荐模式课程方向必填（其他方向空）
    category: str = ""       # 所有模式适用
    inner_path: str = ""     # 推荐模式可选（保持自动内部路径 / 自定义）
    course: str = ""         # 按课程再按类型 / 按类型时复用 category
    file_type: str = ""      # 允许用户把文件归到另一个类型桶
    override_note: str = ""  # 可选的说明


def apply_override_to_plan(
    plan: OrganizePlan,
    source_rel: str,
    params: OverrideParams,
) -> PlanItem:
    """把手动修正应用到计划中的单个条目。

    - 按 file.rel_path 定位（不是 row index / filename）；
    - 自动快照字段保持不变（供恢复自动用）；
    - 计算新 target_dir_rel / target_rel_path / rule_applied / course /
      direction / semester / category / inner_path；
    - **整批**重算唯一名称（连续修正后冲突仍被覆盖）；
    - 返回被修改的 PlanItem 引用（就是 plan.items 里那个），方便调用方
      立即刷新对应表格行。
    """
    target = None
    for item in plan.items:
        if item.file.rel_path == source_rel:
            target = item
            break
    if target is None:
        raise OverrideValidationError(f"计划中不存在源路径：{source_rel}")

    # 1) 按模式提取字段并做结构性校验
    p = params
    mode = plan.mode
    if mode != MODE_BY_DIRECTION_SEMESTER:
        raise OverrideValidationError(
            "本轮只支持在「按方向/学期/类别（推荐）」模式下修正单个文件；"
            "其他模式请使用对应的规则配置入口（编辑课程规则 / 推荐模式配置）。")
    if not p.direction:
        raise OverrideValidationError("方向不能为空（课程/竞赛/项目/校园事务/待定）")
    _validate_component(p.direction, "方向")
    direction_ok = {"课程", "竞赛", "项目", "校园事务", "待定"}
    if p.direction not in direction_ok:
        raise OverrideValidationError(
            f"方向 '{p.direction}' 不在允许集合：{sorted(direction_ok)}")
    # 课程方向：学期必填；其他方向学期空
    if p.direction == "课程":
        _validate_component(p.semester, "学期")
        for seg in p.semester.split(os.sep):
            _validate_component(seg, "学期段")
            if seg in (".", ".."):
                raise OverrideValidationError(
                    f"学期段 '{seg}' 非法（不能是 '.' / '..' / 包含路径分隔符）")
        sem = p.semester
    else:
        sem = ""
    if p.category:
        _validate_component(p.category, "类别")
    new_inner = _normalize_inner_path(p.inner_path)
    cat = p.category
    new_dir = p.direction
    new_sem = sem
    new_cat = cat
    new_ftype = target.file_type
    new_course = cat or new_dir
    new_reason = (
        f"手动修正：方向「{new_dir}」"
        + (f" / 学期「{new_sem}」" if new_sem else "")
        + (f" / 类别「{new_cat}」" if new_cat else "")
        + (f" / 内部「{new_inner}」" if new_inner else "")
    )

    # 2) 构造新目标目录（复用 planner）
    new_target_dir = _compute_target_dir(
        mode, new_dir or None, new_sem or None, new_cat or None,
        new_ftype, inner_path=new_inner,
    )

    # 3) 更新条目字段（自动快照不动）
    target.direction = new_dir or None
    target.semester = new_sem or None
    target.category = new_cat or None
    target.inner_path = new_inner or None
    target.course = new_course
    target.file_type = new_ftype
    target.target_dir_rel = new_target_dir
    target.target_rel_path = os.path.join(new_target_dir, target.file.filename)
    target.target_filename = target.file.filename
    target.rule_applied = new_reason
    target.rule_key = f"manual_override:{source_rel}"
    target.is_manual_overridden = True
    target.override_note = p.override_note or ""

    # 4) 整批重算唯一名称（包括自身 + 可能被连带冲突的其他条目）
    _recompute_unique_names(plan)

    # 5) 如果我们的目标最终分配了 (1)/(2) 后缀，更新依据里提示
    if target.target_filename != target.file.filename:
        target.rule_applied = (
            target.rule_applied
            + f"（重名冲突，程序自动分配唯一名：{target.target_filename}）"
        )

    return target


def revert_auto_override(plan: OrganizePlan, source_rel: str) -> Optional[PlanItem]:
    """恢复自动分类：把 PlanItem 的 auto_* 快照复制回当前分类字段，
    重算目标路径，is_manual_overridden 置 False。

    不能调用 classify_context 或读当前配置——那样会得到"按当前配置重跑"，
    用户要的是"还原到第一次自动生成的结果"（配置可能已被改过）。
    """
    target = None
    for item in plan.items:
        if item.file.rel_path == source_rel:
            target = item
            break
    if target is None:
        return None

    # 只支持推荐模式：其他模式的自动分类字段不记录方向/学期/类别，
    # 恢复只会得到空引用。必须在改动字段之前返回，避免留下半改状态。
    if plan.mode != MODE_BY_DIRECTION_SEMESTER:
        return None

    # 自动快照字段应该始终存在；极端情况下（旧快照/直接拼的 PlanItem）
    # 可能全是空串——那就什么都不做，返回 None
    if not target.auto_direction and not target.auto_category and not target.auto_inner_path:
        return None

    mode = plan.mode
    target.direction = target.auto_direction
    target.semester = target.auto_semester
    target.category = target.auto_category
    target.inner_path = target.auto_inner_path
    target.rule_key = target.auto_rule_key
    target.rule_applied = target.auto_rule_applied
    target.is_manual_overridden = False
    target.override_note = ""

    target.course = target.auto_category or target.auto_direction or ""
    new_target_dir = _compute_target_dir(
        mode, target.auto_direction, target.auto_semester,
        target.auto_category, target.file_type,
        inner_path=target.auto_inner_path or "",
    )

    target.target_dir_rel = new_target_dir
    target.target_filename = target.file.filename
    target.target_rel_path = os.path.join(new_target_dir, target.file.filename)

    _recompute_unique_names(plan)
    return target


def summarize_manual_overrides(plan: OrganizePlan) -> Tuple[int, List[str]]:
    """返回 (手动修正文件数, 每条修正的一行概要)。"""
    overrides = [i for i in plan.items if i.is_manual_overridden]
    lines = []
    for i in overrides:
        old = (f"[{i.auto_direction or ''}/{i.auto_category or ''}]"
               if i.auto_direction or i.auto_category else "自动")
        new = f"{i.target_rel_path}"
        note = f"（{i.override_note}）" if i.override_note else ""
        lines.append(f"{i.file.rel_path}  自动={old}  目标={new}{note}")
    return len(overrides), lines
