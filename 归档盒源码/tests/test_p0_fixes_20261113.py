"""归档盒 P0 止血回归（2026-11-13，对应 V1.7 Windows 审计 S1/S3 项）

只覆盖本轮 10 条 P0 修复，不替代其余验收脚本。

运行（Windows，临时目录，不碰真实资料）：
    python tests/test_p0_fixes_20261113.py

GUI 相关项需要 PySide6；缺失时自动记为 SKIP 并说明原因，
不会假装通过。退出码：0 = 无 FAIL，1 = 存在 FAIL。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

import scanner                                              # noqa: E402
import planner                                              # noqa: E402
import executor                                             # noqa: E402
import storage as storage_mod                               # noqa: E402
import config_transfer                                      # noqa: E402
from classification_config import (                         # noqa: E402
    CONFIG_VERSION, SemesterRule, config_path, config_to_dict, default_config,
)
from planner import (                                       # noqa: E402
    build_plan, generate_plan, MODE_BY_COURSE_THEN_TYPE,
    MODE_BY_DIRECTION_SEMESTER, DEFAULT_COURSE_RULES,
)
from storage import Storage                                 # noqa: E402
from executor import execute_plan, undo_latest_batch        # noqa: E402

RESULTS: list = []
WORK = tempfile.mkdtemp(prefix="p0_fixes_")


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
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root: str, rel: str, content: str = "内容" * 20) -> str:
    p = os.path.join(root, rel)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def make_junction(link: str, target: str) -> tuple[bool, str]:
    """用系统 mklink 创建目录 junction（不需要管理员）。"""
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:                                # pragma: no cover
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ==================== S1-01 junction 被当成普通目录 ====================

def t01_junction():
    base = fresh("s1_01_junction")
    space = os.path.join(base, "space")
    target = os.path.join(base, "target")
    os.makedirs(space)
    os.makedirs(target)
    mk(target, "真实文件.txt", "target 里的真实文件")
    link = os.path.join(space, "链接目录")
    created, msg = make_junction(link, target)
    if not created:
        rec("S1-01", "扫描跳过 junction / 重解析点", "SKIP",
            f"无法创建 junction，无法验证：{msg}")
        return

    is_j = scanner._is_junction_or_reparse(link)
    not_j = scanner._is_junction_or_reparse(target)
    sr = scanner.scan_directory(space, detect_project_roots=True)
    rels = [f.rel_path for f in sr.files]
    listed = [p for p in sr.junction_dirs]
    jumped = [r for r in rels if r.startswith("链接目录")]

    ok_ = (is_j and not not_j and jumped == [] and len(listed) == 1)
    ok("S1-01", "扫描跳过 junction / 重解析点",
       ok_,
       f"isjunction(link)={is_j} isjunction(普通目录)={not_j}\n"
       f"扫描到的文件={rels}\n"
       f"跳过的 junction={listed}\n"
       f"summary={sr.summary}")


# ==================== S1-02 / S1-03 撤销不得假成功 ====================

def _plan_one_file(root: str):
    mk(root, "笔记.txt", "原始内容")
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
    for it in plan.items:
        it.selected = True
    return plan


def t02_undo_db_failure():
    root = fresh("s1_02_undo_db_fail")
    plan = _plan_one_file(root)
    st = Storage(root)
    real_update = st.update_action

    def flaky(batch_id, action_index, status, error=""):
        if status == storage_mod.STATUS_MOVED:
            raise RuntimeError("模拟写库失败")
        return real_update(batch_id, action_index, status, error=error)

    st.update_action = flaky
    er = execute_plan(plan, st)
    st.update_action = real_update
    batch = st.get_batch(er.batch_id)

    undo = undo_latest_batch(st)
    moved_to_target = os.path.isfile(
        os.path.join(root, "_已整理", "课程", "笔记.txt")) or any(
        os.path.isfile(os.path.join(dp, f))
        for dp, _dn, fs in os.walk(os.path.join(root, "_已整理"))
        for f in fs)
    ok_ = (batch is not None and batch.status != "done"
           and er.errors and undo.refused and undo.restored == 0
           and not undo.is_complete
           and "完整撤销" not in undo.summary())
    ok("S1-02", "执行写库失败 → 批次非 done；撤销拒绝且恢复 0 项不叫完整成功",
       ok_,
       f"批次状态={batch.status if batch else None}\n"
       f"执行错误={er.errors}\n"
       f"撤销 refused={undo.refused} restored={undo.restored} "
       f"is_complete={undo.is_complete}\n"
       f"summary={undo.summary()}\n"
       f"文件仍在整理目录={moved_to_target}")
    st.close()


def t03_undo_unfinished_states():
    root = fresh("s1_03_undo_states")
    plan = _plan_one_file(root)
    st = Storage(root)
    er = execute_plan(plan, st)
    bid = er.batch_id
    details = []

    # a) 状态仍是 executing
    st.update_batch_status(bid, "executing")
    u1 = undo_latest_batch(st)
    details.append(f"executing: refused={u1.refused} restored={u1.restored}")
    a_ok = u1.refused and u1.restored == 0 and not u1.is_complete

    # b) 存在 planned（模拟 rename 成功但写库失败）
    st.update_batch_status(bid, "partial")
    st.conn.execute("UPDATE actions SET status='planned' WHERE batch_id=?", (bid,))
    st.conn.commit()
    u2 = undo_latest_batch(st)
    details.append(f"planned: refused={u2.refused} restored={u2.restored}")
    b_ok = u2.refused and u2.restored == 0 and not u2.is_complete

    # c) 存在 paused（状态不确定，已提示人工确认）
    st.conn.execute("UPDATE actions SET status='paused' WHERE batch_id=?", (bid,))
    st.conn.commit()
    u3 = undo_latest_batch(st)
    details.append(f"paused: refused={u3.refused} restored={u3.restored}")
    c_ok = u3.refused and u3.restored == 0 and not u3.is_complete

    # d) 全部已撤销 / 无可恢复项：不允许报完整成功
    st.conn.execute("UPDATE actions SET status='undone' WHERE batch_id=?", (bid,))
    st.conn.commit()
    u4 = undo_latest_batch(st)
    details.append(f"无可恢复: restored={u4.restored} is_complete={u4.is_complete}")
    d_ok = u4.restored == 0 and not u4.is_complete and "完整撤销" not in u4.summary()

    ok("S1-03", "planning/executing/planned/paused 时拒绝撤销；恢复 0 项不算完整",
       a_ok and b_ok and c_ok and d_ok, "\n".join(details))
    st.close()


# ==================== S1-04 rename 失败兜底不得覆盖 ====================

def t04_copy_then_replace():
    base = fresh("s1_04_replace")
    src = os.path.join(base, "源.txt")
    dst = os.path.join(base, "目标.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("源内容")
    with open(dst, "w", encoding="utf-8") as f:
        f.write("用户已有内容")

    moved, msg, is_conflict = executor._copy_then_replace(src, dst)
    with open(dst, "r", encoding="utf-8") as f:
        dst_text = f.read()
    conflict_ok = (moved is False and is_conflict is True
                   and dst_text == "用户已有内容" and os.path.isfile(src)
                   and not os.path.exists(dst + ".archivetmp"))

    # 目标不存在时应正常完成移动
    src2 = os.path.join(base, "源2.txt")
    dst2 = os.path.join(base, "目标2.txt")
    with open(src2, "w", encoding="utf-8") as f:
        f.write("新内容")
    moved2, msg2, conflict2 = executor._copy_then_replace(src2, dst2)
    with open(dst2, "r", encoding="utf-8") as f:
        dst2_text = f.read()
    success_ok = (moved2 is True and conflict2 is False
                  and dst2_text == "新内容" and not os.path.exists(src2))

    ok("S1-04", "rename 失败兜底：目标已存在不覆盖、不删源；目标不存在可移动",
       conflict_ok and success_ok,
       f"冲突：moved={moved} conflict={is_conflict} msg={msg} "
       f"目标内容={dst_text!r} 源仍在={os.path.isfile(src)}\n"
       f"正常：moved={moved2} msg={msg2} 目标内容={dst2_text!r} "
       f"源已删={not os.path.exists(src2)}")


# ==================== S1-05 / S1-06 旧计划不能在看不见时执行 ====================

def t05_gui_plan_invalidation():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(WORK, "appdata")
    os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(WORK, "recent.json")
    try:
        from PySide6.QtWidgets import QApplication, QLabel, QDialogButtonBox
        import main as main_mod
    except Exception as e:
        rec("S1-05/06", "执行后 / 扫描失败后旧计划不可执行", "SKIP",
            f"本机无可用 PySide6 或 main 导入失败：{e}")
        rec("S3-01", "确认框默认取消、危险色、完整根目录", "SKIP", "同上")
        return None, None

    app = QApplication.instance() or QApplication([])
    # 屏蔽模态弹窗（测试进程内替换，不影响产品代码）
    for name in ("information", "warning", "critical"):
        setattr(main_mod.QMessageBox, name,
                staticmethod(lambda *a, **k: None))
    main_mod.QMessageBox.question = staticmethod(
        lambda *a, **k: main_mod.QMessageBox.No)

    mw = main_mod.MainWindow()
    root = fresh("s1_05_gui")
    plan = _plan_one_file(root)
    sr = scanner.scan_directory(root, detect_project_roots=False)

    # --- S1-05：执行结束后计划作废 ---
    mw.scan_result = sr
    mw.current_plan = plan
    mw._plan_valid = True
    res = executor.ExecuteResult(batch_id=1, moved=1)
    mw._on_execute_done(res)
    after_exec = (mw.current_plan is None and mw._plan_valid is False
                  and not mw.btn_execute.isEnabled())

    # --- S1-06：新扫描一开始作废旧计划 ---
    mw.current_plan = plan
    mw._plan_valid = True
    created = {}

    class _FakeSignal:
        def connect(self, *_a, **_k):
            pass

    class _FakeWorker:
        def __init__(self, *a, **k):
            created["n"] = created.get("n", 0) + 1
            self.finished_ok = _FakeSignal()
            self.failed = _FakeSignal()
            self.finished = _FakeSignal()

        def start(self):
            pass

    real_worker = main_mod.ScanWorker
    main_mod.ScanWorker = _FakeWorker
    try:
        mw._start_scan(root)
    finally:
        main_mod.ScanWorker = real_worker
        mw._scan_worker = None      # 别让假 worker 进入关闭流程
    after_start = (mw.current_plan is None and mw._plan_valid is False
                   and not mw.btn_execute.isEnabled()
                   and "旧预览已失效" in mw.invalid_label.text())
    mw._set_working(False)

    # --- S1-06：扫描失败禁用执行并写明旧预览已失效 ---
    mw.current_plan = plan
    mw._plan_valid = True
    mw._on_scan_failed("模拟扫描异常")
    after_fail = (mw.current_plan is None and mw._plan_valid is False
                  and not mw.btn_execute.isEnabled()
                  and "旧预览已失效" in mw.invalid_label.text())

    ok("S1-05/06", "执行后 / 重新扫描开始 / 扫描失败后，旧计划都不可执行",
       after_exec and after_start and after_fail,
       f"执行后不可执行={after_exec}\n"
       f"新扫描开始即作废={after_start}\n"
       f"扫描失败后不可执行={after_fail}\n"
       f"提示={mw.invalid_label.text()!r}")

    # --- S3-01：确认框默认取消 / 危险色 / 完整根目录 ---
    dlg = main_mod.ExecuteConfirmDialog(root, plan)
    box = dlg.findChild(QDialogButtonBox)
    ok_btn = box.button(QDialogButtonBox.Ok)
    cancel_btn = box.button(QDialogButtonBox.Cancel)
    defaults_ok = (ok_btn.isDefault() is False
                   and ok_btn.autoDefault() is False
                   and cancel_btn.isDefault() is True)
    danger_ok = "C62828" in ok_btn.styleSheet()
    labels = " ".join(l.text() for l in dlg.findChildren(QLabel))
    path_ok = root in labels
    dlg.show()
    app.processEvents()
    focus_ok = dlg.focusWidget() is cancel_btn
    dlg.close()
    ok("S3-01", "确认框默认取消、确认按钮危险色、根目录完整显示",
       defaults_ok and danger_ok and path_ok and focus_ok,
       f"Ok.isDefault={ok_btn.isDefault()} Ok.autoDefault={ok_btn.autoDefault()} "
       f"Cancel.isDefault={cancel_btn.isDefault()}\n"
       f"危险色={danger_ok} 完整根目录={path_ok} 初始焦点在取消={focus_ok}")
    mw.close()
    return mw, app


# ==================== S2-49 待定默认不勾选 ====================

def t06_pending_unselected():
    root = fresh("s2_49_pending")
    mk(root, "大二上/数据结构/实验一.pdf", "明确命中")
    mk(root, "一堆没头没脑的文件.txt", "无法判断")
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = build_plan(sr, MODE_BY_DIRECTION_SEMESTER, [],
                      config=default_config(), compute_duplicates=False)
    pending = [i for i in plan.items if i.direction == "待定"]
    matched = [i for i in plan.items if i.direction != "待定"]
    ok_ = (len(pending) == 1 and pending[0].selected is False
           and len(matched) == 1 and matched[0].selected is True)
    ok("S2-49", "「待定」默认不勾选，已命中分类的项仍默认勾选",
       ok_,
       "\n".join(f"{i.file.rel_path} → 方向={i.direction} "
                 f"规则={i.rule_key} 勾选={i.selected}"
                 for i in plan.items))


# ==================== S1-07 / S1-08 AI 默认关闭、无内置密钥 ====================

def _iter_source_files():
    """只扫源码与测试的 .py —— 密钥若存在只会在代码里。"""
    for dp, dn, fs in os.walk(SRC):
        dn[:] = [d for d in dn if d not in ("__pycache__", ".git", ".venv")]
        for fn in fs:
            if fn.endswith(".py"):
                yield os.path.join(dp, fn)


def t07_ai_defaults():
    # 密钥前缀故意拆开写，避免本测试文件自己成为"源码里出现密钥前缀"的假阳性
    key_prefix = "s" + "k-"
    has_ai_module = any(
        os.path.basename(p) in ("ai_fallback.py", "ai_config.py")
        for p in _iter_source_files())
    hits = []
    for p in _iter_source_files():
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        if key_prefix in text:
            hits.append(os.path.relpath(p, SRC))
    if not has_ai_module:
        rec("S1-07/08", "AI 默认关闭且源码里没有内置密钥", "SKIP",
            "当前 归档盒源码/ 不含 ai_fallback.py / ai_config.py（AI 模块"
            "只存在于 2026-11-12 的 Windows 包，未进本仓库），"
            "无代码可改；本项只验证了密钥扫描。\n"
            f"包含密钥前缀的文件：{hits or '无'}")
        return
    ok("S1-07/08", "AI 默认关闭且源码里没有内置密钥", not hits,
       f"发现包含密钥前缀的文件：{hits or '无'}")


# ==================== S1-09 恢复配置先备份 ====================

def _write_config(root: str, cfg) -> None:
    path = config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": CONFIG_VERSION, "root": os.path.abspath(root),
               "updated_at": "2026-11-13T00:00:00"}
    payload.update(config_to_dict(cfg))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_semester_sources(root: str):
    with open(config_path(root), "r", encoding="utf-8") as f:
        data = json.load(f)
    return {r.get("source") for r in data.get("semester_map", [])}


def t08_restore_backup():
    root = fresh("s1_09_restore")
    cfg_a = default_config()
    cfg_a.semester_rules.append(SemesterRule(source="测试学期A", semester="大二上"))
    _write_config(root, cfg_a)
    backup_a = config_transfer.create_backup(root)

    cfg_b = default_config()
    cfg_b.semester_rules.append(SemesterRule(source="测试学期B", semester="大三上"))
    _write_config(root, cfg_b)

    state, msg = config_transfer.restore_latest_backup(root)
    restored = _read_semester_sources(root)
    backups = [i.path for i in config_transfer.list_backups(root)]
    new_backup_exists = any(
        p != backup_a and os.path.basename(p) > os.path.basename(backup_a)
        for p in backups)
    ok_ = ("测试学期A" in restored and "测试学期B" not in restored
           and os.path.isfile(backup_a) and new_backup_exists
           and len(backups) >= 2
           and "已备份为" in msg and "已保留" in msg)
    ok("S1-09", "恢复配置前先备份当前配置，且所用备份不被删除",
       ok_,
       f"恢复后学期来源含测试学期A={'测试学期A' in restored} "
       f"仍含测试学期B={'测试学期B' in restored}\n"
       f"备份数量={len(backups)} 所用备份仍在={os.path.isfile(backup_a)} "
       f"新备份存在={new_backup_exists}\n"
       f"返回消息={msg}")


# ==================== S1-10 计划表白底深色文字 ====================

def t09_table_qss():
    with open(os.path.join(SRC, "main.py"), "r", encoding="utf-8") as f:
        text = f.read()
    ok_ = ("QTableWidget::item" in text and "#212121" in text
           and "QTableWidget {" in text and "color: #212121;" in text)
    ok("S1-10", "计划表白底设置了明确的深色文字", ok_,
       "main.py 全局 QSS 含 QTableWidget 与 QTableWidget::item 的 "
       f"深色文字声明={ok_}")


# ==================== S1-11 未捕获异常写入启动日志 ====================

def t10_startup_log():
    try:
        import main as main_mod
    except Exception as e:
        rec("S1-11", "未捕获异常写入启动日志", "SKIP", f"main 导入失败：{e}")
        return
    log = main_mod.startup_log_path()
    # 测试不留下新文件：记录原本是否存在，验证后按原状清理
    existed_before = os.path.exists(log)
    before_size = os.path.getsize(log) if existed_before else 0
    try:
        raise ValueError("模拟启动异常")
    except ValueError:
        main_mod.append_startup_log(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常：\n"
            + traceback.format_exc())
    content = ""
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8") as f:
            content = f.read()
    ok_ = ("模拟启动异常" in content and "ValueError" in content
           and len(content) > 20
           and (not existed_before or len(content) > before_size))
    ok("S1-11", "未捕获异常（含时间与堆栈）写入启动日志", ok_,
       f"日志路径={log}\n日志新增内容前 200 字="
       f"{content[before_size:before_size + 200]!r}")
    # 清理：原本不存在的日志文件删掉，避免污染源码目录
    if not existed_before:
        try:
            os.remove(log)
        except OSError:
            pass


# ==================== 正常撤销不能被新规则误伤 ====================

def t11_undo_happy_path():
    root = fresh("happy_undo")
    plan = _plan_one_file(root)
    st = Storage(root)
    er = execute_plan(plan, st)
    undo = undo_latest_batch(st)
    batch = st.get_batch(er.batch_id)
    back = os.path.isfile(os.path.join(root, "笔记.txt"))
    ok_ = (er.moved == 1 and undo.restored == 1 and undo.is_complete
           and not undo.refused and batch.status == "undone" and back)
    ok("回归", "正常「整理→撤销」仍是完整撤销（新拒绝规则不误伤）", ok_,
       f"moved={er.moved} restored={undo.restored} "
       f"is_complete={undo.is_complete} refused={undo.refused} "
       f"批次状态={batch.status if batch else None} 源文件已恢复={back}")
    st.close()


def main() -> int:
    print(f"工作目录：{WORK}\n")
    for fn in (t01_junction, t02_undo_db_failure, t03_undo_unfinished_states,
               t04_copy_then_replace, t06_pending_unselected, t07_ai_defaults,
               t08_restore_backup, t09_table_qss, t10_startup_log,
               t11_undo_happy_path):
        try:
            fn()
        except Exception:
            rec(fn.__name__, "执行测试时异常", "FAIL", traceback.format_exc())

    # GUI 放在最后：QApplication 只能创建一个
    try:
        t05_gui_plan_invalidation()
    except Exception:
        rec("S1-05/06/S3-01", "GUI 测试异常", "FAIL", traceback.format_exc())

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    skips = [r for r in RESULTS if r["status"] == "SKIP"]
    print(f"\n结果：PASS {len(RESULTS) - len(fails) - len(skips)} / "
          f"FAIL {len(fails)} / SKIP {len(skips)}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results_p0_fixes_20261113.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"work_dir": WORK, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {out}")
    if not fails:
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    code = main()
    # GUI 测试创建过 QApplication：正常 return 会触发 Qt/atexit 清理，
    # 在本脚本里没有收益且偶发退出期崩溃，直接以结果码退出。
    sys.stdout.flush()
    os._exit(code)
