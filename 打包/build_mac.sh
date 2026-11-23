#!/bin/zsh
# 打出当前 Apple 芯片 Mac 上可安装的归档盒.app 和只读 DMG。
#
# 解释器：
#   ARCHIVEBOX_BUILD_PYTHON  优先。指向已安装 PySide6 与 PyInstaller 的 python。
#   未设置时，若仓库内存在 归档盒源码/.venv，则使用其中的 python。
# 打包始终用「python -m PyInstaller」，不调用写死的 pyinstaller 可执行文件。
#
# 产物：
#   打包/dist/归档盒.app
#   打包/dist/归档盒-<版本>-macos-arm64.dmg
#   打包/dist/SHA256SUMS.txt
#
# 本地 ad-hoc 签名只为了让本机能够启动。正式开发者签名和公证不在此脚本内。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PACK="$ROOT/打包"
SRC="$ROOT/归档盒源码"

if [[ -n "${ARCHIVEBOX_BUILD_PYTHON:-}" ]]; then
  PYBIN="$ARCHIVEBOX_BUILD_PYTHON"
elif [[ -x "$SRC/.venv/bin/python" ]]; then
  PYBIN="$SRC/.venv/bin/python"
else
  echo "未找到构建解释器。" >&2
  echo "请设置 ARCHIVEBOX_BUILD_PYTHON 为已安装 PySide6 和 PyInstaller 的 Python，或在 归档盒源码/.venv 准备该环境。" >&2
  exit 1
fi
if [[ ! -x "$PYBIN" ]]; then
  echo "解释器不可执行：$PYBIN" >&2
  exit 1
fi

"$PYBIN" -c 'import PyInstaller, PySide6'

COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
IDENT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/archivebox-identity.XXXXXX")"
cleanup() {
  rm -rf "$IDENT_DIR"
}
trap cleanup EXIT
export ARCHIVEBOX_BUILD_IDENTITY="$IDENT_DIR/build_identity.json"
"$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); from app_metadata import write_build_identity; write_build_identity(sys.argv[2], sys.argv[3], "macos-arm64")' \
  "$SRC" "$ARCHIVEBOX_BUILD_IDENTITY" "$COMMIT"

"$PYBIN" "$PACK/make_icon.py"

DIST="$PACK/dist"
APP="$DIST/归档盒.app"
rm -rf "$APP" "$DIST/archivebox"
mkdir -p "$DIST"

"$PYBIN" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" \
  --workpath "$PACK/build" \
  "$PACK/archivebox.spec"

rm -rf "$DIST/archivebox" "$PACK/build"
if [[ ! -d "$APP" ]]; then
  echo "未生成应用：$APP" >&2
  exit 1
fi

codesign --force --deep --sign - "$APP"
codesign --verify --verbose=2 "$APP"

VERSION="$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); from app_metadata import APP_VERSION; print(APP_VERSION)' "$SRC")"
DMG_NAME="归档盒-${VERSION}-macos-arm64.dmg"
DMG="$DIST/$DMG_NAME"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/archivebox-dmg.XXXXXX")"
ditto "$APP" "$STAGE/归档盒.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/拖入应用程序文件夹.txt" << 'EOF'
安装归档盒

1. 把「归档盒」拖到「应用程序」文件夹。
2. 从启动台或「应用程序」打开。
3. 打开后可以推出这个磁盘镜像，应用不依赖镜像继续运行。

这是本机本地签名的安装镜像，尚未做 Apple 公证。
EOF

rm -f "$DMG"
hdiutil create \
  -volname "归档盒" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  -fs HFS+ \
  -imagekey zlib-level=9 \
  "$DMG"
rm -rf "$STAGE"
hdiutil verify "$DMG"
hdiutil imageinfo "$DMG" | tee "$DIST/dmg-imageinfo.txt"

shasum -a 256 "$DMG" | tee "$DIST/SHA256SUMS.txt"
echo "已生成：$APP"
echo "磁盘镜像：$DMG"
echo "构建提交：$COMMIT"
