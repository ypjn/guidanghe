# -*- mode: python ; coding: utf-8 -*-
"""归档盒 macOS 免安装包（Apple 芯片）。

在仓库根目录执行「打包/build_mac.sh」。
脚本会打包、去掉旁边多出来的 archivebox 文件夹，并重新做本地签名。
只跑 pyinstaller、不重签的话，程序会因为签名不一致而无法启动。
"""
import os

from PyInstaller.utils.hooks import collect_all

SRC = os.path.abspath(os.path.join(SPECPATH, "..", "归档盒源码"))

datas = []
binaries = []
hiddenimports = [
    "ai_fallback",
    "classification_config",
    "config_dialog",
    "config_transfer",
    "contest_catalog",
    "daily_entry_dialogs",
    "duplicate_dialog",
    "duplicate_report",
    "executor",
    "general_catalog",
    "intake_model",
    "ocr_engine",
    "plan_patch",
    "planner",
    "professional_catalog",
    "protect_config",
    "recent_spaces",
    "rule_preview",
    "scanner",
    "scheme_store",
    "storage",
    "target_tree_dialog",
    "transfer_dialogs",
    "utils",
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
    name="archivebox",
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
    name="archivebox",
)
app = BUNDLE(
    coll,
    name="归档盒.app",
    icon=None,
    bundle_identifier="local.ypjn.archivebox",
    version="1.7.0",
    info_plist={
        "CFBundleName": "归档盒",
        "CFBundleDisplayName": "归档盒",
        "CFBundleExecutable": "archivebox",
        "CFBundleIdentifier": "local.ypjn.archivebox",
        "CFBundleShortVersionString": "1.7.0",
        "CFBundleVersion": "1.7.0",
        "CFBundlePackageType": "APPL",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
