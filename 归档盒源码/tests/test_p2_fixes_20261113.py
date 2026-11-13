"""
归档盒 V1.7 — 第三轮剩余缺陷回归（2026-11-13）

覆盖审计索引里本轮仍在、且已修复的条目；全部在临时目录执行，
不接触桌面与真实资料。没有验证的项目不会写成 PASS。

运行（Windows）：
    QT_QPA_PLATFORM=offscreen python tests/test_p2_fixes_20261113.py
退出码：0 = 无 FAIL；1 = 存在 FAIL。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ai_fallback                                          # noqa: E402
import classification_config as CFG                         # noqa: E402
import config_transfer as CT                                # noqa: E402
import executor                                             # noqa: E402
import main as main_mod                                     # noqa: E402
import ocr_engine                                           # noqa: E402
import planner                                              # noqa: E402
import protect_config                                       # noqa: E402
import recent_spaces                                        # noqa: E402
import scanner                                              # noqa: E402
import scheme_store                                         # noqa: E402
import storage as storage_mod                               # noqa: E402
from classification_config import (                         # noqa: E402
    GenericDirRule, KeywordRule, compile_config, classify_context,
    default_config, sanitize_component_name, validate_config,
)
from duplicate_report import (                              # noqa: E402
    DupEntry, STATUS_VERIFIED, assemble_report,
)
from executor import execute_plan, undo_latest_batch        # noqa: E402
from planner import (                                       # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, _build_target_dir_v2, build_plan,
    generate_plan,
)
from storage import Storage                                 # noqa: E402
from utils import win_long_path                             # noqa: E402

RESULTS: list = []
WORK = tempfile.mkdtemp(prefix="p2_fixes_")


def rec(tid, title, status, evidence=""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    print(f"[{status}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def ok(tid, title, cond, evidence=""):
    rec(tid, title, "PASS" if cond else "FAIL", evidence)
    return cond


def fresh(name):
    root = os.path.join(WORK, name)
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    return root


def mk(root, rel, content="内容" * 20):
    p = os.path.join(root, rel)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def plan_for(root, mode=MODE_BY_DIRECTION_SEMESTER, config=None):
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, mode, [], config=config, compute_duplicates=True)
    for item in plan.items:
        item.selected = True
    return plan, sr


# ==================== 组1 数据安全 ====================

def t01_root_renamed_history():
    root = fresh("g1_root_rename")
    mk(root, "笔记.txt", "根目录改名后仍可撤销")
    plan, _sr = plan_for(root, config=default_config())
    st = Storage(root)
    er = execute_plan(plan, st)
    st.close()
    moved_root = root + "_改名后"
    os.rename(root, moved_root)
    st2 = Storage(moved_root)
    batches = st2.list_batches(include_actions=False)
    note = st2.root_mismatch_note()
    undo = undo_latest_batch(st2)
    restored = os.path.isfile(os.path.join(moved_root, "笔记.txt"))
    ok_ = (er.moved == 1 and len(batches) == 1 and bool(note)
           and undo.restored == 1 and restored)
    ok("P2-01", "根目录改名后历史与撤销不静默消失", ok_,
       f"批次={len(batches)} 撤销恢复={undo.restored} 源恢复={restored}\n"
       f"说明={note.splitlines()[0] if note else '（空）'}")
    st2.close()


def t02_protect_rename_and_missing():
    root = fresh("g1_protect_rename")
    keep = os.path.join(root, "要保护")
    os.makedirs(keep)
    gone = os.path.join(root, "暂时不存在")
    protect_config.save_manual_protect(root, [keep, gone])
    moved = root + "_移动后"
    os.rename(root, moved)
    paths, notes = protect_config.load_manual_protect_report(moved)
    rel_saved = json.load(open(protect_config.config_path(moved),
                               encoding="utf-8"))["manual_paths"]
    renamed_note = any("改名" in n or "移动" in n for n in notes)
    missing_note = any("不存在" in n for n in notes)
    kept_missing = any("暂时不存在" in p for p in paths)
    # 再次保存不能把暂时不存在的项清空
    protect_config.save_manual_protect(moved, paths)
    after = json.load(open(protect_config.config_path(moved),
                           encoding="utf-8"))["manual_paths"]
    ok_ = (len(rel_saved) == 2 and renamed_note and missing_note
           and kept_missing and len(after) == 2)
    ok("P2-02", "保护名单不因根目录改名/目录暂时不存在而被静默清空", ok_,
       f"保存项={rel_saved}\n读取={paths}\n说明={notes}\n再次保存={after}")


def t03_undo_sha_missing_or_unreadable():
    root = fresh("g1_undo_sha")
    mk(root, "资料.txt", "内容不变")
    plan, _ = plan_for(root, config=default_config())
    st = Storage(root)
    er = execute_plan(plan, st)
    # a) 记录里没有指纹：按路径恢复，并说明
    st.conn.execute("UPDATE actions SET source_sha256='' WHERE batch_id=?",
                    (er.batch_id,))
    st.conn.commit()
    u1 = undo_latest_batch(st)
    restored1 = os.path.isfile(os.path.join(root, "资料.txt"))
    note1 = any("没有内容指纹" in e for e in getattr(u1, "notes", []))

    # b) 目标读不出来：说“无法读取”，不能说“内容已被修改”
    mk(root, "二号.txt", "占位")
    plan2, _ = plan_for(root)
    # 去掉刚恢复的文件，避免计划重复
    plan2.items = [i for i in plan2.items if i.file.filename == "二号.txt"]
    er2 = execute_plan(plan2, st)
    real_sha = executor.sha256_file
    executor.sha256_file = lambda *_a, **_k: None
    try:
        u2 = undo_latest_batch(st)
    finally:
        executor.sha256_file = real_sha
    text2 = "\n".join(u2.errors_list + getattr(u2, "conflicts", []))
    ok_ = (u1.restored == 1 and restored1 and note1
           and u2.conflict == 1 and "无法读取" in text2
           and "内容已" not in text2)
    ok("P2-03", "SHA 为空或目标读不了时：不误报“内容已被修改”", ok_,
       f"无指纹：恢复={u1.restored} 已恢复={restored1} 说明含指纹提示={note1}\n"
       f"读失败：冲突={u2.conflict} 文本={text2}")
    st.close()


def t04_history_corrupt_rebuild():
    root = fresh("g1_db_corrupt")
    os.makedirs(os.path.join(root, ".organizer"))
    db = os.path.join(root, ".organizer", "history.db")
    with open(db, "w", encoding="utf-8") as f:
        f.write("这不是 sqlite 文件" * 10)
    st = Storage(root)
    corrupt_files = [n for n in os.listdir(os.path.join(root, ".organizer"))
                     if ".corrupt-" in n]
    empty = st.list_batches() == []
    note = bool(st.recovery_note)
    st.close()
    ok_ = note and empty and bool(corrupt_files)
    ok("P2-04", "history.db 损坏：改名留存并重建，目录仍可打开", ok_,
       f"提示={st.recovery_note.splitlines()[0] if note else '（空）'}\n"
       f"隔离文件={corrupt_files}")


def t05_walk_onerror_and_skipped_paths():
    root = fresh("g1_skip_paths")
    mk(root, "正常.txt")
    mk(root, "venv/包.txt")
    mk(root, ".隐藏/东西.txt")
    mk(root, "$RECYCLE.BIN/旧文件.txt")
    mk(root, "desktop.ini", "x")
    sr = scanner.scan_directory(root, detect_project_roots=False)
    files = [f.rel_path for f in sr.files]
    listed = "\n".join(p for p, _why in sr.skipped_dirs)
    # onerror 机制：权限/路径错误进入结果
    sr2 = scanner.ScanResult(root=root)
    scanner._walk_error(sr2, OSError(13, "Permission denied", "C:\\受限"))
    ok_ = (files == ["正常.txt"]
           and "venv" in listed and ".隐藏" in listed
           and "$RECYCLE.BIN" in listed
           and sr2.permission_errors == 1 and sr2.walk_errors
           and "受限" in sr2.errors[0])
    ok("P2-05", "跳过目录列出完整路径；os.walk 错误不静默", ok_,
       f"纳入文件={files}\n跳过清单=\n{listed}\n"
       f"walk_errors={sr2.walk_errors}")


def t06_long_path_scan_move_undo():
    root = fresh("g1_longpath")
    seg = "很长的目录名称用于测试长路径能力"
    rel_dir = os.sep.join([seg] * 14)
    rel = os.path.join(rel_dir, "深入文件.txt")
    full = os.path.join(root, rel)
    os.makedirs(win_long_path(os.path.dirname(full)), exist_ok=True)
    with open(win_long_path(full), "w", encoding="utf-8") as f:
        f.write("长路径内容")
    over = len(full) > 260
    plan, sr = plan_for(root, config=default_config())
    st = Storage(root)
    er = execute_plan(plan, st)
    target = os.path.join(root, plan.items[0].target_rel_path)
    moved = os.path.isfile(win_long_path(target))
    undo = undo_latest_batch(st)
    back = os.path.isfile(win_long_path(full))
    ok_ = (over and len(sr.files) == 1 and er.moved == 1 and moved
           and undo.restored == 1 and back)
    ok("P2-06", "超过 260 字符的深层文件可扫描、移动、撤销", ok_,
       f"源长度={len(full)} 扫描={len(sr.files)} 移动={er.moved} "
       f"目标存在={moved} 撤销={undo.restored} 源恢复={back}")
    st.close()


def t07_undo_removes_empty_parents():
    root = fresh("g1_empty_parents")
    mk(root, "课程作业.pdf", "整理")
    plan, _ = plan_for(root, config=default_config())
    st = Storage(root)
    execute_plan(plan, st)
    organized = os.path.join(root, "_已整理")
    existed = os.path.isdir(organized)
    undo = undo_latest_batch(st)
    gone = not os.path.isdir(organized)
    ok_ = existed and undo.restored == 1 and gone
    ok("P2-07", "撤销后清理本批次创建的空父目录（含 _已整理）", ok_,
       f"整理目录存在过={existed} 撤销={undo.restored} 现已清空={gone}")
    st.close()


def t08_execute_no_full_table_query():
    root = fresh("g1_bulk")
    mk(root, "a.txt")
    plan, _ = plan_for(root, config=default_config())
    st = Storage(root)
    calls = {"get_actions": 0}

    def counted(batch_id):
        calls["get_actions"] += 1
        raise AssertionError("执行循环不应逐项全表查询")

    real = st.get_actions
    st.get_actions = counted
    try:
        er = execute_plan(plan, st)
    finally:
        st.get_actions = real
    ok_ = er.moved == 1 and calls["get_actions"] == 0
    ok("P2-08", "执行循环不再逐项 get_actions + 两次 commit（批量写库）", ok_,
       f"移动={er.moved} 全表查询次数={calls['get_actions']}")
    st.close()


# ==================== 组2 / 组3 流程与界面 ====================

def t09_headline_and_meanings():
    no_move = SimpleNamespace(was_cancelled=False, moved=0)
    moved = SimpleNamespace(was_cancelled=False, moved=3)
    cancelled = SimpleNamespace(was_cancelled=True, moved=2)
    h1 = main_mod._execute_headline(no_move)
    h2 = main_mod._execute_headline(moved)
    h3 = main_mod._execute_headline(cancelled)
    ok_ = (h1 == "没有移动文件" and h2 == "整理完成！" and h3 == "整理已取消")
    ok("P2-09", "moved=0 时标题是「没有移动文件」，不是「整理完成！」", ok_,
       f"{h1} / {h2} / {h3}")


def t10_zero_byte_not_duplicates():
    entries = [
        DupEntry(rel_path="a/空.txt", abs_path="a", filename="空.txt",
                 size=0, size_display="0 B", sha256="same", status=STATUS_VERIFIED),
        DupEntry(rel_path="b/空.txt", abs_path="b", filename="空.txt",
                 size=0, size_display="0 B", sha256="same", status=STATUS_VERIFIED),
    ]
    report = assemble_report("root", entries, scope_note="测试", source="scan")
    ok_ = report.content_groups == 0 and report.content_files == 0
    ok("P2-10", "0 字节文件不互相算内容重复", ok_,
       f"内容组={report.content_groups} 文件={report.content_files}")


def t11_ocr_cache_and_retry():
    with ocr_engine._cache_lock:
        ocr_engine._cache.clear()
        ocr_engine._cache_order.clear()
    for i in range(ocr_engine.CACHE_LIMIT + 40):
        ocr_engine._cache_put((i,), "文字")
    size = len(ocr_engine._cache)
    ocr_engine._unavailable["windows"] = time.time() + 60
    in_cooldown = ocr_engine._engine_skippable("windows")
    ocr_engine._unavailable["windows"] = time.time() - 1
    retryable = not ocr_engine._engine_skippable("windows")
    ok_ = size <= ocr_engine.CACHE_LIMIT and in_cooldown and retryable
    ok("P2-11", "OCR 缓存有上限；临时失败冷却后允许重试", ok_,
       f"缓存={size}/{ocr_engine.CACHE_LIMIT} 冷却期跳过={in_cooldown} "
       f"过期可重试={retryable}")


def t12_ai_cache_and_prompt_cap():
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    root = fresh("g5_ai_cache")
    mk(root, "完全看不懂的资料.xyz", "x")
    pairs = ai_fallback.allowed_pairs(default_config())
    pair = pairs[0]
    calls = {"n": 0}

    def fake_transport(_settings, _messages):
        calls["n"] += 1
        return json.dumps({"direction": pair[0], "category": pair[1]},
                          ensure_ascii=False)

    settings = ai_fallback.AiSettings(api_key="test-key", enabled=True)
    plan1, _ = plan_for(root, config=default_config())
    note1 = ai_fallback.apply_ai_fallback(
        plan1, settings, transport=fake_transport, config=default_config())
    plan2, _ = plan_for(root, config=default_config())
    note2 = ai_fallback.apply_ai_fallback(
        plan2, settings, transport=fake_transport, config=default_config())
    disabled = ai_fallback.apply_ai_fallback(
        plan1, ai_fallback.AiSettings(api_key="k", enabled=False),
        transport=fake_transport, config=default_config())

    # 请求清单上限
    long_pairs = [("课程", f"类别{i}") for i in range(300)]
    msgs = ai_fallback._messages(plan1.items[0], long_pairs, False)
    prompt = msgs[1]["content"]
    shown = prompt.count("课程/")
    ok_ = (calls["n"] == 1 and "复用" in note2 and "未开启" in disabled
           and shown <= ai_fallback._MAX_PROMPT_PAIRS + 1)
    ok("P2-12", "AI 默认关闭、已问过复用缓存、清单不塞几百条", ok_,
       f"实际请求={calls['n']} 第二次={note2}\n关闭时={disabled}\n"
       f"prompt 分类行={shown}")


def t13_protect_normcase():
    root = fresh("g1_normcase")
    sub = os.path.join(root, "保护目录")
    os.makedirs(sub)
    try:
        got = protect_config.normalize_manual_path(root, sub.upper())
        err = ""
    except Exception as e:      # noqa: BLE001
        got, err = "", str(e)
    ok_ = bool(got) and not err
    ok("P2-13", "保护路径大小写比较统一 normcase", ok_, f"返回={got} 错误={err}")


# ==================== 组4 分类剩余 ====================

def t14_unregistered_inner_and_empty_category():
    root = fresh("g4_inner")
    mk(root, "未被登记的目录/内部子目录/文件.pdf", "x")
    plan, _ = plan_for(root, config=default_config())
    target = plan.items[0].target_rel_path
    keeps_inner = ("内部子目录" in target)
    empty_dir = _build_target_dir_v2("课程", "大二上", "", "文档")
    no_double = (os.sep + os.sep) not in empty_dir
    ok_ = keeps_inner and no_double and "未分类课程" in empty_dir
    ok("P2-14", "未登记顶层目录保留内部路径；空类别不出现 //", ok_,
       f"目标={target}\n空类别目录={empty_dir}")


def t15_sanitize_collision_and_bad_keyword():
    cfg = default_config()
    cfg.generic_dir_rules.extend([
        GenericDirRule("z1", "校园事务", "A:B"),
        GenericDirRule("z2", "校园事务", "A?B"),
    ])
    cfg.keyword_rules.extend([
        KeywordRule(["."], "过宽规则", "课程"),
        KeywordRule(["("], "过宽规则2", "课程"),
    ])
    errors = validate_config(cfg)
    collision = any("同一个目录" in e for e in errors)
    symbol1 = any("只包含符号" in e and "「.」" in e for e in errors)
    symbol2 = any("只包含符号" in e and "「(」" in e for e in errors)
    ok_ = collision and symbol1 and symbol2
    ok("P2-15", "sanitize 撞名在配置里报出；「.」「(」等过宽关键词被拒绝", ok_,
       "\n".join(e for e in errors if "同一个目录" in e or "只包含符号" in e))


def t16_circ_only_fallback():
    compiled = compile_config(default_config())

    def rel(*parts):
        return os.sep.join(parts)

    specific = classify_context(rel("大二上", "数据结构", "作业", "误放.circ"),
                                compiled)
    loose = classify_context("alu.circ", compiled)
    senior = classify_context(rel("2023-2024-2", "杂项", "实验.circ"), compiled)
    ok_ = (specific.category == "数据结构"
           and loose.category == "计算机组成原理"
           and loose.semester == "大二上"
           and senior.category == "计算机组成原理"
           and senior.semester == "2023-2024-2")
    ok("P2-16", ".circ 只在没有更具体目录规则时归计组，学期沿用路径", ok_,
       f"具体目录={specific.category}/{specific.rule_key}\n"
       f"散落={loose.category}/{loose.semester}\n"
       f"学长学期={senior.category}/{senior.semester}")


def t17_revert_manual_mark():
    root = fresh("g4_revert")
    mk(root, "大二上/数据结构/作业/高数作业.pdf", "x")
    plan, _ = plan_for(root, config=default_config())
    item = plan.items[0]
    # 先改成别的分类
    from plan_patch import OverrideParams, apply_override_to_plan
    apply_override_to_plan(plan, item.file.rel_path,
                           OverrideParams(direction="课程", semester="大三上",
                                          category="临时类别"))
    overridden = item.is_manual_overridden
    # 再改回原本的自动分类
    apply_override_to_plan(plan, item.file.rel_path,
                           OverrideParams(
                               direction=item.auto_direction or "",
                               semester=item.auto_semester or "",
                               category=item.auto_category or "",
                               inner_path=item.auto_inner_path or ""))
    cleared = not item.is_manual_overridden
    ok_ = overridden and cleared
    ok("P2-17", "改回原本自动分类后不再标记为「手动修正」", ok_,
       f"先手动={overridden} 改回自动后手动={item.is_manual_overridden} "
       f"依据={item.rule_applied}")


# ==================== 组5 外围 ====================

def t18_atomic_write_error_type():
    root = fresh("g5_atomic")
    blocker = os.path.join(root, "blocker")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    try:
        CT._atomic_write_text(os.path.join(blocker, "sub", "a.json"), "{}")
        err = ""
    except CFG.ClassificationConfigError as e:
        err = str(e)
    except OSError as e:        # 不应该出现
        err = f"OSError:{e}"
    ok_ = ("写入文件失败" in err and "OSError" not in err)
    ok("P2-18", "APPDATA/目标目录不可写时抛出 ClassificationConfigError", ok_,
       f"错误={err.splitlines()[0] if err else '（无）'}")


def t19_recent_read_error_not_overwrite():
    root = fresh("g5_recent")
    path = os.path.join(root, "recent.json")
    os.makedirs(path)  # 同名目录 → open 读会 OSError
    old = os.environ.get(recent_spaces.ENV_OVERRIDE)
    os.environ[recent_spaces.ENV_OVERRIDE] = path
    try:
        err = ""
        try:
            recent_spaces.list_spaces()
        except recent_spaces.RecentSpacesError as e:
            err = str(e)
        still_dir = os.path.isdir(path)
    finally:
        if old is None:
            os.environ.pop(recent_spaces.ENV_OVERRIDE, None)
        else:
            os.environ[recent_spaces.ENV_OVERRIDE] = old
    ok_ = bool(err) and still_dir
    ok("P2-19", "最近空间读失败会提示，不把旧记录当空覆盖", ok_,
       f"错误={err.splitlines()[0] if err else '（无）'} 原路径保留={still_dir}")


def t20_scheme_name_and_default_choice():
    from transfer_dialogs import ConfigChoiceDialog
    old_dir = os.environ.get(scheme_store.ENV_OVERRIDE)
    lib = os.path.join(WORK, "scheme_lib")
    os.environ[scheme_store.ENV_OVERRIDE] = lib
    try:
        cfg = default_config()
        r1 = scheme_store.save_scheme("我的方案", "", cfg)
        r2 = scheme_store.save_scheme("我的方案", "", cfg)
        names = [r1.name, r2.name]
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        dlg = ConfigChoiceDialog(cfg, cfg)
        merge_default = dlg.rb_merge.isChecked()
        dlg.close()
    finally:
        if old_dir is None:
            os.environ.pop(scheme_store.ENV_OVERRIDE, None)
        else:
            os.environ[scheme_store.ENV_OVERRIDE] = old_dir
    ok_ = (names[0] == "我的方案" and names[1] == "我的方案 (2)"
           and merge_default)
    ok("P2-20", "同名方案自动区分；导入默认不选「替换当前配置」", ok_,
       f"方案名={names} 默认合并={merge_default}")


def t21_version_and_guide():
    title = main_mod.APP_TITLE
    payload = CT.export_scheme_payload(default_config(), "x")
    ok_ = (title == "归档盒 V1.7" and payload["app"]["version"] == "1.7")
    ok("P2-21", "版本号统一为 V1.7（窗口标题 + 方案导出）", ok_,
       f"标题={title} 导出版本={payload['app']['version']}")


# ==================== 界面（最后执行） ====================

def t22_gui_busy_table_and_empty_history():
    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication.instance() or QApplication([])
    root = fresh("g3_gui")
    st = Storage(root)
    mw = main_mod.MainWindow()
    mw.path_edit.setText(root)
    mw._storage = st
    mw.task.working = True
    mw._set_preview_controls_enabled(True)
    busy_locked = not mw.table.isEnabled()
    mw.task.working = False
    mw._set_preview_controls_enabled(True)
    idle_enabled = mw.table.isEnabled()

    dlg = main_mod.HistoryDialog(mw, parent=mw)
    empty_text = ""
    if dlg.batch_table.rowCount() >= 1 and dlg.batch_table.item(0, 0):
        empty_text = dlg.batch_table.item(0, 0).text()
    empty_ok = "还没有整理记录" in empty_text

    # 子窗口统一关闭
    child = QDialog(mw)
    child.show()
    mw._history_dialog = dlg
    mw._duplicate_dialog = child
    mw._close_child_windows()
    children_closed = (mw._history_dialog is None and
                       mw._duplicate_dialog is None)

    filt = (mw.type_filter.itemText(0) == "全部类型"
            and mw.dir_filter.itemText(0) == "全部目录"
            and bool(mw.type_filter.accessibleName()))
    col3 = mw.table.columnWidth(3)
    avail = app.primaryScreen().availableGeometry()
    fits = (mw.width() <= max(avail.width(), 820)
            and mw.minimumHeight() <= 520)
    ok_ = (busy_locked and idle_enabled and empty_ok and children_closed
           and filt and col3 == 200 and fits)
    ok("P2-22", "忙碌禁改表格；空历史文案；关闭子窗口；筛选标签；小屏尺寸", ok_,
       f"忙碌锁表={busy_locked} 空闲可用={idle_enabled}\n"
       f"空历史=「{empty_text}」\n子窗口已关={children_closed} "
       f"筛选标签={filt} 分类依据列宽={col3}\n"
       f"窗口={mw.width()}x{mw.height()} 最小高={mw.minimumHeight()} "
       f"屏幕={avail.width()}x{avail.height()}")
    mw._storage = None
    st.close()
    mw.deleteLater()


def t23_gui_tree_retry_and_hidden_dirs():
    import ctypes
    import storage_tree
    from PySide6.QtWidgets import QApplication, QTreeWidgetItem
    app = QApplication.instance() or QApplication([])
    root = fresh("g3_tree")
    hidden = os.path.join(root, "隐藏属性的用户目录")
    os.makedirs(hidden)
    try:
        ctypes.windll.kernel32.SetFileAttributesW(hidden, 0x2)
        attrs_set = True
    except Exception:
        attrs_set = False
    children = storage_tree.list_child_dirs(root)
    shown = any(name == "隐藏属性的用户目录" for name, _p in children)

    tree = storage_tree.StorageTree(places=[])
    item = tree._make_item("目录", root, "folder")
    tree._apply_children(item, [], "模拟读取失败")
    retry = (item.data(0, storage_tree.ROLE_LOADED) is False
             and item.childIndicatorPolicy()
             == QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
    ok_ = shown and retry
    ok("P2-23", "目录树显示隐藏属性目录；读取失败可重试", ok_,
       f"隐藏属性已设置={attrs_set} 树中包含={shown} 可重试={retry}")
    tree.deleteLater()


def t24_gui_apply_blocked_without_impact():
    from PySide6.QtWidgets import QApplication, QDialogButtonBox
    from transfer_dialogs import ApplyPreviewDialog
    app = QApplication.instance() or QApplication([])
    cfg = default_config()
    dlg = ApplyPreviewDialog(
        root=WORK, base_config=cfg, draft_config=cfg, scan_result=None,
        diff=CT.config_diff(cfg, cfg))
    ok_btn = dlg.bb.button(QDialogButtonBox.Ok)
    blocked = not ok_btn.isEnabled()
    dlg.close()
    ok("P2-24", "影响没算出来（未扫描）时不能确认应用", blocked,
       f"确认应用可用={ok_btn.isEnabled()}")


def main() -> int:
    print(f"工作目录：{WORK}\n")
    for fn in (t01_root_renamed_history, t02_protect_rename_and_missing,
               t03_undo_sha_missing_or_unreadable, t04_history_corrupt_rebuild,
               t05_walk_onerror_and_skipped_paths, t06_long_path_scan_move_undo,
               t07_undo_removes_empty_parents, t08_execute_no_full_table_query,
               t09_headline_and_meanings, t10_zero_byte_not_duplicates,
               t11_ocr_cache_and_retry, t12_ai_cache_and_prompt_cap,
               t13_protect_normcase, t14_unregistered_inner_and_empty_category,
               t15_sanitize_collision_and_bad_keyword, t16_circ_only_fallback,
               t17_revert_manual_mark, t18_atomic_write_error_type,
               t19_recent_read_error_not_overwrite,
               t20_scheme_name_and_default_choice, t21_version_and_guide):
        try:
            fn()
        except Exception:
            import traceback
            rec(fn.__name__, "执行测试时异常", "FAIL", traceback.format_exc())

    for fn in (t22_gui_busy_table_and_empty_history,
               t23_gui_tree_retry_and_hidden_dirs,
               t24_gui_apply_blocked_without_impact):
        try:
            fn()
        except Exception:
            import traceback
            rec(fn.__name__, "执行界面测试时异常", "FAIL",
                traceback.format_exc())

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    skips = [r for r in RESULTS if r["status"] == "SKIP"]
    print(f"\n结果：PASS {len(RESULTS) - len(fails) - len(skips)} / "
          f"FAIL {len(fails)} / SKIP {len(skips)}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_p2_fixes_20261113.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {out}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
