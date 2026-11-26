# -*- mode: python ; coding: utf-8 -*-
"""归档盒 macOS 应用（Apple 芯片）。

在仓库根目录执行「打包/build_mac.sh」。
脚本选择解释器、写入构建身份、打包、做本地签名并生成只读 DMG。
只跑 PyInstaller、不重签的话，程序会因为签名不一致而无法启动。
"""
import json
import os
import sys

from PyInstaller.utils.hooks import collect_all

SRC = os.path.abspath(os.path.join(SPECPATH, "..", "归档盒源码"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from app_metadata import (  # noqa: E402
    APP_NAME, APP_VERSION, BUNDLE_ID, EXECUTABLE_NAME, MIN_SYSTEM_VERSION,
)

datas = []
binaries = []
_handbook = os.path.abspath(os.path.join(
    SPECPATH, "..", "docs", "handbooks",
    "2026-11-23-classification-handbook.md"))
if os.path.isfile(_handbook):
    datas.append((_handbook, "handbooks"))
hiddenimports = [
    "ai_fallback",
    "ai_settings_dialog",
    "ai_prompts",
    "ai_consult",
    "ai_consult_store",
    "ai_consult_dialog",
    "consult_chat_format",
    "app_metadata",
    "agent",
    "agent_dialog",
    "classification_config",
    "config_dialog",
    "config_transfer",
    "content_extract",
    "content_search",
    "contest_catalog",
    "daily_entry_dialogs",
    "duplicate_dialog",
    "duplicate_report",
    "executor",
    "general_catalog",
    "handbook_session",
    "handbook_apply",
    "backup_dialog",
    "backup_worker",
    "backup_service",
    "backup_store",
    "index_content",
    "index_dialog",
    "index_policy",
    "index_service",
    "index_store",
    "intake_model",
    "learning",
    "learning_dialog",
    "ocr_engine",
    "pdf_pages",
    "folder_watch",
    "watchdog",
    "plan_patch",
    "planner",
    "pypdf",
    "preferences",
    "product_acceptance",
    "professional_catalog",
    "protect_config",
    "recent_spaces",
    "relation_graph",
    "operation_feedback",
    "ui_theme",
    "media_hints",
    "rule_preview",
    "scan_cache",
    "search_dialog",
    "search_intent",
    "search_session",
    "scanner",
    "scheme_store",
    "storage",
    "storage_tree",
    "target_tree_dialog",
    "transfer_dialogs",
    "utils",
    "volume_identity",
    "Vision",
    "Foundation",
    "objc",
    "CoreFoundation",
    "AppKit",
    "Cocoa",
    "Quartz",
    "CoreML",
]

# 文字识别在函数里才导入，分析阶段看不到，这里整包收进来。
# PySide6 交给 PyInstaller 自带钩子，避免和钩子各收一遍。
for pkg in (
    "rapidocr",
    "onnxruntime",
    "watchdog",
    "pypdfium2",
    "objc",
    "CoreFoundation",
    "Foundation",
    "AppKit",
    "Cocoa",
    "Quartz",
    "CoreML",
    "Vision",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

commit = "unknown"
identity_path = os.environ.get("ARCHIVEBOX_BUILD_IDENTITY", "")
if identity_path and os.path.isfile(identity_path):
    datas.append((identity_path, "."))
    try:
        with open(identity_path, encoding="utf-8") as handle:
            commit = json.load(handle).get("commit") or commit
    except (OSError, ValueError):
        pass

icon = os.path.join(SPECPATH, "assets", "归档盒.icns")

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "tkinter", "PyObjCTest", "winrt"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXECUTABLE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity="-",
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=EXECUTABLE_NAME,
)
app = BUNDLE(
    coll,
    name="归档盒.app",
    icon=icon if os.path.isfile(icon) else None,
    bundle_identifier=BUNDLE_ID,
    version=APP_VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "CFBundlePackageType": "APPL",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": MIN_SYSTEM_VERSION,
        "ArchiveBoxBuildCommit": commit,
    },
)
