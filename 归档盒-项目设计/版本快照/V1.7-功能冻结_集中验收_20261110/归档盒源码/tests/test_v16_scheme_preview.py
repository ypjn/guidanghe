"""
归档盒 V1.6 — 整理方案管理与预览增强 专项验证（offscreen）

覆盖：
S01 方案导出 → 另一根目录导入 → 重新读取配置 → 规则实际命中
S02 替换 / 合并策略：现有优先、同标识冲突、同名冲突、映射冲突
S03 非法方案：损坏 JSON / 未知格式 / 版本不兼容 / 非法路径均拒绝且不改现状
S04 应用失败保留原配置；成功后旧计划失效
S05 恢复上一次配置（自定义 / 内置默认 / 损坏备份不可启用）
S06 备份按根目录隔离
S07 方案库：内置只读、保存/复制/改名/删除、导入导出、应用为副本
S08 方案库修改/删除不影响已应用的目录配置
P01 搜索与筛选只改变视图，不改变计划与勾选
P02 可见批量勾选不改变隐藏条目；计数包含隐藏已勾选项
P03 执行确认按全部勾选项统计并提示隐藏勾选
P04 目标树与计划逐项一致，且不创建真实目录
P05 筛选后定位同名文件仍命中正确对象
P06 切换根目录/计划失效后旧筛选状态不污染新计划
X01 组合演练：导出→另一目录导入→执行→历史→撤销（独立枚举核对）

运行：
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_v16_scheme_preview.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QLabel, QCheckBox

import classification_config as CFG
from classification_config import (
    ClassificationConfig, LongTermRule, default_config, load_config,
    save_config, clone_config, config_to_dict, validate_config,
)
import config_transfer as CT
import scheme_store as SS
import main as main_mod
from main import MainWindow, ExecuteConfirmDialog
from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
from scanner import scan_directory
from storage import Storage
from executor import execute_plan, undo_latest_batch
from target_tree_dialog import TargetTreeDialog

RESULTS = []
BASE = os.path.join("/tmp", "_v16_scheme_preview")
SCHEME_DIR = os.path.join(BASE, "_scheme_library")


def rec(tid, title, ok, evidence=""):
    status = "通过" if ok else "未通过"
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": str(evidence)})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {title}")
    for line in str(evidence).splitlines():
        print("        " + line)


def pump(app, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def mk(root, rel, content="x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(content.encode("utf-8"))


def fresh(name):
    root = os.path.join(BASE, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def lt(rule_id, name, source, direction="项目", category="方案类别",
       match_type=CFG.MATCH_DIR_SCOPE, inner_mode=CFG.INNER_KEEP,
       inner_path="", sub=True):
    return LongTermRule(
        rule_id=rule_id, name=name, match_type=match_type,
        source_path=source, include_subdirs=sub, direction=direction,
        semester="", category=category, inner_mode=inner_mode,
        inner_path=inner_path)


def plan_for(root, cfg=None):
    sr = scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [], config=cfg,
                      compute_duplicates=False)
    return sr, plan


def make_window(app, root):
    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        w = MainWindow()
        w.path_edit.setText(root)
        w._reset_for_new_dir(root)
    finally:
        QMessageBox.question = real_q
    return w


def show_plan(w, plan):
    w.current_plan = plan
    w._plan_valid = True
    w._populate_table(plan, reset_filters=True)
    w._update_button_states()


def main():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE)
    os.environ[SS.ENV_OVERRIDE] = SCHEME_DIR

    app = QApplication.instance() or QApplication([])
    for n in ("information", "warning", "critical"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    # ---------------- S01 导出 → 另一根目录导入 → 命中 ----------------
    try:
        r1 = fresh("s01_a")
        mk(r1, "大二上/数据结构/作业/a.pdf", "S01")
        cfg = default_config()
        cfg.long_term_rules.insert(0, lt("lt-s01", "数据结构归项目",
                                         "大二上/数据结构", category="S01类别"))
        _, plan1 = plan_for(r1, cfg)
        hit1 = [i for i in plan1.items if i.rule_key == "long_term:lt-s01"]
        out = os.path.join(BASE, "s01.scheme.json")
        CT.save_scheme_file(out, CT.export_scheme_payload(cfg, "S01 方案", "说明"))

        r2 = fresh("s01_b")
        mk(r2, "大二上/数据结构/作业/b.pdf", "S01B")
        parsed = CT.parse_scheme_payload(CT.load_scheme_file(out))
        save_config(r2, parsed.config)
        cfg2, src2 = load_config(r2)          # 模拟重新启动后重新读取
        _, plan2 = plan_for(r2, cfg2)
        hit2 = [i for i in plan2.items if i.rule_key == "long_term:lt-s01"]
        ok = (len(hit1) == 1 and src2 == "custom"
              and len(hit2) == 1 and parsed.scheme_id == "")
        rec("S01", "方案导出后可在另一根目录导入并实际命中长期规则", ok,
            f"源目录命中={len(hit1)}；另一目录来源={src2}；命中={len(hit2)}\n"
            f"另一目录命中标识={hit2[0].rule_key if hit2 else '-'}")
    except Exception as e:
        rec("S01", "导出导入命中", False, f"异常：{e}")

    # ---------------- S02 替换 / 合并 ----------------
    try:
        base = default_config()
        base.long_term_rules.append(lt("lt-same", "同名规则", "a", direction="项目"))
        incoming = default_config()
        incoming.long_term_rules.append(lt("lt-same", "同名规则", "b", direction="竞赛"))
        incoming.long_term_rules.append(lt("lt-new", "新增规则", "c", direction="项目"))

        replace_cfg = clone_config(incoming)
        merge_keep = CT.merge_configs(base, incoming)
        merge_take = CT.merge_configs(base, incoming, {"lt-same": "take"})
        base_map = {i.rule_id: i for i in merge_keep.config.long_term_rules}
        take_map = {i.rule_id: i for i in merge_take.config.long_term_rules}

        # 映射冲突：同名来源不同学期标签，现有优先
        b2 = default_config()
        inc2 = default_config()
        inc2.semester_rules[0] = CFG.SemesterRule(
            source=b2.semester_rules[0].source, semester="冲突学期")
        m2 = CT.merge_configs(b2, inc2)
        mapping_conflict = any(c.kind == "mapping_changed" for c in m2.conflicts)
        kept_label = m2.config.semester_rules[0].semester

        # 同名不同标识：默认保留现有；采用导入则替换
        b3 = default_config()
        b3.long_term_rules.append(lt("lt-a", "重复名", "a"))
        inc3 = default_config()
        inc3.long_term_rules.append(lt("lt-b", "重复名", "b"))
        nk = CT.merge_configs(b3, inc3)
        nt = CT.merge_configs(b3, inc3, {"lt-b": "take"})

        ok = (len(replace_cfg.long_term_rules) == 2
              and base_map["lt-same"].source_path == "a"
              and "lt-new" in base_map
              and take_map["lt-same"].source_path == "b"
              and mapping_conflict and kept_label != "冲突学期"
              and any(c.kind == "name_duplicate" for c in nk.conflicts)
              and nk.config.long_term_rules[0].rule_id == "lt-a"
              and nt.config.long_term_rules[0].rule_id == "lt-b"
              and not merge_keep.errors and not m2.errors and not nk.errors)
        rec("S02", "替换/合并：现有优先、同标识冲突、同名冲突、映射冲突均确定可解释", ok,
            f"合并默认={[(r.rule_id, r.source_path) for r in merge_keep.config.long_term_rules]}\n"
            f"采用导入={[(r.rule_id, r.source_path) for r in merge_take.config.long_term_rules]}\n"
            f"映射冲突={mapping_conflict} 保留学期={kept_label}\n"
            f"同名默认/采用={nk.config.long_term_rules[0].rule_id}/"
            f"{nt.config.long_term_rules[0].rule_id}")
    except Exception as e:
        rec("S02", "替换/合并策略", False, f"异常：{e}")

    # ---------------- S03 非法方案 ----------------
    try:
        cases = {}
        good = CT.export_scheme_payload(default_config(), "合法")
        cases["损坏 JSON"] = "{not json"
        cases["未知格式"] = json.dumps({**good, "format": "other"})
        cases["格式版本过高"] = json.dumps({**good, "format_version": 99})
        bad_cfg = clone_config(default_config())
        bad_cfg.version = 99
        cases["配置版本不兼容"] = json.dumps({**good, "config": config_to_dict(bad_cfg)})
        bad_path = clone_config(default_config())
        bad_path.long_term_rules.append(LongTermRule(
            rule_id="lt-bad", name="逃逸", source_path="../逃逸",
            direction="项目"))
        cases["非法路径"] = json.dumps({**good, "config": config_to_dict(bad_path)})
        rejected = {}
        for label, text in cases.items():
            p = os.path.join(BASE, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                if label == "损坏 JSON":
                    CT.parse_scheme_payload(json.loads(text))
                else:
                    CT.parse_scheme_payload(json.loads(text))
                rejected[label] = False
            except Exception:
                rejected[label] = True
        rec("S03", "损坏/未知格式/版本不兼容/非法路径方案均被明确拒绝",
            all(rejected.values()), f"拒绝结果={rejected}")
    except Exception as e:
        rec("S03", "非法方案拒绝", False, f"异常：{e}")

    # ---------------- S04 应用失败保留 / 成功失效计划 ----------------
    try:
        root = fresh("s04")
        mk(root, "大二上/数据结构/作业/a.pdf", "S04")
        w = make_window(app, root)
        base_cfg = default_config()
        save_config(root, base_cfg)
        w._load_root_config(root)
        _, plan = plan_for(root, base_cfg)
        show_plan(w, plan)

        new_cfg = default_config()
        new_cfg.long_term_rules.append(lt("lt-s04", "S04", "大二上/数据结构"))
        real_save = main_mod.save_config
        main_mod.save_config = lambda *a, **k: (_ for _ in ()).throw(
            CFG.ClassificationConfigError("模拟保存失败"))
        failed_preserved = False
        try:
            w._apply_scheme_config(new_cfg)
        except CFG.ClassificationConfigError:
            failed_preserved = True
        finally:
            main_mod.save_config = real_save
        cfg_after_fail, _ = load_config(root)

        w._apply_scheme_config(new_cfg)
        invalidated = (w.current_plan is None and not w._plan_valid)
        cfg_after_ok, _ = load_config(root)
        ok = (failed_preserved and len(cfg_after_fail.long_term_rules) == 0
              and invalidated and len(cfg_after_ok.long_term_rules) == 1)
        rec("S04", "应用失败保留原配置；成功后旧计划立即失效", ok,
            f"失败抛错={failed_preserved}；失败后规则数="
            f"{len(cfg_after_fail.long_term_rules)}；成功后计划失效={invalidated}；"
            f"成功后规则数={len(cfg_after_ok.long_term_rules)}")
        w.close()
        pump(app)
    except Exception as e:
        rec("S04", "应用失败/成功", False, f"异常：{e}")

    # ---------------- S05 恢复上一次配置 ----------------
    try:
        root = fresh("s05")
        mk(root, "大二上/数据结构/作业/a.pdf", "S05")
        w = make_window(app, root)
        c1 = default_config()
        c1.long_term_rules.append(lt("lt-s05", "原始规则", "大二上/数据结构"))
        save_config(root, c1)
        w._load_root_config(root)
        c2 = default_config()
        w._apply_scheme_config(c2)             # 备份 c1
        msg = w._restore_last_config()
        restored, _ = load_config(root)
        restored_ok = [r.rule_id for r in restored.long_term_rules] == ["lt-s05"]

        # 内置默认也能恢复
        os.remove(CFG.config_path(root))
        w._load_root_config(root)
        w._apply_scheme_config(c1)             # 备份 default
        w._restore_last_config()
        _, src_default = load_config(root)
        default_ok = src_default == "default"

        # 损坏备份不能被启用
        bdir = CT.backup_dir(root)
        os.makedirs(bdir, exist_ok=True)
        corrupt = os.path.join(bdir, "config-backup-99999999-999999-000000.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write('{"state":"corrupt","raw":"{bad","error":"坏"}')
        before = open(CFG.config_path(root), "rb").read() if \
            os.path.exists(CFG.config_path(root)) else b""
        refused = False
        try:
            CT.restore_latest_backup(root)
        except CFG.ClassificationConfigError:
            refused = True
        after = open(CFG.config_path(root), "rb").read() if \
            os.path.exists(CFG.config_path(root)) else b""
        ok = restored_ok and default_ok and refused and before == after
        rec("S05", "恢复上一次配置：自定义/内置默认可恢复，损坏备份不可启用", ok,
            f"恢复自定义={restored_ok}；恢复默认={default_ok}；"
            f"损坏备份拒绝={refused}；配置字节未变={before == after}\n提示={msg}")
        w.close()
        pump(app)
    except Exception as e:
        rec("S05", "恢复上一次配置", False, f"异常：{e}")

    # ---------------- S06 备份按根目录隔离 ----------------
    try:
        r1 = fresh("s06_a")
        r2 = fresh("s06_b")
        save_config(r1, default_config())
        CT.create_backup(r1)
        ok = bool(CT.list_backups(r1)) and not CT.list_backups(r2)
        rec("S06", "配置备份按根目录隔离", ok,
            f"根1备份={len(CT.list_backups(r1))}；根2备份={len(CT.list_backups(r2))}")
    except Exception as e:
        rec("S06", "备份隔离", False, f"异常：{e}")

    # ---------------- S07 方案库 ----------------
    try:
        if os.path.exists(SCHEME_DIR):
            shutil.rmtree(SCHEME_DIR)
        builtins = [r for r in SS.list_schemes() if r.builtin]
        builtin_ok = (len(builtins) == 2 and all(r.read_only for r in builtins)
                      and any(r.name == "内置默认方案" for r in builtins)
                      and any(r.name == "空白方案" for r in builtins))
        cfg = default_config()
        cfg.long_term_rules.append(lt("lt-s07", "库规则", "大二上/数据结构"))
        saved = SS.save_scheme("库方案", "说明", cfg)
        dup = SS.duplicate_scheme(saved.scheme_id, "库方案副本")
        SS.update_scheme_meta(dup.scheme_id, "改名后", "新说明")
        renamed = SS.get_scheme(dup.scheme_id)
        out = os.path.join(BASE, "s07_export.json")
        SS.export_scheme_to(saved.scheme_id, out)
        imported = SS.import_scheme_file(out)
        SS.delete_scheme(dup.scheme_id)
        deleted = SS.get_scheme(dup.scheme_id) is None
        ok = (builtin_ok and renamed is not None and renamed.name == "改名后"
              and os.path.exists(out) and imported.scheme_id != saved.scheme_id
              and deleted)
        rec("S07", "方案库：内置只读、保存/复制/改名/导入导出/删除", ok,
            f"内置={[(r.name, r.read_only) for r in builtins]}\n"
            f"保存={saved.scheme_id} 复制={dup.scheme_id} 改名={renamed.name if renamed else '-'}\n"
            f"导出存在={os.path.exists(out)} 导入新标识={imported.scheme_id != saved.scheme_id} "
            f"删除={deleted}")
    except Exception as e:
        rec("S07", "方案库操作", False, f"异常：{e}")

    # ---------------- S08 方案库变更不影响已应用目录 ----------------
    try:
        root = fresh("s08")
        cfg = default_config()
        cfg.long_term_rules.append(lt("lt-s08", "已应用规则", "大二上/数据结构"))
        saved = SS.save_scheme("S08 方案", "", cfg)
        applied = clone_config(saved.config)
        save_config(root, applied)
        before, _ = load_config(root)

        changed = clone_config(saved.config)
        changed.long_term_rules.clear()
        SS.update_scheme_meta(saved.scheme_id, "S08 方案（已改）", "")
        SS.save_scheme("S08 方案（已改）", "", changed,
                       scheme_id=saved.scheme_id)
        after, _ = load_config(root)
        ok = ([r.rule_id for r in before.long_term_rules]
              == [r.rule_id for r in after.long_term_rules] == ["lt-s08"])
        rec("S08", "修改方案库模板不改变已应用到目录的配置", ok,
            f"应用前={[r.rule_id for r in before.long_term_rules]}；"
            f"改库后={[r.rule_id for r in after.long_term_rules]}")
    except Exception as e:
        rec("S08", "方案库隔离", False, f"异常：{e}")

    # ---------------- P01 搜索筛选不改变计划 ----------------
    try:
        root = fresh("p01")
        mk(root, "大二上/数据结构/作业/a.pdf", "P01A")
        mk(root, "大二上/数据结构/实验/b.docx", "P01B")
        mk(root, "大二上/C++程序设计/作业/c.txt", "P01C")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        snapshot = [(i.selected, i.target_rel_path) for i in plan.items]
        w.plan_search.setText("数据结构")
        pump(app)
        visible_after_search = sum(1 for f in w._visible_flags if f)
        hidden_unchanged = [i.selected for i in plan.items]
        w.type_filter.setCurrentIndex(
            max(0, [w.type_filter.itemData(i)
                    for i in range(w.type_filter.count())].index("文档")
                    if "文档" in [w.type_filter.itemData(i)
                                 for i in range(w.type_filter.count())] else 0))
        pump(app)
        # 来源筛选：自动
        idx = [w.source_filter.itemData(i) for i in range(w.source_filter.count())].index("auto")
        w.source_filter.setCurrentIndex(idx)
        pump(app)
        after = [(i.selected, i.target_rel_path) for i in plan.items]
        w._clear_plan_filters()
        pump(app)
        all_visible = all(w._visible_flags)
        ok = (snapshot == after and hidden_unchanged == [i.selected for i in plan.items]
              and visible_after_search < plan.total and all_visible)
        rec("P01", "搜索与筛选只改变视图，不改变计划内容与勾选", ok,
            f"计划未变={snapshot == after}；搜索后可见={visible_after_search}/{plan.total}；"
            f"清空后全部可见={all_visible}")
        w.close()
        pump(app)
    except Exception as e:
        rec("P01", "搜索筛选", False, f"异常：{e}")

    # ---------------- P02 可见批量勾选 ----------------
    try:
        root = fresh("p02")
        mk(root, "大二上/数据结构/作业/a.pdf", "P02A")
        mk(root, "大二上/数据结构/实验/b.pdf", "P02B")
        mk(root, "大二上/C++程序设计/作业/c.pdf", "P02C")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        w.plan_search.setText("C++")
        pump(app)
        hidden_before = [plan.items[i].selected for i, f in enumerate(w._visible_flags) if not f]
        w._set_visible_checked(False)
        pump(app)
        visible_rows = [i for i, f in enumerate(w._visible_flags) if f]
        hidden_after = [plan.items[i].selected for i, f in enumerate(w._visible_flags) if not f]
        counts_text = w.plan_counts_label.text()
        ok = (hidden_before == hidden_after
              and all(not plan.items[i].selected for i in visible_rows)
              and "隐藏但仍勾选" in counts_text
              and any(hidden_after))
        rec("P02", "可见批量勾选不改变隐藏条目，计数包含隐藏已勾选项", ok,
            f"隐藏前后勾选={hidden_before}→{hidden_after}；计数={counts_text}")
        w.close()
        pump(app)
    except Exception as e:
        rec("P02", "批量勾选", False, f"异常：{e}")

    # ---------------- P03 执行确认统计 ----------------
    try:
        root = fresh("p03")
        mk(root, "大二上/数据结构/作业/a.pdf", "P03A")
        mk(root, "大二上/C++程序设计/作业/c.pdf", "P03C")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        w.plan_search.setText("C++")
        pump(app)
        hidden_selected = sum(1 for i, f in enumerate(w._visible_flags)
                              if not f and plan.items[i].selected)
        dlg = ExecuteConfirmDialog(root, plan, None,
                                   hidden_selected=hidden_selected)
        labels = [l.text() for l in dlg.findChildren(QLabel)]
        warn = any("隐藏了" in t and str(hidden_selected) in t for t in labels)
        ok = (plan.selected_count == 2 and hidden_selected == 1 and warn)
        rec("P03", "执行确认按全部勾选项统计并提示隐藏勾选项", ok,
            f"总勾选={plan.selected_count}；隐藏已勾选={hidden_selected}；提示={warn}")
        dlg.close()
        w.close()
        pump(app)
    except Exception as e:
        rec("P03", "执行确认", False, f"异常：{e}")

    # ---------------- P04 目标树 ----------------
    try:
        root = fresh("p04")
        mk(root, "大二上/数据结构/作业/a.pdf", "P04A")
        mk(root, "大二上/数据结构/实验/b.pdf", "P04B")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        before = sorted(os.listdir(root))
        dlg = TargetTreeDialog(plan, [True] * plan.total, None)
        leaves = []

        def walk(item, prefix):
            for c in range(item.childCount()):
                child = item.child(c)
                label = child.text(0)
                if child.childCount() == 0:
                    leaves.append(os.path.join(prefix, label))
                else:
                    walk(child, os.path.join(prefix, label))
        for t in range(dlg.tree.topLevelItemCount()):
            walk(dlg.tree.topLevelItem(t), "")
        after = sorted(os.listdir(root))
        ok = (len(leaves) == plan.total and before == after
              and all(any(i.target_rel_path.endswith(os.path.basename(l))
                          for i in plan.items) for l in leaves))
        rec("P04", "目标树与计划逐项一致，且不创建真实目录", ok,
            f"叶子={len(leaves)} 计划={plan.total}；根目录未变化={before == after}")
        dlg.close()
        w.close()
        pump(app)
    except Exception as e:
        rec("P04", "目标树", False, f"异常：{e}")

    # ---------------- P05 筛选后定位同名文件 ----------------
    try:
        root = fresh("p05")
        mk(root, "大一上/高数/作业/同名.pdf", "P05-1")
        mk(root, "大二上/数据结构/作业/同名.pdf", "P05-2")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        w.plan_search.setText("数据结构")
        pump(app)
        visible_rows = [i for i, f in enumerate(w._visible_flags) if f]
        row = visible_rows[0]
        item = w._item_for_table_row(row)
        target_ok = item is not None and "数据结构" in item.file.rel_path
        # 人为篡改显示文本，模拟“展示内容与计划不一致”
        w.table.item(row, 1).setText("✎ 被篡改.pdf")
        tamper_blocked = w._item_for_table_row(row) is None
        ok = target_ok and tamper_blocked and len(visible_rows) == 1
        rec("P05", "筛选后按根目录+源相对路径定位同名文件，不误改其他同名项", ok,
            f"可见行={visible_rows}；定位={item.file.rel_path if item else '-'}；"
            f"篡改后被拒绝={tamper_blocked}")
        w.close()
        pump(app)
    except Exception as e:
        rec("P05", "同名文件定位", False, f"异常：{e}")

    # ---------------- P06 切换目录清空筛选 ----------------
    try:
        r1 = fresh("p06_a")
        mk(r1, "大二上/数据结构/作业/a.pdf", "P06")
        w = make_window(app, r1)
        _, plan = plan_for(r1, default_config())
        show_plan(w, plan)
        w.plan_search.setText("不存在的关键词")
        pump(app)
        r2 = fresh("p06_b")
        mk(r2, "大二上/数据结构/作业/b.pdf", "P06B")
        w._reset_for_new_dir(r2)
        cleared = (w.plan_search.text() == "" and not w._visible_flags
                   and w._plan_identity is None and w.current_plan is None)
        rec("P06", "切换根目录后旧筛选/视图状态不污染新计划", cleared,
            f"搜索文本={w.plan_search.text()!r} 可见标记={w._visible_flags} "
            f"计划={w.current_plan}")
        w.close()
        pump(app)
    except Exception as e:
        rec("P06", "筛选状态清理", False, f"异常：{e}")

    # ---------------- P07 手动修正后方向筛选项刷新 ----------------
    try:
        from plan_patch import apply_override_to_plan, OverrideParams

        root = fresh("p07")
        mk(root, "大二上/数据结构/作业/a.pdf", "P07A")
        mk(root, "大二上/数据结构/作业/b.pdf", "P07B")
        w = make_window(app, root)
        _, plan = plan_for(root, default_config())
        show_plan(w, plan)
        idx = w.dir_filter.findData("课程")
        w.dir_filter.setCurrentIndex(idx if idx >= 0 else 0)
        pump(app)

        rel = plan.items[0].file.rel_path
        apply_override_to_plan(plan, rel, OverrideParams(
            direction="项目", semester="", category="P07"))
        w._populate_table(plan)     # 同一计划重绘：应刷新筛选项
        pump(app)
        has_project = w.dir_filter.findData("项目") >= 0
        kept_course = w.dir_filter.currentData() == "课程"

        project_idx = w.dir_filter.findData("项目")
        w.dir_filter.setCurrentIndex(project_idx if project_idx >= 0 else 0)
        pump(app)
        visible = [i for i, f in enumerate(w._visible_flags) if f]
        ok = (has_project and kept_course
              and plan.items[0].direction == "项目"
              and visible == [0])
        rec("P07", "手动修正改变方向后，筛选项刷新且仍有效的筛选条件保留", ok,
            f"出现「项目」选项={has_project}；保留「课程」筛选={kept_course}；"
            f"切到项目后可见行={visible}")
        w.close()
        pump(app)
    except Exception as e:
        rec("P07", "方向筛选项刷新", False, f"异常：{e}")

    # ---------------- S09 方案中心界面流程（导出 / 应用编排） ----------------
    try:
        import transfer_dialogs as TD
        from PySide6.QtWidgets import QDialog

        root = fresh("s09")
        mk(root, "大二上/数据结构/作业/a.pdf", "S09")

        class _FakeFileDialog:
            save_path = os.path.join(BASE, "s09_ui_export.json")

            @staticmethod
            def getSaveFileName(*a, **k):
                return _FakeFileDialog.save_path, "json"

            @staticmethod
            def getOpenFileName(*a, **k):
                return _FakeFileDialog.save_path, "json"

        real_fd = TD.QFileDialog
        real_get_text = TD.QInputDialog.getText
        real_exec_choice = TD.ConfigChoiceDialog.exec
        real_exec_preview = TD.ApplyPreviewDialog.exec
        TD.QFileDialog = _FakeFileDialog
        TD.QInputDialog.getText = staticmethod(lambda *a, **k: ("S09 导出", True))
        TD.ConfigChoiceDialog.exec = lambda self: QDialog.Accepted
        TD.ApplyPreviewDialog.exec = lambda self: QDialog.Accepted
        applied = []
        try:
            dlg = TD.SchemeCenterDialog(
                root=root, current_config=default_config(),
                apply_callback=lambda cfg: applied.append(cfg),
                restore_callback=lambda: "已恢复",
                scan_result=None, worker_registry=None)
            builtin_rows = dlg.table.rowCount() >= 2
            dlg.on_export_current()
            exported = os.path.exists(_FakeFileDialog.save_path)
            incoming = default_config()
            incoming.long_term_rules.append(
                lt("lt-s09", "界面应用规则", "大二上/数据结构"))
            dlg._confirm_and_apply(incoming, "S09")
            applied_ok = bool(applied) and len(applied[0].long_term_rules) == 1
            dlg.close()
        finally:
            TD.QFileDialog = real_fd
            TD.QInputDialog.getText = real_get_text
            TD.ConfigChoiceDialog.exec = real_exec_choice
            TD.ApplyPreviewDialog.exec = real_exec_preview
        ok = builtin_rows and exported and applied_ok
        rec("S09", "方案中心可导出当前配置，并按替换/影响确认流程应用方案", ok,
            f"内置行>=2={builtin_rows}；导出文件={exported}；"
            f"应用回调规则数={len(applied[0].long_term_rules) if applied else '-'}")
    except Exception as e:
        rec("S09", "方案中心界面流程", False, f"异常：{e}")

    # ---------------- S10 方案标识校验与路径越界防护 ----------------
    try:
        lib = os.path.realpath(SS.library_dir())
        rejected = False
        try:
            SS.save_scheme("逃逸方案", "", default_config(),
                           scheme_id="../escaped")
        except CFG.ClassificationConfigError:
            rejected = True
        outside_paths = [
            os.path.join(os.path.dirname(lib), "escaped.json"),
            os.path.join(BASE, "escaped.json"),
        ]
        no_outside_write = not any(os.path.exists(p) for p in outside_paths)

        malicious = CT.export_scheme_payload(
            default_config(), "恶意标识方案", scheme_id="../escaped")
        p = os.path.join(BASE, "s10_malicious.json")
        CT.save_scheme_file(p, malicious)
        imported = SS.import_scheme_file(p)
        inside = os.path.realpath(imported.path).startswith(lib + os.sep)
        safe_id = imported.scheme_id != "../escaped"
        ok = rejected and no_outside_write and inside and safe_id
        rec("S10", "方案标识非法时拒绝写入，导入的越界标识被替换为安全标识", ok,
            f"直接保存被拒={rejected}；方案库外文件={no_outside_write}；"
            f"导入路径在库内={inside}；标识安全={safe_id}（{imported.scheme_id}）")
    except Exception as e:
        rec("S10", "方案标识越界防护", False, f"异常：{e}")

    # ---------------- S11 备份失败时停止应用 ----------------
    try:
        root = fresh("s11")
        mk(root, "大二上/数据结构/作业/a.pdf", "S11")
        w = make_window(app, root)
        save_config(root, default_config())
        w._load_root_config(root)
        # 让备份目录无法创建：同名路径先占成普通文件
        os.makedirs(os.path.join(root, ".organizer"), exist_ok=True)
        blocker = os.path.join(root, ".organizer", "config_backups")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("blocked")
        new_cfg = default_config()
        new_cfg.long_term_rules.append(lt("lt-s11", "S11", "大二上/数据结构"))
        raised = False
        try:
            w._apply_scheme_config(new_cfg)
        except CFG.ClassificationConfigError:
            raised = True
        after, src_after = load_config(root)
        unchanged = (src_after == "default"
                     and len(after.long_term_rules) == 0)
        ok = raised and unchanged
        rec("S11", "备份失败时停止应用，原配置与当前状态保持不变", ok,
            f"应用被拒绝={raised}；配置来源={src_after}；规则数="
            f"{len(after.long_term_rules)}")
        w.close()
        pump(app)
    except Exception as e:
        rec("S11", "备份失败阻断应用", False, f"异常：{e}")

    # ---------------- X01 组合演练 ----------------
    try:
        r1 = fresh("x01_a")
        mk(r1, "大二上/数据结构/作业/a.pdf", "X01-A")
        cfg = default_config()
        cfg.long_term_rules.append(lt("lt-x01", "组合规则", "大二上/数据结构",
                                      category="组合类别"))
        out = os.path.join(BASE, "x01.scheme.json")
        CT.save_scheme_file(out, CT.export_scheme_payload(cfg, "X01"))

        r2 = fresh("x01_b")
        mk(r2, "大二上/数据结构/作业/b.pdf", "X01-B")
        parsed = CT.parse_scheme_payload(CT.load_scheme_file(out))
        save_config(r2, parsed.config)
        cfg2, _ = load_config(r2)
        _, plan = plan_for(r2, cfg2)
        hit = [i for i in plan.items if i.rule_key == "long_term:lt-x01"]
        result = execute_plan(plan, Storage(r2))
        st = Storage(r2)
        acts = st.get_actions(result.batch_id)
        exported_keys = all(a.rule_key == "long_term:lt-x01" for a in acts)
        target_files = []
        for base, _dirs, files in os.walk(os.path.join(r2, "_已整理")):
            for n in files:
                target_files.append(n)
        st.close()
        undo = undo_latest_batch(Storage(r2))
        restored = os.path.isfile(os.path.join(r2, "大二上/数据结构/作业/b.pdf"))
        ok = (len(hit) == 1 and result.moved == 1 and exported_keys
              and target_files == ["b.pdf"] and undo.is_complete and restored)
        rec("X01", "组合演练：导出→另一目录导入→执行→历史→撤销", ok,
            f"命中={len(hit)} 移动={result.moved} 历史规则标识={exported_keys}\n"
            f"目标文件={target_files} 撤销完成={undo.is_complete} 源恢复={restored}")
    except Exception as e:
        rec("X01", "组合演练", False, f"异常：{e}")

    # ---------------- 汇总 ----------------
    passed = sum(1 for r in RESULTS if r["status"] == "通过")
    failed = sum(1 for r in RESULTS if r["status"] == "未通过")
    exit_code = 0 if failed == 0 and RESULTS else 1
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "results_v16_scheme_preview.json"), "w",
              encoding="utf-8") as f:
        json.dump({"suite": "test_v16_scheme_preview",
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "counts": {"total": len(RESULTS), "pass": passed,
                              "fail": failed},
                   "exit_code": exit_code, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed} / 未通过 {failed}；退出码 {exit_code}")
    try:
        main_mod.shutdown_widgets()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
