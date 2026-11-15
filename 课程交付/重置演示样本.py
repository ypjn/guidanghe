#!/usr/bin/env python3
"""把阶段三演示用的 3 个文件写回初始内容。

可重复运行。只覆盖脚本自己负责的这 3 个相对路径。
目录里的其他文件、子目录，以及已经存在的同级范围外对照，一律保留。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE / "演示样本"

OWNED = {
    "下载/归档盒/开发说明.md": "接口说明与排期\n",
    "下载/笔记.txt": "高等数学 极限 导数\n",
    "项目/AIGC/项目说明.md": "项目会议记录\n",
}
OUTSIDE_NAME = "范围外-不要放进样本.md"
OUTSIDE_TEXT = "这是空间外对照，扫描不应包含它。\n"


def write_owned(root: Path, rel: str, text: str) -> None:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def reset(root: Path) -> Path:
    root = root.expanduser().resolve()
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError(f"拒绝把演示文件写进 {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"整理空间必须是目录：{root}")
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in OWNED.items():
        write_owned(root, rel, text)
    outside = root.parent / OUTSIDE_NAME
    if not outside.exists():
        outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    return outside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写回归档盒阶段三的 3 个演示文件")
    parser.add_argument("--root", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    outside = root.parent / OUTSIDE_NAME
    outside_existed = outside.exists()
    try:
        outside = reset(root)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"整理空间：{root}")
    print(f"范围外对照：{outside}")
    if outside_existed:
        print("范围外对照已存在，未覆盖")
    else:
        print("已新建范围外对照")
    print("已写回的演示文件：")
    for rel in OWNED:
        print(f"  {rel}")
    extras = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel not in OWNED:
            extras.append(rel)
    if extras:
        print("保留的非演示文件：")
        for rel in extras:
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
