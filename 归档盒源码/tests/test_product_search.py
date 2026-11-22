"""统一搜索：可核对的条件、代次和整理边界。

运行：
  QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover \
    -s tests -p 'test_product_search.py' -v
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

NOW = dt.datetime(2026, 11, 21, 15, 0, 0)


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_search_")
        self.db = os.path.join(self.tmp, "index.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["ARCHIVEBOX_INDEX_DB"] = self.db
        os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(self.tmp, "appdata")
        os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(self.tmp, "recent.json")

    def tearDown(self):
        os.environ.pop("ARCHIVEBOX_INDEX_DB", None)
        os.environ.pop("ARCHIVEBOX_APP_DIR", None)
        os.environ.pop("ARCHIVEBOX_RECENT_FILE", None)

    def test_twenty_labeled_requests_keep_their_conditions(self):
        from search_intent import understand

        labeled = [
            ("找一下归档盒相关的设计文档", "find", "归档盒", [], "文档", ""),
            ("上周修改的数据结构报告，排除答案和复习资料", "find", "数据结构", ["答案", "复习资料"], "", "修改时间 2026-11-09 至 2026-11-15"),
            ("把高数作业整理一下", "organize", "高数作业", [], "", ""),
            ("找数据结构最终版", "find", "数据结构", [], "", ""),
            ("昨天创建的实验代码", "find", "实验", [], "代码", "创建时间 2026-11-20 至 2026-11-20"),
            ("搜索项目说明", "find", "项目说明", [], "", ""),
            ("不要答案", "find", "", ["答案"], "", ""),
            ("今天修改的周报", "find", "周报", [], "", "修改时间 2026-11-21 至 2026-11-21"),
            ("帮我找软件工程需求", "find", "软件工程需求", [], "", ""),
            ("归档这些课件", "organize", "这些课件", [], "", ""),
            ("查找热扩散系数", "find", "热扩散系数", [], "", ""),
            ("上周创建的图片说明，排除草稿", "find", "图片说明", ["草稿"], "", "创建时间 2026-11-09 至 2026-11-15"),
            ("移动高数作业到课程目录", "organize", "高数作业到课程目录", [], "", ""),
            ("找一下AUV控制文档", "find", "AUV控制", [], "文档", ""),
            ("不含复习资料的实验报告", "find", "实验报告", ["复习资料"], "", ""),
            ("找最新版论文", "find", "论文", [], "", ""),
            ("整理代码作业", "organize", "代码作业", [], "代码", ""),
            ("昨天修改的链表笔记", "find", "链表笔记", [], "", "修改时间 2026-11-20 至 2026-11-20"),
            ("搜索资料.pdf", "find", "资料.pdf", [], "", ""),
            ("把实验报告归入课程", "organize", "实验报告", [], "", ""),
        ]
        self.assertEqual(len(labeled), 20)
        problems = []
        for sentence, action, text, excluded, kind, label in labeled:
            got = understand(sentence, NOW)
            if got.action != action or text not in got.text:
                problems.append(f"{sentence} -> {got.action} / {got.text}")
            if excluded != got.excluded:
                problems.append(f"{sentence} 排除 {got.excluded}")
            if kind and kind not in got.kinds:
                problems.append(f"{sentence} 类型 {got.kinds}")
            if label != got.range_label:
                problems.append(f"{sentence} 时间 {got.range_label}")
            if "最终" in sentence or "最新" in sentence:
                if got.modified_after is not None or not got.notes:
                    problems.append(f"{sentence} 被当成了按时间认定的最终稿")
            if "创建" in sentence and got.modified_after is not None:
                problems.append(f"{sentence} 把创建时间写成了修改时间")
            if "修改" in sentence and "创建" not in sentence and got.created_after is not None:
                problems.append(f"{sentence} 把修改时间写成了创建时间")
        self.assertFalse(problems, "\n".join(problems))

    def test_lookup_filters_negation_dates_and_does_not_touch_files(self):
        from index_service import register_space, update_space
        from index_store import IndexStore
        from search_intent import understand
        from search_session import SearchSession, locate_hit, run_lookup

        root = os.path.join(self.tmp, "notes")
        report = _write(root, "数据结构/报告.txt", "数据结构报告\n")
        answer = _write(root, "数据结构/答案.txt", "数据结构答案\n")
        old = _write(root, "周报.txt", "上周的周报\n")
        fresh = _write(root, "今天.txt", "今天的记录\n")
        last_week = dt.datetime(2026, 11, 12, 9, 0, 0).timestamp()
        os.utime(old, (last_week, last_week))
        os.utime(report, (last_week, last_week))
        os.utime(answer, (last_week, last_week))
        before = {
            os.path.relpath(path, root): pathlib.Path(path).read_bytes()
            for path in (report, answer, old, fresh)}
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "笔记", store=store)
        update_space(spec.space_id, store=store)
        understood = understand("上周修改的数据结构报告，排除答案", NOW)
        page = run_lookup(store, understood)
        paths = {hit.rel_path for hit in page.hits}
        self.assertIn("数据结构/报告.txt", paths)
        self.assertNotIn("数据结构/答案.txt", paths)
        self.assertNotIn("今天.txt", paths)
        created = understand("上周创建的周报", NOW)
        created_hits = {hit.rel_path for hit in run_lookup(store, created).hits}
        self.assertNotIn("周报.txt", created_hits)
        after = {
            os.path.relpath(path, root): pathlib.Path(path).read_bytes()
            for path in (report, answer, old, fresh)}
        self.assertEqual(before, after)
        session = SearchSession()
        first = session.begin()
        second = session.begin()
        applied = []
        self.assertFalse(session.deliver(first, "旧查询", applied.append))
        self.assertTrue(session.deliver(second, "新查询", applied.append))
        self.assertEqual(applied, ["新查询"])
        hit = next(item for item in page.hits if item.rel_path == "数据结构/报告.txt")
        found, reason = locate_hit(store, hit)
        self.assertEqual(os.path.realpath(found), os.path.realpath(report), reason)
        os.remove(report)
        missing, reason = locate_hit(store, hit)
        self.assertEqual(missing, "")
        self.assertIn("不在", reason)

    def test_same_name_spaces_and_protected_files_stay_out_of_moves(self):
        from executor import execute_plan, undo_latest_batch
        from index_policy import default_policy
        from index_service import register_space, update_space
        from index_store import IndexStore
        from planner import DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan
        from scanner import scan_directory
        from search_intent import understand
        from search_session import locate_hit, organize_block_reason, run_lookup, selection_plan
        from storage import Storage

        left = os.path.join(self.tmp, "甲")
        right = os.path.join(self.tmp, "乙")
        left_file = _write(left, "课程/资料.txt", "高等数学极限\n")
        right_file = _write(right, "课程/资料.txt", "数据结构链表\n")
        project = os.path.join(left, "demo")
        os.makedirs(os.path.join(project, "src"), exist_ok=True)
        _write(left, "demo/pyproject.toml", "[project]\nname='demo'\n")
        _write(left, "demo/src/main.py", "PROTECTEDTOKEN\n")
        homework = _write(left, "高数作业.txt", "高等数学 极限 作业\n")
        digest = pathlib.Path(homework).read_bytes()
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        policy = default_policy()
        policy["readonly_include"] = ["demo"]
        spec_left = register_space(left, "甲", policy, store)
        spec_right = register_space(right, "乙", store=store)
        update_space(spec_left.space_id, store=store)
        update_space(spec_right.space_id, store=store)
        page = run_lookup(store, understand("资料", NOW))
        by_space = {hit.space_id: hit for hit in page.hits if hit.rel_path == "课程/资料.txt"}
        self.assertEqual(set(by_space), {spec_left.space_id, spec_right.space_id})
        self.assertEqual(
            os.path.realpath(locate_hit(store, by_space[spec_left.space_id])[0]),
            os.path.realpath(left_file))
        self.assertEqual(
            os.path.realpath(locate_hit(store, by_space[spec_right.space_id])[0]),
            os.path.realpath(right_file))
        protected = run_lookup(store, understand("PROTECTEDTOKEN", NOW))
        self.assertTrue(any(hit.rel_path == "demo/src/main.py" for hit in protected.hits))
        self.assertIn("不能进入移动计划", organize_block_reason(left, "demo/src/main.py"))
        scan = scan_directory(left)
        plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
        hits = {hit.rel_path: hit for hit in protected.hits}
        hits["高数作业.txt"] = next(
            hit for hit in run_lookup(store, understand("高数作业", NOW)).hits
            if hit.rel_path == "高数作业.txt")
        kept, notes = selection_plan(
            left, plan, {"高数作业.txt", "demo/src/main.py"}, spec_left.space_id, hits)
        self.assertEqual([item.file.rel_path for item in kept], ["高数作业.txt"])
        self.assertTrue(any("demo/src/main.py" in note for note in notes) or not any(
            item.file.rel_path == "demo/src/main.py" for item in plan.items))
        storage = Storage(left)
        try:
            executed = execute_plan(plan, storage)
            self.assertEqual(executed.moved, 1)
            self.assertFalse(os.path.exists(homework))
            undone = undo_latest_batch(storage)
        finally:
            storage.close()
        self.assertEqual(undone.restored, 1)
        self.assertEqual(pathlib.Path(homework).read_bytes(), digest)
        self.assertTrue(os.path.isfile(os.path.join(project, "src", "main.py")))

    def test_dialog_pages_late_results_and_widths(self):
        from PySide6.QtWidgets import QApplication
        from index_service import register_space, update_space
        from index_store import IndexStore
        from search_dialog import SearchDialog

        root = os.path.join(
            self.tmp, "课程甲", "这是一个很长的中文目录名称用于检查按钮和完整位置")
        _write(root, "归档盒设计.txt", "归档盒设计文档正文\n")
        _write(root, "另一份.txt", "普通笔记\n")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(os.path.join(self.tmp, "课程甲"), "长路径", store=store)
        update_space(spec.space_id, store=store)
        app = QApplication.instance() or QApplication([])
        dialog = SearchDialog(store=store)
        dialog.query.setText("找一下归档盒相关的设计文档")
        dialog.search_blocking()
        self.assertIn("只查找", dialog.summary.text())
        self.assertTrue(any("归档盒设计.txt" in dialog.results.item(i).text()
                            for i in range(dialog.results.count())))
        dialog.results.setCurrentRow(0)
        dialog.prepare_organize()
        self.assertIn("查找不会生成移动计划", dialog.status.text())
        token = dialog.session.begin()
        dialog.session.deliver(token - 1, None, dialog._show)
        self.assertIn("归档盒设计.txt", dialog.results.item(0).text())
        evidence = os.path.join(
            SRC, "..", "docs", "product-development", "evidence", "batch04")
        os.makedirs(evidence, exist_ok=True)
        for width in (820, 1024, 1280):
            dialog.resize(width, 640)
            app.processEvents()
            shot = dialog.grab()
            self.assertGreater(shot.width(), 700)
            shot.save(os.path.join(evidence, f"search-{width}.png"))
        dialog.query.setText("找一下归档盒相关的设计文档")
        dialog.start_search(0)
        worker = dialog.worker
        self.assertIsNotNone(worker)
        self.assertTrue(worker.wait(5000))
        app.processEvents()
        self.assertTrue(any("归档盒设计.txt" in dialog.results.item(i).text()
                            for i in range(dialog.results.count())))
        dialog.shutdown()
        worker = None
        dialog.deleteLater()
        app.processEvents()

    def test_lookup_paginates_after_full_filters(self):
        """多词求交、排除词必须在分页前完成，页间不重复。"""
        import hashlib
        from index_service import update_space
        from index_store import IndexStore, SpaceSpec
        from search_intent import understand
        from search_session import run_lookup

        def ids(page):
            return {hit.file_id for hit in page.hits}

        # 75 份均匹配两个词：页大小 50 时第二页应剩 25，且与第一页无交集。
        root = os.path.join(self.tmp, "multi")
        os.makedirs(root)
        for n in range(75):
            _write(root, f"alpha_beta_{n:03d}.bin", "neutral")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = SpaceSpec("sp-multi", root, "multi", {})
        store.upsert_space(spec, "2026-11-22T00:00:00")
        update_space(spec.space_id, store=store)
        pages = [
            run_lookup(store, understand("alpha beta"), limit=50, offset=offset)
            for offset in (0, 50, 100)
        ]
        self.assertEqual([len(page.hits) for page in pages], [50, 25, 0])
        self.assertFalse(ids(pages[0]) & ids(pages[1]))
        self.assertEqual(len(ids(pages[0]) | ids(pages[1])), 75)

        # beta 只在 alpha 排序最后一份的正文里：完整交集应为 1。
        root2 = os.path.join(self.tmp, "intersect")
        os.makedirs(root2)
        for n in range(80):
            _write(root2, f"alpha_{n:03d}.txt", "neutral")
        store2 = IndexStore(os.path.join(self.tmp, "intersect.db"))
        self.addCleanup(store2.close)
        spec2 = SpaceSpec("sp-intersect", root2, "intersect", {})
        store2.upsert_space(spec2, "2026-11-22T00:00:00")
        update_space(spec2.space_id, store=store2)
        target = max(store2.present_files(), key=lambda item: item.file_id)
        _write(root2, target.rel_path, "beta")
        update_space(spec2.space_id, store=store2, read_content=True)
        page = run_lookup(store2, understand("alpha beta"), limit=50)
        self.assertEqual({hit.file_id for hit in page.hits}, {target.file_id})

        # 前 60 份 draft 应被排除，第一页直接给出 keep。
        root3 = os.path.join(self.tmp, "exclude")
        os.makedirs(root3)
        space_id = "sp-exclude"

        def planned_id(rel):
            return hashlib.sha256(f"{space_id}\n{rel.casefold()}".encode()).hexdigest()[:32]

        candidates = [f"gamma_draft_{n:04d}.bin" for n in range(2000)]
        drafts = [rel for rel in candidates if planned_id(rel) < "8"][:60]
        keep = next(
            f"gamma_keep_{n:04d}.bin"
            for n in range(2000)
            if planned_id(f"gamma_keep_{n:04d}.bin") > "f")
        for rel in drafts + [keep]:
            _write(root3, rel, "neutral")
        store3 = IndexStore(os.path.join(self.tmp, "exclude.db"))
        self.addCleanup(store3.close)
        spec3 = SpaceSpec(space_id, root3, "exclude", {})
        store3.upsert_space(spec3, "2026-11-22T00:00:00")
        update_space(spec3.space_id, store=store3)
        page1 = run_lookup(store3, understand("gamma，排除draft"), limit=50, offset=0)
        page2 = run_lookup(store3, understand("gamma，排除draft"), limit=50, offset=50)
        self.assertEqual([hit.rel_path for hit in page1.hits], [keep])
        self.assertEqual(page2.hits, [])


