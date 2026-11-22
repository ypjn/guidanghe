"""打包后的产品验收入口。

由「归档盒.app」的可执行文件调用，不依赖源码目录或开发虚拟环境：

  归档盒.app/Contents/MacOS/archivebox --product-acceptance <命令> <工作目录> [参数]

命令：
  imports          确认阶段五、阶段六和可选文字识别模块能加载
  organize         扫描、预览计划、确认执行、写入历史
  reopen-undo      重新打开同一空间，按历史撤销并核对 SHA-256
  learning-agent   确认学习建议、复用到后续预览；管家给出方案但不移动文件
  upgrade-seed     写入旧方案、偏好、最近空间和一次可撤销历史
  upgrade-check    重新打开上述数据，确认没有被清空，并完成撤销
  smoke-gui        创建主窗口后退出，返回应用进程退出码
  show-and-quit    显示主窗口若干秒后退出，供原生截图
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("用法：--product-acceptance <命令> <工作目录> [参数]", file=sys.stderr)
        return 2
    command = argv[2]
    workdir = os.path.abspath(argv[3])
    os.makedirs(workdir, exist_ok=True)
    _isolate(workdir)
    extra = argv[4:]
    report = Report(command)
    try:
        handlers = {
            "imports": cmd_imports,
            "organize": cmd_organize,
            "reopen-undo": cmd_reopen_undo,
            "learning-agent": cmd_learning_agent,
            "upgrade-seed": cmd_upgrade_seed,
            "upgrade-check": cmd_upgrade_check,
            "smoke-gui": cmd_smoke_gui,
            "show-and-quit": cmd_show_and_quit,
        }
        handler = handlers.get(command)
        if handler is None:
            report.check("命令存在", False, f"未知命令：{command}")
        else:
            _check_log_outside_bundle(report)
            handler(report, workdir, extra)
    except Exception:
        report.check("命令未抛出异常", False, traceback.format_exc())
    path = report.write(workdir)
    print(f"报告：{path}")
    return 0 if report.ok() else 1


class Report:
    def __init__(self, command: str):
        self.command = command
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({
            "name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if detail:
            print(detail)

    def ok(self) -> bool:
        return bool(self.checks) and all(item["ok"] for item in self.checks)

    def write(self, workdir: str) -> str:
        path = os.path.join(workdir, f"{self.command}.json")
        payload = {
            "command": self.command,
            "ok": self.ok(),
            "executable": os.path.abspath(sys.executable),
            "frozen": bool(getattr(sys, "frozen", False)),
            "checks": self.checks,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path


def _isolate(workdir: str) -> None:
    app_dir = os.environ.get("ARCHIVEBOX_APP_DIR", "").strip()
    if not app_dir:
        app_dir = os.path.join(workdir, "appdata")
        os.environ["ARCHIVEBOX_APP_DIR"] = app_dir
    os.makedirs(app_dir, exist_ok=True)
    if not os.environ.get("ARCHIVEBOX_LOG_DIR", "").strip():
        os.environ["ARCHIVEBOX_LOG_DIR"] = os.path.join(workdir, "logs")
    os.makedirs(os.environ["ARCHIVEBOX_LOG_DIR"], exist_ok=True)


def _bundle_root() -> str:
    forced = os.environ.get("ARCHIVEBOX_PORTABLE_ROOT", "").strip()
    if forced and os.path.isdir(forced):
        return os.path.abspath(forced)
    exe = os.path.realpath(sys.executable)
    parts = exe.split(os.sep)
    for index, part in enumerate(parts):
        if part.endswith(".app"):
            return os.sep.join(parts[:index + 1])
    # Windows 免安装：runtime\pythonw.exe → 上一级为包根
    runtime_dir = os.path.dirname(exe)
    if os.path.basename(runtime_dir).casefold() == "runtime":
        root = os.path.dirname(runtime_dir)
        if os.path.isdir(os.path.join(root, "app")):
            return root
    return ""


def _check_log_outside_bundle(report: Report) -> None:
    import main as main_mod
    path = main_mod.startup_log_path()
    bundle = _bundle_root()
    outside = True if not bundle else not _within(path, bundle)
    report.check(
        "启动日志不在应用包内", outside,
        f"日志={path}\n应用包={bundle or '（源码运行）'}")


def cmd_imports(report: Report, workdir: str, extra: list[str]) -> None:
    del workdir, extra
    for name in (
        "learning", "learning_dialog", "agent", "agent_dialog",
        "ocr_engine", "app_metadata", "preferences", "content_extract",
    ):
        try:
            __import__(name)
        except Exception as exc:
            report.check(f"加载 {name}", False, repr(exc))
        else:
            report.check(f"加载 {name}", True)


def cmd_organize(report: Report, workdir: str, extra: list[str]) -> None:
    del extra
    from executor import execute_plan
    from planner import (
        DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan,
    )
    from scanner import scan_directory
    from storage import Storage

    space = os.path.join(workdir, "space")
    os.makedirs(space, exist_ok=True)
    rel = "高数作业.txt"
    source = os.path.join(space, rel)
    text = "高等数学 极限 作业\n"
    with open(source, "w", encoding="utf-8") as handle:
        handle.write(text)
    digest = _sha256(source)
    scan = scan_directory(space)
    plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
    item = _item_for(plan, rel)
    report.check("预览包含样本", item is not None, f"计划项={len(plan.items)}")
    if item is None:
        return
    for entry in plan.items:
        entry.selected = entry.file.rel_path == rel
    storage = Storage(space)
    try:
        executed = execute_plan(plan, storage)
        batches = storage.list_batches()
    finally:
        storage.close()
    target = os.path.join(space, item.target_rel_path)
    report.check(
        "确认后完成移动",
        executed.moved == 1 and not os.path.exists(source) and os.path.isfile(target),
        f"moved={executed.moved} errors={executed.errors}")
    report.check(
        "历史记下批次与校验值",
        bool(batches) and _sha256(target) == digest,
        f"批次={[batch.batch_id for batch in batches]} sha={digest}")
    _write_json(os.path.join(workdir, "organize-state.json"), {
        "space": space,
        "rel": rel,
        "target_rel": item.target_rel_path,
        "sha256": digest,
        "batch_id": executed.batch_id,
        "count": 1,
    })


def cmd_reopen_undo(report: Report, workdir: str, extra: list[str]) -> None:
    del extra
    from executor import undo_latest_batch
    from storage import Storage

    state = _read_json(os.path.join(workdir, "organize-state.json"))
    space = state["space"]
    source = os.path.join(space, state["rel"])
    target = os.path.join(space, state["target_rel"])
    storage = Storage(space)
    try:
        batches = storage.list_batches()
        found = any(batch.batch_id == state["batch_id"] for batch in batches)
        report.check("重新打开后历史仍在", found,
                     f"期望批次={state['batch_id']} 实际={[b.batch_id for b in batches]}")
        undone = undo_latest_batch(storage)
    finally:
        storage.close()
    restored = (undone.restored == 1 and os.path.isfile(source)
                and _sha256(source) == state["sha256"]
                and not os.path.exists(target))
    report.check(
        "撤销后路径和 SHA-256 恢复", restored,
        f"restored={undone.restored} errors={undone.errors_list}")


def cmd_learning_agent(report: Report, workdir: str, extra: list[str]) -> None:
    del extra
    import agent
    import learning
    from classification_config import default_config
    from planner import (
        DEFAULT_COURSE_RULES, MODE_BY_DIRECTION_SEMESTER, generate_plan,
    )
    from preferences import (
        apply_preferences_to_plan, effective_preferences_resilient,
    )
    from scanner import scan_directory

    learn = os.path.join(workdir, "learn")
    _write(learn, "inbox/周报-一.txt", "第一份周报\n")
    _write(learn, "inbox/周报-二.txt", "第二份周报\n")
    _write(learn, "inbox/周报-三.txt", "第三份周报\n")
    before_plan = _planned_target(learn, "inbox/周报-三.txt", scan_directory,
                                  generate_plan, DEFAULT_COURSE_RULES,
                                  MODE_BY_DIRECTION_SEMESTER, default_config,
                                  effective_preferences_resilient,
                                  apply_preferences_to_plan)
    learning.record_override(
        learn, "inbox/周报-一.txt", "_已整理/待分类", "_已整理/项目/周报",
        "项目", "", "周报")
    learning.record_override(
        learn, "inbox/周报-二.txt", "_已整理/待分类", "_已整理/项目/周报",
        "项目", "", "周报")
    pending = [item for item in learning.build_candidates(learn)
               if item.status == learning.STATUS_PENDING]
    report.check("学习建议待确认", len(pending) == 1,
                 f"待确认={[(item.candidate_id, item.keywords, item.status) for item in pending]}")
    if not pending:
        return
    learning.confirm_candidate(learn, pending[0].candidate_id)
    after_plan = _planned_target(
        learn, "inbox/周报-三.txt", scan_directory, generate_plan,
        DEFAULT_COURSE_RULES, MODE_BY_DIRECTION_SEMESTER, default_config,
        effective_preferences_resilient, apply_preferences_to_plan)
    report.check(
        "确认后的建议被后续预览复用",
        "周报" in after_plan and after_plan != before_plan,
        f"确认前={before_plan}\n确认后={after_plan}")

    agent_root = os.path.join(workdir, "agent")
    for rel in ("大二上/数据结构/实验/报告", "大二上/数据结构/实验/代码",
                "大二上/待整理"):
        os.makedirs(os.path.join(agent_root, rel), exist_ok=True)
    _write(agent_root, "大二上/待整理/数据结构实验一报告.md",
           "数据结构 实验一 链表 实验报告\n")
    _write(agent_root, "大二上/待整理/线性表实验代码.py",
           "def solve():\n    return '链表'\n")
    before = _manifest(agent_root)
    result = agent.run_agent(agent_root, "帮我找这学期的数据结构实验报告")
    after = _manifest(agent_root)
    hit = any(item.file.rel_path.endswith("数据结构实验一报告.md")
              for item in result.hits)
    report.check(
        "管家找到文件并给出方案",
        result.question is None and result.plan is not None and hit
        and result.semester == "大二上",
        f"问题={getattr(result.question, 'text', None)} "
        f"学期={result.semester} 命中={len(result.hits)} "
        f"方案项={0 if result.plan is None else len(result.plan.items)}")
    report.check(
        "未确认方案不移动文件", before == after,
        f"变化={sorted(set(before) ^ set(after))}")
    learn_files = _manifest(learn)
    report.check(
        "学习确认本身不移动样本",
        "inbox/周报-一.txt" in learn_files
        and "inbox/周报-三.txt" in learn_files, "")


def cmd_upgrade_seed(report: Report, workdir: str, extra: list[str]) -> None:
    del extra
    import recent_spaces
    import scheme_store
    from classification_config import default_config
    from executor import execute_plan
    from planner import (
        DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan,
    )
    from preferences import (
        MATCH_FILENAME_KEYWORD, SCOPE_GLOBAL, Preference, save_preference,
    )
    from scanner import scan_directory
    from storage import Storage

    space = os.path.join(workdir, "old-space")
    os.makedirs(space, exist_ok=True)
    sentinel = os.path.join(workdir, "范围外哨兵.txt")
    with open(sentinel, "w", encoding="utf-8") as handle:
        handle.write("不要动我\n")
    rel = "线代作业.txt"
    _write(space, rel, "线性代数 矩阵 作业\n")
    digest = _sha256(os.path.join(space, rel))
    scheme = scheme_store.save_scheme("旧方案", "升级前就存在", default_config())
    save_preference(space, Preference(
        preference_id="pref-old-marker",
        name="旧全局偏好",
        match_type=MATCH_FILENAME_KEYWORD,
        keywords=["旧偏好"],
        direction="项目",
        category="旧偏好",
        scope=SCOPE_GLOBAL,
        source_kind="user_confirmed",
    ))
    recent_spaces.touch(space, "旧空间")
    scan = scan_directory(space)
    plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
    item = _item_for(plan, rel)
    report.check("旧样本进入预览", item is not None, "")
    if item is None:
        return
    for entry in plan.items:
        entry.selected = entry.file.rel_path == rel
    storage = Storage(space)
    try:
        executed = execute_plan(plan, storage)
    finally:
        storage.close()
    report.check("旧历史已执行", executed.moved == 1, f"errors={executed.errors}")
    _write_json(os.path.join(workdir, "upgrade-state.json"), {
        "space": space,
        "rel": rel,
        "target_rel": item.target_rel_path,
        "sha256": digest,
        "batch_id": executed.batch_id,
        "scheme_path": scheme.path,
        "scheme_sha256": _sha256(scheme.path),
        "recent_path": recent_spaces.store_path(),
        "recent_sha256": _sha256(recent_spaces.store_path()),
        "sentinel": sentinel,
        "sentinel_sha256": _sha256(sentinel),
        "preference_name": "旧全局偏好",
    })


def cmd_upgrade_check(report: Report, workdir: str, extra: list[str]) -> None:
    del extra
    import recent_spaces
    import scheme_store
    from executor import undo_latest_batch
    from preferences import list_global_preferences
    from storage import Storage

    state = _read_json(os.path.join(workdir, "upgrade-state.json"))
    names = [item.name for item in scheme_store.list_schemes() if not item.builtin]
    prefs = [item.name for item in list_global_preferences()]
    recent = [item.path for item in recent_spaces.list_spaces()]
    report.check("旧方案还在且文件未被重写",
                 names == ["旧方案"]
                 and _sha256(state["scheme_path"]) == state["scheme_sha256"],
                 f"方案={names}")
    report.check("旧全局偏好还在", state["preference_name"] in prefs, f"偏好={prefs}")
    report.check("最近空间还在且文件未被重写",
                 os.path.realpath(state["space"]) in recent
                 and _sha256(state["recent_path"]) == state["recent_sha256"],
                 f"最近={recent}")
    storage = Storage(state["space"])
    try:
        batches = storage.list_batches()
        found = any(batch.batch_id == state["batch_id"] for batch in batches)
        undone = undo_latest_batch(storage)
    finally:
        storage.close()
    source = os.path.join(state["space"], state["rel"])
    report.check("旧历史可打开并撤销",
                 found and undone.restored == 1
                 and os.path.isfile(source)
                 and _sha256(source) == state["sha256"],
                 f"found={found} restored={undone.restored} errors={undone.errors_list}")
    report.check("范围外哨兵未变",
                 _sha256(state["sentinel"]) == state["sentinel_sha256"], "")


def cmd_smoke_gui(report: Report, workdir: str, extra: list[str]) -> None:
    del workdir, extra
    _exercise_window(report, show_seconds=0)


def cmd_show_and_quit(report: Report, workdir: str, extra: list[str]) -> None:
    del workdir
    seconds = float(extra[0]) if extra else 5.0
    _exercise_window(report, show_seconds=seconds)


def _exercise_window(report: Report, show_seconds: float) -> None:
    if show_seconds > 0:
        # 真显示窗口：不要强制平台插件，留给系统默认（Windows/macOS）。
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main as main_mod

    app = QApplication.instance() or QApplication([])
    window = main_mod.MainWindow()
    window.show()
    app.processEvents()
    title = window.windowTitle()
    report.check("主窗口标题带当前版本", "V1.8" in title, f"标题={title}")
    visible = window.isVisible()
    report.check("主窗口已创建", visible, f"visible={visible}")
    if show_seconds > 0:
        QTimer.singleShot(int(show_seconds * 1000), app.quit)
        code = app.exec()
        report.check("原生窗口事件循环退出", code == 0, f"exec={code}")
    else:
        window.close()
        app.processEvents()
        report.check("离屏窗口可以关闭", True, "")
    main_mod.append_startup_log("product-acceptance-window")
    main_mod.shutdown_widgets()


def _planned_target(root, rel, scan_directory, generate_plan, rules, mode,
                    default_config, effective, apply):
    scan = scan_directory(root)
    plan = generate_plan(
        scan, mode, rules, config=default_config())
    prefs, _note = effective(root)
    apply(plan, prefs, root)
    item = _item_for(plan, rel)
    return "" if item is None else item.target_dir_rel


def _item_for(plan, rel: str):
    for item in plan.items:
        if item.file.rel_path.replace("\\", "/") == rel:
            return item
    return None


def _write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _manifest(root: str) -> dict:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".organizer"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            found[rel] = _sha256(path)
    return found


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"状态文件不是对象：{path}")
    return data


def _within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([
            os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        return False
