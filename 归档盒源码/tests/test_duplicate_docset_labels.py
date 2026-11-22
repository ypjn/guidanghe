"""同一份内容分组文案与只读保留建议。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from duplicate_report import (  # noqa: E402
    KIND_CONTENT, KIND_SAME_NAME_DIFF, STATUS_VERIFIED, DupEntry,
    assemble_report, verify_entries,
)
from utils import sha256_file  # noqa: E402


def _entry(root: str, rel: str, data: bytes) -> DupEntry:
    abs_path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(abs_path) or root, exist_ok=True)
    with open(abs_path, "wb") as fh:
        fh.write(data)
    st = os.stat(abs_path)
    return DupEntry(
        rel_path=rel, abs_path=abs_path, filename=os.path.basename(rel),
        size=st.st_size, size_display=f"{st.st_size} B",
        expected_mtime_ns=getattr(st, "st_mtime_ns", 0),
    )


def main() -> None:
    root = tempfile.mkdtemp(prefix="archivebox_docset_")
    a = _entry(root, "a/讲义.pdf", b"same-bytes-payload")
    b = _entry(root, "b/讲义副本.pdf", b"same-bytes-payload")
    c = _entry(root, "c/讲义.pdf", b"other-content")
    d = _entry(root, "d/讲义.pdf", b"third-content")
    entries = [a, b, c, d]
    assert verify_entries(entries)
    report = assemble_report(root, entries, scope_note="测试", source="scan")
    content = [g for g in report.groups if g.kind == KIND_CONTENT]
    assert len(content) == 1, report.groups
    g = content[0]
    assert "同一份内容" in g.label
    assert "建议核对手动保留" in g.note
    assert "不会删除" in g.note
    versions = [g for g in report.groups if g.kind == KIND_SAME_NAME_DIFF]
    assert versions, "同名不同内容应成组"
    assert "可能不同版本" in versions[0].label or "不同内容" in versions[0].note
    assert "不要当成重复" in versions[0].note
    print("duplicate docset label checks passed")


if __name__ == "__main__":
    main()
