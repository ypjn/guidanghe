#!/bin/zsh
# 验收已打好的 DMG：挂载、复制到隔离的应用程序目录、卸载后仍运行。
# 记录的是应用进程退出码，不用 open 的退出码代替。
#
# 用法：
#   打包/accept_mac_app.sh <dmg路径> <证据目录>
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "用法：$0 <dmg路径> <证据目录>" >&2
  exit 2
fi
DMG="$1"
EVIDENCE="$2"
mkdir -p "$EVIDENCE"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/archivebox-accept.XXXXXX")"
MOUNT="$(mktemp -d "${TMPDIR:-/tmp}/archivebox-mount.XXXXXX")"

cleanup() {
  if mount | awk -v target="$MOUNT" '$0 ~ target { found=1 } END { exit !found }'; then
    hdiutil detach "$MOUNT" >/dev/null 2>&1 || hdiutil detach -force "$MOUNT" >/dev/null 2>&1 || true
  fi
  rm -rf "$MOUNT"
}
trap cleanup EXIT

echo "挂载：$DMG"
hdiutil attach "$DMG" -readonly -nobrowse -mountpoint "$MOUNT" | tee "$EVIDENCE/attach.txt"
if touch "$MOUNT/写入探测.txt" 2>/dev/null; then
  echo "镜像被挂成了可写卷" >&2
  exit 1
fi
test -d "$MOUNT/归档盒.app"
test -L "$MOUNT/Applications"
test -f "$MOUNT/拖入应用程序文件夹.txt"

INSTALLED="$WORK/Applications/归档盒.app"
mkdir -p "$WORK/Applications"
ditto "$MOUNT/归档盒.app" "$INSTALLED"
hdiutil detach "$MOUNT" | tee "$EVIDENCE/detach.txt"
rmdir "$MOUNT" 2>/dev/null || true
if hdiutil info | grep -F "$DMG" >/dev/null; then
  echo "镜像卸载后仍挂载" >&2
  exit 1
fi

EXE="$INSTALLED/Contents/MacOS/archivebox"
test -x "$EXE"
plutil -p "$INSTALLED/Contents/Info.plist" > "$EVIDENCE/Info.plist.txt"

fingerprint() {
  local app="$1"
  local out="$2"
  python3 - "$app" "$out" << 'PY'
import hashlib, os, sys
app, out = sys.argv[1], sys.argv[2]
rows = []
for dirpath, dirnames, filenames in os.walk(app):
    dirnames.sort()
    for name in sorted(filenames):
        path = os.path.join(dirpath, name)
        if os.path.islink(path):
            kind = "link"
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        else:
            kind = "file"
            h = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        rel = os.path.relpath(path, app)
        rows.append(f"{kind} {digest} {rel}")
with open(out, "w", encoding="utf-8") as handle:
    handle.write("\n".join(rows))
    handle.write("\n")
print(len(rows))
PY
}

run_app() {
  local name="$1"
  shift
  local log="$EVIDENCE/${name}.log"
  local exit_file="$EVIDENCE/${name}.exit"
  set +e
  "$EXE" "$@" >"$log" 2>&1
  local code=$?
  set -e
  echo "$code" > "$exit_file"
  echo "$name 进程退出码=$code"
  if [[ "$code" -ne 0 ]]; then
    echo "$name 失败，日志：$log" >&2
    tail -n 80 "$log" >&2 || true
    exit "$code"
  fi
}

fingerprint "$INSTALLED" "$EVIDENCE/bundle-before.txt"
run_app imports --product-acceptance imports "$WORK/imports"
run_app organize --product-acceptance organize "$WORK/organize"
run_app reopen-undo --product-acceptance reopen-undo "$WORK/organize"
run_app learning-agent --product-acceptance learning-agent "$WORK/learning"
export ARCHIVEBOX_APP_DIR="$WORK/upgrade-appdata"
export ARCHIVEBOX_LOG_DIR="$WORK/upgrade-logs"
run_app upgrade-seed --product-acceptance upgrade-seed "$WORK/upgrade"
REPLACEMENT="$WORK/Applications-replaced/归档盒.app"
mkdir -p "$WORK/Applications-replaced"
ditto "$INSTALLED" "$REPLACEMENT"
EXE="$REPLACEMENT/Contents/MacOS/archivebox"
run_app upgrade-check --product-acceptance upgrade-check "$WORK/upgrade"
EXE="$INSTALLED/Contents/MacOS/archivebox"
QT_QPA_PLATFORM=offscreen run_app smoke-gui --product-acceptance smoke-gui "$WORK/smoke"
fingerprint "$INSTALLED" "$EVIDENCE/bundle-after.txt"
if ! cmp -s "$EVIDENCE/bundle-before.txt" "$EVIDENCE/bundle-after.txt"; then
  echo "应用包指纹在运行后发生变化" >&2
  diff -u "$EVIDENCE/bundle-before.txt" "$EVIDENCE/bundle-after.txt" | head -n 80 >&2 || true
  exit 1
fi

leftover="$(ps ax -o pid=,command= | awk -v exe="$INSTALLED" -v repl="$REPLACEMENT" 'index($0, exe) || index($0, repl) { print }' || true)"
if [[ -n "$leftover" ]]; then
  echo "验收结束后仍有应用进程：" >&2
  echo "$leftover" >&2
  exit 1
fi

echo "隔离副本：$INSTALLED"
echo "证据目录：$EVIDENCE"
echo "镜像卸载后的验收已通过"
