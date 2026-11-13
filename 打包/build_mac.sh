#!/bin/zsh
# 打出当前这台 Apple 芯片 Mac 上的免安装包。
# 打完后还要再签一次：PyInstaller 自带的签名和 Python 动态库不是同一套，
# 不重签的话程序会在启动时因为签名不一致直接退出。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/归档盒源码/.venv/bin/pyinstaller"
DIST="$ROOT/打包/归档盒-macOS-免安装"
APP="$DIST/归档盒.app"

"$PY" --noconfirm --clean \
  --distpath "$DIST" \
  --workpath "$ROOT/打包/build" \
  "$ROOT/打包/archivebox.spec"

rm -rf "$DIST/archivebox" "$ROOT/打包/build"
codesign --force --deep --sign - "$APP"
echo "已生成：$APP"
