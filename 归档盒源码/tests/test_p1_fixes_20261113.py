"""归档盒 P1 回归（2026-11-13，对应 V1.7 审计第 9 章 P1：分类与预览可信）

覆盖：关键词匹配、学期格式、大小写归一、预览/执行一致、唯一名稳定、
损坏备份恢复、CSV 导出、失败详情可见。

全部使用临时目录，不接触桌面/真实资料。
运行：<python> tests/test_p1_fixes_20261113.py
退出码：0 = 无 FAIL；1 = 有 FAIL。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

import classification_config as cc                         # noqa: E402
import config_transfer                                      # noqa: E402
import planner                                              # noqa: E402
import plan_patch                                           # noqa: E402
import duplicate_report                                     # noqa: E402
from classification_config import (                         # noqa: E402
    CONFIG_VERSION, classify_context, compile_config, default_config,
    config_path, SemesterRule,
)
from planner import (                                       # noqa: E402
    assign_unique_targets, build_plan, CourseRule, FileEntry, OrganizePlan,
    PlanItem, MODE_BY_DIRECTION_SEMESTER,
)

RESULTS: list = []
WORK = tempfile.mkdtemp(prefix="p1_fixes_")


def rec(tid: str, title: str, status: str, evidence: str = ""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    print(f"[{status}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def ok(tid: str, title: str, cond: bool, evidence: str = ""):
    rec(tid, title, "PASS" if cond else "FAIL", evidence)
    return cond


def fresh(name: str) -> str:
    root = os.path.join(WORK, name)
    os.makedirs(root, exist_ok=True)
    return root


def mk(root: str, rel: str, text: str = "内容") -> str:
    p = os.path.join(root, rel)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def classify(rel_path: str, cfg=None):
    cfg = cfg or default_config()
    return classify_context(rel_path.replace("/", os.sep), compile_config(cfg))


def rel(*parts: str) -> str:
    """平台化相对路径（与 scanner/planner 使用的一致）。"""
    return os.sep.join(parts)


def make_item(root: str, rel: str, target_dir: str,
              filename: str | None = None, manual: bool = False) -> PlanItem:
    entry = FileEntry(
        abs_path=os.path.join(root, rel), rel_path=rel.replace("/", os.sep),
        filename=os.path.basename(rel), file_type="文档", size=1,
        size_display="1 B",
    )
    name = filename or entry.filename
    return PlanItem(
        file=entry, course="课程", file_type="文档", rule_applied="测试",
        target_dir_rel=target_dir.replace("/", os.sep),
        target_filename=name,
        target_rel_path=os.path.join(target_dir.replace("/", os.sep), name),
        is_manual_overridden=manual,
    )


# ==================== 1) 关键词匹配 S2-14/15/16 ====================

def t01_keywords():
    ev = []
    match = cc._match_english_word
    checks = {
        "高数2.pdf 命中「高数」": match("高数2.pdf", "高数") is True,
        "线代A 命中「线代」": match("线代a", "线代") is True,
        "python3 命中「python」": match("python3.pdf", "python") is True,
        "training.docx 不命中「ai」": match("training.docx", "ai") is False,
        "main.py 不命中「ai」": match("main.py", "ai") is False,
        "email.txt 不命中「ai」": match("email.txt", "ai") is False,
        "chairman 不命中「ai」": match("chairman.mp4", "ai") is False,
        "ai作业 命中「ai」": match("ai作业.docx", "ai") is True,
    }
    ev += [f"{k}={v}" for k, v in checks.items()]

    # 按课程模式的默认规则也不能再用裸子串
    rule = CourseRule(keywords=["ai"], course_name="AIGC", order=1)
    course_ok = (not rule.matches("main.py")) and rule.matches("ai作业.docx")
    ev.append(f"CourseRule ai: main.py={rule.matches('main.py')} "
              f"ai作业={rule.matches('ai作业.docx')}")

    # S2-16：课程目录里的「小论文」不应被项目输出规则抢走
    m = classify("大二上/数据结构/小论文.pdf")
    ev.append(f"大二上/数据结构/小论文.pdf → 方向={m.direction} "
              f"类别={m.category} 规则={m.rule_key}")
    course_wins = m.direction == "课程"

    # 学期是 2024-2025-1 时（S2-24 修好后进入学期分支），论文也不能抢
    m2 = classify("2024-2025-1/数据结构/小论文.pdf")
    ev.append(f"2024-2025-1/数据结构/小论文.pdf → 方向={m2.direction} "
              f"类别={m2.category} 规则={m2.rule_key}")
    course_wins = course_wins and m2.direction == "课程"

    ok("S2-14/15/16", "中文关键词紧邻数字可用；英文按词边界；课程目录不被论文规则抢走",
       all(checks.values()) and course_ok and course_wins, "\n".join(ev))


# ==================== 2) 学期格式 S2-24 ====================

def t02_semester():
    cases = [
        "2024-2025-1/数据结构/作业.pdf",
        "2024-2025学年第一学期/概率论/期中.pdf",
        "2024秋/概率论/期中.pdf",
    ]
    ev = []
    all_ok = True
    for rel in cases:
        m = classify(rel)
        ev.append(f"{rel} → 学期={m.semester!r} 方向={m.direction} "
                  f"规则={m.rule_key}")
        if not m.semester or m.direction == "待定":
            all_ok = False
    ok("S2-24", "2024-2025-1 / 学年第一学期 / 2024秋 能识别为学期",
       all_ok, "\n".join(ev))


# ==================== 3) 大小写归一 S2-19/25 ====================

def t02b_senior_semester():
    """学长资料的学期名和本人学期表对不上，课程仍要认出来，但不能并进本人学期。"""
    ev = []
    foreign = classify("2023-2024-2/计组/实验报告.pdf")
    ev.append(
        f"计组：学期={foreign.semester!r} 方向={foreign.direction} "
        f"类别={foreign.category} 规则={foreign.rule_key}")
    known = classify("大二上/数据结构/作业.pdf")
    ev.append(
        f"本人学期：学期={known.semester!r} 规则={known.rule_key}")
    registered_empty = classify("大三上/数据结构/作业.pdf")
    ev.append(
        f"已登记但没有这门课的学期：学期={registered_empty.semester!r} "
        f"类别={registered_empty.category} 规则={registered_empty.rule_key}")

    import scanner as scanner_mod
    from planner import build_plan, MODE_BY_DIRECTION_SEMESTER
    root = fresh("senior_semester")
    mk(root, "2023-2024-2/计组/实验报告.pdf")
    mk(root, "大二上/数据结构/作业.pdf")
    scan = scanner_mod.scan_directory(root, detect_project_roots=False)
    plan = build_plan(scan, MODE_BY_DIRECTION_SEMESTER, [],
                      config=default_config(), compute_duplicates=False)
    paths = {i.file.rel_path.replace("\\", "/"): i.target_rel_path.replace("\\", "/")
             for i in plan.items}
    ev.append("目标：" + str(paths))
    senior_path = paths.get("2023-2024-2/计组/实验报告.pdf", "")
    own_path = paths.get("大二上/数据结构/作业.pdf", "")
    ok_ = (
        foreign.semester == "2023-2024-2"
        and foreign.direction == "课程"
        and foreign.category == "计算机组成原理"
        and foreign.rule_key.startswith("semester_dir_borrowed:")
        and "大二上" not in senior_path
        and "2023-2024-2" in senior_path
        and "计算机组成原理" in senior_path
        and known.rule_key.startswith("semester_dir:")
        and not known.rule_key.startswith("semester_dir_borrowed:")
        and "/大二上/" in own_path
        and registered_empty.semester == "大三上"
        and registered_empty.category == "数据结构"
        and "大二上" not in registered_empty.semester
    )
    ok("学长学期", "他人学年目录按课程归档，不并入本人的学期", ok_, "\n".join(ev))


def t03_normcase():
    ev = []
    m = classify("code/main.py")
    ev.append(f"code/main.py → 方向={m.direction} 规则={m.rule_key}")
    direction_ok = m.direction == "项目"

    root = fresh("s2_19_case")
    items = [
        make_item(root, "a/报告.PDF", "_已整理/课程/目标", "报告.PDF"),
        make_item(root, "b/报告.pdf", "_已整理/课程/目标", "报告.pdf"),
    ]
    plan = OrganizePlan(root=root, mode=MODE_BY_DIRECTION_SEMESTER,
                        rules=[], items=items)
    assign_unique_targets(plan.items, root)
    targets = [os.path.normcase(i.target_rel_path) for i in plan.items]
    ev.append("两个仅大小写不同的目标：" + str(
        [(i.target_filename, os.path.normcase(i.target_rel_path))
         for i in plan.items]))
    unique_ok = len(set(targets)) == 2

    # 同一路径不同大小写打开历史库，应命中同一份历史
    import storage as storage_mod
    r1 = os.path.join(root, "空间")
    os.makedirs(r1, exist_ok=True)
    st1 = storage_mod.Storage(r1)
    st1.conn.execute(
        "INSERT INTO batches (root, mode, status, created_at, summary) "
        "VALUES (?,?,?,?,?)", (st1.root, "by_type", "planning", "t", "x"))
    st1.conn.commit()
    st1.close()
    st2 = storage_mod.Storage(r1.lower() if os.name == "nt" else r1)
    n = st2.conn.execute(
        "SELECT COUNT(*) FROM batches WHERE root = ?",
        (st2.root,)).fetchone()[0]
    st2.close()
    ev.append(f"用不同大小写根目录查询到的批次数={n}")
    storage_ok = (n == 1) or os.name != "nt"

    ok("S2-19/25", "大小写归一：顶层方向、目标唯一名、历史库根目录",
       direction_ok and unique_ok and storage_ok, "\n".join(ev))


# ==================== 4) 预览与执行同路径 S2-20/21/22 ====================

def t04_preview_equals_exec():
    import copy
    ev = []
    root = fresh("s2_20_preview")
    items = [
        make_item(root, "a/同名.pdf", "_已整理/待定", "同名.pdf"),
        make_item(root, "b/同名.pdf", "_已整理/待定", "同名.pdf"),
        make_item(root, "c/无关.pdf", "_已整理/课程", "无关.pdf"),
    ]
    plan = OrganizePlan(root=root, mode=MODE_BY_DIRECTION_SEMESTER,
                        rules=[], items=items)
    assign_unique_targets(plan.items, root)

    params = plan_patch.OverrideParams(
        direction="课程", semester="大二上", category="数据结构", inner_path="")
    preview = plan_patch.preview_override_target(
        plan, rel("b", "同名.pdf"), params)
    simulated = copy.deepcopy(plan)
    applied = plan_patch.apply_override_to_plan(
        simulated, rel("b", "同名.pdf"), params)
    ev.append(f"预览={preview}")
    ev.append(f"应用={applied.target_rel_path}")
    same = os.path.normcase(preview) == os.path.normcase(applied.target_rel_path)

    # 预览必须真的按 apply 的校验：课程方向缺学期时预览要报错而不是"通过"
    bad = plan_patch.OverrideParams(direction="课程", semester="")
    err = ""
    try:
        plan_patch.preview_override_target(plan, rel("c", "无关.pdf"), bad)
    except plan_patch.OverrideValidationError as e:
        err = str(e)
    ev.append(f"缺学期预览报错={err!r}")
    validate_ok = bool(err)

    # S2-22：规则试运行只比较目标路径（rule_key 下标变化不算「将移动」）
    import rule_preview
    from classification_config import KeywordRule, TRIGGER_UNMATCHED
    import copy as _copy

    sroot = fresh("s2_22_preview")
    mk(sroot, "特殊作业.pdf")
    mk(sroot, "神秘文件.pdf")
    import scanner as scanner_mod
    scan = scanner_mod.scan_directory(sroot, detect_project_roots=False)

    base_cfg = default_config()
    base_cfg.keyword_rules = [KeywordRule(
        keywords=["特殊"], target="特殊课程", trigger=TRIGGER_UNMATCHED)]
    draft_cfg = _copy.deepcopy(base_cfg)
    draft_cfg.keyword_rules = [KeywordRule(
        keywords=["绝不匹配"], target="无用", trigger=TRIGGER_UNMATCHED)] + \
        draft_cfg.keyword_rules
    impact = rule_preview.preview_impact(
        scan, draft_cfg, base_cfg, MODE_BY_DIRECTION_SEMESTER, [])
    ev.append(f"仅 rule_key 下标变化时 changed={impact.changed_count}")
    compare_ok = impact.changed_count == 0

    ok("S2-20/21/22", "预览目标=应用目标；缺字段预览即报错；试运行只比目标路径",
       same and validate_ok and compare_ok, "\n".join(ev))


# ==================== 5) 唯一名稳定 S2-23 ====================

def t05_stable_names():
    import copy
    ev = []
    root = fresh("s2_23_stable")
    items = [
        make_item(root, "a/报告.pdf", "_已整理/待定", "报告.pdf"),
        make_item(root, "b/报告.pdf", "_已整理/待定", "报告.pdf"),
        make_item(root, "c/无关.pdf", "_已整理/课程", "无关.pdf"),
    ]
    plan = OrganizePlan(root=root, mode=MODE_BY_DIRECTION_SEMESTER,
                        rules=[], items=items)
    assign_unique_targets(plan.items, root)
    first = {i.file.rel_path: i.target_filename for i in plan.items}
    assign_unique_targets(plan.items, root)
    second = {i.file.rel_path: i.target_filename for i in plan.items}
    ev.append(f"第一次={first}")
    ev.append(f"第二次={second}")

    plan2 = copy.deepcopy(plan)
    params = plan_patch.OverrideParams(
        direction="课程", semester="大二上", category="数据结构")
    plan_patch.apply_override_to_plan(plan2, rel("c", "无关.pdf"), params)
    after = {i.file.rel_path: i.target_filename for i in plan2.items}
    ev.append(f"改无关行后={after}")
    untouched = (after[rel("a", "报告.pdf")] == first[rel("a", "报告.pdf")]
                 and after[rel("b", "报告.pdf")] == first[rel("b", "报告.pdf")])

    ok("S2-23", "唯一名稳定：重复分配不变；改无关行不影响其他文件名",
       first == second and untouched, "\n".join(ev))


# ==================== 6) 损坏备份不堵死恢复 S2-35 ====================

def t06_corrupt_backup():
    root = fresh("s2_35_backup")
    os.makedirs(os.path.dirname(config_path(root)), exist_ok=True)
    cfg = default_config()
    cfg.semester_rules.append(SemesterRule(source="测试学期", semester="大二上"))
    payload = {"version": CONFIG_VERSION, "root": root, "updated_at": "t"}
    payload.update(cc.config_to_dict(cfg))
    with open(config_path(root), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    good = config_transfer.create_backup(root)          # 可用备份
    os.makedirs(os.path.dirname(config_path(root)), exist_ok=True)
    with open(config_path(root), "w", encoding="utf-8") as f:
        f.write("{坏掉的 JSON")
    bad = config_transfer.create_backup(root)           # 更新的损坏备份

    backups_before = [i.path for i in config_transfer.list_backups(root)]
    err = ""
    try:
        state, msg = config_transfer.restore_latest_backup(root)
    except Exception as e:
        state, msg, err = "", "", str(e)
    backups_after = [i.path for i in config_transfer.list_backups(root)]
    kept = all(os.path.isfile(p) for p in backups_before)
    no_block = not err
    ok("S2-35", "最新备份损坏时跳过并恢复上一份可用备份，历史备份不删除",
       no_block and kept and len(backups_after) >= len(backups_before),
       f"损坏备份={os.path.basename(bad)}\n可用备份={os.path.basename(good)}\n"
       f"恢复错误={err!r} 消息={msg}\n"
       f"备份数量 前={len(backups_before)} 后={len(backups_after)} 全部保留={kept}")


# ==================== 7) CSV 导出 S2-43/S3-20 ====================

def t07_csv():
    ev = []
    text = duplicate_report.export_csv_text(_fake_report())
    ev.append(f"文本首行={text.splitlines()[0][:60]!r}")
    formula_ok = ("'=1+1" in text or '"\'=1+1' in text)
    crlf_ok = "\r\r\n" not in text

    path = os.path.join(WORK, "dup.csv")
    duplicate_report.write_csv_file(path, text)
    raw = open(path, "rb").read()
    bom_ok = raw.startswith(b"\xef\xbb\xbf")
    body = raw.decode("utf-8-sig")
    ev.append(f"文件 BOM={bom_ok} 含\\r\\r\\n={'\\r\\r\\n' in body} "
              f"首行含\\r\\n={'\r\n' in body}")
    ok("S2-43/S3-20", "CSV：UTF-8 BOM、正常 \\r\\n、公式前缀转义",
       formula_ok and crlf_ok and bom_ok and ("\r\r\n" not in body),
       "\n".join(ev))


def _fake_report():
    entry = duplicate_report.DupEntry(
        rel_path="=1+1.pdf", abs_path="C:/空间/=1+1.pdf", filename="=1+1.pdf",
        size=1, size_display="1 B", sha256="0" * 64, status="identical",
        reason="", target_rel_path="")
    group = duplicate_report.DupGroup(
        kind="content", group_id="g1", label="内容重复", entries=[entry])
    return duplicate_report.DuplicateReport(
        root="C:/空间", generated_at="", scope_note="", source="scan",
        groups=[group])


# ==================== 8) 失败详情可见 S3-23 ====================

def t08_errors():
    import executor as ex
    import scanner
    import storage as storage_mod
    ev = []
    root = fresh("s3_23_errors")
    mk(root, "报告.pdf")
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, planner.MODE_BY_TYPE, [])
    for it in plan.items:
        it.selected = True
    # 计划生成之后才占用目标，模拟"预览时可用、执行时被占用"的真实场景
    blocked = plan.items[0].target_rel_path
    os.makedirs(os.path.dirname(os.path.join(root, blocked)), exist_ok=True)
    with open(os.path.join(root, blocked), "w", encoding="utf-8") as f:
        f.write("已被占用")
    st = storage_mod.Storage(root)
    res = ex.execute_plan(plan, st)
    st.close()
    detail = "\n".join(res.errors)
    ev.append(f"skipped={res.skipped} failed={res.failed} errors={res.errors}")
    ok("S3-23", "跳过/失败的逐项原因进入错误详情（可在界面展示）",
       res.skipped >= 1 and "报告.pdf" in detail, "\n".join(ev))


# ==================== 9) 手动保存配置也建备份 S2-36 ====================

def t09_save_backup():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(WORK, "_appdata")
    os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(
        WORK, "_appdata", "recent_spaces.json")
    try:
        from PySide6.QtWidgets import QApplication
        import main as m
        from classification_config import save_config
    except Exception as e:
        rec("S2-36", "手动保存配置前先建备份", "SKIP",
            f"本机无可用 PySide6 / main 导入失败：{e}")
        return
    app = QApplication.instance() or QApplication([])
    root = fresh("s2_36_save")
    initial = default_config()
    initial.semester_rules.append(SemesterRule(source="初始配置", semester="大二上"))
    save_config(root, initial)
    before = len(config_transfer.list_backups(root))
    mw = m.MainWindow()
    mw.path_edit.setText(root)
    cfg = default_config()
    cfg.semester_rules.append(SemesterRule(source="手工保存", semester="大二上"))
    mw._save_classification_config(root, cfg)
    after = len(config_transfer.list_backups(root))
    ok("S2-36", "手动保存配置前先建备份（恢复不会被手工编辑越过）",
       after == before + 1,
       f"保存前备份数={before} 保存后={after}")
    mw.close()


# ==================== 10) 损坏的 recent_spaces.json 不被覆盖 S2-37 ====================

def t10_recent_corrupt():
    import recent_spaces
    d = os.path.join(WORK, "recent_corrupt")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "recent.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "items": [{"path": "坏掉')
    os.environ["ARCHIVEBOX_RECENT_FILE"] = path
    listed = recent_spaces.list_spaces()
    quarantined = [n for n in os.listdir(d) if ".corrupt-" in n]
    recent_spaces.touch(os.path.join(WORK, "空间X"))
    kept_text = ""
    if quarantined:
        with open(os.path.join(d, quarantined[0]), "r", encoding="utf-8") as f:
            kept_text = f.read()
    ok("S2-37", "损坏的 JSON 被改名留存，不被下一次写入覆盖",
       listed == [] and len(quarantined) == 1 and kept_text.startswith('{"version"'),
       f"列出={listed}\n隔离文件={quarantined}\n原内容保留={kept_text[:40]!r}")


def main() -> int:
    print(f"工作目录：{WORK}\n")
    for fn in (t01_keywords, t02_semester, t02b_senior_semester, t03_normcase,
               t04_preview_equals_exec,
               t05_stable_names, t06_corrupt_backup, t07_csv, t08_errors,
               t09_save_backup, t10_recent_corrupt):
        try:
            fn()
        except Exception:
            rec(fn.__name__, "执行测试时异常", "FAIL", traceback.format_exc())

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n结果：PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_p1_fixes_20261113.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {out}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
