#!/bin/zsh
# 在 Mac 上组装 Windows x64 免安装包。
# PyInstaller 不能跨系统编译，所以这里用官方 Windows 嵌入式 Python，
# 装上 Windows 版依赖，再用 Go 编一个双击启动的 exe。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PACK="$ROOT/打包"
CACHE="$PACK/cache"
DIST="$PACK/归档盒-Windows-免安装"
PY_VER="3.14.8"
EMBED_URL="https://mirrors.huaweicloud.com/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
EMBED_ZIP="$CACHE/python-${PY_VER}-embed-amd64.zip"
GO_VER="1.25.4"
GO_TGZ="$CACHE/go${GO_VER}.darwin-arm64.tar.gz"
GO_URL="https://mirrors.aliyun.com/golang/go${GO_VER}.darwin-arm64.tar.gz"
GO="$CACHE/go/bin/go"
SITE="$DIST/runtime/Lib/site-packages"
PIP="$ROOT/归档盒源码/.venv/bin/pip"
INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

mkdir -p "$CACHE"

if [[ ! -x "$GO" ]]; then
  echo "下载 Go ${GO_VER} ……"
  curl -fL --http1.1 --retry 3 -o "$GO_TGZ.part" "$GO_URL"
  mv "$GO_TGZ.part" "$GO_TGZ"
  rm -rf "$CACHE/go"
  tar -C "$CACHE" -xzf "$GO_TGZ"
fi

if [[ ! -f "$EMBED_ZIP" ]]; then
  echo "下载 Windows 嵌入式 Python ${PY_VER} ……"
  curl -fL --http1.1 --retry 3 -A "Mozilla/5.0" -o "$EMBED_ZIP.part" "$EMBED_URL"
  mv "$EMBED_ZIP.part" "$EMBED_ZIP"
fi

rm -rf "$DIST"
mkdir -p "$DIST/runtime" "$DIST/app" "$SITE"

unzip -q "$EMBED_ZIP" -d "$DIST/runtime"

# 嵌入式 Python 默认不加载 site-packages。打开之后才能用到界面库。
PTH=("$DIST"/runtime/python3*._pth)
if [[ ! -f "${PTH[1]}" ]]; then
  echo "没有找到嵌入式 Python 的 ._pth 文件" >&2
  exit 1
fi
ZIPNAME="$(basename "$DIST"/runtime/python3*.zip)"
cat > "${PTH[1]}" <<EOF
$ZIPNAME
.
Lib\\site-packages
..\\app
import site
EOF

COMMON_PIP=(
  --target "$SITE"
  --platform win_amd64
  --python-version 3.14
  --implementation cp
  --abi cp314
  --only-binary=:all:
  --upgrade
  --index-url "$INDEX"
  --trusted-host pypi.tuna.tsinghua.edu.cn
)

echo "安装 Windows x64 依赖……"
# 界面只用 Qt Widgets。不装 WebEngine，否则会带上一整份浏览器内核。
"$PIP" install "${COMMON_PIP[@]}" \
  rapidocr==3.10.0 \
  onnxruntime==1.31.1 \
  "winrt-Windows.Media.Ocr==4!10.0.28000.2957" \
  "winrt-Windows.Graphics.Imaging==4!10.0.28000.2957" \
  "winrt-Windows.Storage==4!10.0.28000.2957" \
  "winrt-Windows.Globalization==4!10.0.28000.2957"
"$PIP" install "${COMMON_PIP[@]}" --no-deps \
  PySide6==6.12.0 \
  PySide6_Essentials==6.12.0 \
  shiboken6==6.12.0

cp "$PACK/win-launcher/archivebox_dllpath.py" "$SITE/archivebox_dllpath.py"
printf 'import archivebox_dllpath\n' > "$SITE/archivebox_dllpath.pth"

# 只带程序本身，不带测试、虚拟环境和缓存。
for src in "$ROOT/归档盒源码"/*.py; do
  cp "$src" "$DIST/app/"
done

# S3 / lead L-01/L-06：去掉 macOS AppleDouble、.DS_Store 与 Python 缓存，
# 否则发行包里会出现成百上千个 ._* 和 __pycache__。
find "$DIST" \
  \( -name '._*' -o -name '.DS_Store' -o -name '__pycache__' \
     -o -name '*.pyc' -o -name '*.pyo' \) \
  -prune -exec rm -rf {} +

if ! find "$SITE/PySide6" -name 'qwindows.dll' | grep -q .; then
  echo "Windows 界面组件不完整：没有 qwindows.dll" >&2
  exit 1
fi

echo "编译 归档盒.exe ……"
(
  cd "$PACK/win-launcher"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 \
    "$GO" build -ldflags="-H windowsgui -s -w" -o "$DIST/归档盒.exe" .
)

cat > "$DIST/使用说明.txt" <<'EOF'
归档盒 Windows 免安装版（V1.7）

这是给 64 位 Windows 用的。请把整个文件夹一起拷走，不要只拷「归档盒.exe」。
双击「归档盒.exe」就能打开，不需要安装 Python。

解压后再打开。压缩包里直接双击，程序会缺文件。

第一次如果系统提示「Windows 已保护你的电脑」：
点「更多信息」，再点「仍要运行」。
这个包没有软件发布者签名，适合自己和熟人试用。

如果窗口没有出现，看同一文件夹里新生成的「启动日志.txt」。

整理记录保存在你的账户里，不在这个文件夹里：
%APPDATA%\归档盒

AI 补充分类默认关闭，程序不内置任何密钥；只有你自己打开并填写密钥后才会发请求。

Apple 芯片的 Mac 请用另一份「归档盒.app」，不要用这个 exe。
EOF

rm -f "$PACK/归档盒-Windows-免安装.zip"
# --norsrc + COPYFILE_DISABLE：不把 AppleDouble 资源叉打进发布包
COPYFILE_DISABLE=1 ditto -c -k --norsrc --keepParent "$DIST" \
  "$PACK/归档盒-Windows-免安装.zip"
echo "已生成：$DIST/归档盒.exe"
echo "压缩包：$PACK/归档盒-Windows-免安装.zip"
