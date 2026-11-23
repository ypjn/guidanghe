"""沾边归类依据文案与 EXIF 软证据门槛。"""
from __future__ import annotations

import os
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from media_hints import CaptureHint, capture_hint, clear_hint_cache  # noqa: E402
from planner import MODE_BY_DIRECTION_SEMESTER, classify_entries  # noqa: E402
from product_prefs import (  # noqa: E402
    default_prefs, load_prefs, save_prefs, set_folder_watch_enabled,
)
from scanner import scan_directory  # noqa: E402


def _scan(paths, bodies=None):
    root = tempfile.mkdtemp(prefix="archivebox_ev_")
    bodies = bodies or {}
    for rel in paths:
        full = os.path.join(root, *rel.replace("\\", "/").split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(bodies.get(rel, b"x"))
    return scan_directory(root, detect_project_roots=False)


def _by(paths, ocr_texts=None, bodies=None):
    items, _ = classify_entries(
        _scan(paths, bodies=bodies), MODE_BY_DIRECTION_SEMESTER, [],
        ocr_texts=ocr_texts)
    return {item.file.rel_path.replace("\\", "/"): item for item in items}


def test_neighbor_evidence_lines():
    got = _by([
        "大二上/数据结构/实验一.pdf",
        "images/实验一.png",
    ])
    hit = got["images/实验一.png"]
    assert hit.rule_key.startswith("neighbor:"), hit.rule_applied
    assert "沾边归类建议" in hit.rule_applied
    assert "依据：" in hit.rule_applied
    assert "重合" in hit.rule_applied
    assert "未自动勾选" in hit.rule_applied
    assert hit.selected is False
    assert "文件名主干" in (hit.decision_evidence or hit.rule_applied)


def test_exif_alone_does_not_adopt(monkeypatch=None):
    """同设备同日不够门槛：没有文件名/类别重合时不归类。"""
    clear_hint_cache()

    def fake_hint(path: str) -> CaptureHint:
        return CaptureHint(device_key="cam|x", day_bucket="2026-01-02")

    import media_hints
    import planner
    planner.capture_hint = None  # force import path inside function
    # Patch at media_hints so planner's local import sees it
    original = media_hints.capture_hint
    media_hints.capture_hint = fake_hint  # type: ignore
    try:
        # Re-run: camera-named images without stem should stay fallback
        got = _by([
            "大二上/数据结构/笔记.pdf",
            "images/IMG_0001.jpg",
            "images/IMG_0002.jpg",
        ])
        # IMG names are camera generic stems → empty neighbor stem
        assert got["images/IMG_0001.jpg"].rule_key == "fallback"
        assert got["images/IMG_0002.jpg"].rule_key == "fallback"
    finally:
        media_hints.capture_hint = original
        clear_hint_cache()


def test_product_prefs_watch_default_off(tmp_path=None):
    root = tempfile.mkdtemp(prefix="archivebox_prefs_")
    os.environ["ARCHIVEBOX_APP_DIR"] = root
    try:
        prefs = load_prefs()
        assert prefs.folder_watch_enabled is False
        set_folder_watch_enabled(True)
        assert load_prefs().folder_watch_enabled is True
        set_folder_watch_enabled(False)
        assert load_prefs().folder_watch_enabled is False
        assert default_prefs().folder_watch_enabled is False
    finally:
        os.environ.pop("ARCHIVEBOX_APP_DIR", None)


def main() -> None:
    test_neighbor_evidence_lines()
    test_exif_alone_does_not_adopt()
    test_product_prefs_watch_default_off()
    print("neighbor evidence + prefs checks passed")


if __name__ == "__main__":
    main()
