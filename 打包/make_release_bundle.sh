#!/usr/bin/env bash
# 在任意平台打出「可交给 Mac 一键生成 DMG」的发布源码包。
# 本脚本不调用 hdiutil/codesign；真正的 .app/.dmg 仍须在 Apple 芯片 Mac 上执行 build_mac.sh。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PACK="$ROOT/打包"
SRC="$ROOT/归档盒源码"
DIST="$PACK/dist"
mkdir -p "$DIST"

PYBIN="${ARCHIVEBOX_BUILD_PYTHON:-}"
if [[ -z "$PYBIN" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYBIN="$ROOT/.venv/bin/python"
  else
    PYBIN="$(command -v python3)"
  fi
fi

COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
VERSION="$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); from app_metadata import APP_VERSION; print(APP_VERSION)' "$SRC")"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/archivebox-release.XXXXXX")"
NAME="归档盒-${VERSION}-src-with-packaging"
DEST="$STAGE/$NAME"
mkdir -p "$DEST"

# 只带产品源码与打包脚本，不含 .venv、大 zip、分类清单 CSV。
tar -C "$ROOT" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='打包/dist' \
  --exclude='打包/build' \
  --exclude='*.zip' \
  --exclude='归档盒-分类清单' \
  -cf - . | tar -C "$DEST" -xf -

mkdir -p "$DEST/打包/dist"
printf '%s\n' "$COMMIT" > "$DEST/打包/dist/SOURCE_COMMIT.txt"
printf '%s\n' "$VERSION" > "$DEST/打包/dist/SOURCE_VERSION.txt"
cat > "$DEST/打包/dist/在Mac上生成DMG.txt" << EOF
在 Apple 芯片 Mac 上：

  export ARCHIVEBOX_BUILD_PYTHON="/path/to/python-with-PySide6-PyInstaller"
  zsh 打包/build_mac.sh
  zsh 打包/accept_mac_app.sh 打包/dist/归档盒-${VERSION}-macos-arm64.dmg docs/product-development/evidence/batch07

源码包对应提交：$COMMIT
版本：$VERSION
EOF

BUNDLE="$DIST/${NAME}.tar.gz"
rm -f "$BUNDLE"
tar -C "$STAGE" -czf "$BUNDLE" "$NAME"
rm -rf "$STAGE"

(
  cd "$DIST"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$(basename "$BUNDLE")" | tee SHA256SUMS-src.txt
  else
    sha256sum "$(basename "$BUNDLE")" | tee SHA256SUMS-src.txt
  fi
)

echo "已生成源码发布包：$BUNDLE"
echo "构建提交：$COMMIT"
echo "版本：$VERSION"
echo "下一步：在 Mac 上解压后运行 打包/build_mac.sh 生成正式 DMG。"
