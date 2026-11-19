"""用 Python zipfile 打包，确保非 ASCII 路径带 UTF-8 标志位。

用法：
  python 打包/zip_unicode.py <源目录> <输出.zip>

归档名以源目录 basename 为顶层；跳过 __MACOSX、.DS_Store 和 ._* 资源叉。
"""

from __future__ import annotations

import os
import stat
import sys
import time
import zipfile


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in ("__MACOSX", ".git", "__pycache__"))
        for name in sorted(filenames):
            if name == ".DS_Store" or name.startswith("._"):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                yield full
                continue
            if not os.path.isfile(full):
                continue
            yield full


def main():
    if len(sys.argv) != 3:
        print("用法: zip_unicode.py <源目录> <输出.zip>", file=sys.stderr)
        return 2
    source = os.path.abspath(sys.argv[1])
    output = os.path.abspath(sys.argv[2])
    top = os.path.basename(source.rstrip(os.sep))
    if not os.path.isdir(source):
        print(f"源目录不存在: {source}", file=sys.stderr)
        return 2
    if os.path.exists(output):
        os.unlink(output)
    count = 0
    non_ascii = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        for full in iter_files(source):
            rel = os.path.relpath(full, source)
            arc = os.path.join(top, rel).replace(os.sep, "/")
            if not arc.isascii():
                non_ascii += 1
            info = zipfile.ZipInfo(arc, date_time=time.localtime(
                os.lstat(full).st_mtime)[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            if os.path.islink(full):
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, os.readlink(full).encode("utf-8"))
            else:
                info.external_attr = (os.lstat(full).st_mode & 0xFFFF) << 16
                with open(full, "rb") as handle:
                    zf.writestr(info, handle.read())
            count += 1
    # 复核：所有非 ASCII 名字都必须带 0x800 标志。
    missing = []
    with zipfile.ZipFile(output) as zf:
        for info in zf.infolist():
            if not info.filename.isascii() and not (info.flag_bits & 0x800):
                missing.append(info.filename)
            if info.filename.startswith("__MACOSX/"):
                missing.append(info.filename)
    if missing:
        print("UTF-8 标志缺失或包含 __MACOSX：", missing[:5], file=sys.stderr)
        return 1
    print(f"已生成 {output}")
    print(f"文件数 {count}，非 ASCII 路径 {non_ascii}，全部带 UTF-8 标志")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
