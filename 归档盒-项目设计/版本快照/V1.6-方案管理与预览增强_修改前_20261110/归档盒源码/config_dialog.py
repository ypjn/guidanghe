"""
归档盒 V1.3 — 推荐模式分类配置编辑器

只影响「按方向/学期/类别（推荐）」模式：
- 学期映射、顶层方向、学期内目录、通用目录前缀、关键词兜底；
- 增删改、启用/停用、上移/下移（列表顺序即匹配优先级）、恢复默认；
- 「试运行／查看影响」用独立草稿计算，不移动文件、不写历史；
- 保存失败时留在对话框内并指出具体规则，不显示虚假的“已保存”。

「按类型」「按课程再按类型」不使用本配置；课程关键词规则仍在主窗口
「编辑课程规则…」中维护（只影响「按课程再按类型」）。
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from classification_config import (
    CONFIG_VERSION, DIRECTIONS, LONG_TERM_DIRECTIONS,
    INNER_CLEAR, INNER_KEEP, INNER_REPLACE,
    INNER_MODE_SHORT, INNER_SHORT_TO_MODE,
    SCOPE_FILENAME, SCOPE_LABELS, SCOPE_PATH,
    MATCH_EXACT_PATH, MATCH_DIR_SCOPE, MATCH_FILENAME_KEYWORD,
    MATCH_TYPE_LABELS, MATCH_LABEL_TO_TYPE,
    TRIGGER_LABELS, TRIGGER_SUBDIR, TRIGGER_UNMATCHED,
    ClassificationConfig, ClassificationConfigError, GenericDirRule,
    KeywordRule, LongTermRule, SemesterDirRule, SemesterRule, TopDirectionRule,
    default_config, is_default_config, new_rule_id, validate_config,
)
from planner import MODE_BY_DIRECTION_SEMESTER
from rule_preview import ImpactResult, PreviewWorker


SCOPE_KEY_TO_LABEL = dict(SCOPE_LABELS)
SCOPE_LABEL_TO_KEY = {v: k for k, v in SCOPE_LABELS.items()}
TRIGGER_KEY_TO_LABEL = dict(TRIGGER_LABELS)
TRIGGER_LABEL_TO_KEY = {v: k for k, v in TRIGGER_LABELS.items()}


def _norm_root(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _check_item(checked: bool) -> QTableWidgetItem:
    item = QTableWidgetItem()
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
    item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    return item


def _text_item(value: str, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(str(value))
    if tip:
        item.setToolTip(tip)
    return item


def _make_table(headers: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    return t


def _set_rows(table: QTableWidget, rows: List[list]):
    table.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, value in enumerate(cells):
            if c == 0:
                table.setItem(r, c, _check_item(bool(value)))
            else:
                table.setItem(r, c, _text_item(value))


def _row_text(table: QTableWidget, row: int, col: int) -> str:
    item = table.item(row, col)
    return item.text().strip() if item is not None else ""


def _row_enabled(table: QTableWidget, row: int) -> bool:
    item = table.item(row, 0)
    return item is not None and item.checkState() == Qt.Checked


def _add_blank_row(table: QTableWidget, defaults: List[str]):
    r = table.rowCount()
    table.insertRow(r)
    table.setItem(r, 0, _check_item(True))
    for c, value in enumerate(defaults, start=1):
        table.setItem(r, c, _text_item(value))
    table.selectRow(r)


def _delete_selected(table: QTableWidget):
    rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
    for r in rows:
        table.removeRow(r)


def _move_selected(table: QTableWidget, delta: int):
    rows = sorted({i.row() for i in table.selectedIndexes()})
    if len(rows) != 1:
        return
    r = rows[0]
    target = r + delta
    if target < 0 or target >= table.rowCount():
        return
    values = []
    for c in range(table.columnCount()):
        item = table.item(r, c)
        if c == 0:
            values.append(item.checkState() == Qt.Checked if item else False)
        else:
            values.append(item.text() if item else "")
    other = []
    for c in range(table.columnCount()):
        item = table.item(target, c)
        if c == 0:
            other.append(item.checkState() == Qt.Checked if item else False)
        else:
            other.append(item.text() if item else "")
    for c, value in enumerate(values):
        if c == 0:
            table.item(target, c).setCheckState(Qt.Checked if value else Qt.Unchecked)
        else:
            table.item(target, c).setText(str(value))
    for c, value in enumerate(other):
        if c == 0:
            table.item(r, c).setCheckState(Qt.Checked if value else Qt.Unchecked)
        else:
            table.item(r, c).setText(str(value))
    table.selectRow(target)


# ======================== 影响预览结果窗口 ========================

class ImpactPreviewDialog(QDialog):
    """只读展示试运行结果（不提供任何文件操作）。"""

    def __init__(self, result: ImpactResult, parent=None):
        super().__init__(parent)
        self.setWindowTitle("规则试运行 / 影响预览（不会移动文件）")
        self.resize(1040, 700)
        lay = QVBoxLayout(self)

        summary = QLabel("\n".join(result.summary_lines()))
        summary.setWordWrap(True)
        summary.setStyleSheet(
            "background:#ECEFF1;padding:8px 10px;border-radius:4px;")
        lay.addWidget(summary)

        lay.addWidget(QLabel(f"各规则命中数量（共 {len(result.hits)} 条规则命中）："))
        hit_table = _make_table(["分类依据", "命中数量"])
        hit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hit_table.setRowCount(len(result.hits))
        for r, h in enumerate(result.hits):
            hit_table.setItem(r, 0, _text_item(h.label, h.key))
            n = QTableWidgetItem(str(h.count))
            n.setTextAlignment(Qt.AlignCenter)
            hit_table.setItem(r, 1, n)
        lay.addWidget(hit_table, 1)

        lay.addWidget(QLabel(
            f"相比当前生效配置发生变化的文件（显示前 {min(len(result.changed), 500)} / "
            f"{len(result.changed)} 项）："))
        change_table = _make_table(
            ["原位置", "原分类依据", "新分类依据", "原目标位置", "新目标位置"])
        change_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        rows = result.changed[:500]
        change_table.setRowCount(len(rows))
        for r, c in enumerate(rows):
            change_table.setItem(r, 0, _text_item(c.rel_path))
            change_table.setItem(r, 1, _text_item(c.old_reason))
            change_table.setItem(r, 2, _text_item(c.new_reason))
            change_table.setItem(r, 3, _text_item(c.old_target))
            change_table.setItem(r, 4, _text_item(c.new_target))
        lay.addWidget(change_table, 2)

        reps = "\n".join(
            f"{i}. {c.rel_path}\n   依据：{c.new_reason}\n   目标：{c.new_target}"
            for i, c in enumerate(result.representatives, 1)
        ) or "（无）"
        rep_label = QLabel("代表性文件：\n" + reps)
        rep_label.setWordWrap(True)
        rep_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(rep_label)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


# ======================== 配置编辑窗口 ========================

class ClassificationConfigDialog(QDialog):
    """推荐模式规则编辑器（独立草稿；取消不改变生效配置）。"""

    def __init__(self, root: str, config: ClassificationConfig,
                 using_defaults: bool = False, config_error: str = "",
                 scan_result=None, base_config: Optional[ClassificationConfig] = None,
                 save_callback=None, worker_registry=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"推荐模式分类配置 — {os.path.basename(root) or root}")
        self.resize(1080, 720)
        self.root = os.path.abspath(root)
        self.config_error = config_error or ""
        self.scan_result = scan_result
        self.base_config = base_config or config
        self.save_callback = save_callback
        self.worker_registry = worker_registry
        self.result_config: Optional[ClassificationConfig] = None
        self._draft = default_config()
        self._preview_seq = 0
        self._worker: Optional[PreviewWorker] = None
        self._previewing = False
        self._closed = False

        self._build_ui()
        self._load_config(config, using_defaults)

    # ---------------- 界面 ----------------

    def _build_ui(self):
        lay = QVBoxLayout(self)

        tip = QLabel(
            "本配置只影响「按方向/学期/类别（推荐）」模式。"
            "「按类型」「按课程再按类型」不使用这里的映射；"
            "课程关键词规则请用主窗口的「编辑课程规则…」（只影响「按课程再按类型」）。\n"
            "匹配顺序：临时手动修正（仅当前计划）→ 长期修正规则（从上到下取第一条）"
            "→ 学期映射 → 顶层方向 → 学期内目录"
            "（类别留空时用关键词细分）→ 通用目录前缀 → 未登记目录锚定 → 待定。\n"
            "每张表中，启用勾选与从上到下的行顺序就是实际匹配优先级。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#37474F;background:#ECEFF1;padding:8px 10px;"
                          "border-radius:4px;")
        lay.addWidget(tip)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "color:#B71C1C;background:#FFEBEE;padding:6px 10px;border-radius:4px;")
        self.error_label.hide()
        lay.addWidget(self.error_label)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        self.table_semester = _make_table(["启用", "来源目录名", "学期标签"])
        self._set_header_tip(self.table_semester, {
            1: "路径中出现该目录名时，归入右侧学期标签（如「大二上」）。",
            2: "写入目标路径的学期层级；不能包含 / 或 ..。",
        })
        self.tabs.addTab(self._tab_page(self.table_semester, ["", ""]), "学期映射")

        self.table_top = _make_table(["启用", "来源目录名", "方向"])
        self._set_header_tip(self.table_top, {
            1: "仅当路径中没有已登记学期时，按顶层目录名精确匹配。",
            2: "只能是：" + " / ".join(DIRECTIONS),
        })
        self.tabs.addTab(self._tab_page(self.table_top, ["", DIRECTIONS[0]]),
                         "顶层方向")

        self.table_semdir = _make_table(
            ["启用", "学期", "目录名", "方向", "类别/课程（留空=关键词细分）"])
        self._set_header_tip(self.table_semdir, {
            1: "必须是「学期映射」中已登记的学期标签。",
            2: "学期目录下的一级目录名（精确匹配）。",
            4: "留空时，用「关键词兜底」中触发场景为「类别留空的目录」的规则细分。",
        })
        self.tabs.addTab(
            self._tab_page(self.table_semdir, ["", "", DIRECTIONS[0], ""]),
            "学期内目录")

        self.table_generic = _make_table(
            ["启用", "目录名前缀", "方向", "类别"])
        self._set_header_tip(self.table_generic, {
            1: "学期内未登记目录按前缀匹配（如「附件-」）。",
        })
        self.tabs.addTab(
            self._tab_page(self.table_generic, ["", DIRECTIONS[0], "通知"]),
            "通用目录前缀")

        self.table_keyword = _make_table(
            ["启用", "关键词（用 / 分隔）", "目标类别", "方向",
             "匹配范围", "触发场景"])
        self._set_header_tip(self.table_keyword, {
            1: "任一关键词命中即触发；英文按词边界匹配。顺序即优先级。",
            4: "可选：" + " / ".join(SCOPE_LABELS.values()),
            5: "可选：" + " / ".join(TRIGGER_LABELS.values()),
        })
        self.tabs.addTab(
            self._tab_page(
                self.table_keyword,
                ["", "", DIRECTIONS[0], SCOPE_LABELS[SCOPE_PATH],
                 TRIGGER_LABELS[TRIGGER_SUBDIR]]),
            "关键词兜底")

        # ---- V1.5：长期修正规则 ----
        self.table_longterm = _make_table([
            "启用", "名称", "匹配方式", "来源路径/目录", "含子目录",
            "关键词（用 / 分隔）", "方向", "学期", "类别", "内部路径", "规则ID"])
        self.table_longterm.setColumnHidden(10, True)  # 稳定标识，仅内部使用
        self._set_header_tip(self.table_longterm, {
            2: "可选：" + " / ".join(MATCH_TYPE_LABELS.values())
               + "。精确路径匹配单个文件；目录范围匹配整个目录；"
                 "文件名关键词匹配文件名（可限定目录）。",
            3: "精确路径填文件的相对路径；目录范围/关键词填来源目录（可留空=根目录）。"
               "一律按路径边界比较，「课程A」不会匹配「课程AB」。",
            4: "仅「目录范围」使用：填「是」包含子目录，填「否」只匹配该层目录。",
            5: "仅「文件名关键词」使用；任一关键词命中即生效，英文按词边界匹配。",
            9: "保留 / 清空 / 替换:路径。替换时填「替换:实验一」这种形式。",
            10: "规则稳定标识，不随排序变化；不需要手动修改。",
        })
        self.tabs.addTab(self._long_term_page(), "长期修正规则")

        bottom = QHBoxLayout()
        self.btn_restore = QPushButton("恢复默认")
        self.btn_restore.clicked.connect(self._on_restore_default)
        self.btn_preview = QPushButton("试运行／查看影响…")
        self.btn_preview.clicked.connect(self._on_preview)
        self.preview_label = QLabel("")
        self.preview_label.setStyleSheet("color:#1565C0;")
        self.preview_label.setWordWrap(True)
        bottom.addWidget(self.btn_restore)
        bottom.addWidget(self.btn_preview)
        bottom.addWidget(self.preview_label, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Save).setText("保存并应用")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_save)
        self.buttons.rejected.connect(self.reject)
        bottom.addWidget(self.buttons)
        lay.addLayout(bottom)

    def _tab_page(self, table: QTableWidget, defaults: List[str]) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(table, 1)
        row = QHBoxLayout()
        b_add = QPushButton("添加一行")
        b_add.clicked.connect(lambda: _add_blank_row(table, defaults))
        b_del = QPushButton("删除选中")
        b_del.clicked.connect(lambda: _delete_selected(table))
        b_up = QPushButton("上移")
        b_up.clicked.connect(lambda: _move_selected(table, -1))
        b_down = QPushButton("下移")
        b_down.clicked.connect(lambda: _move_selected(table, 1))
        for b in (b_add, b_del, b_up, b_down):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _long_term_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        note = QLabel(
            "长期修正规则：命中后**覆盖**上面的学期 / 顶层方向 / 学期内目录等映射，"
            "但仍低于「当前计划的临时手动修正」。\n"
            "优先级：临时手动修正 > 长期规则（从上到下取第一条命中）> 原有推荐模式逻辑。\n"
            "目录匹配按路径边界判断，「课程A」不会匹配「课程AB」。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#4E342E;background:#FFF3E0;padding:6px 8px;"
                           "border-radius:4px;")
        v.addWidget(note)
        v.addWidget(self.table_longterm, 1)
        row = QHBoxLayout()
        b_add = QPushButton("添加规则")
        b_add.clicked.connect(self._add_long_term_row)
        b_del = QPushButton("删除选中")
        b_del.clicked.connect(lambda: _delete_selected(self.table_longterm))
        b_up = QPushButton("上移（提高优先级）")
        b_up.clicked.connect(lambda: _move_selected(self.table_longterm, -1))
        b_down = QPushButton("下移（降低优先级）")
        b_down.clicked.connect(lambda: _move_selected(self.table_longterm, 1))
        for b in (b_add, b_del, b_up, b_down):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _add_long_term_row(self):
        t = self.table_longterm
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, _check_item(True))
        defaults = [
            "新规则",
            MATCH_TYPE_LABELS[MATCH_EXACT_PATH],
            "",
            "是",
            "",
            DIRECTIONS[0],
            "",
            "",
            INNER_MODE_SHORT[INNER_KEEP],
            new_rule_id(),
        ]
        for c, value in enumerate(defaults, start=1):
            t.setItem(r, c, _text_item(value))
        t.selectRow(r)

    @staticmethod
    def _set_header_tip(table: QTableWidget, tips: dict):
        for col, text in tips.items():
            item = table.horizontalHeaderItem(col)
            if item is not None:
                item.setToolTip(text)

    # ---------------- 草稿载入 / 收集 ----------------

    def _load_config(self, cfg: ClassificationConfig, using_defaults: bool):
        self._draft = cfg
        _set_rows(self.table_semester, [
            [r.enabled, r.source, r.semester] for r in cfg.semester_rules])
        _set_rows(self.table_top, [
            [r.enabled, r.source, r.direction]
            for r in cfg.top_direction_rules])
        _set_rows(self.table_semdir, [
            [r.enabled, r.semester, r.source, r.direction, r.category]
            for r in cfg.semester_dir_rules])
        _set_rows(self.table_generic, [
            [r.enabled, r.prefix, r.direction, r.category]
            for r in cfg.generic_dir_rules])
        _set_rows(self.table_keyword, [
            [r.enabled, " / ".join(r.keywords), r.target, r.direction,
             SCOPE_KEY_TO_LABEL.get(r.scope, r.scope),
             TRIGGER_KEY_TO_LABEL.get(r.trigger, r.trigger)]
            for r in cfg.keyword_rules])
        _set_rows(self.table_longterm, [
            [r.enabled, r.name,
             MATCH_TYPE_LABELS.get(r.match_type, r.match_type),
             r.source_path,
             "是" if r.include_subdirs else "否",
             " / ".join(r.keywords),
             r.direction, r.semester, r.category,
             self._inner_display(r),
             r.rule_id]
            for r in cfg.long_term_rules])
        if self.config_error:
            self.error_label.setText(
                f"当前配置不可用，已阻止推荐模式生成计划：\n{self.config_error}\n"
                "保存将用当前草稿覆盖损坏/不兼容的配置文件。")
            self.error_label.show()
        self.preview_label.setText(
            "当前使用内置默认配置" if using_defaults
            else "当前使用该目录的自定义配置")

    @staticmethod
    def _inner_display(rule: LongTermRule) -> str:
        short = INNER_MODE_SHORT.get(rule.inner_mode, rule.inner_mode)
        if rule.inner_mode == INNER_REPLACE and rule.inner_path:
            return f"{short}:{rule.inner_path}"
        return short

    @staticmethod
    def _parse_inner_display(raw: str):
        """把表格里的「保留 / 清空 / 替换:路径」解析成 (inner_mode, inner_path)。"""
        text = (raw or "").strip()
        replace_short = INNER_MODE_SHORT[INNER_REPLACE]
        clear_short = INNER_MODE_SHORT[INNER_CLEAR]
        keep_short = INNER_MODE_SHORT[INNER_KEEP]
        if text.startswith(replace_short):
            body = text[len(replace_short):].lstrip(":： ").strip()
            return INNER_REPLACE, body
        if text.startswith(clear_short):
            return INNER_CLEAR, ""
        if text.startswith(keep_short) or not text:
            return INNER_KEEP, ""
        # 未知写法：保持原文本作为替换路径更容易被发现并修正
        return INNER_REPLACE, text

    def _collect_config(self) -> ClassificationConfig:
        semester_rules = [
            SemesterRule(
                source=_row_text(self.table_semester, r, 1),
                semester=_row_text(self.table_semester, r, 2),
                enabled=_row_enabled(self.table_semester, r),
            )
            for r in range(self.table_semester.rowCount())
        ]
        top_rules = [
            TopDirectionRule(
                source=_row_text(self.table_top, r, 1),
                direction=_row_text(self.table_top, r, 2),
                enabled=_row_enabled(self.table_top, r),
            )
            for r in range(self.table_top.rowCount())
        ]
        sem_dir_rules = [
            SemesterDirRule(
                semester=_row_text(self.table_semdir, r, 1),
                source=_row_text(self.table_semdir, r, 2),
                direction=_row_text(self.table_semdir, r, 3),
                category=_row_text(self.table_semdir, r, 4),
                enabled=_row_enabled(self.table_semdir, r),
            )
            for r in range(self.table_semdir.rowCount())
        ]
        generic_rules = [
            GenericDirRule(
                prefix=_row_text(self.table_generic, r, 1),
                direction=_row_text(self.table_generic, r, 2),
                category=_row_text(self.table_generic, r, 3),
                enabled=_row_enabled(self.table_generic, r),
            )
            for r in range(self.table_generic.rowCount())
        ]
        keyword_rules = []
        for r in range(self.table_keyword.rowCount()):
            raw_kw = _row_text(self.table_keyword, r, 1)
            kws = [k.strip() for k in
                   raw_kw.replace("，", "/").replace(",", "/")
                   .replace(";", "/").replace("；", "/").split("/") if k.strip()]
            scope_raw = _row_text(self.table_keyword, r, 4)
            trigger_raw = _row_text(self.table_keyword, r, 5)
            keyword_rules.append(KeywordRule(
                keywords=kws,
                target=_row_text(self.table_keyword, r, 2),
                direction=_row_text(self.table_keyword, r, 3),
                scope=SCOPE_LABEL_TO_KEY.get(scope_raw, scope_raw),
                trigger=TRIGGER_LABEL_TO_KEY.get(trigger_raw, trigger_raw),
                enabled=_row_enabled(self.table_keyword, r),
            ))
        long_term_rules = []
        for r in range(self.table_longterm.rowCount()):
            raw_kw = _row_text(self.table_longterm, r, 5)
            kws = [k.strip() for k in re.split(r"[/，,;；]", raw_kw) if k.strip()]
            inner_mode, inner_path = self._parse_inner_display(
                _row_text(self.table_longterm, r, 9))
            subdir_raw = _row_text(self.table_longterm, r, 4)
            include_subdirs = subdir_raw.lower() not in (
                "否", "不", "不包含", "no", "false", "0")
            rule_id = _row_text(self.table_longterm, r, 10) or new_rule_id()
            match_label = _row_text(self.table_longterm, r, 2)
            long_term_rules.append(LongTermRule(
                rule_id=rule_id,
                name=_row_text(self.table_longterm, r, 1),
                match_type=MATCH_LABEL_TO_TYPE.get(match_label, match_label),
                source_path=_row_text(self.table_longterm, r, 3),
                include_subdirs=include_subdirs,
                keywords=kws,
                direction=_row_text(self.table_longterm, r, 6),
                semester=_row_text(self.table_longterm, r, 7),
                category=_row_text(self.table_longterm, r, 8),
                inner_mode=inner_mode,
                inner_path=inner_path,
                enabled=_row_enabled(self.table_longterm, r),
            ))
        return ClassificationConfig(
            semester_rules=semester_rules,
            top_direction_rules=top_rules,
            semester_dir_rules=sem_dir_rules,
            generic_dir_rules=generic_rules,
            keyword_rules=keyword_rules,
            long_term_rules=long_term_rules,
            version=CONFIG_VERSION,
        )

    def _validated(self) -> Optional[ClassificationConfig]:
        cfg = self._collect_config()
        errors = validate_config(cfg)
        if errors:
            QMessageBox.critical(
                self, "配置未通过校验",
                "请修正以下规则后再保存/试运行：\n\n"
                + "\n".join(f"· {e}" for e in errors))
            return None
        return cfg

    # ---------------- 操作 ----------------

    def _on_restore_default(self):
        if self._previewing:
            return
        ret = QMessageBox.question(
            self, "恢复默认",
            "将把编辑框恢复为内置默认规则（尚未保存）。\n"
            "点击「保存并应用」后才会删除该目录的自定义配置文件。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._load_config(default_config(), using_defaults=True)
        self.preview_label.setText("草稿已恢复默认，保存后生效")

    def _on_save(self):
        if self._previewing:
            return
        cfg = self._validated()
        if cfg is None:
            return
        if self.save_callback is None:
            self.result_config = cfg
            self.accept()
            return
        try:
            self.save_callback(cfg)
        except ClassificationConfigError as e:
            QMessageBox.critical(
                self, "保存失败",
                f"{e}\n\n已生效的规则保持不变，配置文件未被覆盖。")
            return
        except OSError as e:
            QMessageBox.critical(
                self, "保存失败",
                f"无法写入配置文件，已生效的规则保持不变：\n{e}")
            return
        self.result_config = cfg
        self.accept()

    def _set_previewing(self, value: bool):
        self._previewing = value
        self.tabs.setEnabled(not value)
        self.btn_restore.setEnabled(not value)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(not value)
        self.btn_preview.setEnabled(not value)
        if value:
            self.preview_label.setText("试运行中……（不移动文件、不写历史）")

    def _on_preview(self):
        if self._previewing or self._host_closing():
            return
        if self.scan_result is None:
            QMessageBox.information(
                self, "试运行",
                "请先在主窗口选择目录并点击「扫描」，再查看规则影响。\n"
                "试运行只使用当前扫描结果，不会重新读取文件内容。")
            return
        cfg = self._validated()
        if cfg is None:
            return

        self._preview_seq += 1
        seq = self._preview_seq
        # 不把线程挂在本对话框下：取消编辑后对话框可以先关闭，
        # 线程由主窗口登记表保活，真正结束后再回收。
        worker = PreviewWorker(
            seq, self.root, self.scan_result, cfg,
            self.base_config, MODE_BY_DIRECTION_SEMESTER, rules=[])
        self._worker = worker
        worker.progress.connect(self._on_preview_progress)
        worker.finished_ok.connect(self._on_preview_done)
        worker.failed.connect(self._on_preview_failed)
        worker.cancelled.connect(self._on_preview_cancelled)
        worker.finished.connect(self._on_worker_finished)
        if self.worker_registry is not None:
            try:
                self.worker_registry.register_preview_worker(worker)
            except Exception:
                pass
        self._set_previewing(True)
        worker.start()

    def _on_worker_finished(self):
        """线程真正结束后再注销/释放引用（不阻塞界面线程）。"""
        worker = self.sender()
        if isinstance(worker, PreviewWorker):
            self._release_worker(worker)

    def _release_worker(self, worker):
        if self.worker_registry is not None:
            try:
                self.worker_registry.unregister_preview_worker(worker)
            except Exception:
                pass
        if self._worker is worker:
            self._worker = None

    def _host_closing(self) -> bool:
        """所属主窗口是否已进入关闭等待/关闭相位。"""
        host = self.parent()
        if host is None:
            return False
        return bool(getattr(host, "_close_pending", False)
                    or getattr(host, "_closing", False))

    def _source_ok(self, seq: int, root: str) -> bool:
        return (not self._closed and not self._host_closing()
                and seq == self._preview_seq and bool(root)
                and _norm_root(root) == _norm_root(self.root))

    def _on_preview_progress(self, seq, root, cur, total, path):
        if not self._source_ok(seq, root) or not self._previewing:
            return
        self.preview_label.setText(f"试运行中…… [{cur}/{total}] {path}")

    def _on_preview_done(self, seq, root, result):
        if not self._source_ok(seq, root):
            return
        self._set_previewing(False)
        self.preview_label.setText(
            f"试运行完成：样本 {result.sample_total} 个，变化 "
            f"{result.changed_count} 个，未匹配 {result.unmatched} 个")
        ImpactPreviewDialog(result, self).exec()

    def _on_preview_failed(self, seq, root, err):
        if not self._source_ok(seq, root):
            return
        self._set_previewing(False)
        self.preview_label.setText("试运行失败")
        QMessageBox.critical(self, "试运行失败", str(err))

    def _on_preview_cancelled(self, seq, root):
        if not self._source_ok(seq, root):
            return
        self._set_previewing(False)
        self.preview_label.setText("试运行已取消")

    def reject(self):
        """取消编辑：协作式取消 + 异步清理，不在界面线程同步等待线程退出。"""
        self._closed = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self.preview_label.setText("试运行正在停止……（线程结束后自动回收）")
            # 不调用 wait()：对话框可以先关闭，线程由主窗口登记表保活，
            # finished 回调再完成注销；避免阻塞 Qt 事件循环。
        super().reject()
