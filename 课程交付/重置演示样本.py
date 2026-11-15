#!/usr/bin/env python3
"""重置阶段三课程演示样本。

每次运行都删掉旧样本再写回同一套小文件，方便从首次分析重新演示。
样本目录默认是本脚本旁边的「演示样本」。整理空间就是这个目录，
范围外对照文件放在它的上一级，不会被扫描进去。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE / "演示样本"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def reset(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    write(root, "下载/归档盒/开发说明.md", "接口说明与排期\n")
    write(root, "下载/笔记.txt", "高等数学 极限 导数\n")
    write(root, "项目/AIGC/项目说明.md", "项目会议记录\n")
    outside = root.parent / "范围外-不要放进样本.md"
    outside.write_text("这是空间外对照，扫描不应包含它。\n", encoding="utf-8")
    return outside


def main() -> None:
    parser = argparse.ArgumentParser(description="重置归档盒阶段三演示样本")
    parser.add_argument("--root", type=Path, default=DEFAULT)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    outside = reset(root)
    print(f"整理空间：{root}")
    print(f"范围外对照：{outside}")
    print("样本内 3 个文件：")
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        print(f"  {path.relative_to(root)}")


if __name__ == "__main__":
    main()
