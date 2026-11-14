"""阶段 1 · 智能整理 MVP 的合成样本自测。

所有样本都由本脚本在临时目录生成，不使用真实资料。默认不联网：
AI 相关用例全部使用 mock transport。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="stage1_app_")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = tempfile.mkdtemp(prefix="stage1_scheme_")
os.environ["ARCHIVEBOX_RECENT_FILE"] = tempfile.mktemp(prefix="stage1_recent_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import ai_fallback  # noqa: E402
from ai_fallback import AiSettings, apply_ai_fallback  # noqa: E402
from classification_config import (  # noqa: E402
    MATCH_DIR_SCOPE, LongTermRule, default_config,
)
from content_extract import extract_contents  # noqa: E402
from planner import (  # noqa: E402
    MODE_BY_DIRECTION_SEMESTER, generate_plan,
)
from scanner import scan_directory  # noqa: E402
from storage import Storage  # noqa: E402
from executor import execute_plan, undo_latest_batch  # noqa: E402

_APP = None
_MAIN = None


def app():
    global _APP, _MAIN
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    if _MAIN is None:
        import main as main_module
        _MAIN = main_module
    return _APP


def root_dir() -> str:
    return tempfile.mkdtemp(prefix="stage1_sample_")


def write(root: str, rel: str, data, binary: bool = False) -> str:
    full = os.path.join(root, rel)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(full, mode, **kwargs) as handle:
        handle.write(data)
    return full


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_root(root: str):
    return scan_directory(root, detect_project_roots=False)


def build(root: str, config=None):
    scan = scan_root(root)
    extraction = extract_contents(scan.files)
    plan = generate_plan(
        scan, MODE_BY_DIRECTION_SEMESTER, [], config=config or default_config(),
        content_texts=extraction.texts, content_errors=extraction.errors,
    )
    return scan, extraction, plan


def item_by(plan, rel):
    for item in plan.items:
        if item.file.rel_path == rel:
            return item
    raise AssertionError(f"plan 中找不到 {rel}: {[i.file.rel_path for i in plan.items]}")


def make_window(plan, root):
    main = _MAIN
    window = main.MainWindow()
    window.current_plan = plan
    window._plan_valid = True
    window._current_plan_seq = 1
    window._plan_identity = None
    window._plan_revision = 0
    window._confirmed_revision = 0
    window._storage = Storage(root)
    window._populate_table(plan)
    window._update_plan_counts()
    window._refresh_ambiguity_panel()
    return window


# ======================== 各项检查 ========================

def check_content_extraction():
    root = root_dir()
    long_text = "长" * 3000
    write(root, "下载/长.md", long_text)
    write(root, "下载/pdf.pdf", b"%PDF-1.4 fake", binary=True)
    write(root, "下载/坏.md", b"\xff\xfe\x00abc", binary=True)
    write(root, "下载/截图.png", b"\x89PNG\r\n\x1a\n", binary=True)
    write(root, "下载/无正文.md", "没有关键词的说明")

    scan = scan_root(root)
    extraction = extract_contents(scan.files)
    long_text_got = extraction.texts.get("下载/长.md", "")
    assert long_text_got, "长文本没有读到"
    assert len(long_text_got) < 2000, len(long_text_got)
    assert "4096" in extraction.errors.get("下载/长.md", ""), extraction.errors

    pdf_error = extraction.errors.get("下载/pdf.pdf", "")
    assert "未支持" in pdf_error and "PDF" in pdf_error, pdf_error
    assert "下载/pdf.pdf" not in extraction.texts

    broken = extraction.texts.get("下载/坏.md", "")
    assert "\ufffd" in broken, repr(broken)
    assert "UTF-8" in extraction.errors.get("下载/坏.md", ""), extraction.errors

    assert extraction.kinds.get("下载/截图.png") == "图片", extraction.kinds
    assert "下载/截图.png" not in extraction.errors
    assert extraction.texts.get("下载/无正文.md") == "没有关键词的说明"

    cancel = threading.Event()
    cancel.set()
    stopped = extract_contents(scan_root(root).files, cancel_event=cancel)
    assert stopped.cancelled
    assert not stopped.texts and not stopped.errors

    unreadable = write(root, "下载/无权限.md", "打不开")
    os.chmod(unreadable, 0)
    from scanner import FileEntry
    blocked = FileEntry(
        abs_path=unreadable, rel_path="下载/无权限.md", filename="无权限.md",
        file_type="文档", size=3, size_display="3 B")
    try:
        got = extract_contents([blocked])
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            assert "无法读取正文" in got.errors.get("下载/无权限.md", ""), got.errors
    finally:
        os.chmod(unreadable, 0o600)


def check_body_distinguishes_same_names():
    root = root_dir()
    # 同一批样本同时包含课程资料、项目文档和普通下载。
    write(root, "大二上/高等数学/课程资料.md", "第三章习题与课堂笔记")
    write(root, "项目/AIGC/项目说明.md", "项目会议记录与排期")
    write(root, "下载/笔记.md", "高等数学 极限 导数 期末复习")
    write(root, "下载/笔记.txt", "AIGC 生成式人工智能 模型训练")
    _scan, _extraction, plan = build(root)
    first = item_by(plan, "下载/笔记.md")
    second = item_by(plan, "下载/笔记.txt")
    course = item_by(plan, "大二上/高等数学/课程资料.md")
    project = item_by(plan, "项目/AIGC/项目说明.md")
    assert course.direction == "课程", course.rule_applied
    assert project.direction == "项目", project.rule_applied
    assert first.target_dir_rel != second.target_dir_rel, (
        first.target_dir_rel, second.target_dir_rel)
    assert first.category == "高等数学", first.rule_applied
    assert second.category == "AIGC（生成式人工智能）", second.rule_applied
    assert "正文" in first.rule_applied, first.rule_applied
    assert "正文" in second.rule_applied, second.rule_applied
    assert first.selected and second.selected
    assert first.content_source == "正文", first.content_source
    assert second.content_source == "正文", second.content_source
    assert not plan.ai_note and not plan.ai_rejections
    # 联网关闭：没有任何 AI 结果，分类完全来自本地规则 + 正文。


def check_path_evidence():
    root = root_dir()
    body = "同样的中性说明文字，不含课程关键词。"
    write(root, "大二上/高等数学/说明.md", body)
    write(root, "项目/AIGC/说明.md", body)
    _scan, _extraction, plan = build(root)
    course = item_by(plan, "大二上/高等数学/说明.md")
    project = item_by(plan, "项目/AIGC/说明.md")
    assert course.target_dir_rel != project.target_dir_rel, (
        course.target_dir_rel, project.target_dir_rel)
    assert course.direction == "课程", course.rule_applied
    assert project.direction == "项目", project.rule_applied
    assert "相对路径" in course.rule_applied, course.rule_applied
    assert "课程目录" in course.rule_applied, course.rule_applied
    assert "相对路径" in project.rule_applied, project.rule_applied
    assert "项目目录" in project.rule_applied, project.rule_applied
    assert not course.ambiguity and not project.ambiguity


def check_ambiguity_resolution_requires_reconfirm():
    app()
    root = root_dir()
    write(root, "项目/AIGC/混合.md", "高等数学 极限 导数")
    source_path = os.path.join(root, "项目/AIGC/混合.md")
    before_sha = sha256_file(source_path)
    _scan, _extraction, plan = build(root)
    item = item_by(plan, "项目/AIGC/混合.md")
    assert item.ambiguity is not None, item.rule_applied
    assert not item.selected, "未确认的歧义默认不能勾选"
    old_target = item.target_dir_rel
    assert old_target != item.ambiguity["candidates"][1]["target_dir_rel"]

    window = make_window(plan, root)
    try:
        assert not window.ambiguity_panel.isHidden()
        assert "项目/AIGC/混合.md" in window.ambiguity_label.text()
        assert window._confirmed_revision == 0
        ok = window.apply_ambiguity_choice("项目/AIGC/混合.md", 1)
        assert ok
        assert item.direction == "课程" and item.category == "高等数学", (
            item.direction, item.category)
        assert item.target_dir_rel != old_target
        assert "高等数学" in item.target_dir_rel
        assert "用户改选" in item.rule_applied, item.rule_applied
        assert "正文" in item.rule_applied, item.rule_applied
        assert item.ambiguity is None and item.selected
        assert window._confirmed_revision is None, "改选后旧确认必须作废"
        assert window._plan_revision >= 1

        # 必须重新确认：这里让确认框返回取消，不应移动任何文件。
        calls = []

        class FakeConfirm:
            def __init__(self, *args, **kwargs):
                calls.append(args)

            def exec(self):
                return QDialog.Rejected

        real = _MAIN.ExecuteConfirmDialog
        _MAIN.ExecuteConfirmDialog = FakeConfirm
        try:
            window.on_execute()
        finally:
            _MAIN.ExecuteConfirmDialog = real
        assert len(calls) == 1, calls
        assert os.path.exists(source_path)
        assert sha256_file(source_path) == before_sha
        assert not os.path.exists(os.path.join(root, item.target_rel_path))
    finally:
        window._storage.close()
        window.close()

    # 多个正文候选都成立、分不出先后 → 同样默认不勾选。
    root2 = root_dir()
    write(root2, "下载/混合.md", "高等数学 AIGC")
    _scan2, _extraction2, plan2 = build(root2)
    tied = item_by(plan2, "下载/混合.md")
    assert tied.ambiguity is not None, tied.rule_applied
    assert len(tied.ambiguity["candidates"]) >= 2
    assert not tied.selected


def check_skip_keeps_source():
    app()
    root = root_dir()
    source = write(root, "项目/AIGC/跳过.md", "高等数学 极限")
    before = sha256_file(source)
    _scan, _extraction, plan = build(root)
    item = item_by(plan, "项目/AIGC/跳过.md")
    assert item.ambiguity is not None
    target_before = item.target_rel_path

    window = make_window(plan, root)
    try:
        assert window.skip_ambiguity("项目/AIGC/跳过.md")
        assert item.selected is False
        assert item.skipped is True
        assert item.ambiguity is None
        assert "用户跳过" in item.rule_applied, item.rule_applied
        assert os.path.exists(source)
        assert sha256_file(source) == before
        assert not os.path.exists(os.path.join(root, target_before))
        assert "跳过" in window.impact_label.text()
        assert window._confirmed_revision is None
    finally:
        window._storage.close()
        window.close()


def check_invalid_ai_responses():
    root = root_dir()
    source = write(root, "下载/未知.md", "这是一份没有关键词的说明")
    before = sha256_file(source)
    _scan, _extraction, plan = build(root)
    item = item_by(plan, "下载/未知.md")
    original_target = item.target_dir_rel
    original_selected = item.selected
    assert not original_selected

    settings = AiSettings(api_key="test-key", enabled=True)
    rel = "下载/未知.md"
    responses = {
        "非法 JSON": "这不是 JSON",
        "清单外类别": ('{"file":"%s","direction":"课程","category":"不存在的课",'
                   '"action":"move","evidence":"正文提到课程"}' % rel),
        "绝对路径": ('{"file":"%s","direction":"课程","category":"高等数学",'
                 '"action":"move","evidence":"正文提到高等数学",'
                 '"target":"/tmp/外面"}' % rel),
        "越界路径": ('{"file":"%s","direction":"课程","category":"高等数学",'
                 '"action":"move","evidence":"正文提到高等数学",'
                 '"target":"../../外面"}' % rel),
        "清单外目标": ('{"file":"%s","direction":"课程","category":"高等数学",'
                  '"action":"move","evidence":"正文提到高等数学",'
                  '"target":"_已整理/项目/别的"}' % rel),
        "空依据": ('{"file":"%s","direction":"课程","category":"高等数学",'
                '"action":"move","evidence":""}' % rel),
        "文件不属于本次计划": ('{"file":"别处.md","direction":"课程",'
                       '"category":"高等数学","action":"move",'
                       '"evidence":"正文提到高等数学"}'),
    }
    for label, response in responses.items():
        ai_fallback._result_cache.clear()
        ai_fallback._result_cache_order.clear()
        plan.ai_rejections.clear()
        item.target_dir_rel = original_target
        item.selected = original_selected
        item.rule_key = "fallback"
        note = apply_ai_fallback(
            plan, settings, transport=lambda _s, _m, text=response: text,
            require_evidence=True)
        assert plan.ai_rejections, label
        assert "未通过校验" in note, (label, note)
        assert item.target_dir_rel == original_target, (label, item.target_dir_rel)
        assert item.selected is False, label
        assert os.path.exists(source) and sha256_file(source) == before, label
        assert not os.path.exists(os.path.join(root, original_target))

    # 合法建议：方向/类别在清单内、依据非空、目标由现有路径函数生成。
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    plan.ai_rejections.clear()
    item.target_dir_rel = original_target
    item.selected = original_selected
    valid = ('{"file":"%s","direction":"课程","category":"高等数学",'
             '"action":"move","evidence":"正文提到高等数学复习"}' % rel)
    note = apply_ai_fallback(
        plan, settings,
        transport=lambda _s, _m, text=valid: text,
        require_evidence=True)
    assert "AI 补充分类 1 个" in note, note
    assert item.direction == "课程" and item.category == "高等数学"
    assert item.selected is True
    assert "依据：" in item.rule_applied, item.rule_applied
    assert os.path.exists(source) and sha256_file(source) == before


def check_stale_results_discarded():
    app()
    root = root_dir()
    write(root, "下载/说明.md", "高等数学")
    source_path = os.path.join(root, "下载/说明.md")
    before = sha256_file(source_path)
    _scan, _extraction, plan = build(root)

    window = _MAIN.MainWindow()
    try:
        window.current_plan = None
        window._plan_valid = False
        window._current_plan_seq = 2
        window._on_plan_done(1, plan)   # 旧序号，必须丢弃
        assert window.current_plan is None
        assert window._plan_valid is False
        assert window.table.rowCount() == 0
        assert os.path.exists(source_path)
        assert sha256_file(source_path) == before
    finally:
        window.close()

    # 取消后 PlanWorker 不交付半成品计划。
    class Capture:
        def __init__(self):
            self.events = []

        def ok(self, seq, got):
            self.events.append(("ok", seq))

        def cancelled(self, seq):
            self.events.append(("cancelled", seq))

    capture = Capture()
    worker = _MAIN.PlanWorker(
        7, _scan, MODE_BY_DIRECTION_SEMESTER, [], config=default_config(),
        ocr_enabled=False)
    worker.finished_ok.connect(capture.ok)
    worker.cancelled.connect(capture.cancelled)
    worker.cancel_event.set()
    worker.run()
    assert capture.events == [("cancelled", 7)], capture.events


def check_plan_worker_reads_body():
    root = root_dir()
    write(root, "下载/资料.md", "高等数学 极限")
    scan = scan_root(root)
    captured = []
    worker = _MAIN.PlanWorker(
        3, scan, MODE_BY_DIRECTION_SEMESTER, [], config=default_config(),
        ocr_enabled=False)
    worker.finished_ok.connect(lambda seq, plan: captured.append((seq, plan)))
    worker.run()
    assert captured, "PlanWorker 没有交付计划"
    seq, plan = captured[0]
    assert seq == 3
    item = item_by(plan, "下载/资料.md")
    assert item.content_text == "高等数学 极限"
    assert item.category == "高等数学", item.rule_applied
    assert "正文" in item.rule_applied


def check_full_execute_and_undo():
    app()
    root = root_dir()
    rels = ["下载/笔记.md", "下载/笔记.txt"]
    write(root, rels[0], "高等数学 极限 导数")
    write(root, rels[1], "AIGC 生成式人工智能 模型训练")
    before = {rel: sha256_file(os.path.join(root, rel)) for rel in rels}

    _scan, _extraction, plan = build(root)
    assert all(os.path.exists(os.path.join(root, rel)) for rel in rels)
    for item in plan.items:
        assert not os.path.exists(os.path.join(root, item.target_rel_path))

    window = make_window(plan, root)
    try:
        calls = []

        class FakeConfirm:
            def __init__(self, *args, **kwargs):
                calls.append(args)

            def exec(self):
                return QDialog.Rejected

        real = _MAIN.ExecuteConfirmDialog
        _MAIN.ExecuteConfirmDialog = FakeConfirm
        try:
            window.on_execute()
        finally:
            _MAIN.ExecuteConfirmDialog = real
        assert len(calls) == 1
        for rel in rels:
            assert os.path.exists(os.path.join(root, rel))
            assert sha256_file(os.path.join(root, rel)) == before[rel]
    finally:
        window.close()

    storage = Storage(root)
    try:
        result = execute_plan(plan, storage)
        assert result.moved == 2, (result.moved, result.failed, result.skipped)
        for item in plan.items:
            assert not os.path.exists(os.path.join(root, item.file.rel_path))
            target = os.path.join(root, item.target_rel_path)
            assert os.path.exists(target)
            assert sha256_file(target) == before[item.file.rel_path]
        undo = undo_latest_batch(storage)
        assert undo.restored == 2, undo
        for rel in rels:
            assert os.path.exists(os.path.join(root, rel))
            assert sha256_file(os.path.join(root, rel)) == before[rel]
    finally:
        storage.close()


def check_ai_defaults_off():
    app()
    window = _MAIN.MainWindow()
    try:
        assert window.ai_check.isChecked() is False
        assert window._ai_consent_given is False
        assert "正文片段" in window._ai_consent_text()
        assert "图片识别文字" in window._ai_consent_text()
    finally:
        window.close()


def check_long_term_dir_is_not_body_evidence():
    root = root_dir()
    write(root, "大二上/数据结构/说明.md", "今天的会议只记录了出席名单。")
    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-review-dir", name="数据结构归项目",
        match_type=MATCH_DIR_SCOPE, source_path="大二上/数据结构",
        include_subdirs=True, direction="项目", category="数据大创",
    ))
    _scan, _extraction, plan = build(root, config=cfg)
    item = item_by(plan, "大二上/数据结构/说明.md")
    assert item.target_dir_rel.startswith("_已整理/项目/数据大创"), item.target_dir_rel
    assert item.selected is True, "长期目录规则已决定去向，不应取消勾选"
    assert item.ambiguity is None, item.ambiguity
    assert item.content_source == "路径", item.content_source
    assert "来源目录" in item.rule_applied, item.rule_applied
    assert "正文命中" not in item.rule_applied, item.rule_applied

    # 正文真的指向另一个落点时仍然要提问，两边依据各写各的来源。
    root2 = root_dir()
    write(root2, "大二上/数据结构/说明.md", "高等数学 极限 导数")
    _scan2, _extraction2, plan2 = build(root2, config=cfg)
    item2 = item_by(plan2, "大二上/数据结构/说明.md")
    assert item2.ambiguity is not None, item2.rule_applied
    assert item2.selected is False
    texts = [c["evidence"] for c in item2.ambiguity["candidates"]]
    assert any("来源目录" in t for t in texts), texts
    assert any("正文关键词「高等数学」" in t for t in texts), texts
    assert not any("正文命中长期规则" in t for t in texts), texts


def check_content_source_matches_actual_evidence():
    root = root_dir()
    write(root, "大二上/高等数学/说明.md", "同样的中性说明文字，不含课程关键词。")
    _scan, _extraction, plan = build(root)
    item = item_by(plan, "大二上/高等数学/说明.md")
    assert item.content_source == "路径", item.content_source
    assert "课程目录" in item.rule_applied, item.rule_applied
    app()
    window = make_window(plan, root)
    try:
        window.table.selectRow(0)
        window._update_inspector()
        assert "路径" in window.inspector_evidence.text(), (
            window.inspector_evidence.text())
    finally:
        window._storage.close()
        window.close()


def check_invalid_ai_keeps_local_selection():
    root = root_dir()
    write(root, "大二上/高等数学/板书.png", b"\x89PNG\r\n\x1a\n", binary=True)
    _scan, _extraction, plan = build(root)
    item = item_by(plan, "大二上/高等数学/板书.png")
    assert item.direction == "课程" and item.category == "高等数学"
    assert item.selected is True
    item.ocr_error = "识别失败"
    original_target = item.target_dir_rel
    original_reason = item.rule_applied
    settings = AiSettings(api_key="test-key", enabled=True)

    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    apply_ai_fallback(
        plan, settings, transport=lambda _s, _m: "不是 JSON",
        require_evidence=True)
    assert item.target_dir_rel == original_target
    assert item.selected is True, "无效 AI 响应不能推翻已勾选的本地分类"
    assert item.rule_applied == original_reason
    assert any(r["reason"] == "模型没有返回 JSON"
               for r in plan.ai_rejections), plan.ai_rejections

    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    plan.ai_rejections.clear()
    skip_text = (
        '{"file":"大二上/高等数学/板书.png","direction":"课程",'
        '"category":"高等数学","action":"skip","evidence":"识别失败"}')
    apply_ai_fallback(
        plan, settings,
        transport=lambda _s, _m: skip_text, require_evidence=True)
    assert item.selected is True, "AI 建议跳过不能覆盖本地分类"
    assert item.skipped is False
    assert item.rule_applied == original_reason
    assert any("AI 建议跳过" in r["reason"]
               for r in plan.ai_rejections), plan.ai_rejections

    # 待定文件的既有行为保持不变：非法响应仍不勾选、目标不变。
    root2 = root_dir()
    write(root2, "下载/未知.md", "这是一份没有关键词的说明")
    _scan2, _extraction2, plan2 = build(root2)
    pending = item_by(plan2, "下载/未知.md")
    target = pending.target_dir_rel
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    apply_ai_fallback(
        plan2, settings, transport=lambda _s, _m: "不是 JSON",
        require_evidence=True)
    assert pending.selected is False
    assert pending.target_dir_rel == target
    assert plan2.ai_rejections


CHECKS = [
    ("正文提取与未支持格式", check_content_extraction),
    ("同名文件靠正文区分（关网）", check_body_distinguishes_same_names),
    ("课程目录与项目目录路径依据", check_path_evidence),
    ("长期目录规则不是正文依据", check_long_term_dir_is_not_body_evidence),
    ("依据来源与实际证据一致", check_content_source_matches_actual_evidence),
    ("无效 AI 响应不推翻本地分类", check_invalid_ai_keeps_local_selection),
    ("歧义改选后必须重新确认", check_ambiguity_resolution_requires_reconfirm),
    ("跳过后源路径与 SHA 不变", check_skip_keeps_source),
    ("非法目标与无效模型响应", check_invalid_ai_responses),
    ("取消后迟到结果被丢弃", check_stale_results_discarded),
    ("PlanWorker 接入正文", check_plan_worker_reads_body),
    ("执行前后与撤销后的路径和 SHA", check_full_execute_and_undo),
    ("联网默认关闭与授权说明", check_ai_defaults_off),
]


def main() -> int:
    failures = []
    for name, func in CHECKS:
        try:
            func()
        except Exception as exc:  # noqa: BLE001 — 自测脚本汇总所有失败
            failures.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    passed = len(CHECKS) - len(failures)
    print(f"合计 {len(CHECKS)} 项：通过 {passed} / 未通过 {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
