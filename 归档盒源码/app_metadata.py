"""应用名称、版本和构建身份。

关于页、方案导出和 macOS 打包脚本共用这里的常量。
构建脚本把当次提交写进 build_identity.json，打进应用包；
源码运行时没有该文件，就读取当前仓库提交。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

APP_NAME = "归档盒"
APP_VERSION = "1.8.2"
APP_VERSION_LABEL = "V1.8.2"
APP_TITLE = f"{APP_NAME} {APP_VERSION_LABEL}"
BUNDLE_ID = "local.ypjn.archivebox"
EXECUTABLE_NAME = "archivebox"
DMG_VOLUME_NAME = "归档盒"
MIN_SYSTEM_VERSION = "12.0"
IDENTITY_FILENAME = "build_identity.json"


def dmg_filename() -> str:
    return f"归档盒-{APP_VERSION}-macos-arm64.dmg"


def write_build_identity(path: str, commit: str, channel: str) -> dict:
    """构建脚本写入身份文件，再由 PyInstaller 打进应用包。"""
    data = {
        "name": APP_NAME,
        "version": APP_VERSION,
        "version_label": APP_VERSION_LABEL,
        "bundle_id": BUNDLE_ID,
        "commit": commit or "unknown",
        "channel": channel,
    }
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return data


def _identity_candidates() -> list[str]:
    paths: list[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        paths.append(os.path.join(meipass, IDENTITY_FILENAME))
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, IDENTITY_FILENAME))
    exe = os.path.abspath(getattr(sys, "executable", "") or "")
    contents = os.path.dirname(os.path.dirname(exe))
    if contents and os.path.basename(contents) == "Contents":
        paths.append(os.path.join(contents, "Resources", IDENTITY_FILENAME))
        paths.append(os.path.join(contents, "Frameworks", IDENTITY_FILENAME))
    return paths


def _git_commit() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return "dev"
    commit = out.strip()
    return commit or "dev"


def load_build_identity() -> dict:
    """返回版本和提交。打包身份优先于源码仓库状态。"""
    for path in _identity_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("version"):
            return data
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "version_label": APP_VERSION_LABEL,
        "bundle_id": BUNDLE_ID,
        "commit": _git_commit(),
        "channel": "source",
    }


def about_html(data_dir: str) -> str:
    identity = load_build_identity()
    commit = str(identity.get("commit") or "dev")
    short = commit[:12]
    return (
        f"<b>{APP_TITLE}</b><br>"
        f"版本 {APP_VERSION} · 构建 {short}<br><br>"
        "把散落的文件按学期、课程与类别整理到 _已整理，"
        "并在移动前给出可核对的预览。<br><br>"
        f"整理记录保存在：<br>{data_dir}<br>"
        "（每个整理空间的记录在其 .organizer/history.db）<br><br>"
        "AI 补充分类默认关闭；只有你自己打开并填写密钥后才会发请求。"
    )
