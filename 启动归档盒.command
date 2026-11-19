#!/bin/zsh
# ============================================================
# 归档盒 V1.7 — macOS 双击启动入口
#
# 用法：在 Finder 中双击本文件（或右键 → 打开）。
# 从入口自身位置定位源码与虚拟环境，不依赖固定个人路径，
# 支持含中文与空格的路径；不做任何依赖升级或系统改动。
# ============================================================
set -u

ENTRY="$(cd "$(dirname "$0")" && pwd)"
SRC="$ENTRY/归档盒源码"
PY="$SRC/.venv/bin/python"
MAIN="$SRC/main.py"

echo "=================================================="
echo "  归档盒 V1.7"
echo "=================================================="

fail() {
    echo ""
    echo "❌ $1"
    echo "$2"
    echo ""
    printf '按回车键关闭本窗口…'
    read _
    exit 1
}

if [[ ! -d "$SRC" ]]; then
    fail "未找到源码目录：$SRC" \
        "请把本脚本放回「归档盒」项目文件夹（与「归档盒源码」目录同级）。"
fi

if [[ ! -f "$MAIN" ]]; then
    fail "源码不完整：缺少 $MAIN" \
        "请从项目快照重新复制完整源码。"
fi

if [[ ! -x "$PY" ]]; then
    fail "未找到虚拟环境解释器：$PY" \
        "首次使用请在终端执行：
  cd \"$SRC\"
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt"
fi

echo "源码目录：$SRC"
echo "解释器　：$PY"
echo "正在启动主窗口……（关闭归档盒窗口后本终端会提示退出）"
echo ""

cd "$SRC"
"$PY" "$MAIN"
STATUS=$?

echo ""
if [[ $STATUS -ne 0 ]]; then
    echo "❌ 程序异常退出（退出码 $STATUS）。"
    echo "   可重试；若反复失败，请把本窗口内容截图反馈。"
else
    echo "✓ 归档盒已正常退出。"
fi
printf '按回车键关闭本窗口…'
read _
exit $STATUS
