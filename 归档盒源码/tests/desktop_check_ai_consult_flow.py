"""全流程：配置 AI → 整理顾问 → 可勾选预览 → 与旧入口衔接；并截图。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SRC)
sys.path.insert(0, SRC)

# 本机 TEMP 可能不可写，tempfile 会回落到源码 cwd；而源码目录默认受保护。
# 因此样本与 appdir 建在仓库根下的 .flow_tmp（不在 归档盒源码/ 内）。
_FLOW_TMP = os.path.join(REPO, ".flow_tmp")
os.makedirs(_FLOW_TMP, exist_ok=True)
APP_DIR = tempfile.mkdtemp(prefix="archivebox_ai_flow_app_", dir=_FLOW_TMP)
SPACE = tempfile.mkdtemp(prefix="archivebox_ai_flow_space_", dir=_FLOW_TMP)
os.environ["ARCHIVEBOX_APP_DIR"] = APP_DIR
os.environ["ARCHIVEBOX_LOG_DIR"] = os.path.join(APP_DIR, "logs")

EVIDENCE = os.path.join(
    REPO, "docs", "product-development", "evidence", "ai-consult-flow")
shutil.rmtree(EVIDENCE, ignore_errors=True)
os.makedirs(EVIDENCE, exist_ok=True)

REPORT = {"ok": True, "checks": [], "issues": [], "screenshots": []}


def check(name: str, ok: bool, detail: str = ""):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        REPORT["ok"] = False
        REPORT["issues"].append(f"{name}: {detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def issue(text: str):
    REPORT["issues"].append(text)
    print(f"[ISSUE] {text}", flush=True)


def shot(widget, name: str):
    path = os.path.join(EVIDENCE, f"{name}.png")
    ok = bool(widget.grab().save(path)) and os.path.isfile(path)
    REPORT["screenshots"].append(path)
    check(f"截图 {name}", ok, path)
    return path


def wait_until(app, predicate, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(step)
    app.processEvents()
    return bool(predicate())


def build_sample_space():
    os.makedirs(os.path.join(SPACE, "大二上", "数据结构"), exist_ok=True)
    os.makedirs(os.path.join(SPACE, "下载"), exist_ok=True)
    for path, content in {
        os.path.join(SPACE, "杂乱笔记.pdf"): "pdf",
        os.path.join(SPACE, "下载", "截图.png"): "png",
        os.path.join(SPACE, "大二上", "数据结构", "实验一报告.md"): "# report\n",
    }.items():
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)


def main() -> int:
    print("start flow", flush=True)
    build_sample_space()

    from PySide6.QtWidgets import QApplication, QMessageBox
    import main as main_mod
    import ai_consult
    from ai_fallback import AiSettings, save_settings
    from ai_settings_dialog import AiSettingsDialog
    from ai_consult_dialog import AiConsultDialog

    app = QApplication.instance() or QApplication([])
    # 离屏自动化不能点模态框，否则会永久卡住。
    QMessageBox.warning = staticmethod(lambda *args, **kwargs: QMessageBox.Ok)
    QMessageBox.information = staticmethod(lambda *args, **kwargs: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *args, **kwargs: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.Yes)
    QMessageBox.about = staticmethod(lambda *args, **kwargs: None)
    settings = AiSettings(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk-flow-test-key",
        enabled=True,
        setup_dismissed=True,
    )
    save_settings(settings)

    window = main_mod.MainWindow()
    window._ai_settings = settings
    window._refresh_ai_presence()
    window.resize(1280, 820)
    window.show()
    app.processEvents()

    check("主入口是 AI 整理顾问", window.btn_agent.text() == "AI 整理顾问")
    check("标题栏有配置 AI",
          "配置 AI" in window.btn_ai_header.text()
          or "管理 AI" in window.btn_ai_header.text())
    check("AI 状态可见", bool(window.ai_status_label.text()),
          window.ai_status_label.text())
    shot(window, "01-main-with-ai-presence")

    window.path_edit.setText(SPACE)
    window._touch_recent_space(SPACE)
    window._refresh_folder_list()
    window._update_button_states()
    app.processEvents()
    check("顾问按钮可点", window.btn_agent.isEnabled())

    settings_dlg = AiSettingsDialog(window._ai_settings, window)
    settings_dlg.show()
    app.processEvents()
    shot(settings_dlg, "02-ai-settings-dialog")
    settings_dlg.close()

    advice = (
        "1. 现状判断\n根目录有散落文件，下载区较乱。\n"
        "2. 建议目标结构\n- 散落 PDF 归入大二上\n"
        "3. 建议分步动作\n先收拢根目录。\n"
        "4. 需要确认\n项目目录是否保留？"
    )
    plan_json = json.dumps({
        "summary": "收拢根目录与下载区抽样",
        "moves": [
            {
                "file": "杂乱笔记.pdf",
                "target_dir": "_已整理/课程/大二上/高等数学/文档",
                "direction": "课程",
                "semester": "大二上",
                "category": "高等数学",
                "evidence": "根目录散落",
                "selected": True,
            },
            {
                "file": "下载/截图.png",
                "target_dir": "_已整理/课程/大二上/其他/图片",
                "direction": "课程",
                "semester": "大二上",
                "category": "其他",
                "evidence": "下载区图片",
                "selected": True,
            },
        ],
    }, ensure_ascii=False)

    original_start = ai_consult.start_consult_session
    original_continue = ai_consult.continue_consult_session
    original_build = ai_consult.build_preview_plan_from_consult

    def fake_start(settings, snapshot, goal="", transport=None, cancel_event=None):
        return original_start(
            settings, snapshot, goal=goal,
            transport=lambda _s, _m: advice, cancel_event=cancel_event)

    def fake_continue(settings, session, user_text, transport=None, cancel_event=None):
        return original_continue(
            settings, session, user_text,
            transport=lambda _s, _m: advice + "\n本轮：暂不动项目目录。",
            cancel_event=cancel_event)

    def fake_build(settings, snapshot, session, goal="", protect_dirs=None,
                   transport=None, cancel_event=None):
        return original_build(
            settings, snapshot, session, goal=goal, protect_dirs=protect_dirs,
            transport=lambda _s, _m: plan_json, cancel_event=cancel_event)

    ai_consult.start_consult_session = fake_start
    ai_consult.continue_consult_session = fake_continue
    ai_consult.build_preview_plan_from_consult = fake_build

    # 跳过构造时的自动启动，避免竞态；稍后再手动启动。
    _orig_auto = AiConsultDialog.start_scan_and_analyze
    AiConsultDialog.start_scan_and_analyze = lambda self: None
    consult = AiConsultDialog(window, SPACE, parent=window)
    AiConsultDialog.start_scan_and_analyze = _orig_auto
    consult.show()
    app.processEvents()
    consult.start_scan_and_analyze()

    ok = wait_until(app, lambda: consult.session is not None and not consult._busy)
    check("顾问完成首轮建议", ok,
          f"session={consult.session is not None} busy={consult._busy} "
          f"status={consult.status.text()}")
    check("结构摘要非空", bool(consult.snapshot_view.toPlainText().strip()))
    check("可生成预览按钮可用", consult.btn_make_plan.isEnabled())
    shot(consult, "03-consult-after-advice")

    consult.input_edit.setText("项目目录不要动")
    consult.send_message()
    ok = wait_until(app, lambda: not consult._busy)
    check("多轮调整完成", ok and consult.session is not None)
    shot(consult, "04-consult-after-followup")

    consult.generate_preview_plan()
    ok = wait_until(
        app, lambda: window.current_plan is not None and window._plan_valid)
    check("主界面已有顾问预览方案", ok)
    plan = window.current_plan
    if plan is not None:
        selected = sum(1 for item in plan.items if item.selected)
        check("预览项可勾选且默认勾选", selected >= 1,
              f"selected={selected} total={len(plan.items)}")
        check("方案带 AI 顾问标记",
              any("AI 顾问" in (item.rule_applied or "") for item in plan.items))
        check("开始整理可点", window.btn_execute.isEnabled(),
              f"enabled={window.btn_execute.isEnabled()} "
              f"storage={window._storage is not None}")
        check("扫描结果已衔接", window.scan_result is not None)
        check("可重新生成预览", window.btn_regenerate.isEnabled())
    shot(window, "05-main-preview-from-consult")

    window.on_agent_quick()
    app.processEvents()
    check("旧 AI 管家入口仍可打开", window._agent_dialog is not None)
    if window._agent_dialog is not None:
        shot(window._agent_dialog, "06-legacy-agent-dialog")
        window._agent_dialog.close()
        app.processEvents()

    if plan is not None and plan.items:
        plan.items[0].selected = False
        window._populate_table(plan, reset_filters=False)
        window._update_plan_counts()
        window._update_button_states()
        app.processEvents()
        still = sum(1 for item in plan.items if item.selected)
        check("取消勾选后仍按剩余项控制开始整理",
              window.btn_execute.isEnabled() == (still > 0),
              f"still={still} execute={window.btn_execute.isEnabled()}")
        shot(window, "07-main-after-uncheck")

    if (window.btn_ai_settings.property("role") == "primary"
            and window.btn_agent.property("role") == "primary"):
        issue("侧栏同时两个 primary（配置 AI / 整理顾问）")

    ai_consult.start_consult_session = original_start
    ai_consult.continue_consult_session = original_continue
    ai_consult.build_preview_plan_from_consult = original_build

    consult._closing = True
    consult.close()
    window.close()
    app.processEvents()

    report_path = os.path.join(EVIDENCE, "flow_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(REPORT, handle, ensure_ascii=False, indent=2)
    print("report:", report_path, flush=True)
    print("issues:", len(REPORT["issues"]), flush=True)
    return 0 if REPORT["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
    finally:
        shutil.rmtree(SPACE, ignore_errors=True)
        shutil.rmtree(APP_DIR, ignore_errors=True)
