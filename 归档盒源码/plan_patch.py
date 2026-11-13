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

import copy
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from planner import (
    PlanItem, OrganizePlan, TARGET_ROOT,
    _build_target_dir_v2,
    _sanitize_component, _assign_unique_names,
    assign_unique_targets,
    MODE_BY_DIRECTION_SEMESTER, build_plan, ocr_maps_from_plan,
)
from classification_config import (
    LongTermRule, new_rule_id, clone_config, validate_config, ensure_valid,
    MATCH_EXACT_PATH, MATCH_DIR_SCOPE, MATCH_FILENAME_KEYWORD,
    MATCH_TYPE_LABELS, MATCH_LABEL_TO_TYPE,
    INNER_KEEP, INNER_CLEAR, INNER_REPLACE,
    INNER_MODE_LABELS, INNER_LABEL_TO_MODE,
    LONG_TERM_DIRECTIONS, ClassificationConfig,
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
            QMessageBox, QCheckBox, QTextEdit,
        )
        return (Qt, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                QLineEdit, QComboBox, QLabel, QDialogButtonBox, QPushButton,
                QMessageBox, QCheckBox, QTextEdit), None
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

    def __init__(self, plan: OrganizePlan, source_rel: str, mode: str = None,
                 rule_context=None):
        if mode is None:
            mode = plan.mode
        self.plan = plan
        self.source_rel = source_rel
        self.mode = mode
        # 保存长期规则所需上下文（scan_result / config / save_callback）；
        # 为 None 时「保存为长期规则…」按钮禁用。
        self.rule_context = rule_context
        self.long_term_saved_config = None
        self.saved_rule = None
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
         QMessageBox, QCheckBox, QTextEdit) = self._qt_ok
        self.Qt = Qt
        self.QDialogButtonBox = QDialogButtonBox
        self.QMessageBox = QMessageBox

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

        self.override_hint = QLabel("这次修改只对当前预览生效。重新扫描、重新生成预览或切换文件夹后会恢复自动分类。")
        self.override_hint.setStyleSheet("color:#E65100;")
        self.override_hint.setWordWrap(True)
        lay.addWidget(self.override_hint)

        btn_row = QHBoxLayout()
        self.btn_revert = QPushButton("恢复自动分类")
        self.btn_revert.clicked.connect(self._on_revert_auto)
        btn_row.addWidget(self.btn_revert)
        self.btn_save_rule = QPushButton("保存为长期规则…")
        self.btn_save_rule.clicked.connect(self._on_save_as_rule)
        if rule_context is None:
            self.btn_save_rule.setEnabled(False)
            self.btn_save_rule.setToolTip(
                "当前没有可用的配置上下文（缺少扫描结果或保存入口），"
                "只能应用到本次计划。")
        btn_row.addWidget(self.btn_save_rule)
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
            # S2-20/S2-21：预览与「点确定」走完全相同的校验与唯一名重算，
            # 不再出现"预览通过、点确定报错"或"预览 x.pdf、实际 x (1).pdf"。
            checked = validate_override_params(self.mode, params)
            target_path = preview_override_target(
                self.plan, self.source_rel, checked)
            target_dir = os.path.dirname(target_path)
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
            self.QMessageBox.information(
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

    def _on_save_as_rule(self):
        """把当前编辑结果转成长期规则草稿，交给 RuleDraftDialog 试运行并保存。"""
        if self._qt_ok is None or self.rule_context is None:
            return
        try:
            params = validate_override_params(self.mode, self._current_params())
        except OverrideValidationError as e:
            self.QMessageBox.warning(self.dlg, "无法保存长期规则", str(e))
            return

        ctx = self.rule_context
        draft = RuleDraftDialog(
            mode=self.mode,
            plan=self.plan,
            source_rel=self.source_rel,
            params=params,
            scan_result=ctx.get("scan_result"),
            config=ctx.get("config"),
            save_callback=ctx.get("save_callback"),
            worker_registry=ctx.get("worker_registry"),
            parent=self.dlg,
        )
        if not draft.exec():
            return
        self.saved_rule = draft.saved_rule
        self.long_term_saved_config = draft.saved_config
        self._revert_on_ok = False
        # 长期规则已保存 → 本次计划必然失效，直接关闭；
        # main.py 依据 long_term_saved_config 处理配置刷新与提示。
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
    """重新分配批次内唯一名称。

    S2-21/S2-23：与 planner 共用同一个稳定分配器（assign_unique_targets），
    因此「预览看到的目标路径」与「点确定后实际写入的目标路径」完全一致，
    重复计算也不会把 (1)(2) 换来换去，只改无关的一行不会连累其他条目。
    """
    assign_unique_targets(plan.items, plan.root)


def preview_override_target(plan: OrganizePlan, source_rel: str,
                            params: OverrideParams) -> str:
    """只读预览：返回「点确定后」该条目真正会用的目标相对路径。

    实现方式与 apply_override_to_plan 完全一致（在同一份计划副本上应用 +
    整批重算唯一名），因此预览结果不可能是"看起来合法、点确定报错"
    或"预览 x.pdf、实际 x (1).pdf"。
    校验失败时抛 OverrideValidationError，供对话框显示原因。
    """
    # 只做浅拷贝：apply_override_to_plan 只改条目的目标字段，
    # 不会改 FileEntry；大计划下比 deepcopy 轻得多。
    simulated = copy.copy(plan)
    simulated.items = [copy.copy(item) for item in plan.items]
    applied = apply_override_to_plan(simulated, source_rel, params)
    return applied.target_rel_path


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


def validate_override_params(mode: str, params: OverrideParams) -> OverrideParams:
    """校验并规范化手动修正参数，返回新对象。

    「应用到本次计划」与「保存为长期规则」共用这一套规则，
    保证两处接受的字段范围完全一致。
    """
    if mode != MODE_BY_DIRECTION_SEMESTER:
        raise OverrideValidationError(
            "本轮只支持在「按方向/学期/类别（推荐）」模式下修正单个文件；"
            "其他分类方式请使用「课程关键词」或「学期和目录规则」。")
    p = params
    if not p.direction:
        raise OverrideValidationError("方向不能为空（课程/竞赛/项目/校园事务/待定）")
    _validate_component(p.direction, "方向")
    direction_ok = {"课程", "竞赛", "项目", "校园事务", "待定"}
    if p.direction not in direction_ok:
        raise OverrideValidationError(
            f"方向 '{p.direction}' 不在允许集合：{sorted(direction_ok)}")
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
    inner = _normalize_inner_path(p.inner_path)
    return OverrideParams(
        direction=p.direction,
        semester=sem,
        category=p.category,
        inner_path=inner,
        file_type=p.file_type,
        override_note=p.override_note,
    )


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

    # 1) 校验并规范化参数（与「保存为长期规则」共用同一套规则）
    mode = plan.mode
    p = validate_override_params(mode, params)
    new_inner = p.inner_path
    new_dir = p.direction
    new_sem = p.semester
    new_cat = p.category
    new_ftype = target.file_type
    new_course = new_cat or new_dir
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


# ======================== 保存为长期规则（V1.5） ========================

@dataclass
class RuleImpact:
    """长期规则试运行结果（纯数据，供界面展示）。"""
    matched: int = 0
    changed: int = 0
    conflicts: int = 0
    # (源相对路径, 旧目标, 新目标, 命中依据)
    samples: List[Tuple[str, str, str, str]] = field(default_factory=list)
    error: str = ""


def build_rule_draft(
    *,
    params: OverrideParams,
    name: str,
    match_type: str,
    source_path: str,
    include_subdirs: bool,
    keywords: List[str],
    inner_mode: str,
    inner_path: str,
    reason: str = "",
) -> LongTermRule:
    """由一次手动修正的目标分类 + 用户选择的匹配条件构造规则。"""
    return LongTermRule(
        rule_id=new_rule_id(),
        name=name,
        match_type=match_type,
        source_path=source_path,
        include_subdirs=bool(include_subdirs),
        keywords=[k for k in keywords if k],
        direction=params.direction,
        semester=params.semester,
        category=params.category,
        inner_mode=inner_mode,
        inner_path=inner_path,
        enabled=True,
        reason=reason,
    )


def preview_rule_impact(scan_result, mode: str, rules, config,
                        rule: LongTermRule, cancel_event=None,
                        ocr_texts=None, ocr_errors=None) -> RuleImpact:
    """在不改动任何文件的前提下，计算"加入该规则后"的分类变化。

    - 与正式计划共用 build_plan（同一套分类、路径构造与冲突处理）；
    - compute_duplicates=False，不计算 SHA-256、不写历史；
    - 新规则插入到最前，代表"保存后立即生效"的优先级；
    - 支持 cancel_event，供后台线程在关闭窗口时协作取消。
    """
    if scan_result is None:
        return RuleImpact(error="当前没有可用的扫描结果，无法试运行。")
    try:
        base_cfg = clone_config(config)
        new_cfg = clone_config(config)
        new_cfg.long_term_rules.insert(0, rule)
        ensure_valid(new_cfg)
    except Exception as e:
        return RuleImpact(error=f"规则未通过校验：{e}")
    try:
        old_plan = build_plan(scan_result, mode, rules, config=base_cfg,
                              compute_duplicates=False,
                              cancel_event=cancel_event,
                              ocr_texts=ocr_texts, ocr_errors=ocr_errors)
        if cancel_event is not None and cancel_event.is_set():
            return RuleImpact(error="已取消")
        new_plan = build_plan(scan_result, mode, rules, config=new_cfg,
                              compute_duplicates=False,
                              cancel_event=cancel_event,
                              ocr_texts=ocr_texts, ocr_errors=ocr_errors)
        if cancel_event is not None and cancel_event.is_set():
            return RuleImpact(error="已取消")
    except Exception as e:
        return RuleImpact(error=f"试运行失败：{e}")

    old_map = {i.file.rel_path: i for i in old_plan.items}
    key = f"long_term:{rule.rule_id}"
    matched = changed = conflicts = 0
    samples: List[Tuple[str, str, str, str]] = []
    for item in new_plan.items:
        if item.rule_key == key:
            matched += 1
        old = old_map.get(item.file.rel_path)
        if old is not None and old.target_rel_path != item.target_rel_path:
            changed += 1
            if len(samples) < 8:
                samples.append((item.file.rel_path, old.target_rel_path,
                                item.target_rel_path, item.rule_applied))
        if item.conflict_with_existing:
            conflicts += 1
    return RuleImpact(matched, changed, conflicts, samples)


_RULE_WORKER_CLS = None


def rule_preview_worker_class():
    """延迟构造 QThread 子类：模块导入阶段不接触 Qt。

    每次试运行创建一个新线程；结果带 seq，界面据此丢弃迟到结果。
    """
    global _RULE_WORKER_CLS
    if _RULE_WORKER_CLS is not None:
        return _RULE_WORKER_CLS

    import threading
    from PySide6.QtCore import QThread, Signal

    class RulePreviewWorker(QThread):
        finished_ok = Signal(int, object)   # seq, RuleImpact
        failed = Signal(int, str)           # seq, error
        cancelled = Signal(int)             # seq

        def __init__(self, seq: int, scan_result, mode: str, rules,
                     config, rule, parent=None,
                     ocr_texts=None, ocr_errors=None):
            # 不把 parent 设为对话框：线程生命周期可能长于对话框，
            # 由主窗口登记表保活，结束后再回收（同 config_dialog 的做法）。
            super().__init__(None)
            self.seq = seq
            self.scan_result = scan_result
            self.mode = mode
            self.rules = rules
            self.config = config
            self.rule = rule
            self.ocr_texts = ocr_texts
            self.ocr_errors = ocr_errors
            self.cancel_event = threading.Event()

        def request_cancel(self):
            self.cancel_event.set()

        def run(self):
            try:
                impact = preview_rule_impact(
                    self.scan_result, self.mode, self.rules,
                    self.config, self.rule,
                    cancel_event=self.cancel_event,
                    ocr_texts=self.ocr_texts, ocr_errors=self.ocr_errors)
                if self.cancel_event.is_set() or impact.error == "已取消":
                    self.cancelled.emit(self.seq)
                    return
                self.finished_ok.emit(self.seq, impact)
            except Exception as e:              # 线程内异常不能外抛
                try:
                    self.failed.emit(self.seq, str(e))
                except Exception:
                    pass

    _RULE_WORKER_CLS = RulePreviewWorker
    return _RULE_WORKER_CLS


class RuleDraftDialog:
    """为一条长期修正规则选择匹配条件、试运行并保存。

    组合 QDialog（不在类定义阶段创建 Qt 对象），
    与 ManualOverrideDialog 保持一致的延迟导入方式。
    """

    def __init__(self, *, mode: str, plan: OrganizePlan, source_rel: str,
                 params: OverrideParams, scan_result, config,
                 save_callback, worker_registry=None, parent=None):
        self.mode = mode
        self.plan = plan
        self.source_rel = source_rel
        self.params = params
        self.scan_result = scan_result
        self.config = config
        self.save_callback = save_callback
        self.worker_registry = worker_registry
        self.saved_rule: Optional[LongTermRule] = None
        self.saved_config = None
        self._impact: Optional[RuleImpact] = None
        self._previewed_rule: Optional[LongTermRule] = None
        self._preview_seq = 0
        self._worker = None
        self._previewing = False
        self._closed = False
        self._qt_ok, self._err = _ensure_qt()
        if self._qt_ok is None:
            return

        (Qt, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
         QLineEdit, QComboBox, QLabel, QDialogButtonBox, QPushButton,
         QMessageBox, QCheckBox, QTextEdit) = self._qt_ok
        self.Qt = Qt
        self.QDialogButtonBox = QDialogButtonBox
        self.QMessageBox = QMessageBox

        self.dlg = QDialog(parent)
        self.dlg.setWindowTitle("保存为长期规则 — 匹配条件与目标")
        self.dlg.setMinimumWidth(700)
        self.dlg.resize(760, 640)
        lay = QVBoxLayout(self.dlg)

        head = QLabel(
            f"来源文件：{self.source_rel}\n"
            f"本次修正后的目标：{params.direction} / "
            f"{params.semester or '（无学期）'} / "
            f"{params.category or '（无类别）'}"
            + (f" / {params.inner_path}" if params.inner_path else "")
            + "\n长期规则会覆盖原有学期/方向/目录映射，但仍低于当前计划的临时修正。"
        )
        head.setStyleSheet("padding:6px;background:#f5f5f5;border-radius:4px;")
        head.setWordWrap(True)
        lay.addWidget(head)

        form = QFormLayout()
        default_name = (os.path.basename(os.path.dirname(source_rel))
                        or os.path.basename(source_rel) or "新规则")
        self.name_edit = QLineEdit(default_name + " 修正规则")
        form.addRow("规则名称", self.name_edit)

        self.match_combo = QComboBox()
        for key in (MATCH_EXACT_PATH, MATCH_DIR_SCOPE,
                    MATCH_FILENAME_KEYWORD):
            self.match_combo.addItem(MATCH_TYPE_LABELS[key], key)
        form.addRow("匹配方式", self.match_combo)

        self.source_edit = QLineEdit(source_rel)
        self.source_label = QLabel("来源相对路径")
        form.addRow(self.source_label, self.source_edit)

        self.subdir_check = QCheckBox("包含子目录")
        self.subdir_check.setChecked(True)
        form.addRow("目录范围", self.subdir_check)

        self.kw_edit = QLineEdit("")
        self.kw_edit.setPlaceholderText("多个关键词用 / 分隔，如 期末/试卷")
        form.addRow("文件名关键词", self.kw_edit)

        self.dir_combo = QComboBox()
        self.dir_combo.addItems(LONG_TERM_DIRECTIONS)
        if params.direction in LONG_TERM_DIRECTIONS:
            self.dir_combo.setCurrentText(params.direction)
        form.addRow("目标方向", self.dir_combo)

        self.sem_edit = QLineEdit(params.semester or "")
        form.addRow("目标学期", self.sem_edit)

        self.cat_edit = QLineEdit(params.category or "")
        form.addRow("目标类别", self.cat_edit)

        self.inner_combo = QComboBox()
        for key in (INNER_KEEP, INNER_REPLACE, INNER_CLEAR):
            self.inner_combo.addItem(INNER_MODE_LABELS[key], key)
        orig_inner = None
        for i in plan.items:
            if i.file.rel_path == source_rel:
                orig_inner = i.inner_path or ""
                break
        if params.inner_path and params.inner_path != (orig_inner or ""):
            self.inner_combo.setCurrentText(INNER_MODE_LABELS[INNER_REPLACE])
        form.addRow("内部路径", self.inner_combo)

        self.inner_edit = QLineEdit(params.inner_path or "")
        self.inner_edit.setPlaceholderText("仅「替换为指定路径」时生效，如 实验一/分组报告")
        form.addRow("内部路径值", self.inner_edit)
        lay.addLayout(form)

        self.rule_desc = QLabel("")
        self.rule_desc.setWordWrap(True)
        self.rule_desc.setStyleSheet(
            "padding:6px;background:#E3F2FD;border-radius:4px;")
        lay.addWidget(self.rule_desc)

        self.impact_view = QTextEdit()
        self.impact_view.setReadOnly(True)
        self.impact_view.setMinimumHeight(150)
        lay.addWidget(self.impact_view, 1)

        btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("试运行（查看会影响哪些文件）")
        self.btn_preview.clicked.connect(self._on_preview)
        btn_row.addWidget(self.btn_preview)
        self.preview_hint = QLabel("")
        self.preview_hint.setStyleSheet("color:#1565C0;")
        self.preview_hint.setWordWrap(True)
        btn_row.addWidget(self.preview_hint, 1)
        lay.addLayout(btn_row)

        self.bb = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Save).setText("保存为长期规则")
        self.bb.button(QDialogButtonBox.Cancel).setText("取消")
        self.bb.accepted.connect(self._on_save)
        self.bb.rejected.connect(self.dlg.reject)
        lay.addWidget(self.bb)

        # 所有控件就绪后再连接信号，避免构造期间访问尚未创建的控件
        self.match_combo.currentIndexChanged.connect(self._on_match_changed)
        self.source_edit.textChanged.connect(self._on_match_changed)
        self.subdir_check.stateChanged.connect(self._on_match_changed)
        self.kw_edit.textChanged.connect(self._on_match_changed)
        self.name_edit.textChanged.connect(self._on_match_changed)
        self.dir_combo.currentIndexChanged.connect(self._on_match_changed)
        self.sem_edit.textChanged.connect(self._on_match_changed)
        self.cat_edit.textChanged.connect(self._on_match_changed)
        # 内部路径的方式与取值同样影响最终保存内容，必须接入缓存失效
        self.inner_combo.currentIndexChanged.connect(self._on_match_changed)
        self.inner_edit.textChanged.connect(self._on_match_changed)
        # 关闭（取消/ESC/窗口关闭）时协作式取消后台试运行，不阻塞界面
        self.dlg.rejected.connect(self._on_closed)
        self._on_match_changed()

    # ---- 内部 ----

    def _match_type(self) -> str:
        return self.match_combo.currentData()

    def _on_match_changed(self, *_):
        if self._qt_ok is None or self._previewing:
            return
        mt = self._match_type()
        if mt == MATCH_EXACT_PATH:
            self.source_label.setText("来源相对路径")
            self.source_edit.setEnabled(True)
            self.subdir_check.setEnabled(False)
            self.kw_edit.setEnabled(False)
        elif mt == MATCH_DIR_SCOPE:
            self.source_label.setText("来源目录（留空=根目录）")
            self.source_edit.setEnabled(True)
            self.subdir_check.setEnabled(True)
            self.kw_edit.setEnabled(False)
        else:
            self.source_label.setText("限定目录（留空=不限目录）")
            self.source_edit.setEnabled(True)
            self.subdir_check.setEnabled(False)
            self.kw_edit.setEnabled(True)
        had_impact = self._impact is not None
        self._impact = None
        self._previewed_rule = None
        if had_impact:
            # 条件变化后旧结果作废，避免把过期影响写进保存确认
            self.impact_view.clear()
            self.preview_hint.setText("匹配条件已修改，请重新试运行")
        self._update_rule_desc()

    def _update_rule_desc(self):
        mt = self._match_type()
        arrow = (f"→ {self.dir_combo.currentText() or '（空）'} / "
                 f"{self.sem_edit.text().strip() or '（无学期）'} / "
                 f"{self.cat_edit.text().strip() or '（无类别）'}")
        if mt == MATCH_EXACT_PATH:
            cond = f"来源路径 = {self.source_edit.text().strip() or '（空）'}"
        elif mt == MATCH_DIR_SCOPE:
            cond = (f"来源目录 = {self.source_edit.text().strip() or '根目录'}"
                    + ("（含子目录）" if self.subdir_check.isChecked() else "（仅本级）"))
        else:
            kws = self.kw_edit.text().strip() or "（未填关键词）"
            scope = self.source_edit.text().strip()
            cond = f"文件名关键词 = {kws}" + (f"，限定目录 = {scope}" if scope else "")
        self.rule_desc.setText(
            f"<b>规则预览：</b>{self.name_edit.text().strip() or '（未命名）'}<br>"
            f"<b>匹配：</b>{cond}<br><b>目标：</b>{arrow}")

    def _current_rule(self) -> LongTermRule:
        mt = self._match_type()
        kws = [k.strip() for k in re.split(r"[/，,;；]", self.kw_edit.text())
               if k.strip()]
        inner_mode = self.inner_combo.currentData()
        inner_path = (self.inner_edit.text().strip()
                      if inner_mode == INNER_REPLACE else "")
        return build_rule_draft(
            params=OverrideParams(
                direction=self.dir_combo.currentText(),
                semester=self.sem_edit.text().strip(),
                category=self.cat_edit.text().strip(),
                inner_path="",
                file_type=self.params.file_type,
            ),
            name=self.name_edit.text().strip(),
            match_type=mt,
            source_path=self.source_edit.text().strip(),
            include_subdirs=self.subdir_check.isChecked(),
            keywords=kws,
            inner_mode=inner_mode,
            inner_path=inner_path,
            reason=f"由手动修正生成：{self.source_rel}",
        )

    def _validate_current(self) -> Optional[LongTermRule]:
        rule = self._current_rule()
        try:
            cfg = clone_config(self.config)
            cfg.long_term_rules.insert(0, rule)
            ensure_valid(cfg)
        except Exception as e:
            self.QMessageBox.warning(
                self.dlg, "规则未通过校验",
                f"{e}\n\n请检查名称、匹配条件、方向/学期/类别与内部路径。")
            return None
        return rule

    @staticmethod
    def _rule_signature(rule) -> tuple:
        """业务字段签名（不含 rule_id）。

        `_current_rule()` 每次都会生成新的随机 rule_id，不能拿它判断
        “条件是否变化”；这里只比较真正会影响命中与目标的字段。
        """
        if rule is None:
            return ()
        return (
            rule.name,
            rule.match_type,
            rule.source_path,
            bool(rule.include_subdirs),
            tuple(rule.keywords),
            rule.direction,
            rule.semester,
            rule.category,
            rule.inner_mode,
            rule.inner_path,
            bool(rule.enabled),
        )

    def _invalidate_preview(self):
        """作废试运行缓存，避免保存过期规则。"""
        self._impact = None
        self._previewed_rule = None
        if self._qt_ok is not None:
            self.impact_view.clear()
            self.preview_hint.setText("匹配条件或目标已修改，请重新试运行")

    def _set_previewing(self, value: bool):
        self._previewing = value
        for w in (self.match_combo, self.source_edit, self.subdir_check,
                  self.kw_edit, self.name_edit, self.dir_combo,
                  self.sem_edit, self.cat_edit, self.inner_combo,
                  self.inner_edit):
            w.setEnabled(not value)
        self.btn_preview.setEnabled(not value)
        self.bb.button(self.QDialogButtonBox.Save).setEnabled(not value)
        if value:
            self.preview_hint.setText("试运行中……（不移动文件、不写历史）")

    def _on_preview(self):
        """后台计算试运行结果；界面线程立即返回，不阻塞。"""
        if self._previewing or self._closed:
            return
        if self.scan_result is None:
            self.QMessageBox.information(
                self.dlg, "试运行",
                "当前没有可用的扫描结果，无法试运行。\n"
                "请先在主窗口扫描目录后再打开修正对话框。")
            return
        rule = self._validate_current()
        if rule is None:
            return
        # 记住本次试运行用的规则对象，保存时复用同一 rule_id，
        # 使确认框里的命中统计与最终写入配置的标识一致。
        self._previewed_rule = rule

        ocr_texts, ocr_errors = ocr_maps_from_plan(self.plan)
        try:
            Worker = rule_preview_worker_class()
        except Exception:
            # 没有 Qt 线程环境时退化为同步计算（仅影响非界面使用场景）
            impact = preview_rule_impact(
                self.scan_result, self.mode, self.plan.rules,
                self.config, rule,
                ocr_texts=ocr_texts, ocr_errors=ocr_errors)
            self._apply_impact(impact)
            return

        self._preview_seq += 1
        seq = self._preview_seq
        worker = Worker(seq, self.scan_result, self.mode, self.plan.rules,
                        self.config, rule,
                        ocr_texts=ocr_texts, ocr_errors=ocr_errors)
        self._worker = worker
        worker.finished_ok.connect(self._on_preview_done)
        worker.failed.connect(self._on_preview_failed)
        worker.cancelled.connect(self._on_preview_cancelled)
        # 组合式对话框不是 QObject，没有 sender()，用默认参数绑定本次线程
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        if self.worker_registry is not None:
            try:
                self.worker_registry.register_preview_worker(worker)
            except Exception:
                pass
        self._set_previewing(True)
        worker.start()

    def _apply_impact(self, impact: RuleImpact):
        self._impact = impact
        self.impact_view.setPlainText(self._render_impact(impact))
        if impact.error:
            self.preview_hint.setText("试运行失败，请修正后再试")
        else:
            self.preview_hint.setText(
                f"命中 {impact.matched} 个文件，"
                f"{impact.changed} 个目标位置变化")

    def _seq_ok(self, seq: int) -> bool:
        """迟到结果：窗口已关闭或 seq 不是最新时丢弃。"""
        return (not self._closed) and seq == self._preview_seq

    def _on_preview_done(self, seq, impact):
        if not self._seq_ok(seq):
            return
        self._set_previewing(False)
        self._apply_impact(impact)

    def _on_preview_failed(self, seq, err):
        if not self._seq_ok(seq):
            return
        self._set_previewing(False)
        self._impact = None
        self._previewed_rule = None
        self.preview_hint.setText("试运行失败")
        self.impact_view.setPlainText(f"⚠ {err}")

    def _on_preview_cancelled(self, seq):
        if not self._seq_ok(seq):
            return
        self._set_previewing(False)
        self._previewed_rule = None
        self.preview_hint.setText("试运行已取消")

    def _on_worker_finished(self, worker):
        """线程真正结束后回收引用（不阻塞界面线程）。"""
        if worker is not None:
            self._release_worker(worker)

    def _release_worker(self, worker):
        if self.worker_registry is not None:
            try:
                self.worker_registry.unregister_preview_worker(worker)
            except Exception:
                pass
        if self._worker is worker:
            self._worker = None
        # 断开 finished 连接，避免「线程对象→信号→闭包→线程对象」的引用环，
        # 使 QThread 能在应用仍存活时被回收，而不是留到解释器退出阶段。
        try:
            worker.finished.disconnect()
        except Exception:
            pass
        try:
            worker.deleteLater()
        except Exception:
            pass

    @staticmethod
    def _render_impact(impact: RuleImpact) -> str:
        if impact.error:
            return f"⚠ {impact.error}"
        lines = [
            f"命中文件数：{impact.matched}",
            f"目标位置变化数：{impact.changed}",
            f"目标冲突（磁盘已存在同名）：{impact.conflicts}",
        ]
        if impact.samples:
            lines.append("")
            lines.append("代表样本：")
            for rel, old_t, new_t, why in impact.samples:
                lines.append(f"· {rel}")
                lines.append(f"   旧目标：{old_t}")
                lines.append(f"   新目标：{new_t}")
                lines.append(f"   依据：{why}")
        else:
            lines.append("")
            lines.append("（没有文件的目标位置会发生变化）")
        return "\n".join(lines)

    def _on_save(self):
        rule = self._validate_current()
        if rule is None:
            return
        # 保存前不再同步复算：直接使用最近一次后台试运行的结果，
        # 避免在界面线程重复做一遍完整分类（会卡住窗口）。
        impact = self._impact
        if impact is None or self._previewed_rule is None:
            self.QMessageBox.information(
                self.dlg, "请先试运行",
                "请先点「试运行」查看会影响哪些文件，确认后再保存规则。")
            return
        # 一致性校验：比较当前草稿与已试运行快照的业务字段。
        # 不能只依赖控件信号（可能漏接）或对象引用（rule_id 每次新生成）。
        if self._rule_signature(rule) != self._rule_signature(self._previewed_rule):
            self._invalidate_preview()
            self.QMessageBox.information(
                self.dlg, "请重新试运行",
                "匹配条件或目标字段已修改，之前试运行的结果已作废。\n"
                "请重新点「试运行」，确认结果后再保存。")
            return
        # 与试运行使用同一规则对象，保证 rule_id 一致
        rule = self._previewed_rule
        if impact.error:
            self.QMessageBox.warning(self.dlg, "试运行失败", impact.error)
            return
        if impact.matched == 0:
            ret = self.QMessageBox.question(
                self.dlg, "没有文件命中",
                "按当前条件试运行没有命中任何文件。\n"
                "保存后该规则暂时不会生效，仍要保存吗？",
                self.QMessageBox.Yes | self.QMessageBox.No,
                self.QMessageBox.No)
            if ret != self.QMessageBox.Yes:
                return
        ret = self.QMessageBox.question(
            self.dlg, "确认保存长期规则",
            f"规则：{rule.name}\n"
            f"命中 {impact.matched} 个文件，{impact.changed} 个目标位置变化。\n\n"
            "保存后：\n"
            "· 规则写入该目录的配置并在重启后继续生效；\n"
            "· 当前计划的临时手动修正会随计划一起失效；\n"
            "· 需要点击「重新生成预览」才能按新规则生成计划。\n\n继续保存吗？",
            self.QMessageBox.Yes | self.QMessageBox.No,
            self.QMessageBox.Yes)
        if ret != self.QMessageBox.Yes:
            return

        new_cfg = clone_config(self.config)
        new_cfg.long_term_rules.insert(0, rule)
        try:
            ensure_valid(new_cfg)
        except Exception as e:
            self.QMessageBox.critical(
                self.dlg, "保存失败", f"{e}\n现有配置未被修改。")
            return
        if self.save_callback is None:
            self.QMessageBox.critical(
                self.dlg, "保存失败", "没有可用的保存入口。")
            return
        try:
            self.save_callback(new_cfg)
        except Exception as e:
            self.QMessageBox.critical(
                self.dlg, "保存失败",
                f"{e}\n\n已生效的规则保持不变，配置文件未被覆盖。")
            return

        self.saved_rule = rule
        self.saved_config = new_cfg
        self.dlg.accept()

    def _on_closed(self):
        """窗口已关闭：标记关闭并请求取消，不等待线程退出。"""
        self._closed = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()

    # ---- 对外 ----

    def exec(self) -> bool:
        if self._qt_ok is None:
            return False
        result = self.dlg.exec()
        # 无论确定还是取消，离开模态后都不再接收迟到结果
        self._on_closed()
        return result == 1

