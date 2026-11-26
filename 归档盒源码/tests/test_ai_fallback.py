"""规则没定下的文件才问 AI，回答必须落在现有分类里。"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="archivebox_ai_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from ai_fallback import (  # noqa: E402
    DEEPSEEK_BASE, DEEPSEEK_MODEL, AiSettings, AiStop,
    allowed_pairs,
    apply_ai_fallback, load_settings, needs_ai, parse_choice, save_settings,
)
from ai_settings_dialog import AiSettingsDialog  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE, OrganizePlan, PlanItem,
)
from scanner import FileEntry  # noqa: E402


def _item(name, direction, category, rule_key, ocr_text=None, ocr_error=""):
    entry = FileEntry(
        abs_path="/tmp/" + name, rel_path=name, filename=name,
        file_type="图片" if name.endswith(".png") else "文档",
        size=12, size_display="12 B",
    )
    item = PlanItem(
        file=entry, course=category or direction, file_type=entry.file_type,
        rule_applied=rule_key, target_dir_rel="_已整理/待定",
        target_filename=name, target_rel_path="_已整理/待定/" + name,
        direction=direction, category=category, rule_key=rule_key,
        ocr_text=ocr_text, ocr_error=ocr_error,
    )
    return item


def main() -> None:
    fresh = load_settings()
    assert not fresh.enabled and not fresh.ready()
    assert fresh.api_key == ""
    assert fresh.base_url == DEEPSEEK_BASE
    assert fresh.model == DEEPSEEK_MODEL

    loose = _item("shot.png", "待定", "", "fallback", ocr_error="识别失败")
    named = _item("高数作业.pdf", "课程", "高等数学", "keyword_unmatched:1:高数")
    assert needs_ai(loose, MODE_BY_DIRECTION_SEMESTER)
    assert not needs_ai(named, MODE_BY_DIRECTION_SEMESTER)
    failed = _item("笔记.png", "课程", "高等数学", "keyword_unmatched:1:高数",
                   ocr_error="识别失败")
    assert needs_ai(failed, MODE_BY_DIRECTION_SEMESTER)
    assert not needs_ai(loose, MODE_BY_TYPE)

    pairs = allowed_pairs()
    allowed = set(pairs)
    assert ("课程", "高等数学") in allowed
    assert parse_choice(
        "```json\n{\"direction\":\"课程\",\"category\":\"高等数学\"}\n```",
        allowed,
    ) == ("课程", "高等数学")
    assert parse_choice('{"direction":"","category":""}', allowed) is None
    assert parse_choice(
        '{"direction":"课程","category":"不存在的课"}', allowed) is None

    calls = []

    def transport(_settings, messages):
        calls.append(messages)
        text = messages[-1]["content"]
        if isinstance(text, list):
            text = text[0]["text"]
        if "shot.png" in text:
            return '{"direction":"课程","category":"高等数学"}'
        return '{"direction":"","category":""}'

    kept = _item("大二上/数据结构/作业/a.png", "课程", "数据结构", "semester_dir:x")
    kept.target_dir_rel = "_已整理/课程/大二上/数据结构/图片"
    plan = OrganizePlan(
        root="/tmp", mode=MODE_BY_DIRECTION_SEMESTER, rules=[],
        items=[loose, named, kept],
    )
    note = apply_ai_fallback(
        plan, AiSettings(api_key="test-key", enabled=True),
        transport=transport,
    )
    assert len(calls) == 1, len(calls)
    assert loose.category == "高等数学", loose.rule_applied
    assert loose.direction == "课程"
    assert "图片识别失败" in loose.rule_applied
    assert "高等数学" in loose.target_dir_rel
    assert named.category == "高等数学"
    assert named.rule_key.startswith("keyword_unmatched")
    assert kept.category == "数据结构"
    assert "补充分类 1 个" in note

    def down(_settings, _messages):
        raise AiStop("连不上 AI 接口。")

    other = _item("b.png", "待定", "", "fallback")
    plan2 = OrganizePlan(
        root="/tmp", mode=MODE_BY_DIRECTION_SEMESTER, rules=[], items=[other])
    note2 = apply_ai_fallback(
        plan2, AiSettings(api_key="test-key", enabled=True),
        transport=down,
    )
    assert other.direction == "待定"
    assert "连不上" in note2

    save_settings(AiSettings(
        provider_id="custom",
        base_url="http://127.0.0.1:9/v1",
        model="other",
        api_key="secret",
        enabled=True,
        setup_dismissed=True,
    ))
    loaded = load_settings()
    assert loaded.api_key == "secret"
    assert loaded.enabled
    assert loaded.base_url == "http://127.0.0.1:9/v1"
    assert loaded.model == "other"
    assert loaded.provider_id == "custom"
    assert "secret" not in note

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    assert window.ai_check.text() == "待定文件问 AI"
    assert hasattr(window, "btn_ai_settings")
    assert hasattr(window, "btn_ai_header")
    assert hasattr(window, "ai_banner")
    assert "配置 AI" in window.btn_ai_header.text() or "管理 AI" in window.btn_ai_header.text()
    assert window.btn_agent.text() == "总管家"
    assert window.btn_ai_assist.text() == "总管家"
    assert window.btn_ai_assist.objectName() == "aiAssist"
    assert "根目录" in window.btn_ai_assist.toolTip()
    assert window.btn_folder_ai.text() == "AI 管家"
    assert window.btn_folder_ai.parent() is window.classifying_bar
    assert window.classifying_bar.parent() is window.table.parent()
    window.path_edit.setText("/tmp/课程资料")
    window._update_button_states()
    assert window.classifying_name.text() == "课程资料"
    assert window.btn_folder_ai.isEnabled()
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    header_y = window.btn_ai_assist.mapTo(
        window, window.btn_ai_assist.rect().center()).y()
    bar_top = window.classifying_bar.mapTo(
        window, window.classifying_bar.rect().topLeft()).y()
    table_top = window.table.mapTo(
        window, window.table.rect().topLeft()).y()
    folder_x = window.btn_folder_ai.mapTo(
        window, window.btn_folder_ai.rect().center()).x()
    name_x = window.classifying_name.mapTo(
        window, window.classifying_name.rect().center()).x()
    assert header_y < bar_top
    assert bar_top < table_top
    assert folder_x > name_x
    from ai_consult_dialog import AiConsultDialog
    old_scan = AiConsultDialog.start_scan_and_analyze
    AiConsultDialog.start_scan_and_analyze = lambda self: None
    try:
        root_dlg = AiConsultDialog(window, "/tmp/课程资料", role="root")
        folder_dlg = AiConsultDialog(window, "/tmp/课程资料", role="folder")
        assert root_dlg.windowTitle() == "总管家 · 根目录"
        assert folder_dlg.windowTitle() == "AI 管家 · 课程资料"
        assert "总管家" in root_dlg.scope_note
        assert "这个文件夹" in folder_dlg.scope_note
        assert "当前分类预览" not in folder_dlg._goal_for_model()
        embedded = AiConsultDialog(
            window, "/tmp/课程资料", role="folder", embedded=True)
        assert embedded.embedded
        assert not hasattr(embedded, "snapshot_view")
        assert all("总管家" not in text for _role, text in embedded._messages)
        embedded._on_scan_done(
            type("Snap", (), {"text": "摘要", "file_count": 2, "dir_count": 1})(),
            False)
        assert embedded.snapshot.text == "摘要"
        assert embedded.status.text().startswith("扫描完成")
        root_dlg.close()
        folder_dlg.close()
        embedded.close()
        space = tempfile.mkdtemp(prefix="folder_ai_")
        window.path_edit.setText(space)
        window.on_folder_agent()
        deadline = time.time() + 1.2
        while time.time() < deadline:
            app.processEvents()
            if window.main_splitter.sizes()[3] >= 300:
                break
            time.sleep(0.02)
        app.processEvents()
        rail = window._folder_consult_dialog
        assert rail is not None and rail.embedded and not rail.isWindow()
        assert rail.parent() is window.folder_ai_host
        assert window.folder_ai_host.isVisible()
        assert window.main_splitter.sizes()[3] >= 300
        assert window.btn_folder_ai.text() == "收起"
        window._set_inspector_open(True)
        window._set_inspector_open(False)
        assert len(window.main_splitter.sizes()) == 4
        window.on_folder_agent()
        deadline = time.time() + 1.2
        while time.time() < deadline:
            app.processEvents()
            if not window.folder_ai_host.isVisible():
                break
            time.sleep(0.02)
        app.processEvents()
        assert window.btn_folder_ai.text() == "AI 管家"
        assert not window.folder_ai_host.isVisible()
    finally:
        AiConsultDialog.start_scan_and_analyze = old_scan
    dlg = AiSettingsDialog(window._ai_settings, window)
    assert dlg.windowTitle() == "配置 AI 接口"
    dlg.reject()
    window.close()
    del app
    print("ai fallback checks passed")


if __name__ == "__main__":
    main()
