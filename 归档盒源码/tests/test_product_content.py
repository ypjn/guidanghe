"""PDF 与中文正文检索。

预期查询写在 EXPECTED 里，再跑检索。运行：
  QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover \
    -s tests -p 'test_product_content.py' -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from xml.sax.saxutils import escape

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _pdf(path: str, pages: list[str], password: str = "") -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        font_ref = writer._add_object(font)
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font_ref
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 18 Tf 72 720 Td ({safe}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    with open(path, "wb") as handle:
        writer.write(handle)


def _docx(path: str, paragraphs: list[str], pad: int = 0) -> None:
    body = []
    for paragraph in paragraphs:
        body.append(f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>")
    if pad:
        body.append("<w:p><w:r><w:t>" + ("垫" * pad) + "</w:t></w:r></w:p>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))


def _pptx(path: str, slides: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for number, text in enumerate(slides, start=1):
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f"<a:t>{escape(text)}</a:t></p:sld>"
            )
            archive.writestr(f"ppt/slides/slide{number}.xml", xml.encode("utf-8"))


def _xlsx(path: str, sheets: list[list[str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shared = []
    for cells in sheets:
        for cell in cells:
            if cell not in shared:
                shared.append(cell)
    items = "".join(f"<si><t>{escape(cell)}</t></si>" for cell in shared)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{items}</sst>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml.encode("utf-8"))
        for number, cells in enumerate(sheets, start=1):
            refs = "".join(
                f'<c t="s"><v>{shared.index(cell)}</v></c>' for cell in cells)
            sheet = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData><row>{refs}</row></sheetData></worksheet>"
            )
            archive.writestr(f"xl/worksheets/sheet{number}.xml", sheet.encode("utf-8"))


def _paths(page, scope: str) -> set[str]:
    return {hit.rel_path for hit in page.hits if hit.field == scope or scope == "any"}


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archivebox_content_")
        self.db = os.path.join(self.tmp, "index.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["ARCHIVEBOX_INDEX_DB"] = self.db
        os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(self.tmp, "appdata")
        os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(self.tmp, "recent.json")

    def tearDown(self):
        os.environ.pop("ARCHIVEBOX_INDEX_DB", None)
        os.environ.pop("ARCHIVEBOX_APP_DIR", None)
        os.environ.pop("ARCHIVEBOX_RECENT_FILE", None)

    def test_labeled_queries_separate_same_names_and_late_text(self):
        from content_search import search_files
        from index_service import register_space, update_space
        from index_store import IndexStore, SearchQuery

        root = os.path.join(self.tmp, "corpus")
        os.makedirs(root)
        _pdf(os.path.join(root, "课程甲/资料.pdf"), ["cover", "", "gaodeng shuxue jixian PAGE3TOKEN"])
        _pdf(os.path.join(root, "课程乙/资料.pdf"), ["cover", "shuju jiegou lianbiao"])
        _docx(os.path.join(root, "课程甲/资料.docx"), ["物理实验衍射"])
        _docx(os.path.join(root, "课程乙/资料.docx"), ["软件工程需求"])
        _pptx(os.path.join(root, "汇报/资料.pptx"), ["封面", "SLIDE2TOKEN"])
        _xlsx(os.path.join(root, "表格/成绩.xlsx"), [["姓名"], ["SHEET2TOKEN"]])
        late = "注" * 5000
        _write(root, "长文/后部.txt", late + "\nLATEBODYTOKEN\n")
        _write(root, "代码/后部.py", ("# 注释\n" * 800) + "LATECODETOKEN\n")
        _pdf(os.path.join(root, "手册/多页.pdf"), ["first", "second", "PAGE3ONLY"])
        _pdf(os.path.join(root, "坏文件/加密.pdf"), ["secret"], password="secret")
        with open(os.path.join(root, "坏文件/损坏.pdf"), "wb") as handle:
            os.makedirs(os.path.dirname(handle.name), exist_ok=True)
            handle.write(b"%PDF-1.7\nthis is not a pdf")
        _pdf(os.path.join(root, "坏文件/空白.pdf"), [""])
        _write(root, "词/归档盒.txt", "归档盒是本地文件工具\n")
        _write(root, "词/实验.txt", "本周实验安排\n")
        _write(root, "词/扩散.txt", "热扩散系数\n")
        _write(root, "词/图.txt", "见图\n")
        _write(root, "词/版本.txt", "发布版本 1.8.0\n")
        _write(root, "词/混合.md", "AUV控制回路\n")
        _write(root, "词/英文.txt", "autonomous underwater vehicle AUV\n")
        for number in range(41):
            _write(root, f"笔记/记录-{number:02d}.txt", f"普通笔记 {number} 没有特殊词\n")
        self.assertEqual(sum(len(files) for _, _, files in os.walk(root)), 60)

        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "语料", store=store)
        started = time.perf_counter()
        report = update_space(spec.space_id, store=store)
        elapsed = time.perf_counter() - started
        self.assertFalse(report.cancelled)
        text_rows = store.conn.execute("SELECT status, reason FROM file_text").fetchall()
        failed = [row["reason"] for row in text_rows if row["status"] == "failed"]
        self.assertTrue(any("加密" in reason for reason in failed))
        self.assertTrue(any("无法解析" in reason for reason in failed))
        blank = store.conn.execute(
            "SELECT t.status FROM file_text t JOIN files f ON f.file_id=t.file_id "
            "WHERE f.rel_path=?", ("坏文件/空白.pdf",)).fetchone()
        self.assertEqual(blank["status"], "unsupported")

        expected = [
            ("归档盒", "body", {"词/归档盒.txt"}, {"词/实验.txt"}),
            ("实验", "body", {"词/实验.txt"}, {"词/归档盒.txt"}),
            ("扩散", "body", {"词/扩散.txt"}, {"词/图.txt"}),
            ("图", "body", {"词/图.txt"}, {"词/归档盒.txt"}),
            ("AUV", "body", {"词/英文.txt", "词/混合.md"}, {"词/实验.txt"}),
            ("1.8", "body", {"词/版本.txt"}, {"词/实验.txt"}),
            ("AUV控制", "body", {"词/混合.md"}, {"词/英文.txt"}),
            ("PAGE3TOKEN", "body", {"课程甲/资料.pdf"}, {"课程乙/资料.pdf"}),
            ("jixian", "body", {"课程甲/资料.pdf"}, {"课程乙/资料.pdf"}),
            ("lianbiao", "body", {"课程乙/资料.pdf"}, {"课程甲/资料.pdf"}),
            ("衍射", "body", {"课程甲/资料.docx"}, {"课程乙/资料.docx"}),
            ("需求", "body", {"课程乙/资料.docx"}, {"课程甲/资料.docx"}),
            ("SLIDE2TOKEN", "body", {"汇报/资料.pptx"}, set()),
            ("SHEET2TOKEN", "body", {"表格/成绩.xlsx"}, set()),
            ("LATEBODYTOKEN", "body", {"长文/后部.txt"}, set()),
            ("LATECODETOKEN", "body", {"代码/后部.py"}, {"长文/后部.txt"}),
            ("PAGE3ONLY", "body", {"手册/多页.pdf"}, set()),
            ("物理实验", "body", {"课程甲/资料.docx"}, {"课程乙/资料.docx"}),
            ("软件工程", "body", {"课程乙/资料.docx"}, {"课程甲/资料.docx"}),
            ("热扩散", "body", {"词/扩散.txt"}, {"词/实验.txt"}),
            ("见图", "body", {"词/图.txt"}, {"词/扩散.txt"}),
            ("发布版本", "body", {"词/版本.txt"}, set()),
            ("underwater", "body", {"词/英文.txt"}, {"词/混合.md"}),
            ("普通笔记", "body", {"笔记/记录-00.txt", "笔记/记录-40.txt"}, {"词/归档盒.txt"}),
            ("资料.pdf", "name", {"课程甲/资料.pdf", "课程乙/资料.pdf"}, {"课程甲/资料.docx"}),
            ("资料.docx", "name", {"课程甲/资料.docx", "课程乙/资料.docx"}, {"课程甲/资料.pdf"}),
            ("归档盒.txt", "name", {"词/归档盒.txt"}, {"词/实验.txt"}),
            ("记录-07", "name", {"笔记/记录-07.txt"}, {"笔记/记录-08.txt"}),
            ("后部.py", "name", {"代码/后部.py"}, {"长文/后部.txt"}),
            ("多页.pdf", "name", {"手册/多页.pdf"}, {"课程甲/资料.pdf"}),
        ]
        self.assertEqual(len(expected), 30)
        misses = []
        started = time.perf_counter()
        for text, scope, include, exclude in expected:
            page = search_files(store, SearchQuery(text=text, scope=scope, limit=100))
            found = _paths(page, scope)
            missing = include - found
            extra = exclude & found
            if missing or extra:
                misses.append(f"{text}/{scope} 漏={sorted(missing)} 误={sorted(extra)}")
        search_elapsed = time.perf_counter() - started
        page = search_files(store, SearchQuery(text="PAGE3TOKEN", scope="body"))
        located = [hit.location for hit in page.hits if hit.rel_path == "课程甲/资料.pdf"]
        self.assertTrue(any("第3页" in item for item in located), located)
        slide = search_files(store, SearchQuery(text="SLIDE2TOKEN", scope="body"))
        self.assertTrue(any("幻灯片 2" in hit.location for hit in slide.hits))
        sheet = search_files(store, SearchQuery(text="SHEET2TOKEN", scope="body"))
        self.assertTrue(any("工作表 2" in hit.location for hit in sheet.hits))
        code = search_files(store, SearchQuery(text="LATECODETOKEN", scope="body", kinds=["代码"]))
        self.assertEqual(_paths(code, "body"), {"代码/后部.py"})
        empty_kind = search_files(
            store, SearchQuery(text="LATECODETOKEN", scope="body", kinds=["文档"]))
        self.assertEqual(empty_kind.hits, [])
        self.assertEqual(
            search_files(store, SearchQuery(
                text="归档盒", scope="body", modified_before_ns=1)).hits, [])
        self.assertFalse(misses, "\n".join(misses))
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = os.path.getsize(self.db)
        print(
            f"\n内容索引 文件=60 失败原因={failed} 建索引={elapsed:.2f}s "
            f"30次查询={search_elapsed:.3f}s 库={size}字节")

    def test_limits_timeout_and_bad_files_keep_going(self):
        from content_search import search_files
        from index_content import ContentLimits
        from index_service import register_space, update_space
        from index_store import IndexStore, SearchQuery

        root = os.path.join(self.tmp, "limits")
        _write(root, "短.txt", "开头能看见")
        _write(root, "长.txt", "开头" + ("字" * 200) + "LATEHIDDEN")
        _pdf(os.path.join(root, "慢.pdf"), ["COVER", "MIDDLE", "LATEPAGE"])
        _write(root, "后续.txt", "NEXTFILETOKEN")
        _docx(os.path.join(root, "大部件.docx"), ["炸弹标记" + ("爆" * 80)])
        _write(root, "无权限/后面.txt", "AFTERDENY")
        blocked = _write(root, "无权限/秘密.txt", "秘密正文")
        os.chmod(blocked, 0)
        self.addCleanup(os.chmod, blocked, 0o644)
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "上限", store=store)
        report = update_space(
            spec.space_id, store=store,
            limits=ContentLimits(max_chars=20, max_seconds=0, max_member_bytes=40))
        self.assertTrue(any("秘密" in item or "Permission" in item or "权限" in item
                            or "Operation not permitted" in item for item in report.errors))
        hidden = search_files(store, SearchQuery(text="LATEHIDDEN", scope="body"))
        self.assertEqual(hidden.hits, [])
        partial = store.conn.execute(
            "SELECT t.status, t.reason FROM file_text t JOIN files f ON f.file_id=t.file_id "
            "WHERE f.rel_path='长.txt'").fetchone()
        self.assertEqual(partial["status"], "partial")
        self.assertIn("未读", partial["reason"])
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="NEXTFILETOKEN", scope="body")), "body"),
            {"后续.txt"})
        self.assertEqual(
            search_files(store, SearchQuery(text="LATEPAGE", scope="body")).hits, [])
        timed = store.conn.execute(
            "SELECT status, reason FROM file_text t JOIN files f ON f.file_id=t.file_id "
            "WHERE f.rel_path='慢.pdf'").fetchone()
        self.assertEqual(timed["status"], "partial")
        self.assertIn("超时", timed["reason"])
        bomb = store.conn.execute(
            "SELECT status, reason FROM file_text t JOIN files f ON f.file_id=t.file_id "
            "WHERE f.rel_path='大部件.docx'").fetchone()
        self.assertEqual(bomb["status"], "failed")
        self.assertIn("解压", bomb["reason"])
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="AFTERDENY", scope="body")), "body"),
            {"无权限/后面.txt"})

    def test_change_during_read_and_link_escape_are_discarded(self):
        from content_search import search_files
        from index_content import extract_document
        from index_service import register_space, update_space
        from index_store import IndexStore, SearchQuery

        root = os.path.join(self.tmp, "race")
        path = _write(root, "race.txt", "OLDTOKEN\n")
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("SENTINELUNIQUE\n")
        outside_hash = _sha(outside)
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "竞争", store=store)

        def rewrite(target):
            if target.endswith("race.txt"):
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("NEWTOKEN\n")

        update_space(spec.space_id, store=store, before_body=rewrite)
        self.assertEqual(search_files(store, SearchQuery(text="OLDTOKEN")).hits, [])
        self.assertEqual(search_files(store, SearchQuery(text="NEWTOKEN")).hits, [])
        stale = store.conn.execute(
            "SELECT status FROM file_text").fetchone()
        self.assertEqual(stale["status"], "stale")
        update_space(spec.space_id, store=store)
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="NEWTOKEN", scope="body")), "body"),
            {"race.txt"})
        os.remove(path)
        os.symlink(outside, path)
        update_space(spec.space_id, store=store)
        self.assertEqual(search_files(store, SearchQuery(text="SENTINELUNIQUE")).hits, [])
        self.assertEqual(_sha(outside), outside_hash)
        direct = extract_document(path, root)
        self.assertEqual(direct.status, "failed")
        self.assertEqual(direct.chunks, [])

    def test_pause_remove_rebuild_upgrade_and_query_safety(self):
        from content_search import search_files
        from index_service import register_space, remove_text, rebuild_text, update_space
        from index_store import IndexStore, SearchQuery

        root = os.path.join(self.tmp, "keep")
        _write(root, "保留.txt", "保留正文关键字\n")
        before = _sha(os.path.join(root, "保留.txt"))
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "保留", store=store)
        update_space(spec.space_id, store=store, read_content=False)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        update_space(spec.space_id, store=store)
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="保留正文", scope="body")), "body"),
            {"保留.txt"})
        rows_before = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        for nasty in ("", "' OR 1=1 --", '"', "-", "*", "图" * 300, "1.8", "AUV"):
            page = search_files(store, SearchQuery(text=nasty, limit=10))
            self.assertIsNotNone(page)
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], rows_before)
        long_page = search_files(store, SearchQuery(text="保留" * 150))
        self.assertTrue(long_page.truncated_query)
        remove_text(spec.space_id, store=store)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], rows_before)
        self.assertEqual(_sha(os.path.join(root, "保留.txt")), before)
        rebuild_text(spec.space_id, store=store)
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="保留正文", scope="body")), "body"),
            {"保留.txt"})
        with self.assertRaises(RuntimeError):
            store.rebuild(fail=True)
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="保留正文", scope="body")), "body"),
            {"保留.txt"})
        store.rebuild()
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="保留正文", scope="body")), "body"),
            {"保留.txt"})

        legacy = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE spaces (
                space_id TEXT PRIMARY KEY, root_path TEXT NOT NULL,
                display_name TEXT NOT NULL, enabled INTEGER NOT NULL,
                policy_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '');
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, space_id TEXT NOT NULL,
                rel_path TEXT NOT NULL, path_key TEXT NOT NULL, name TEXT NOT NULL,
                file_type TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL, dev INTEGER NOT NULL, ino INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL DEFAULT '', availability TEXT NOT NULL,
                index_state TEXT NOT NULL, parser_version INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL, exclude_reason TEXT NOT NULL DEFAULT '',
                present INTEGER NOT NULL DEFAULT 1, UNIQUE(space_id, path_key));
            INSERT INTO spaces VALUES ('sp-old','/tmp/old','旧空间',1,'{}','t','t',1,'');
            INSERT INTO files VALUES (
                'file-old','sp-old','旧.txt','旧.txt','旧.txt','文档',1,1,1,1,1,
                'abc','online','content',1,'t','',1);
            """
        )
        conn.commit()
        conn.close()
        upgraded = IndexStore(legacy)
        self.addCleanup(upgraded.close)
        version = upgraded.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        self.assertEqual(version, "2")
        self.assertEqual(upgraded.get_file("sp-old", "旧.txt")["rel_path"], "旧.txt")
        self.assertEqual(
            search_files(upgraded, SearchQuery(text="旧", scope="body")).hits, [])

    def test_cancel_then_update_and_organize_still_undoes(self):
        from content_search import search_files
        from executor import execute_plan, undo_latest_batch
        from index_service import register_space, update_space
        from index_store import IndexStore, SearchQuery
        from planner import (
            DEFAULT_COURSE_RULES, MODE_BY_COURSE_THEN_TYPE, generate_plan,
        )
        from scanner import scan_directory
        from storage import Storage

        root = os.path.join(self.tmp, "cancel")
        _write(root, "a.txt", "ALPHAONE\n")
        _write(root, "b.txt", "BETATWO\n")
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "取消", store=store)
        gate = threading.Event()

        def progress(done, total, rel):
            del done
            if total and rel.endswith("a.txt"):
                gate.set()

        report = update_space(spec.space_id, gate, progress, store)
        self.assertTrue(report.cancelled)
        self.assertEqual(search_files(store, SearchQuery(text="BETATWO")).hits, [])
        update_space(spec.space_id, store=store)
        self.assertEqual(
            _paths(search_files(store, SearchQuery(text="BETATWO", scope="body")), "body"),
            {"b.txt"})

        space = os.path.join(self.tmp, "organize")
        source = _write(space, "高数作业.txt", "高等数学 极限 作业\n")
        digest = _sha(source)
        other = register_space(space, "整理", store=store)
        update_space(other.space_id, store=store, read_content=False)
        scan = scan_directory(space)
        plan = generate_plan(scan, MODE_BY_COURSE_THEN_TYPE, DEFAULT_COURSE_RULES)
        item = next(entry for entry in plan.items if entry.file.rel_path == "高数作业.txt")
        for entry in plan.items:
            entry.selected = entry.file.rel_path == "高数作业.txt"
        storage = Storage(space)
        try:
            executed = execute_plan(plan, storage)
            self.assertEqual(executed.moved, 1)
            undone = undo_latest_batch(storage)
        finally:
            storage.close()
        self.assertEqual(undone.restored, 1)
        self.assertEqual(_sha(source), digest)
        self.assertTrue(os.path.isfile(os.path.join(space, ".organizer", "history.db")))
        from content_extract import MAX_TEXT_BYTES
        self.assertEqual(MAX_TEXT_BYTES, 4096)

    def test_pages_are_stable(self):
        from content_search import search_files
        from index_service import register_space, update_space
        from index_store import IndexStore, SearchQuery

        root = os.path.join(self.tmp, "pages")
        _pdf(os.path.join(root, "课程甲/资料.pdf"), ["alpha marker"])
        _pdf(os.path.join(root, "课程乙/资料.pdf"), ["beta marker"])
        store = IndexStore(self.db)
        self.addCleanup(store.close)
        spec = register_space(root, "分页", store=store)
        update_space(spec.space_id, store=store)
        first = search_files(store, SearchQuery(text="资料.pdf", scope="name", limit=1, offset=0))
        second = search_files(store, SearchQuery(text="资料.pdf", scope="name", limit=1, offset=1))
        again = search_files(store, SearchQuery(text="资料.pdf", scope="name", limit=1, offset=0))
        self.assertEqual(len(first.hits), 1)
        self.assertEqual(len(second.hits), 1)
        self.assertNotEqual(first.hits[0].file_id, second.hits[0].file_id)
        self.assertEqual(first.hits[0].file_id, again.hits[0].file_id)
        self.assertNotEqual(first.hits[0].rel_path, second.hits[0].rel_path)
